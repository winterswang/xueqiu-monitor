#!/usr/bin/env python3
"""Backfill reply (comment) threads for high-discussion posts via opencli.

The 13:00 pipeline captures post bodies and per-post comment *counts*, but
never the reply text itself (crawler.py:357 actively drops replies). This
script picks the most-discussed posts of the recent window and fetches their
reply threads via `opencli xueqiu replies` (statuses/comments.json through
the browser bridge — verified 2026-09-18).

Selection:
    per company, top --per-stock posts by comment_count (>= --min-comments)
    from the last --hours of crawl_snapshots; posts already present in
    post_replies are skipped (idempotent across daily runs).

Output table post_replies is script-owned (same convention as
post_fulltext): created here, never touched by schema.sql migrations.

Pipeline integration (proposed):
    after xueqiu-pipeline groups + before the intelligence prep run;
    budget ≈ (companies × per-stock) opencli calls, ~1s each.

Usage:
    python3 scripts/backfill_replies.py --dry-run          # see candidates
    python3 scripts/backfill_replies.py                    # top 5/company
    python3 scripts/backfill_replies.py --per-stock 8 --hours 26

Requirements: opencli installed + Chrome extension connected (opencli doctor).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill_replies")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = str(ROOT / "data" / "monitor.db")

POST_URL_RE = re.compile(r"^https://xueqiu\.com/\d+/\d+$")
REPLY_CAP = 20  # replies per post (page 1) — enough for intelligence purposes


def _opencli_ok() -> bool:
    if not shutil.which("opencli"):
        return False
    try:
        r = subprocess.run(["opencli", "doctor"], capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def _clean_json(stdout: str) -> str:
    """Strip opencli update-noise lines around the JSON payload."""
    i, j = stdout.find("["), stdout.rfind("]")
    if i < 0 or j <= i:
        raise ValueError("no JSON array in opencli output")
    return stdout[i:j + 1]


def ensure_table(conn: sqlite3.Connection) -> None:
    """post_replies is script-owned (post_fulltext convention): additive, idempotent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS post_replies (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id     TEXT    NOT NULL,
            reply_id    TEXT    NOT NULL DEFAULT '',
            stock_code  TEXT    NOT NULL DEFAULT '',
            author      TEXT    NOT NULL DEFAULT '',
            likes       INTEGER NOT NULL DEFAULT 0,
            text        TEXT    NOT NULL DEFAULT '',
            created_at  TEXT    NOT NULL DEFAULT '',
            reply_to    TEXT    NOT NULL DEFAULT '',
            fetched_at  INTEGER NOT NULL
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_post_replies ON post_replies(post_id, reply_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_post_replies_code ON post_replies(stock_code)")
    conn.commit()


def load_candidates(conn: sqlite3.Connection, hours: int, min_comments: int,
                    per_stock: int) -> list[dict]:
    """Per-company top posts by comment_count from the recent snapshot window."""
    since = int(time.time()) - hours * 3600
    rows = conn.execute(
        "SELECT stock_code, posts_data FROM crawl_snapshots "
        "WHERE crawl_time >= ? ORDER BY crawl_time DESC", (since,)).fetchall()
    by_post: dict[str, dict] = {}
    for stock_code, posts_json in rows:
        try:
            posts = json.loads(posts_json)
        except Exception:
            continue
        for p in posts:
            url = (p.get("link") or p.get("post_id") or "").strip()
            if not POST_URL_RE.match(url) or p.get("type") not in ("discussion", "article"):
                continue
            try:
                n_comments = int(p.get("comment_count") or 0)
            except (TypeError, ValueError):
                n_comments = 0
            key = url.rsplit("/", 1)[-1]
            item = {"post_id": url, "post_key": key, "stock_code": stock_code,
                    "comments": n_comments, "author": (p.get("author") or "").strip()}
            prev = by_post.get(key)
            if prev is None or n_comments > prev["comments"]:  # keep the max seen
                by_post[key] = item
    # filter + per-company top-N
    per_company: dict[str, list[dict]] = {}
    for item in by_post.values():
        if item["comments"] >= min_comments:
            per_company.setdefault(item["stock_code"], []).append(item)
    cands: list[dict] = []
    for code, items in per_company.items():
        items.sort(key=lambda c: -c["comments"])
        cands.extend(items[:per_stock])
    cands.sort(key=lambda c: -c["comments"])
    return cands


def fetch_replies(post_url: str, limit: int = REPLY_CAP) -> list[dict]:
    """Call the opencli adapter and return normalized reply rows."""
    r = subprocess.run(
        ["opencli", "xueqiu", "replies", post_url, "--limit", str(limit), "-f", "json"],
        capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        raise RuntimeError(f"opencli rc={r.returncode}: {(r.stderr or '')[:120]}")
    return json.loads(_clean_json(r.stdout))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--per-stock", type=int, default=5,
                    help="top N most-discussed posts per company (default 5)")
    ap.add_argument("--top", type=int, default=60,
                    help="global cap on posts fetched this run")
    ap.add_argument("--hours", type=int, default=26)
    ap.add_argument("--min-comments", type=int, default=5,
                    help="posts with fewer replies are skipped")
    ap.add_argument("--limit", type=int, default=REPLY_CAP,
                    help="replies fetched per post (page 1)")
    ap.add_argument("--sleep", type=float, default=1.8)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    ensure_table(conn)

    have = {r["post_id"] for r in conn.execute("SELECT DISTINCT post_id FROM post_replies")}
    cands = [c for c in load_candidates(conn, args.hours, args.min_comments, args.per_stock)
             if c["post_id"] not in have][:args.top]
    logger.info(f"候选 {len(cands)} 帖 (窗口{args.hours}h, 评论≥{args.min_comments}, "
                f"每家top{args.per_stock}, 全局≤{args.top}; 已抓过 {len(have)})")
    if args.dry_run:
        for c in cands:
            logger.info(f"  [{c['comments']:>3}评] {c['stock_code']} {c['author'][:12]} | "
                        f"{c['post_id'].rsplit('/', 1)[-1]}")
        return 0
    if not cands:
        logger.info("无可回抓条目")
        return 0

    if not _opencli_ok():
        logger.error("opencli 不可用(未安装或 Chrome 扩展未连接),退出")
        return 2

    ok_posts = fail = total_replies = 0
    for i, c in enumerate(cands, 1):
        try:
            replies = fetch_replies(c["post_id"], args.limit)
        except Exception as e:
            fail += 1
            logger.warning(f"[{i}/{len(cands)}] ✗ {c['stock_code']} "
                           f"{c['post_id'].rsplit('/', 1)[-1]}: {str(e)[:100]}")
            if "opencli rc" in str(e):
                break  # bridge dead, stop wasting attempts
            time.sleep(args.sleep)
            continue
        now = int(time.time())
        for rep in replies:
            conn.execute(
                "INSERT OR IGNORE INTO post_replies "
                "(post_id, reply_id, stock_code, author, likes, text, created_at, reply_to, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (c["post_id"], str(rep.get("id") or ""), c["stock_code"],
                 str(rep.get("author") or ""), int(rep.get("likes") or 0),
                 str(rep.get("text") or ""), str(rep.get("created_at") or ""),
                 str(rep.get("reply_to") or ""), now))
        conn.commit()
        ok_posts += 1
        total_replies += len(replies)
        logger.info(f"[{i}/{len(cands)}] ✓ {c['stock_code']} "
                    f"{c['post_id'].rsplit('/', 1)[-1]} +{len(replies)}条回复")
        time.sleep(args.sleep)

    logger.info(f"完成: {ok_posts} 帖成功 / {fail} 失败 / 共 {total_replies} 条回复入库")
    return 0 if ok_posts or not cands else 1


if __name__ == "__main__":
    raise SystemExit(main())
