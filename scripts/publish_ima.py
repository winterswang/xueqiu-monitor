#!/usr/bin/env python3
"""
自选股舆情日报 → IMA 笔记发布脚本

读取生成的 Markdown 日报，通过 IMA OpenAPI import_doc 创建笔记。

凭证配置（同 crawler 的 publish_daily_report.py）:
- 环境变量: IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY
- 回退文件: ~/.config/ima/client_id / ~/.config/ima/api_key

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
from typing import Optional

PROJECT_DIR = Path(__file__).resolve().parent.parent


def _read_ima_credential(env_key: str, file_path: str) -> str:
    """Read IMA credential: env var first, fall back to file."""
    value = os.environ.get(env_key)
    if value:
        return value
    cred_file = Path(file_path).expanduser()
    if cred_file.exists():
        return cred_file.read_text().strip()
    return ""


def create_ima_note(
    title: str,
    content: str,
    folder_id: str,
    folder_name: str,
) -> Optional[str]:
    """Create IMA note via import_doc API. Returns note_id on success."""
    client_id = _read_ima_credential("IMA_OPENAPI_CLIENTID", "~/.config/ima/client_id")
    api_key = _read_ima_credential("IMA_OPENAPI_APIKEY", "~/.config/ima/api_key")

    if not client_id or not api_key:
        print("❌ IMA 凭证缺失: 请设置 IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY")
        return None

    # If folder_id empty, search by folder_name to find/create it
    if not folder_id:
        folder_id = _find_or_create_folder(client_id, api_key, folder_name) or ""
        if not folder_id:
            print(f"❌ 无法定位或创建笔记本: {folder_name}")
            return None

    url = "https://ima.qq.com/openapi/note/v1/import_doc"
    body = {
        "content_format": 1,  # 1 = markdown
        "content": content,
        "folder_id": folder_id,
        "folder_name": folder_name,
    }
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
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            code = result.get("code")
            if code == 0:
                note_id = result.get("data", {}).get("note_id")
                print(f"✅ IMA 笔记创建成功: note_id={note_id}")
                print(f"   URL: https://ima.qq.com/note/{note_id}")
                return note_id
            else:
                print(
                    f"❌ IMA API 失败: code={code}, msg={result.get('message', 'N/A')}"
                )
                return None
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        print(f"❌ IMA HTTP 错误: {e.code} {e.reason}, body={body_text}")
        return None
    except Exception as e:
        print(f"❌ IMA 请求异常: {e}")
        return None


def _find_or_create_folder(
    client_id: str, api_key: str, folder_name: str
) -> Optional[str]:
    """Search for existing folder by name, return folder_id."""
    url = "https://ima.qq.com/openapi/note/v1/search_note_book"
    headers = {
        "ima-openapi-clientid": client_id,
        "ima-openapi-apikey": api_key,
        "Content-Type": "application/json",
    }
    body = json.dumps({"keyword": folder_name})
    req = urllib.request.Request(
        url, data=body.encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            folders = result.get("data", {}).get("folder_list", [])
            for f in folders:
                if f.get("folder_name") == folder_name:
                    return f.get("folder_id")
            # Folder not found — use empty folder_id, IMA will create it
            print(f"⚠️  笔记本 '{folder_name}' 未找到，将使用默认位置")
            return ""
    except Exception as e:
        print(f"⚠️  搜索笔记本失败: {e}")
        return ""


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

    note_id = create_ima_note(title, content, folder_id, folder_name)
    if note_id:
        print(f"\n✅ 发布完成: https://ima.qq.com/note/{note_id}")
    else:
        print("\n❌ 发布失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
