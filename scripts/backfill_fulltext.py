#!/usr/bin/env python3
"""Backfill full text for high-value posts via `opencli web article`.

The 13:00 pipeline captures timeline *card* text only (~230 char cap, set by
xueqiu's card rendering, not by us). This script picks the day's high-value
posts from monitor.db and fetches their FULL article text through the
`opencli web article` adapter (opencli-adapters/article.js) — the same channel
`src/detail_fetcher.fetch_page_opencli` uses.

2026-10-13 换通道: 原先走 `opencli browser <session> open` + 逐选择器
`extract --selector`。那条路**不触发插件钩子**，所以既不受统一限频保护、也不进
`~/.opencli/adapter-calls.jsonl` 台账（上游 jackwener/opencli#2606）。换成
`web article` 后两者都有，且不再需要手工 settle/探针选择器 —— 台账里那批
`browser:extract` 探针噪声也随之消失。

Selection (OR of):
    - engagement score = likes + comments*3 + forwards*2 >= --min-score
    - card text length >= --near-cap (likely truncated long post)

Pipeline integration (proposed):
    13:20 cron → after xueqiu-pipeline        (covers posts up to 13:00)
    07:30 cron → before intelligence prep     (covers evening/overnight posts)
    Output table post_fulltext is consumed by the intelligence prep layer;
    export_csv.py does NOT need changes (full text is read from this table).

Usage:
    python3 scripts/backfill_fulltext.py                     # top 20, last 26h
    python3 scripts/backfill_fulltext.py --top 10 --dry-run  # see candidates
    python3 scripts/backfill_fulltext.py --min-score 15 --hours 20

Requirements: opencli installed + Chrome extension connected (opencli doctor),
plus the `web article` adapter registered —— 适配器缺失时明确失败（exit 3），
绝不静默回退 legacy browser 通道（issue #74 第 1 条）。

Exit codes:
    0  正常结束（含无可回抓条目；失败未全灭时按历史口径也返回 0）
    1  候选全失败
    2  opencli 不可用（未安装或 Chrome 扩展未连接）
    3  `web article` 适配器未注册
    4  触发风控（BLOCKED_WAF 或熔断冷却），已停止本轮（issue #74 第 2 条）
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill_fulltext")

DEFAULT_DB = str(ROOT / "data" / "monitor.db")
POST_URL_RE = re.compile(r"^https?://xueqiu\.com/\d+/\d+$")


def _opencli_ok() -> bool:
    if not shutil.which("opencli"):
        return False
    try:
        r = subprocess.run(["opencli", "doctor"], capture_output=True, text=True, timeout=10)
        return "[OK] Extension: connected" in r.stdout
    except Exception:
        return False


def _article_adapter_ok() -> bool:
    """`web article` 适配器是否注册 —— 回抓的硬前置，缺失就明确失败。

    `fetch_page_opencli` 在命令未注册时会静默回退 legacy browser 探针序列，
    那条路不受统一限频保护、不进调用台账，正是 issue #74 第 1 条不让走的路。
    """
    try:
        from src.detail_fetcher import _article_adapter_available

        return bool(_article_adapter_available())
    except Exception:
        return False


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS post_fulltext (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id        TEXT NOT NULL UNIQUE,
            url            TEXT NOT NULL DEFAULT '',
            stock_code     TEXT NOT NULL DEFAULT '',
            author         TEXT NOT NULL DEFAULT '',
            published_time TEXT NOT NULL DEFAULT '',
            card_text      TEXT NOT NULL DEFAULT '',
            full_text      TEXT NOT NULL DEFAULT '',
            card_len       INTEGER NOT NULL DEFAULT 0,
            full_len       INTEGER NOT NULL DEFAULT 0,
            status         TEXT NOT NULL DEFAULT 'ok',
            fetched_at     INTEGER NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fulltext_post ON post_fulltext(post_id)")


def load_candidates(conn: sqlite3.Connection, hours: int, min_score: int,
                    near_cap: int) -> list[dict]:
    """Flatten recent posts from crawl_snapshots and rank by value."""
    since = int(time.time()) - hours * 3600
    rows = conn.execute(
        "SELECT stock_code, crawl_time, posts_data FROM crawl_snapshots "
        "WHERE crawl_time >= ? ORDER BY crawl_time DESC", (since,)).fetchall()
    seen: dict[str, dict] = {}
    for stock_code, crawl_time, posts_json in rows:
        try:
            posts = json.loads(posts_json)
        except Exception:
            continue
        for p in posts:
            url = (p.get("link") or p.get("post_id") or "").strip()
            if not POST_URL_RE.match(url):
                continue  # news/announcements have their own sources
            if p.get("type") not in ("discussion", "article"):
                continue
            card = (p.get("content") or "").strip()
            if not card:
                continue
            score = (_int(p.get("like_count")) + _int(p.get("comment_count")) * 3
                     + _int(p.get("forward_count")) * 2)
            truncated = len(card) >= near_cap
            if score < min_score and not truncated:
                continue
            key = url.rsplit("/", 1)[-1]
            if key not in seen:  # keep the first (newest) snapshot's copy
                seen[key] = {
                    "post_key": key, "url": url, "stock_code": stock_code,
                    "author": (p.get("author") or "").strip(),
                    "published_time": p.get("time") or "",
                    "card_text": card, "card_len": len(card),
                    "score": score, "truncated": truncated,
                }
    cands = sorted(seen.values(), key=lambda c: (-c["score"], -c["card_len"]))
    return cands


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def fetch_fulltext(url: str) -> tuple[str, str]:
    """Fetch one post's full text through `opencli web article`.

    复用 ``src/detail_fetcher.fetch_page_opencli``：同一份实现、同一条主通道，
    因而也共享它的超时预算、限频槽与调用台账。不要在这里另写一份 opencli 调用。

    Returns:
        ``(text, status)``，status ∈ ``ok`` | ``empty`` | ``error`` | ``opencli-error``
        | ``blocked-waf`` | ``cooldown``。
        ``opencli-error`` 表示**通道本身不可用**（调用方据此提前收工，不再空耗），
        与"这一篇抓不到"（``error``）区分开。
        ``blocked-waf`` / ``cooldown`` 是**风控信号**（撞 WAF 页 / 插件熔断冷却中），
        调用方必须停止整轮，而不是换下一篇继续撞（issue #74 第 2 条）。
    """
    from src.detail_fetcher import fetch_page_opencli

    # max_chars=None：**不要**详情页那套 8000 字上限。本脚本的存在意义就是拿到卡片
    # 之外的完整正文，截断会让长文静默存错（`full_len` 记的是截断后的长度却标 ok）。
    # 见 monitor issue #74 第 3 条。
    r = fetch_page_opencli(url, max_chars=None)
    status = r.get("status")
    if status == "ok":
        return (r.get("content") or "").strip(), "ok"
    if status in ("empty", "junk"):
        return "", "empty"
    if status == "cooldown" or r.get("blocked_waf"):
        # 风控信号与「这一篇坏了」必须分开：doctor 显示桥还连着 ≠ 还能继续打。
        return "", "blocked-waf" if r.get("blocked_waf") else "cooldown"
    # 单篇失败与桥断都落到 detail_fetcher 的 "error"；用 doctor 再判一次，
    # 只有通道真的不可用才让调用方提前终止。
    return "", "opencli-error" if not _opencli_ok() else "error"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--hours", type=int, default=26)
    ap.add_argument("--min-score", type=int, default=10,
                    help="engagement floor: likes + comments*3 + forwards*2")
    ap.add_argument("--near-cap", type=int, default=200,
                    help="card length >= this also qualifies (likely truncated)")
    ap.add_argument("--min-gain", type=int, default=50,
                    help="only keep results whose full text beats card by N chars")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    ensure_table(conn)

    have = {r["post_id"] for r in conn.execute("SELECT post_id FROM post_fulltext")}
    cands = [c for c in load_candidates(conn, args.hours, args.min_score, args.near_cap)
             if c["post_key"] not in have]
    logger.info(f"候选 {len(cands)} 条 (窗口{args.hours}h, score≥{args.min_score} 或 卡片≥{args.near_cap}字; 已库存 {len(have)})")
    if args.dry_run:
        for c in cands[:args.top]:
            logger.info(f"  [score {c['score']:>3}] {c['stock_code']} {c['author'][:12]} "
                        f"卡{c['card_len']}字 | {c['card_text'][:45]}")
        return 0
    if not cands:
        logger.info("无可回抓条目")
        return 0

    if not _opencli_ok():
        logger.error("opencli 不可用(未安装或 Chrome 扩展未连接),退出")
        return 2
    if not _article_adapter_ok():
        logger.error("web article 适配器未注册 —— 回抓拒绝回退 legacy 通道"
                     "(不受限频、不进台账),退出")
        return 3

    picked, ok, fail, no_gain = cands[:args.top], 0, 0, 0
    risk_stop = ""
    for i, c in enumerate(picked, 1):
        text, status = fetch_fulltext(c["url"])
        if status != "ok" or not text:
            fail += 1
            logger.warning(f"[{i}/{len(picked)}] ✗ {status} | {c['url']}")
            if status in ("blocked-waf", "cooldown"):
                # 独立风控状态：撞墙后换一篇继续只会把墙养得更凶（issue #74 第 2 条）
                risk_stop = status
                logger.error(f"[{i}/{len(picked)}] 触发风控({status}),停止本轮,"
                             f"剩余 {len(picked) - i} 条未尝试")
                break
            if status.startswith("opencli"):
                break  # bridge dead, stop wasting attempts
            continue
        if len(text) - c["card_len"] < args.min_gain:
            # full text adds nothing over the card (short post) — record cheaply
            no_gain += 1
            conn.execute(
                "INSERT OR IGNORE INTO post_fulltext (post_id,url,stock_code,author,"
                "published_time,card_text,full_text,card_len,full_len,status,fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (c["post_key"], c["url"], c["stock_code"], c["author"], c["published_time"],
                 c["card_text"], text, c["card_len"], len(text), "no_gain", int(time.time())))
            conn.commit()
            continue
        conn.execute(
            "INSERT OR IGNORE INTO post_fulltext (post_id,url,stock_code,author,"
            "published_time,card_text,full_text,card_len,full_len,status,fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (c["post_key"], c["url"], c["stock_code"], c["author"], c["published_time"],
             c["card_text"], text, c["card_len"], len(text), "ok", int(time.time())))
        conn.commit()
        ok += 1
        logger.info(f"[{i}/{len(picked)}] ✓ {c['stock_code']} 卡{c['card_len']}→全{len(text)}字 "
                    f"({c['author'][:10]}) | {text[:50]}")
        time.sleep(1.0 + (i % 3) * 0.4)  # polite pacing

    logger.info(f"回抓完成: 成功 {ok}, 无增益 {no_gain}, 失败 {fail}"
                + (f", 风控停轮({risk_stop})" if risk_stop else "")
                + f" (共试 {len(picked)})")
    if risk_stop:
        return 4
    return 0 if fail < len(picked) else 1


if __name__ == "__main__":
    sys.exit(main())
