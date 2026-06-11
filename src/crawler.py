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
from .db import get_existing_post_ids, get_last_crawl_time, update_last_crawl_time
from . import sentiment

# Add xueqiu-analyzer to path (configurable via XUEQIU_ANALYZER_PATH env var)
# Default: sibling directory ../xueqiu-analyzer-skill/src (works on macOS/Linux/Docker)
_DEFAULT_XA = str(Path(__file__).resolve().parent.parent.parent / "xueqiu-analyzer-skill" / "src")
_XA_PATH = os.environ.get("XUEQIU_ANALYZER_PATH", _DEFAULT_XA)
if _XA_PATH and _XA_PATH not in sys.path:
    sys.path.insert(0, _XA_PATH)

logger = logging.getLogger(__name__)


def _clean_author(raw: str) -> str:
    """Clean author name: trim trailing metadata and filter noise."""
    if not raw:
        return ""
    # "新闻" is not a real author — xueqiu news placeholder
    if raw.strip() in ("新闻", ""):
        return ""
    # "作者名\n发布于2026-..." → keep only the first line
    if "\n" in raw:
        raw = raw.split("\n")[0].strip()
    # "作者名 发布于2026-..." → also common
    if " 发布于" in raw:
        raw = raw.split(" 发布于")[0].strip()
    if " 修改于" in raw:
        raw = raw.split(" 修改于")[0].strip()
    return raw


def _extract_news_source(title: str) -> str:
    """Extract media source from news title when author/source is empty."""
    import re
    known_sources = [
        '证券时报', '财联社', '华尔街见闻', '澎湃新闻', '第一财经',
        '中国证券报', '上海证券报', '经济观察报', '21世纪经济报道',
        '时代财经', '中国基金报', '界面新闻', '新浪财经', '智通财经',
        '格隆汇', '每日经济新闻', '中证网', '证券日报', '中国经营报',
        '中国商报', '新京报', '北京商报', '财经网', '巨潮资讯', '金融界',
        '市场资讯', '财说', '智东西', '南方财经', '羊城晚报',
    ]
    for s in known_sources:
        if s in title:
            return s
    # Generic pattern
    m = re.search(r'[(（]?来源[：:]\s*(\S{2,12})[)）]?', title)
    if m:
        return m.group(1)
    return ""


# ════════════════════════════════════════════════════════
# Watchlist loading
# ════════════════════════════════════════════════════════

def load_watchlist(config: dict) -> list[dict]:
    """Load watchlist from morning-brief DB or fallback JSON.

    Returns list of {'stock_code': str, 'stock_name': str}.
    """
    # Try morning-brief DB first (configurable via config.crawler.morning_brief_db)
    mb_db = config.get("morning_brief_db", os.environ.get("MORNING_BRIEF_DB", "/root/code/morning-brief/data/morning-brief.db"))
    if mb_db and os.path.exists(mb_db):
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

def crawl_single_stock(stock_code: str, timeout: int = 1200, db_path: str | None = None) -> dict:
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
        # ── Tier 1: opencli pre-fetch (main thread, zero-WAF) ──
        _opencli_posts = []
        _opencli_notices = []
        try:
            import sys as _sys
            _sys.path.insert(0, os.path.expanduser("~/code/claude_code/xueqiu-analyzer-skill/src"))
            from xueqiu_analyzer.fetcher_opencli import is_available as _ocli_ok
            from xueqiu_analyzer.fetcher_opencli import fetch_discussions as _ocli_discs
            from xueqiu_analyzer.fetcher_opencli import fetch_notices as _ocli_notices
            if _ocli_ok():
                logger.info(f"  opencli: 预取 {stock_code}")
                try:
                    for item in (_ocli_discs(stock_code, limit=100) or []):
                        _opencli_posts.append({
                            "type": "discussion",
                            "post_id": item.get("url", "") or item.get("id", ""),
                            "title": (item.get("text", "") or "")[:100],
                            "content": (item.get("text", "") or "")[:500],
                            "link": item.get("url", ""),
                            "author": item.get("author", ""),
                            "time": item.get("created_at", ""),
                            "sentiment_score": 0.0,
                            "comment_count": item.get("replies", 0) or 0,
                            "like_count": item.get("likes", 0) or 0,
                        })
                    logger.info(f"  opencli: {len(_opencli_posts)} 条讨论")
                except Exception as e:
                    logger.warning(f"  opencli 讨论失败: {e}")
                try:
                    for item in (_ocli_notices(stock_code, limit=50) or []):
                        _opencli_notices.append({
                            "title": item.get("title", ""),
                            "time": item.get("created_at", ""),
                            "notice_type": item.get("type", ""),
                        })
                    logger.info(f"  opencli: {len(_opencli_notices)} 条公告")
                except Exception as e:
                    logger.warning(f"  opencli 公告失败: {e}")
        except Exception as e:
            logger.debug(f"  opencli 跳过: {e}")

        # ── opencli got data → skip Playwright entirely ──
        if _opencli_posts or _opencli_notices:
            logger.info(f"  opencli 已获取数据，跳过 Playwright")
            crawl_info = {
                "result": None,
                "diagnostic": {
                    "timed_out": False,
                    "error_type": None,
                    "error_message": None,
                    "crawl_duration_ms": 0,
                    "discussions_count": 0,
                    "news_count": 0,
                    "articles_count": 0,
                    "notices_count": 0,
                },
            }
        else:
            logger.info(f"  opencli 无数据，回退 Playwright (timeout={timeout}s)")
            crawl_info = _crawl_with_timeout(stock_code, timeout)
        crawl_result = crawl_info["result"]
        diagnostic = crawl_info["diagnostic"]
        result["diagnostic"] = diagnostic

        # ── Merge opencli data early (survives Playwright failure) ──
        posts = list(_opencli_posts) if _opencli_posts else []
        for item in _opencli_notices:
            result["announcements"].append({
                "title": item.get("title", ""),
                "time": item.get("time", ""),
                "notice_type": item.get("notice_type", ""),
            })

        if crawl_result is None:
            if diagnostic.get("timed_out"):
                result["status"] = "partial"
                result["error"] = diagnostic.get("error_message", f"Playwright 超时（{timeout}s）")
                logger.warning(f"{stock_code} Playwright 超时, opencli 提供 {len(posts)} 条帖子")
            else:
                result["status"] = "failed"
                result["error"] = diagnostic.get("error_message", "未知爬取错误")
            if not posts:
                elapsed = time.time() - t_start
                return result
        else:
            for d in crawl_result.discussions:
                posts.append({
                    "type": "discussion",
                    "post_id": d.link or d.content[:20],
                    "title": d.content[:100] or "",
                    "content": d.content or "",
                    "link": d.link or "",
                    "author": _clean_author(d.author or ""),
                    "time": d.time or "",
                    "sentiment_score": 0.0,
                    "comment_count": getattr(d, "comment_count", 0) or 0,
                    "forward_count": getattr(d, "forward_count", 0) or 0,
                    "like_count": getattr(d, "like_count", 0) or 0,
                })
            for n in crawl_result.news:
                posts.append({
                    "type": "news",
                    "post_id": n.link or n.title,
                    "title": n.title or "",
                    "content": n.content or "",
                    "link": n.link or "",
                    "author": _clean_author(n.source or "") or _extract_news_source(n.title or ""),
                    "time": n.time or "",
                    "sentiment_score": 0.0,
                })
            for a in crawl_result.articles:
                posts.append({
                    "type": "article",
                    "post_id": a.link or a.article_id or a.title,
                    "title": a.title or "",
                    "content": a.content or "",
                    "link": a.link or "",
                    "author": _clean_author(a.author or ""),
                    "time": a.time or "",
                    "sentiment_score": 0.0,
                })
            for nt in crawl_result.notices:
                title_raw = nt.title or ""
                lines = title_raw.split('\n')
                if len(lines) > 1:
                    title = '\n'.join(lines[1:]).strip()[:100]
                else:
                    title = title_raw.strip()[:100]
                if not title:
                    title = title_raw.strip()[:100]
                result["announcements"].append({
                    "title": title,
                    "time": nt.time or "",
                    "notice_type": nt.notice_type or "",
                })

        result["posts_count"] = len(posts)
        result["posts_data"] = posts

        # ── Time-based filtering (incremental crawl) ──
        if db_path:
            _now = time.time()
            last_crawl_ts = get_last_crawl_time(db_path, stock_code)
            if last_crawl_ts > 0:
                # 预加载所有已存储的 post_id，用于回查兜底
                # （避免时间戳判断把"上次没爬到的新帖"误过滤）
                existing_ids = get_existing_post_ids(db_path, stock_code)
                filtered_posts = []
                skipped = 0
                rescued = 0  # 时间戳判断要过滤，但 DB 里没有 → 救回
                for post in posts:
                    post_time_str = post.get("time", "")
                    post_ts = _parse_post_time(post_time_str, _now)
                    if post_ts == 0 or post_ts >= last_crawl_ts:
                        filtered_posts.append(post)
                    elif post.get("post_id") not in existing_ids:
                        # 时间戳 < last_crawl 但 post_id 不在 DB → 上次没爬到，保留
                        rescued += 1
                        filtered_posts.append(post)
                    else:
                        skipped += 1
                if skipped > 0 or rescued > 0:
                    logger.info(f"  {stock_code}: 过滤 {skipped} 条旧帖, 救回 {rescued} 条(DB无记录)")
                posts = filtered_posts
                result["posts_count"] = len(posts)
                result["posts_data"] = posts

            # Update last post time with max of current batch
            if posts:
                all_ts = []
                for p in posts:
                    ts = _parse_post_time(p.get("time", ""), _now)
                    if ts > 0:
                        all_ts.append(ts)
                if all_ts:
                    update_last_crawl_time(db_path, stock_code, max(all_ts))

        result["status"] = "success"

        # ── LLM Sentiment Analysis (batch, per stock) ──
        if posts:
            try:
                scores = sentiment.analyze_sentiment_batch(posts)
                for j, s in enumerate(scores):
                    posts[j]["sentiment_score"] = s
                result["posts_data"] = posts
            except Exception as e:
                logger.warning(f"{stock_code} sentiment 分析失败: {e}")
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


def crawl_watchlist(stocks: list[dict], timeout: int = 30, db_path: str | None = None,
                    concurrency: int = 1) -> list[dict]:
    """Crawl all stocks with configurable concurrency.

    Sequential mode (concurrency=1): simple for-loop.
    Parallel mode (concurrency>1): ThreadPoolExecutor for I/O-bound Playwright.
    Single stock failure does not block others.

    Returns list of crawl result dicts (order may differ from input when parallel).
    """
    if concurrency <= 1:
        return _crawl_sequential(stocks, timeout, db_path)
    return _crawl_parallel(stocks, timeout, db_path, concurrency)


def _crawl_sequential(stocks: list[dict], timeout: int, db_path: str | None) -> list[dict]:
    results = []
    total = len(stocks)
    for i, s in enumerate(stocks):
        code = s["stock_code"]
        logger.info(f"[{i+1}/{total}] 爬取 {code} ...")
        start = time.time()
        r = crawl_single_stock(code, timeout, db_path)
        elapsed = time.time() - start
        r["_elapsed"] = round(elapsed, 1)
        results.append(r)
        logger.info(f"  → {r['status']} ({r['posts_count']}贴, {elapsed:.1f}s)")
    success = sum(1 for r in results if r["status"] == "success")
    logger.info(f"爬取完成: {success}/{total} 成功")
    return results


def _crawl_parallel(stocks: list[dict], timeout: int, db_path: str | None,
                   concurrency: int) -> list[dict]:
    import concurrent.futures
    results = []
    total = len(stocks)
    logger.info(f"并行爬取: {total} 只股票, concurrency={concurrency}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        future_map: dict[concurrent.futures.Future, str] = {}
        for s in stocks:
            f = executor.submit(crawl_single_stock, s["stock_code"], timeout, db_path)
            future_map[f] = s["stock_code"]
        for f in concurrent.futures.as_completed(future_map):
            code = future_map[f]
            try:
                r = f.result()
            except Exception as e:
                r = {
                    "status": "failed",
                    "stock_code": code,
                    "error": str(e),
                    "posts_count": 0,
                    "posts_data": [],
                    "announcements": [],
                    "sentiment_avg": 0.0,
                    "crawl_time": int(time.time()),
                    "diagnostic": {"error_type": type(e).__name__},
                    "_elapsed": 0,
                }
            results.append(r)
            logger.info(f"  → {r['status']} ({r['posts_count']}贴, {r.get('_elapsed', '?')}s)")
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
        _crawler = None
        try:
            xq_code = _to_xueqiu_code(stock_code)
            _crawler = XueqiuCrawler({"headless": True})
            # days=N 时间窗口驱动 — 遇到超过 N 天的帖子就停止翻页,
            # max_pages/max_articles 仅做安全兜底，实际由 days 控制停止
            result_holder["result"] = _crawler.crawl(
                xq_code, max_pages=1000, max_articles=200, days=2
            )
        except Exception as e:
            result_holder["error"] = str(e)
            result_holder["error_type"] = type(e).__name__
        finally:
            if _crawler is not None:
                try:
                    _crawler.close()
                except Exception:
                    pass
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


def _parse_post_time(time_str: str, now: float) -> float:
    """Parse xueqiu post time string to Unix timestamp. Returns 0 if unparseable.

    Supported formats:
      - "X分钟前", "X小时前", "X秒前" → relative time
      - "昨天 HH:MM" → yesterday
      - "MM-DD HH:MM" → this year
      - "MM-DD" → this year 00:00
      - "HH:MM" → today
    """
    import re
    from datetime import datetime, timedelta

    if not time_str or not isinstance(time_str, str):
        return 0.0
    time_str = time_str.strip()
    if not time_str:
        return 0.0

    # "X分钟前"
    m = re.match(r'(\d+)\s*分钟前', time_str)
    if m:
        return now - int(m.group(1)) * 60

    # "X小时前"
    m = re.match(r'(\d+)\s*小时前', time_str)
    if m:
        return now - int(m.group(1)) * 3600

    # "X秒前"
    m = re.match(r'(\d+)\s*秒前', time_str)
    if m:
        return now - int(m.group(1))

    now_dt = datetime.fromtimestamp(now)

    # "昨天 HH:MM"
    m = re.match(r'昨天\s+(\d{1,2}):(\d{2})', time_str)
    if m:
        yesterday = now_dt - timedelta(days=1)
        target = yesterday.replace(
            hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0
        )
        return target.timestamp()

    # "MM-DD HH:MM"
    m = re.match(r'(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})', time_str)
    if m:
        month, day, hour, minute = (
            int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        )
        target = now_dt.replace(
            month=month, day=day, hour=hour, minute=minute, second=0, microsecond=0
        )
        if target.timestamp() > now:
            target = target.replace(year=target.year - 1)
        return target.timestamp()

    # "MM-DD"
    m = re.match(r'(\d{2})-(\d{2})$', time_str)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        target = now_dt.replace(
            month=month, day=day, hour=0, minute=0, second=0, microsecond=0
        )
        if target.timestamp() > now:
            target = target.replace(year=target.year - 1)
        return target.timestamp()

    # "HH:MM"
    m = re.match(r'(\d{1,2}):(\d{2})$', time_str)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        target = now_dt.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        return target.timestamp()

    return 0.0