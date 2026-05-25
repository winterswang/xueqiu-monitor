"""xueqiu-monitor: crawler integration with xueqiu-analyzer

Wraps xueqiu-analyzer's XueqiuCrawler, handles watchlist loading,
per-stock crawling with timeout, and snapshot persistence.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

# Add xueqiu-analyzer to path
_XA_PATH = "/root/code/xueqiu-analyzer-skill/src"
if _XA_PATH not in sys.path:
    sys.path.insert(0, _XA_PATH)

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════
# Watchlist loading
# ════════════════════════════════════════════════════════

def load_watchlist(config: dict) -> list[dict]:
    """Load watchlist from morning-brief DB or fallback JSON.

    Returns list of {'stock_code': str, 'stock_name': str}.
    """
    # Try morning-brief DB first
    mb_db = "/root/code/morning-brief/data/morning-brief.db"
    if os.path.exists(mb_db):
        try:
            conn = sqlite3.connect(mb_db)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT stock_code, stock_name FROM watchlist WHERE is_active=1"
            ).fetchall()
            conn.close()
            if rows:
                result = [{"stock_code": r["stock_code"], "stock_name": r["stock_name"]} for r in rows]
                logger.info(f"从 morning-brief 加载 {len(result)} 只自选股")
                return result
        except Exception as e:
            logger.warning(f"morning-brief DB 读取失败: {e}")

    # Fallback: config path
    wl_path = config.get("watchlist_path", "")
    if wl_path and os.path.exists(wl_path):
        try:
            data = json.loads(Path(wl_path).read_text())
            if isinstance(data, list):
                return data
        except Exception as e:
            logger.warning(f"自选股文件读取失败: {e}")

    logger.error("无法加载自选股列表 — 请确认 morning-brief DB 或配置路径")
    return []


# ════════════════════════════════════════════════════════
# Crawler wrapper
# ════════════════════════════════════════════════════════

def crawl_single_stock(stock_code: str, timeout: int = 30) -> dict:
    """Crawl a single stock using xueqiu-analyzer.

    Returns:
        {
            "status": "success"|"failed"|"timeout",
            "stock_code": str,
            "crawl_time": int (unix),
            "posts_count": int,
            "posts_data": list[dict],  # converted from CrawlResult
            "announcements": list[dict],
            "sentiment_avg": float,
            "error": str | None,
        }
    """
    result = {
        "status": "failed",
        "stock_code": stock_code,
        "crawl_time": int(time.time()),
        "posts_count": 0,
        "posts_data": [],
        "announcements": [],
        "sentiment_avg": 0.0,
        "error": None,
    }
    try:
        # Lazy import to avoid startup overhead
        from xueqiu_analyzer.crawler import XueqiuCrawler

        crawler = XueqiuCrawler({"headless": True})
        crawl_result = crawler.crawl(stock_code, max_pages=3, max_articles=10)

        # Convert CrawlResult → dicts
        posts = []
        for d in crawl_result.discussions:
            posts.append({
                "type": "discussion",
                "post_id": d.link or d.content[:20],
                "title": d.content[:100] or "",
                "content": d.content or "",
                "author": d.author or "",
                "time": d.time or "",
                "sentiment_score": 0.0,  # placeholder — LLM later
            })
        for n in crawl_result.news:
            posts.append({
                "type": "news",
                "post_id": n.link or n.title,
                "title": n.title or "",
                "content": n.content or "",
                "author": n.source or "",
                "time": n.time or "",
                "sentiment_score": 0.0,
            })
        for a in crawl_result.articles:
            posts.append({
                "type": "article",
                "post_id": a.link or a.article_id or a.title,
                "title": a.title or "",
                "content": a.content or "",
                "author": a.author or "",
                "time": a.time or "",
                "sentiment_score": 0.0,
            })
        for nt in crawl_result.notices:
            result["announcements"].append({
                "title": nt.title or "",
                "time": nt.time or "",
                "notice_type": nt.notice_type or "",
            })

        result["posts_count"] = len(posts)
        result["posts_data"] = posts
        result["status"] = "success"
        result["sentiment_avg"] = _compute_sentiment_avg(posts)
    except Exception as e:
        result["status"] = "failed"
        result["error"] = str(e)
        logger.error(f"爬取 {stock_code} 失败: {e}", exc_info=True)
    return result


def crawl_watchlist(stocks: list[dict], timeout: int = 30) -> list[dict]:
    """Crawl all stocks sequentially. Single stock failure does not block others.

    Returns list of crawl result dicts.
    """
    results = []
    total = len(stocks)
    for i, s in enumerate(stocks):
        code = s["stock_code"]
        logger.info(f"[{i+1}/{total}] 爬取 {code} ...")
        start = time.time()
        r = crawl_single_stock(code, timeout)
        elapsed = time.time() - start
        r["_elapsed"] = round(elapsed, 1)
        results.append(r)
        logger.info(f"  → {r['status']} ({r['posts_count']}贴, {elapsed:.1f}s)")
    success = sum(1 for r in results if r["status"] == "success")
    logger.info(f"爬取完成: {success}/{total} 成功")
    return results


# ════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════

def _compute_sentiment_avg(posts: list[dict]) -> float:
    """Placeholder: average sentiment from posts_data. All 0.0 for Phase 1."""
    if not posts:
        return 0.0
    scores = [p.get("sentiment_score", 0.0) for p in posts]
    return sum(scores) / len(scores) if scores else 0.0
