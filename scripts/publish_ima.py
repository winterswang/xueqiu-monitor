#!/usr/bin/env python3
"""
自选股舆情日报 → IMA 笔记发布脚本

读取生成的 Markdown 日报，通过 IMA OpenAPI import_doc 创建笔记，并归入指定笔记本。

凭证配置（同 crawler 的 publish_daily_report.py）:
- 环境变量: IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY
- 回退文件: ~/.config/ima/client_id / ~/.config/ima/api_key

笔记本解析策略（2026-07-18 修正）:
- 优先用 config.ima.folder_id（若非空，直接使用）
- 否则调 list_notebook 按名称查找，命中则缓存 folder_id 回 config（避免重复请求）
- 若仍找不到 → ❌ 明确报错退出，不再静默降级到根目录
  （早期版本依赖 "folder_name 自动创建笔记本" 的虚假行为，实际 import_doc
    传空 folder_id + folder_name 会创建笔记但不归入任何笔记本，导致历史日报
    全部落到未分类。详见 SKILL.md references/folder-autocreation-correction.md）

用法:
  python3 scripts/publish_ima.py --report data/daily_reports/2026-07-04-sentiment.md
  python3 scripts/publish_ima.py --report data/daily_reports/2026-07-04-sentiment.md --config etc/config.report.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

PROJECT_DIR = Path(__file__).resolve().parent.parent

IMA_BASE = "https://ima.qq.com"


def _read_ima_credential(env_key: str, file_path: str) -> str:
    """Read IMA credential: env var first, fall back to file."""
    value = os.environ.get(env_key)
    if value:
        return value
    cred_file = Path(file_path).expanduser()
    if cred_file.exists():
        return cred_file.read_text().strip()
    return ""


def _ima_request(
    path: str,
    body: dict,
    client_id: str,
    api_key: str,
    timeout: int = 30,
) -> Optional[dict]:
    """Send a POST to IMA OpenAPI. Returns parsed JSON on any HTTP 200, None on error."""
    url = f"{IMA_BASE}/{path.lstrip('/')}"
    headers = {
        "ima-openapi-clientid": client_id,
        "ima-openapi-apikey": api_key,
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        print(f"❌ IMA HTTP 错误: {e.code} {e.reason}, body={body_text}")
        return None
    except Exception as e:
        print(f"❌ IMA 请求异常 ({path}): {e}")
        return None


def _resolve_folder_id(
    client_id: str,
    api_key: str,
    folder_id_hint: str,
    folder_name: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve the target folder_id.

    Returns (folder_id, folder_name) on success, (None, None) on failure.
    Strategy:
      1. If folder_id_hint is non-empty → trust it (caller is expected to keep config valid).
      2. Otherwise paginate list_notebook and match by name.
    """
    if folder_id_hint:
        return folder_id_hint, folder_name

    if not folder_name:
        print("❌ config.ima.folder_id 和 folder_name 都为空，无法确定目标笔记本")
        return None, None

    cursor = "0"
    while cursor:
        result = _ima_request(
            "openapi/note/v1/list_notebook",
            {"cursor": cursor, "limit": 20},
            client_id,
            api_key,
            timeout=15,
        )
        if not result or result.get("code") != 0:
            print(
                f"❌ list_notebook 失败: code={result.get('code') if result else 'N/A'}, "
                f"msg={result.get('msg') if result else 'N/A'}"
            )
            return None, None

        data = result.get("data", {})
        folders = data.get("note_folder_infos", [])
        for f in folders:
            # 同时返回 folder_id 和实际 name，以便配置里名称与实际不一致时仍能归位
            if f.get("name") == folder_name:
                return f.get("folder_id"), f.get("name")

        if data.get("is_end"):
            break
        cursor = data.get("next_cursor") or ""

    print(
        f"❌ 笔记本 '{folder_name}' 不存在。请在 IMA 桌面端/Web 端手动创建该笔记本，"
        f"或将 config.ima.folder_id 设为已存在笔记本的 ID。"
    )
    print(
        f"   （早期文档记载的 'import_doc + folder_name 自动创建笔记本' 行为经实测不存在："
        f"笔记会被创建但 folder_id 留空，归入未分类。）"
    )
    return None, None


def _cache_folder_id_to_config(config_path: Path, folder_id: str, folder_name: str) -> None:
    """Persist resolved folder_id back to config.ima.folder_id (idempotent, fails soft)."""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if cfg.get("ima", {}).get("folder_id") == folder_id:
            return  # already cached
        cfg.setdefault("ima", {})["folder_id"] = folder_id
        cfg["ima"]["folder_name"] = folder_name
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"💾 已缓存 folder_id 到 {config_path.name}")
    except Exception as e:
        print(f"⚠️  缓存 folder_id 失败（不影响本次发布）: {e}")


def create_ima_note(
    title: str,
    content: str,
    folder_id_hint: str,
    folder_name: str,
    config_path: Optional[Path] = None,
) -> Optional[str]:
    """Create IMA note via import_doc API. Returns note_id on success.

    folder_id_hint: 若非空直接使用；为空时按 folder_name 查找 list_notebook。
    config_path: 若提供且本次通过名称解析到了 folder_id，会缓存回 config。
    """
    client_id = _read_ima_credential("IMA_OPENAPI_CLIENTID", "~/.config/ima/client_id")
    api_key = _read_ima_credential("IMA_OPENAPI_APIKEY", "~/.config/ima/api_key")

    if not client_id or not api_key:
        print("❌ IMA 凭证缺失: 请设置 IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY")
        return None

    folder_id, resolved_name = _resolve_folder_id(
        client_id, api_key, folder_id_hint, folder_name
    )
    if not folder_id:
        return None

    # 若本次是按名称解析得到 folder_id，缓存回 config（避免下次重复请求）
    if config_path and not folder_id_hint:
        _cache_folder_id_to_config(config_path, folder_id, resolved_name or folder_name)

    body = {
        "content_format": 1,  # 1 = markdown
        "content": content,
        "folder_id": folder_id,
    }
    result = _ima_request(
        "openapi/note/v1/import_doc",
        body,
        client_id,
        api_key,
        timeout=30,
    )
    if not result:
        return None

    code = result.get("code")
    if code == 0:
        note_id = result.get("data", {}).get("note_id")
        print(f"✅ IMA 笔记创建成功: note_id={note_id}")
        print(f"   归入笔记本: {resolved_name or folder_name} ({folder_id})")
        print(f"   URL: https://ima.qq.com/note/{note_id}")
        return note_id

    print(f"❌ IMA API 失败: code={code}, msg={result.get('msg', 'N/A')}")
    return None


def main():
    parser = argparse.ArgumentParser(description="发布日报到 IMA 笔记")
    parser.add_argument(
        "--report", required=True, help="日报 Markdown 文件路径"
    )
    parser.add_argument(
        "--config",
        default=str(PROJECT_DIR / "etc" / "config.report.json"),
        help="报告配置 JSON 路径",
    )
    parser.add_argument(
        "--date", default=None, help="日期 YYYY-MM-DD（默认从文件名提取）"
    )
    args = parser.parse_args()

    # Read report content
    report_path = Path(args.report)
    if not report_path.exists():
        print(f"❌ 日报文件不存在: {report_path}")
        sys.exit(1)
    content = report_path.read_text(encoding="utf-8")
    print(f"📄 日报读取成功: {len(content)} 字符")

    # Extract date from filename or arg
    date_str = args.date
    if not date_str:
        # filename like 2026-07-04-sentiment.md
        date_str = report_path.stem.rsplit("-sentiment", 1)[0]
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")

    # Read config for folder
    with open(args.config, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    folder_name = cfg.get("ima", {}).get("folder_name", "自选股舆情日报")
    folder_id = cfg.get("ima", {}).get("folder_id", "")

    # Publish
    title = f"自选股舆情日报 - {date_str}"
    print(f"📝 发布: {title} → {folder_name}")

    note_id = create_ima_note(
        title,
        content,
        folder_id,
        folder_name,
        config_path=Path(args.config),
    )
    if note_id:
        print(f"\n✅ 发布完成: https://ima.qq.com/note/{note_id}")
    else:
        print("\n❌ 发布失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
