"""xueqiu-monitor: crawler integration with xueqiu-analyzer

Wraps xueqiu-analyzer's XueqiuCrawler, handles watchlist loading,
per-stock crawling with timeout, and snapshot persistence.
"""

from __future__ import annotations

import concurrent.futures
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
                "SELECT stock_code, stock_name FROM watchlist WHERE is_active=1 AND is_index=0"
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
            "diagnostic": {
                "timed_out": bool,
                "error_type": str | None,
                "error_message": str | None,
                "crawl_duration_ms": int,
                "discussions_count": int,
                "news_count": int,
                "articles_count": int,
                "notices_count": int,
            },
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
        "diagnostic": {},
    }
    t_start = time.time()
    try:
        crawl_info = _crawl_with_timeout(stock_code, timeout)
        crawl_result = crawl_info["result"]
        diagnostic = crawl_info["diagnostic"]
        result["diagnostic"] = diagnostic

        if crawl_result is None:
            if diagnostic.get("timed_out"):
                result["status"] = "timeout"
                result["error"] = diagnostic.get("error_message", f"爬取超时（{timeout}s）")
                logger.warning(
                    f"{stock_code} 爬取超时 ({timeout}s, elapsed={diagnostic.get('crawl_duration_ms', 0)}ms)"
                )
            else:
                result["status"] = "failed"
                result["error"] = diagnostic.get("error_message", "未知爬取错误")
                logger.error(
                    f"{stock_code} 爬取异常: type={diagnostic.get('error_type')}, "
                    f"msg={result['error']}"
                )
            elapsed = time.time() - t_start
            logger.debug(
                f"{stock_code} 总耗时: {elapsed:.1f}s, status={result['status']}, "
                f"posts={result['posts_count']}"
            )
            return result

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
        result["diagnostic"] = {
            "timed_out": False,
            "error_type": type(e).__name__,
            "error_message": str(e),
            "crawl_duration_ms": int((time.time() - t_start) * 1000),
            "discussions_count": 0,
            "news_count": 0,
            "articles_count": 0,
            "notices_count": 0,
        }
        logger.error(f"爬取 {stock_code} 失败: {e}", exc_info=True)

    elapsed = time.time() - t_start
    logger.debug(
        f"{stock_code} 总耗时: {elapsed:.1f}s, status={result['status']}, "
        f"posts={result['posts_count']}"
    )
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

def _to_xueqiu_code(stock_code: str) -> str:
    """Convert watchlist code format to Xueqiu URL format.

    Watchlist uses dot-separated suffixes (e.g. 600519.SH, CRWV.US, 1913.HK).
    Xueqiu URLs use different conventions per market:
      - SH: 600519.SH → SH600519
      - SZ: 000858.SZ → SZ000858
      - HK: 1913.HK → 01913 (zero-pad to 5 digits)
      - US: CRWV.US → CRWV (strip .US suffix)
      - BRK.B.US → BRK.B (preserve dot within ticker)

    Returns unchanged code if format is unrecognized (e.g. already SH600519).
    """
    if stock_code.endswith('.SH'):
        return 'SH' + stock_code[:-3]
    if stock_code.endswith('.SZ'):
        return 'SZ' + stock_code[:-3]
    if stock_code.endswith('.HK'):
        num_part = stock_code[:-3]
        return num_part.zfill(5)
    if stock_code.endswith('.US'):
        return stock_code[:-3]  # strip .US suffix
    return stock_code


def _crawl_with_timeout(stock_code: str, timeout: int) -> dict:
    """Execute xueqiu-analyzer crawl with a hard timeout.

    Runs crawl in a daemon thread. If timeout expires, returns the diagnostic
    with timed_out=True — the crawl thread continues in background but the
    caller is detached. Subsequent calls to crawl_single_stock will start fresh.

    Returns:
        {
            "result": CrawlResult | None,
            "diagnostic": {
                "timed_out": bool,
                "error_type": str | None,
                "error_message": str | None,
                "crawl_duration_ms": int,
                "discussions_count": int,
                "news_count": int,
                "articles_count": int,
                "notices_count": int,
            },
        }
    """
    from xueqiu_analyzer.crawler import XueqiuCrawler
    import threading

    result_holder = {
        "result": None,
        "done": False,
        "error": None,
        "error_type": None,
    }

    def _do_crawl():
        try:
            xq_code = _to_xueqiu_code(stock_code)
            crawler = XueqiuCrawler({"headless": True})
            result_holder["result"] = crawler.crawl(
                xq_code, max_pages=3, max_articles=10
            )
        except Exception as e:
            result_holder["error"] = str(e)
            result_holder["error_type"] = type(e).__name__
        finally:
            result_holder["done"] = True

    t = threading.Thread(target=_do_crawl, daemon=True)
    thread_start = time.time()
    t.start()
    t.join(timeout=timeout)
    elapsed_ms = int((time.time() - thread_start) * 1000)

    diagnostic = {
        "timed_out": False,
        "error_type": None,
        "error_message": None,
        "crawl_duration_ms": elapsed_ms,
        "discussions_count": 0,
        "news_count": 0,
        "articles_count": 0,
        "notices_count": 0,
    }

    if not result_holder["done"]:
        # Timeout — daemon thread continues in bg, caller detached
        diagnostic["timed_out"] = True
        diagnostic["error_type"] = "timeout"
        diagnostic["error_message"] = (
            f"爬取超时（{timeout}s, elapsed={elapsed_ms}ms）"
        )
        return {"result": None, "diagnostic": diagnostic}

    if result_holder["error"] is not None:
        # Exception in crawl thread
        diagnostic["error_type"] = result_holder["error_type"]
        diagnostic["error_message"] = result_holder["error"]
        return {"result": None, "diagnostic": diagnostic}

    # Success — extract content-type counts from CrawlResult
    cr = result_holder["result"]
    if cr is not None:
        try:
            diagnostic["discussions_count"] = (
                len(cr.discussions) if cr.discussions else 0
            )
        except Exception:
            pass
        try:
            diagnostic["news_count"] = len(cr.news) if cr.news else 0
        except Exception:
            pass
        try:
            diagnostic["articles_count"] = len(cr.articles) if cr.articles else 0
        except Exception:
            pass
        try:
            diagnostic["notices_count"] = len(cr.notices) if cr.notices else 0
        except Exception:
            pass

    return {"result": result_holder["result"], "diagnostic": diagnostic}


def _compute_sentiment_avg(posts: list[dict]) -> float:
    """Placeholder: average sentiment from posts_data. All 0.0 for Phase 1."""
    if not posts:
        return 0.0
    scores = [p.get("sentiment_score", 0.0) for p in posts]
    return sum(scores) / len(scores) if scores else 0.0
