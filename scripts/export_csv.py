#!/usr/bin/env python3
"""Export crawled posts to CSV and upload to IMA Knowledge Base.

Reads all stocks' posts from monitor.db (crawl_snapshots + announcements),
exports a single daily CSV file (UTF-8 BOM for Excel compatibility), and
uploads it to the IMA knowledge base "雪球内容数据".

Pipeline integration:
    19:00 cron → generate_daily_report() → publish_ima.py → THIS SCRIPT

Usage:
    python3 scripts/export_csv.py --config etc/config.report.json
    python3 scripts/export_csv.py --config etc/config.report.json --date 2026-07-09

Credentials (same as publish_ima.py):
    env: IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY
    fallback files: ~/.config/ima/client_id / ~/.config/ima/api_key
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

PROJECT_DIR = Path(__file__).resolve().parent.parent

# Make src/ importable regardless of CWD (cron runs from PROJECT_DIR, but
# robustness against direct script invocation is cheap to guarantee).
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.report_common import filter_posts_by_recency  # noqa: E402

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════
# Constants
# ════════════════════════════════════════════════════════

CSV_HEADERS = [
    "date",
    "stock_code",
    "stock_name",
    "post_type",
    "title",
    "content",
    "author",
    "time",
    "like_count",
    "comment_count",
    "forward_count",
    "sentiment_score",
    "link",
]

# IMA API
IMA_BASE_URL = "https://ima.qq.com"
IMA_WIKI_PATH = "/openapi/wiki/v1"

# 2026-08-22 (v0.8 环境修复): cos-upload.cjs 硬编码 ~/.hermes/skills/ima 在本机不存在,
# 导致 export_csv 上传失败. 改为环境自动适配:
#   1. IMA_COS_SCRIPT 环境变量最高优先 (显式指定)
#   2. 常见 IMA skill 安装位置候选 (my-agent-skills / openclaw workspace / .hermes / home)
#   3. 都找不到时回退到第一个候选 (日志会报错, 便于人工指定)
_COS_SCRIPT_CANDIDATES = [
    # IMA skill 权威仓 (morning-brief 也用它)
    PROJECT_DIR.parent / "my-agent-skills" / "ima" / "knowledge-base" / "scripts" / "cos-upload.cjs",
    # 运行时 workspace
    Path.home() / ".openclaw" / "workspace" / "skills" / "ima" / "knowledge-base" / "scripts" / "cos-upload.cjs",
    # 旧/其他 hermes 安装
    Path.home() / ".hermes" / "skills" / "ima" / "knowledge-base" / "scripts" / "cos-upload.cjs",
    # 项目内 vendor
    PROJECT_DIR / "ima" / "knowledge-base" / "scripts" / "cos-upload.cjs",
]
if os.environ.get("IMA_COS_SCRIPT"):
    COS_UPLOAD_SCRIPT = Path(os.environ["IMA_COS_SCRIPT"])
else:
    COS_UPLOAD_SCRIPT = next((p for p in _COS_SCRIPT_CANDIDATES if p.exists()), _COS_SCRIPT_CANDIDATES[0])

# Content cap (same as report_generator.fetch_stock_posts)
CONTENT_MAX_CHARS = 2000


# ════════════════════════════════════════════════════════
# Credential helper (shared logic with publish_ima.py)
# ════════════════════════════════════════════════════════


def _read_ima_credential(env_key: str, file_path: str) -> str:
    """Read IMA credential: env var first, fall back to file."""
    value = os.environ.get(env_key)
    if value:
        return value
    cred_file = Path(file_path).expanduser()
    if cred_file.exists():
        return cred_file.read_text().strip()
    return ""


def _get_ima_headers() -> dict:
    """Build IMA API auth headers."""
    client_id = _read_ima_credential("IMA_OPENAPI_CLIENTID", "~/.config/ima/client_id")
    api_key = _read_ima_credential("IMA_OPENAPI_APIKEY", "~/.config/ima/api_key")
    if not client_id or not api_key:
        raise RuntimeError("IMA 凭证缺失: 请设置 IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY")
    return {
        "ima-openapi-clientid": client_id,
        "ima-openapi-apikey": api_key,
        "Content-Type": "application/json",
    }


# ════════════════════════════════════════════════════════
# Data fetch (from monitor.db)
# ════════════════════════════════════════════════════════


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def fetch_all_posts(
    db_path: str, stocks_cfg: dict, date_str: str, min_length: int = 30,
    max_age_days: int = 7,
) -> list[dict]:
    """Fetch all posts (discussion + news + article) for all stocks on a date.

    Filtering (shared with report paths via src/report_common.py, v0.7 F3):
    - Recency: only posts published within ``max_age_days`` (default 7) of the
      report run are kept — the knowledge-base feed must not accumulate stale
      month-old posts. Unparseable times are kept (fail-open), matching the
      pipeline's policy.
    - Skip reply posts (回复@ prefix)
    - Skip posts shorter than min_length chars
    - Content capped to CONTENT_MAX_CHARS

    Deduplication:
    - Snapshot level: only the latest snapshot per stock is used
    - Post level: deduplicate by post_id across stocks and snapshots,
      so the same post mentioned for multiple stocks only appears once
    """
    conn = _connect(db_path)
    rows_data = []
    seen_post_ids: set[str] = set()
    try:
        rows = conn.execute(
            """SELECT stock_code, posts_data FROM crawl_snapshots
               WHERE date(crawl_time,'unixepoch','localtime')=?
               ORDER BY stock_code, crawl_time DESC, id DESC""",
            (date_str,),
        ).fetchall()

        seen_stocks = set()
        for row in rows:
            stock_code = row["stock_code"]
            # Only take the latest snapshot per stock
            if stock_code in seen_stocks:
                continue
            seen_stocks.add(stock_code)

            if not row["posts_data"]:
                continue

            stock_name = stocks_cfg.get(stock_code, {}).get("name", stock_code)
            posts = json.loads(row["posts_data"])
            # Recency filter first: drop stale posts before per-post work.
            posts = filter_posts_by_recency(posts, max_age_days=max_age_days)
            for p in posts:
                # Post-level dedup by post_id (cross-stock + cross-snapshot)
                post_id = p.get("post_id") or p.get("link") or ""
                if post_id and post_id in seen_post_ids:
                    continue

                title = (p.get("title") or "")[:200]
                content = p.get("content") or ""
                # Skip reply posts
                if title.startswith("回复@") or content.startswith("回复@"):
                    continue
                # Skip very short posts
                full_text = f"{title} {content}".strip()
                if len(full_text) < min_length:
                    continue

                if post_id:
                    seen_post_ids.add(post_id)
                rows_data.append(
                    {
                        "date": date_str,
                        "stock_code": stock_code,
                        "stock_name": stock_name,
                        "post_type": p.get("type", ""),
                        "title": title,
                        "content": content[:CONTENT_MAX_CHARS],
                        "author": p.get("author", ""),
                        "time": p.get("time", ""),
                        "like_count": p.get("like_count", 0),
                        "comment_count": p.get("comment_count", 0),
                        "forward_count": p.get("forward_count", 0),
                        "sentiment_score": p.get("sentiment_score", 0.0),
                        "link": p.get("link", ""),
                    }
                )
    finally:
        conn.close()
    return rows_data


def fetch_announcements(db_path: str, stocks_cfg: dict, date_str: str) -> list[dict]:
    """Fetch announcements for all stocks on a date."""
    conn = _connect(db_path)
    rows_data = []
    try:
        rows = conn.execute(
            """SELECT a.stock_code, a.ann_title, a.ann_date, a.ann_type
               FROM announcements a
               JOIN crawl_snapshots s ON a.snapshot_id = s.id
               WHERE date(s.crawl_time,'unixepoch','localtime')=?
               ORDER BY a.stock_code""",
            (date_str,),
        ).fetchall()
        for row in rows:
            stock_code = row["stock_code"]
            stock_name = stocks_cfg.get(stock_code, {}).get("name", stock_code)
            # Convert unix timestamp to readable string
            ann_ts = row["ann_date"]
            time_str = (
                datetime.fromtimestamp(ann_ts).strftime("%Y-%m-%d %H:%M")
                if ann_ts and ann_ts > 0
                else ""
            )
            rows_data.append(
                {
                    "date": date_str,
                    "stock_code": stock_code,
                    "stock_name": stock_name,
                    "post_type": "announcement",
                    "title": row["ann_title"][:200],
                    "content": "",
                    "author": "",
                    "time": time_str,
                    "like_count": 0,
                    "comment_count": 0,
                    "forward_count": 0,
                    "sentiment_score": 0.0,
                    "link": "",
                }
            )
    finally:
        conn.close()
    return rows_data


# ════════════════════════════════════════════════════════
# CSV generation
# ════════════════════════════════════════════════════════


def generate_csv(rows: list[dict], output_path: Path) -> int:
    """Write rows to a CSV file (UTF-8 BOM for Excel).

    Returns the number of data rows written.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig ensures Excel opens Chinese correctly
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return len(rows)


# ════════════════════════════════════════════════════════
# IMA Knowledge Base upload
# ════════════════════════════════════════════════════════


def _ima_api(path: str, body: dict, timeout: int = 30) -> dict:
    """Call IMA wiki API. Returns parsed JSON response."""
    headers = _get_ima_headers()
    url = f"{IMA_BASE_URL}{IMA_WIKI_PATH}/{path}"
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
        raise RuntimeError(f"IMA HTTP {e.code} {e.reason}: {body_text}") from e


def find_knowledge_base_id(kb_name: str) -> Optional[str]:
    """Search for a knowledge base by name. Returns kb_id or None."""
    result = _ima_api("search_knowledge_base", {"query": kb_name, "cursor": "", "limit": 10})
    if result.get("code") != 0:
        logger.error(f"搜索知识库失败: {result.get('msg')}")
        return None
    info_list = result.get("data", {}).get("info_list", [])
    for item in info_list:
        if item.get("kb_name") == kb_name:
            return item.get("kb_id")
    return None


def upload_csv_to_kb(
    csv_path: Path,
    kb_id: str,
    file_name: Optional[str] = None,
) -> Optional[str]:
    """Upload CSV file to IMA knowledge base.

    Full flow: check_repeated_names → create_media → COS upload → add_knowledge.
    Returns media_id on success, None on failure.
    """
    file_name = file_name or csv_path.name
    file_size = csv_path.stat().st_size
    file_ext = csv_path.suffix.lstrip(".")
    content_type = "text/csv"
    media_type = 5  # Excel/CSV

    logger.info(f"  [1/4] 检查重名: {file_name}")
    repeat_result = _ima_api(
        "check_repeated_names",
        {
            "params": [{"name": file_name, "media_type": media_type}],
            "knowledge_base_id": kb_id,
        },
    )
    if repeat_result.get("code") != 0:
        logger.error(f"  重名检查失败: {repeat_result.get('msg')}")
        return None
    results = repeat_result.get("data", {}).get("results", [])
    if results and results[0].get("is_repeated"):
        # Append timestamp to avoid collision
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        name_parts = file_name.rsplit(".", 1)
        file_name = f"{name_parts[0]}_{ts}.{name_parts[1]}" if len(name_parts) == 2 else f"{file_name}_{ts}"
        logger.info(f"  文件已存在，重命名为: {file_name}")

    # Step 2: create_media
    logger.info(f"  [2/4] 创建媒体: {file_name} ({file_size} bytes)")
    create_result = _ima_api(
        "create_media",
        {
            "file_name": file_name,
            "file_size": file_size,
            "content_type": content_type,
            "knowledge_base_id": kb_id,
            "file_ext": file_ext,
        },
    )
    if create_result.get("code") != 0:
        logger.error(f"  创建媒体失败: {create_result.get('msg')}")
        return None
    data = create_result.get("data", {})
    media_id = data.get("media_id", "")
    cos_cred = data.get("cos_credential", {})
    if not media_id or not cos_cred:
        logger.error("  创建媒体返回数据缺失 media_id 或 cos_credential")
        return None

    # Step 3: COS upload via cos-upload.cjs
    logger.info(f"  [3/4] COS 上传...")
    if not COS_UPLOAD_SCRIPT.exists():
        logger.error(f"  COS 上传脚本不存在: {COS_UPLOAD_SCRIPT}")
        return None
    cos_cmd = [
        "node",
        str(COS_UPLOAD_SCRIPT),
        "--file", str(csv_path),
        "--secret-id", cos_cred["secret_id"],
        "--secret-key", cos_cred["secret_key"],
        "--token", cos_cred["token"],
        "--bucket", cos_cred["bucket_name"],
        "--region", cos_cred["region"],
        "--cos-key", cos_cred["cos_key"],
        "--content-type", content_type,
        "--start-time", str(cos_cred["start_time"]),
        "--expired-time", str(cos_cred["expired_time"]),
        "--timeout", "60000",
    ]
    try:
        proc = subprocess.run(
            cos_cmd, capture_output=True, text=True, timeout=120
        )
        if proc.returncode != 0:
            logger.error(f"  COS 上传失败 (exit={proc.returncode}): {proc.stderr.strip()}")
            return None
    except subprocess.TimeoutExpired:
        logger.error("  COS 上传超时 (120s)")
        return None
    logger.info(f"  COS 上传成功")

    # Step 4: add_knowledge
    logger.info(f"  [4/4] 添加知识条目...")
    add_result = _ima_api(
        "add_knowledge",
        {
            "media_type": media_type,
            "media_id": media_id,
            "title": file_name,
            "knowledge_base_id": kb_id,
            "file_info": {
                "cos_key": cos_cred["cos_key"],
                "file_size": file_size,
                "file_name": file_name,
            },
        },
    )
    if add_result.get("code") != 0:
        logger.error(f"  添加知识失败: {add_result.get('msg')}")
        return None
    final_media_id = add_result.get("data", {}).get("media_id", media_id)
    logger.info(f"  ✅ 知识库添加成功: media_id={final_media_id}")
    return final_media_id


# ════════════════════════════════════════════════════════
# Main orchestration
# ════════════════════════════════════════════════════════


def export_and_upload(
    config_path: str = "etc/config.report.json",
    date_str: Optional[str] = None,
) -> dict:
    """Export posts to CSV and upload to IMA knowledge base.

    Returns a dict with status info (rows, csv_path, media_id, error).
    """
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    db_path = cfg["db_path"]
    stocks_cfg = cfg["stocks"]
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")

    # Knowledge base config
    kb_cfg = cfg.get("knowledge_base", {})
    kb_id = kb_cfg.get("id", "") or os.environ.get("IMA_KB_ID", "")
    kb_name = kb_cfg.get("name", "雪球内容数据")

    logger.info(f"=== CSV 导出 + 知识库上传 {date_str} ===")

    # 1. Fetch data
    min_len = cfg.get("llm", {}).get("min_post_length", 30)
    logger.info(f"[1/3] 拉取数据 (min_length={min_len})...")
    posts = fetch_all_posts(db_path, stocks_cfg, date_str, min_length=min_len)
    announcements = fetch_announcements(db_path, stocks_cfg, date_str)
    all_rows = posts + announcements
    logger.info(f"  帖子: {len(posts)}, 公告: {len(announcements)}, 合计: {len(all_rows)}")

    if not all_rows:
        logger.warning("  ⚠️ 今日无数据，跳过上传")
        return {"status": "skipped", "reason": "no_data", "rows": 0}

    # 2. Generate CSV
    logger.info("[2/3] 生成 CSV...")
    output_dir = Path(cfg.get("report_output_dir", "data/daily_reports"))
    csv_path = output_dir / f"xueqiu_posts_{date_str}.csv"
    row_count = generate_csv(all_rows, csv_path)
    file_size = csv_path.stat().st_size
    logger.info(f"  CSV 生成: {csv_path} ({row_count} 行, {file_size} bytes)")

    # 3. Upload to knowledge base
    logger.info("[3/3] 上传到知识库...")
    if not kb_id:
        logger.info(f"  知识库 ID 未配置，按名称搜索: {kb_name}")
        kb_id = find_knowledge_base_id(kb_name)
    if not kb_id:
        logger.error(f"  ❌ 未找到知识库「{kb_name}」")
        return {
            "status": "failed",
            "reason": "kb_not_found",
            "rows": row_count,
            "csv_path": str(csv_path),
        }

    media_id = upload_csv_to_kb(csv_path, kb_id)

    if media_id:
        return {
            "status": "success",
            "rows": row_count,
            "csv_path": str(csv_path),
            "media_id": media_id,
            "kb_id": kb_id,
        }
    else:
        return {
            "status": "upload_failed",
            "rows": row_count,
            "csv_path": str(csv_path),
            "kb_id": kb_id,
        }


def main():
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    parser = argparse.ArgumentParser(description="导出 CSV 并上传到 IMA 知识库")
    parser.add_argument(
        "--config",
        default=str(PROJECT_DIR / "etc" / "config.report.json"),
        help="报告配置 JSON 路径",
    )
    parser.add_argument(
        "--date", default=None, help="日期 YYYY-MM-DD（默认今天）"
    )
    args = parser.parse_args()

    result = export_and_upload(config_path=args.config, date_str=args.date)

    print(f"\n{'='*60}")
    print(f"导出结果: {result['status']} | {result.get('rows', 0)} 行")
    if result.get("csv_path"):
        print(f"CSV: {result['csv_path']}")
    if result.get("media_id"):
        print(f"知识库 media_id: {result['media_id']}")
    if result.get("reason"):
        print(f"原因: {result['reason']}")
    print(f"{'='*60}")

    # Exit code: 0 for success/skipped, 1 for failure
    sys.exit(0 if result["status"] in ("success", "skipped") else 1)


if __name__ == "__main__":
    main()
