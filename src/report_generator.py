"""Daily report generator — 自选股舆情日报.

Aggregates one day of crawled posts for all monitored stocks, runs one LLM
analysis per stock (full post content, leveraging minimax-m3's 1M context),
and assembles a structured Markdown daily report.

Pipeline:
  1. SQL fetch: posts_data + sentiment + alerts + hot_words from monitor.db
  2. Per-stock LLM analysis (concurrent, one call per stock)
  3. Assemble Markdown report (market thermometer + per-stock sections + hot words)
  4. Write to data/daily_reports/YYYY-MM-DD-sentiment.md
"""

import json
import logging
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from . import db

logger = logging.getLogger(__name__)


_KOL_WHITELIST_CACHE: Optional[set] = None


def _load_kol_whitelist() -> set:
    """Load KOL/media author names from etc/kol_whitelist.json."""
    global _KOL_WHITELIST_CACHE
    if _KOL_WHITELIST_CACHE is not None:
        return _KOL_WHITELIST_CACHE
    path = Path(__file__).resolve().parent.parent / "etc" / "kol_whitelist.json"
    names: set = set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        names.update(data.get("kol_authors", []))
        names.update(data.get("media_accounts", []))
    except (OSError, ValueError):
        logger.warning("kol_whitelist.json missing/invalid; KOL tagging disabled")
    _KOL_WHITELIST_CACHE = names
    return names


# ════════════════════════════════════════════════════════
# Config loading
# ════════════════════════════════════════════════════════


def load_report_config(config_path: str = "etc/config.report.json") -> dict:
    """Load report config, applying .env overrides for ARK API."""
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # Apply IMA folder_id from env if config is empty
    if not cfg.get("ima", {}).get("folder_id"):
        cfg.setdefault("ima", {})["folder_id"] = os.environ.get(
            "IMA_REPORT_FOLDER_ID", ""
        )
    return cfg


# ════════════════════════════════════════════════════════
# SQL data fetch
# ════════════════════════════════════════════════════════


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def fetch_day_post_count(db_path: str, stock_code: str, date_str: str) -> int:
    """Deduplicated day-union post count for a stock (thermometer caliber).

    2026-09-20 修: 此前取当日最新快照的 posts_count, 增量爬取语义下一天多轮
    爬取时早间帖子不在最新快照里, 计数失真且可能被 100/次 的抓取上限截断
    出假象。改为当日全部快照并集去重后的真实帖数, 与温度计口径一致。
    """
    union = db.fetch_day_posts_union(db_path, date_str, stock_code)
    return len(union.get(stock_code, []))


def fetch_stock_posts(
    db_path: str,
    stock_code: str,
    date_str: str,
    min_length: int = 30,
    max_age_days: int = 1,
) -> list[dict]:
    """Fetch recent posts for a stock, filtered from the day-union of snapshots.

    The snapshot from 雪球 is a mixed stream of latest posts + historical
    hot posts. This function filters to keep only posts authored within
    the ``max_age_days``-day window ending at the end of ``date_str``,
    so the LLM analyzes recent discussion instead of rehashing old
    high-engagement posts. Default ``max_age_days=1`` means posts
    authored on ``date_str`` only (the classic daily-report behavior).

    2026-09-20 修: 数据源从"当日最新一个快照"改为"当日全部快照并集(去重)"
    —— 增量爬取下每个快照只含水位后的新帖, 只读最新快照会丢当天早间的帖子
    (export_csv 2026-09-14 修过同款, 实测 9/13 单日丢 197 帖)。

    Filters out:
    - Reply posts ("回复@" prefix in title)
    - Posts shorter than min_length chars
    - Posts whose parsed time falls outside the window (fail-open:
      posts with unparseable time are kept)

    Sort: newest first (by timestamp), ties broken by engagement desc.
    Posts with unknown time sort last, ordered by engagement.
    """
    union = db.fetch_day_posts_union(db_path, date_str, stock_code)
    posts = union.get(stock_code)
    if not posts:
        return []

    # Compute the [window_start, day_end) window: max_age_days days
    # back from date_str, inclusive of date_str itself (local time).
    target_date = datetime.strptime(date_str, "%Y-%m-%d")
    day_end = int((target_date + timedelta(days=1)).timestamp())
    window_start = int(
        (target_date - timedelta(days=max_age_days - 1)).timestamp()
    )

    # Lazy import to avoid a module-load-time circular dependency
    from .crawler import _parse_post_time

    now = time.time()

    filtered = []
    for p in posts:
        title = (p.get("title") or "")[:200]
        content = p.get("content") or ""
        # Skip reply posts (both "回复@" and "回复 @" prefixes appear in
        # xueqiu data; the spaced form is the common one and was leaking
        # into the report as top engagement items)
        if title.startswith("回复@") or title.startswith("回复 @"):
            continue
        # Skip very short posts
        full_text = f"{title} {content}".strip()
        if len(full_text) < min_length:
            continue
        # Filter out posts authored outside the lookback window.
        # Fail-open: unparseable time (ts == 0) is kept, so missing
        # time data never blanks out a stock's entire feed.
        post_time_str = p.get("time", "")
        post_ts = _parse_post_time(post_time_str, now)
        if post_ts > 0 and not (window_start <= post_ts < day_end):
            continue
        filtered.append(
            {
                "title": title,
                "content": content[:2000],  # cap per-post length
                "author": p.get("author", ""),
                "like_count": p.get("like_count", 0),
                "forward_count": p.get("forward_count", 0),
                "comment_count": p.get("comment_count", 0),
                "link": p.get("link", ""),
                "time": post_time_str,
                "_ts": post_ts,  # internal sort key, stripped before return
            }
        )
    # Sort: engagement desc first (primary), timestamp desc second.
    # This makes high-interaction posts land first in the LLM input,
    # instead of burying them behind a wall of zero-engagement posts
    # (v0.7.5, note lever 2 - the "dan bin" miss was caused by reading
    # posts in pure time order). Unknown-time posts (ts=0) sink to the end.
    filtered.sort(key=lambda p: p["_ts"], reverse=True)
    filtered.sort(
        key=lambda p: p["like_count"]
        + p["forward_count"]
        + p["comment_count"],
        reverse=True,
    )
    for p in filtered:
        p.pop("_ts", None)
    return filtered


def fetch_sentiment_trend(
    db_path: str, stock_code: str, days: int = 14
) -> dict:
    """Fetch sentiment trend for a stock."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """SELECT stat_date, sentiment_mean, sentiment_std, posts_count
               FROM sentiment_stats
               WHERE stock_code=? AND stat_date >= ?
               ORDER BY stat_date DESC LIMIT ?""",
            (stock_code, int(time.time()) - days * 86400, days),
        ).fetchall()

        if not rows:
            return {"has_trend": False, "mean": None, "std": None, "days": 0}

        means = [r["sentiment_mean"] for r in rows if r["sentiment_mean"] != 0.0]
        if len(means) < 2:
            return {"has_trend": False, "mean": None, "std": None, "days": len(rows)}

        avg_mean = sum(means) / len(means)
        avg_std = sum(r["sentiment_std"] for r in rows if r["sentiment_std"] > 0) / max(
            1, sum(1 for r in rows if r["sentiment_std"] > 0)
        )

        # Today's value
        today_mean = rows[0]["sentiment_mean"]
        # Trend direction
        if len(means) >= 2:
            if today_mean > means[1] + avg_std * 0.5:
                trend = "上升"
            elif today_mean < means[1] - avg_std * 0.5:
                trend = "下降"
            else:
                trend = "平稳"
        else:
            trend = "数据不足"

        return {
            "has_trend": True,
            "today_mean": round(today_mean, 3),
            "avg_mean": round(avg_mean, 3),
            "avg_std": round(avg_std, 3),
            "trend": trend,
            "days": len(rows),
        }
    finally:
        conn.close()


def fetch_stock_alerts(
    db_path: str, stock_code: str, date_str: str
) -> list[dict]:
    """Fetch non-announcement alerts for a stock on a given date."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """SELECT alert_type, priority, z_score, detail
               FROM change_alert
               WHERE stock_code=? AND date(alert_time,'unixepoch','localtime')=?
               AND alert_type != 'new_announcement'
               ORDER BY z_score DESC LIMIT 5""",
            (stock_code, date_str),
        ).fetchall()
        return [
            {
                "type": r["alert_type"],
                "priority": r["priority"],
                "z_score": round(r["z_score"], 2),
                "detail": json.loads(r["detail"]) if r["detail"] else {},
            }
            for r in rows
        ]
    finally:
        conn.close()


def fetch_stock_announcements(
    db_path: str, stock_code: str, date_str: str, limit: int = 10
) -> list[dict]:
    """Fetch today's announcements for a stock, newest first.

    Returns list of {title, time, notice_type, link}. Used to surface
    official announcements (with detail-page URLs) in the LLM prompt so
    the analysis can reference primary sources alongside user discussion.
    """
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """SELECT a.id, a.ann_title, a.ann_type, a.ann_link, a.ann_detail, s.crawl_time
               FROM announcements a
               JOIN crawl_snapshots s ON a.snapshot_id = s.id
               WHERE a.stock_code=?
                 AND date(s.crawl_time,'unixepoch','localtime')=?
               ORDER BY s.crawl_time DESC LIMIT ?""",
            (stock_code, date_str, limit),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "title": r["ann_title"],
                "notice_type": r["ann_type"],
                "link": r["ann_link"],
                "detail": r["ann_detail"] or "",
                "time": str(r["crawl_time"]),
            }
            for r in rows
        ]
    finally:
        conn.close()


def fetch_hot_words(
    db_path: str, stock_code: str, date_str: str
) -> list[str]:
    """Fetch top hot words for a stock on a given date."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """SELECT word, printf('%.2f', tfidf_score) score
               FROM hot_word_event
               WHERE stock_code=? AND date(event_time,'unixepoch','localtime')=?
               ORDER BY tfidf_score DESC LIMIT 10""",
            (stock_code, date_str),
        ).fetchall()
        return [f"{r['word']}({r['score']})" for r in rows]
    finally:
        conn.close()


def fetch_hot_word_streaks(
    db_path: str, stock_code: str, date_str: str, lookback_days: int = 7
) -> list[dict]:
    """Fetch hot words with their consecutive-day streak count.

    For each of today's top hot words, counts how many of the past
    `lookback_days` days it also appeared in. Used to distinguish
    persistent narratives from newly emerging topics.

    Returns list of {word, today_tfidf, streak_days, is_persistent} sorted by
    streak_days DESC, then today_tfidf DESC.
    """
    conn = _connect(db_path)
    try:
        # Parse date_str to unix timestamp range
        target_date = datetime.strptime(date_str, "%Y-%m-%d")
        end_ts = int((target_date + timedelta(days=1)).timestamp())
        start_ts = int((target_date - timedelta(days=lookback_days - 1)).timestamp())

        # Today's hot words
        today_rows = conn.execute(
            """SELECT word, tfidf_score FROM hot_word_event
               WHERE stock_code=? AND date(event_time,'unixepoch','localtime')=?
               ORDER BY tfidf_score DESC LIMIT 15""",
            (stock_code, date_str),
        ).fetchall()
        if not today_rows:
            return []

        # History: which words appeared on which days
        hist_rows = conn.execute(
            """SELECT DISTINCT word, date(event_time,'unixepoch','localtime') as day
               FROM hot_word_event
               WHERE stock_code=? AND event_time >= ? AND event_time < ?""",
            (stock_code, start_ts, end_ts),
        ).fetchall()
        word_days: dict[str, set[str]] = {}
        for r in hist_rows:
            word_days.setdefault(r["word"], set()).add(r["day"])

        results = []
        for r in today_rows:
            word = r["word"]
            days = word_days.get(word, set())
            results.append({
                "word": word,
                "today_tfidf": round(r["tfidf_score"], 2),
                "streak_days": len(days),
                "is_persistent": len(days) >= 3,
            })
        # Sort: persistent first (by streak), then by today's tfidf
        results.sort(key=lambda x: (-x["streak_days"], -x["today_tfidf"]))
        return results
    finally:
        conn.close()


def fetch_yesterday_summary(
    db_path: str, stock_code: str, date_str: str
) -> dict:
    """Fetch yesterday's sentiment + hot words for delta comparison.

    Returns {has_data, yesterday_str, sentiment, posts_count, top_hot_words}.
    The day-over-day delta is computed in _build_analysis_prompt, not here.
    """
    conn = _connect(db_path)
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d")
        yesterday_str = (target_date - timedelta(days=1)).strftime("%Y-%m-%d")

        # Yesterday sentiment_stats
        yest_ts_start = int((target_date - timedelta(days=1)).timestamp())
        yest_ts_end = int(target_date.timestamp())
        row = conn.execute(
            """SELECT sentiment_mean, posts_count FROM sentiment_stats
               WHERE stock_code=? AND stat_date >= ? AND stat_date < ?""",
            (stock_code, yest_ts_start, yest_ts_end),
        ).fetchone()

        # Yesterday hot words (top 5)
        yest_hw = conn.execute(
            """SELECT word FROM hot_word_event
               WHERE stock_code=? AND date(event_time,'unixepoch','localtime')=?
               ORDER BY tfidf_score DESC LIMIT 5""",
            (stock_code, yesterday_str),
        ).fetchall()
        yest_words = [r["word"] for r in yest_hw] if yest_hw else []

        if not row:
            return {"has_data": False, "yesterday_str": yesterday_str}

        return {
            "has_data": True,
            "yesterday_str": yesterday_str,
            "sentiment": round(row["sentiment_mean"], 3),
            "posts_count": row["posts_count"],
            "top_hot_words": yest_words,
        }
    finally:
        conn.close()


def fetch_market_thermometer(db_path: str, date_str: str) -> list[dict]:
    """Fetch sentiment overview for all stocks on a given date.

    2026-09-20 修: 改用当日全部快照并集(db.fetch_day_posts_union), 情感与
    帖数均按并集重算 —— 此前只取当日 MAX(id) 快照, 增量爬取语义下一天多轮
    爬取时早间帖子丢失, 温度计读数只反映最后一次爬取批次(且受 100/次抓取
    上限截断)。单快照日数值与旧实现一致(并集=该快照)。

    Returns one dict per stock:
      stock_code, posts (day-union dedup count), sentiment (equal-weight
      avg), sentiment_weighted (interaction-weighted avg, see
      _weighted_sentiment), weighted_diff (weighted - equal).
    """
    union = db.fetch_day_posts_union(db_path, date_str)
    result = []
    for code, posts in union.items():
        equal, weighted, used = _sentiment_aggregates(posts)
        if weighted is None:
            # No usable per-post data -> fall back to equal weight
            weighted = equal if equal is not None else 0.0
        if equal is None:
            equal = 0.0
        result.append({
            "stock_code": code,
            "posts": len(posts),
            "sentiment": round(equal, 3),
            "sentiment_weighted": round(weighted, 3),
            "weighted_diff": round(weighted - equal, 3),
            "posts_used": used,
        })
    result.sort(key=lambda x: x["sentiment"], reverse=True)
    return result


def _sentiment_aggregates(posts: list[dict]) -> tuple[float | None, float | None, int]:
    """Equal-weight & interaction-weighted sentiment over a post list.

    Weight = like_count + comment_count + forward_count + 1 (Laplace
    floor so zero-engagement posts still count once). Posts without a
    usable sentiment_score are skipped. Returns (equal, weighted, used);
    equal/weighted are None when no post has a score.
    """
    if not posts:
        return None, None, 0
    scores: list[tuple[float, float]] = []  # (score, weight)
    for p in posts:
        s = p.get("sentiment_score")
        if s is None:
            continue
        try:
            s = float(s)
        except (TypeError, ValueError):
            continue
        w = float(p.get("like_count", 0) or 0) + float(
            p.get("comment_count", 0) or 0
        ) + float(p.get("forward_count", 0) or 0) + 1.0
        scores.append((s, w))
    if not scores:
        return None, None, 0
    total_w = sum(w for _, w in scores)
    equal = sum(s for s, _ in scores) / len(scores)
    weighted = sum(s * w for s, w in scores) / total_w if total_w > 0 else None
    return equal, weighted, len(scores)


def _weighted_sentiment(posts_data: str | None) -> tuple[float | None, int]:
    """Interaction-weighted sentiment from a snapshot's posts_data JSON.

    Weight = like_count + comment_count + forward_count + 1 (Laplace
    floor so zero-engagement posts still count once). Returns
    (weighted_mean, posts_used); (None, 0) when posts_data is empty or
    no post has a usable sentiment_score.
    """
    if not posts_data:
        return None, 0
    try:
        posts = json.loads(posts_data)
    except (ValueError, TypeError):
        return None, 0
    if not posts:
        return None, 0
    total_w = 0.0
    total_ws = 0.0
    used = 0
    for p in posts:
        s = p.get("sentiment_score")
        if s is None:
            continue
        try:
            s = float(s)
        except (TypeError, ValueError):
            continue
        w = float(p.get("like_count", 0) or 0) + float(
            p.get("comment_count", 0) or 0
        ) + float(p.get("forward_count", 0) or 0) + 1.0
        total_w += w
        total_ws += w * s
        used += 1
    if total_w <= 0 or used == 0:
        return None, 0
    return total_ws / total_w, used


# ════════════════════════════════════════════════════════
# LLM analysis
# ════════════════════════════════════════════════════════


def _get_llm_client():
    """Get LLM client — reuse the same ARK coding plan as sentiment.py."""
    from openai import OpenAI

    api_key = os.environ.get("ARK_API_KEY", "") or os.environ.get(
        "ARKCODE_API_KEY", ""
    )
    base_url = os.environ.get(
        "ARK_CODING_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding/v3"
    )
    if not api_key:
        raise RuntimeError("ARK_API_KEY/ARKCODE_API_KEY not set")

    return OpenAI(api_key=api_key, base_url=base_url, max_retries=1, timeout=300)


def _build_analysis_prompt(
    stock_name: str,
    stock_code: str,
    posts: list[dict],
    trend: dict,
    alerts: list[dict],
    hot_words: list[str],
    yesterday: Optional[dict] = None,
    streaks: Optional[list] = None,
    announcements: Optional[list[dict]] = None,
    scope: str = "今日",
    news_details: Optional[dict[str, str]] = None,
    tier: str = "deep",
) -> str:
    """Build the LLM prompt for per-stock analysis.

    Args:
        yesterday: Yesterday's sentiment + hot words for delta comparison.
        streaks: Hot words with consecutive-day streak counts, used to
            distinguish persistent narratives from new topics.
        scope: Time-window label, e.g. "今日" or "近7日" (fallback
            window). Injected into section titles so the LLM does not
            claim "today" when analyzing a multi-day window.
        news_details: {link: full_text} for news posts enriched by
            enrich_details (v2 Phase 2) — replaces the ~110-char sina
            snippet with the article body so the LLM reasons over the
            actual content instead of a headline.
        tier: v2 增量档位 (deep/std/flat) — 只切换输出指令模板,
            不决定是否分析 (全部股票照常调用)。
    """
    news_details = news_details or {}
    # Format posts (tag KOL/media authors so the LLM weights them higher)
    kol_names = _load_kol_whitelist()
    posts_text = ""
    for i, p in enumerate(posts, 1):
        engagement = (
            f"❤️{p['like_count']} 💬{p['comment_count']} 🔄{p['forward_count']}"
        )
        author = p.get("author", "") or ""
        kol_tag = " ⭐KOL" if author in kol_names else ""
        time_str = p.get("time", "") or "时间未知"
        body = p["content"]
        # news 详情注入: 有全文的 news 用全文替换摘要 (v2 Phase 2)
        detail = news_details.get(p.get("link") or "")
        if detail:
            body = f"【全文】{detail[:5000]}"
        posts_text += (
            f"\n---\n[{i}] {author}{kol_tag} | 🕐{time_str} | ({engagement})\n"
            f"{p['title']}\n{body}\n"
        )
        if i >= 100:  # safety cap
            posts_text += f"\n...（共 {len(posts)} 帖，已截取前 100 帖）\n"
            break

    # Format trend
    if trend.get("has_trend"):
        trend_text = (
            f"今日情感分: {trend['today_mean']}\n"
            f"14日均值: {trend['avg_mean']}, 标准差: {trend['avg_std']}\n"
            f"趋势: {trend['trend']} (基于 {trend['days']} 天数据)"
        )
    else:
        trend_text = f"数据积累中（仅 {trend.get('days', 0)} 天），暂无趋势"

    # Format yesterday delta
    delta_text = "无昨日数据（首次覆盖或数据缺失）"
    if yesterday and yesterday.get("has_data"):
        y_sent = yesterday["sentiment"]
        today_sent = trend.get("today_mean")
        if today_sent is not None:
            delta = round(today_sent - y_sent, 3)
            direction = "↑" if delta > 0.05 else ("↓" if delta < -0.05 else "→")
        else:
            delta = "N/A"
            direction = ""
        y_words = ", ".join(yesterday["top_hot_words"][:5]) if yesterday["top_hot_words"] else "无"
        delta_text = (
            f"昨日情感分: {y_sent} | 今日 vs 昨日: {delta} {direction}\n"
            f"昨日热词: {y_words}"
        )

    # Format alerts
    if alerts:
        alert_text = "\n".join(
            f"- {a['type']} z={a['z_score']} (P{a['priority']})" for a in alerts
        )
    else:
        alert_text = "无显著异常信号"

    # Format announcements (official filings with detail-page URLs).
    # v2 Phase 2: 高权重公告排前; 有详情(ann_detail)的附正文/解读,
    # 例行披露(翌日披露/督导等)只留标题 —— LLM 素材信源分级。
    if announcements:
        from .detail_fetcher import classify_announcement

        def _ann_rank(a: dict) -> int:
            return 0 if classify_announcement(a.get("title", "")) == "high" else 1

        ann_parts = []
        for a in sorted(announcements, key=_ann_rank):
            link = a.get("link", "")
            link_md = f" [🔗]({link})" if link else ""
            line = f"- {a['title']}{link_md}"
            if a.get("detail"):
                line += f"\n  详情: {a['detail'][:5000]}"
            ann_parts.append(line)
        ann_text = "\n".join(ann_parts)
    else:
        ann_text = "今日无新公告"

    # Format hot words with streak annotations
    if streaks:
        hw_parts = []
        for s in streaks[:10]:
            tag = f"📊连续{s['streak_days']}天" if s["is_persistent"] else "🆕新增"
            hw_parts.append(f"{s['word']}({s['today_tfidf']}) {tag}")
        hot_words_text = "\n".join(f"- {w}" for w in hw_parts)
    else:
        hot_words_text = ", ".join(hot_words) if hot_words else "无"

    return f"""你是雪球舆情分析师。请分析以下「{stock_name}（{stock_code}）」{scope}的雪球讨论。

## 情感数据
{trend_text}

## 昨日对比
{delta_text}

## 今日异常信号
{alert_text}

## 今日公告（官方披露，附详情页链接）
{ann_text}

## 今日热词（TF-IDF top，已标注连续天数）
{hot_words_text}

## {scope}讨论帖（共 {len(posts)} 帖，按互动量排序；标注 ⭐KOL 的为高影响力作者帖，可在提炼讨论焦点时优先引用）
{posts_text}

---
{_tier_instructions(tier, scope)}"""


def _tier_instructions(tier: str, scope: str) -> str:
    """按档位返回输出指令模板 (v2: 档位决定呈现, 不决定是否分析).

    - deep (深读): 新五段, 增量优先 —— 🆕新增/交锋/判断/风险/读数,
      并要求单列"⭐最有价值观点"供全场要点节挑选
    - std (标准): 只写主线新进展一段 —— 延续型讨论不重复展开
    - flat (平稳): 低流量指令 —— 无增量时允许输出一行结论
    """
    if tier == TIER_STD:
        return f"""请输出（Markdown 格式）：

### 主线新进展
{scope}讨论属于既有主线的延续。用 2-3 句说明**{scope}有何新进展/新论据/新角度**（附帖子编号）。不要重复背景介绍，不要罗列存量观点。

### 情感读数
一行：结合情感数据（今日分 vs 14日基准）与帖子内容总结情绪

⚠️ 格式要求：
- 两段标题必须用 `#### `（4 个 #）开头，严禁输出更高级别标题
- 帖子引用格式统一用 `[编号]`"""
    if tier == TIER_FLAT:
        return f"""{scope}帖子较少（低关注度）。请结合公告与资讯详情（如有）分析：

- 若确有值得注意的新事件/新观点/新风险：用 `#### ` 标题输出简短分析（新增内容 + 情感读数，共 2-4 句）
- 若{scope}确无增量（纯闲聊/零互动噪音）：只输出一行 `（无新增量）情感分 {scope}读数 X`，不要为了填篇幅编造内容

⚠️ 帖子引用格式统一用 `[编号]`，严禁输出 `#`/`##`/`###` 级别标题"""
    # deep (默认, 兼容旧调用)
    return f"""请输出结构化分析（Markdown 格式），包含以下五个部分：

### 🆕 {scope}新增
{scope}新出现的事件/观点/数据（每条一句话，附帖子编号；news/公告详情中的重要信息在此引用）。若全部为既有话题延续，请明确说明"{scope}无新增，均为存量主线演进"并简述演进点。

### 多空交锋
只写{scope}有新论据的真实交锋（看多 vs 看空的新论点，附编号）；纯延续、无新论据的分歧不要重复展开。

### 💡 值得关注的判断
{scope}最有价值的观点或判断（KOL⭐/高互动帖优先），附可信度备注（样本量/互动量）。

### ⚠️ 新增风险
只列{scope}新出现的风险（旧风险不重复）。如无则写"暂无新增风险"。

### 情感读数
一行：今日情感分 vs 14日基准（含偏离幅度），结合内容一句话定性

**⭐最有价值观点**（单独一行，供全场要点挑选）：
`⭐ [股票]一句话观点（引用编号）`

⚠️ 格式要求：
- 五个小节标题必须用 `#### `（4 个 #）开头，**严禁**输出 `#`/`##`/`###` 级别的大标题
- 正文中的帖子引用格式统一用 `[编号]`（如 `[3]`），不要加 `#` 或反斜杠"""


# Fallback window (days) when a stock has no posts on date_str itself.
# Low-traffic stocks' snapshots are dominated by historical hot posts;
# widen the window instead of showing "今日无帖子数据" (P0-2: LKNCY etc.
# had 47 posts in the snapshot, all authored before the report date).
FALLBACK_MAX_AGE_DAYS = 7

# LLM output must never introduce headings above the stock-section level
# (####): the report outline is # title / ## section / ### sector / ####
# stock. Bigger headings read like a new report and break rendering.
_MAX_LLM_HEADING_LEVEL = 5
_HEADING_RE = re.compile(r"^(#{1,6})\s*(.+?)\s*#*\s*$", re.MULTILINE)


def _normalize_llm_headings(text: str) -> str:
    """Demote any LLM-generated heading to h5 (stock-subsection level).

    The prompt asks for #### subsections, but models routinely emit # or
    ## headlines (17 of them in the 2026-08-18 report), which leak into
    the report and break the document outline. Rule: 1-4 #'s → #####
    (one level below the #### stock header); 5-6 #'s kept as-is.
    """
    def _demote(m: "re.Match[str]") -> str:
        hashes, heading_text = m.group(1), m.group(2)
        if len(hashes) < _MAX_LLM_HEADING_LEVEL:
            return f"{'#' * _MAX_LLM_HEADING_LEVEL} {heading_text}"
        return m.group(0)

    return _HEADING_RE.sub(_demote, text)


def analyze_stock(
    stock_code: str,
    stock_name: str,
    db_path: str,
    date_str: str,
    config: dict,
    news_details: Optional[dict[str, str]] = None,
) -> str:
    """Run LLM analysis for a single stock. Returns Markdown section.

    Post-count calibers surfaced in the section header:
    - 分析帖数: posts surviving fetch_stock_posts filtering (LLM input)
    - 当日帖数: day-union dedup count (thermometer caliber) — the two
      legitimately differ (replies/short-posts filtered out).

    news_details (v2 Phase 2): {link: full_text} enriched news bodies;
    announcements with fetched detail are pulled via fetch_stock_announcements.

    When the day-1 window is empty, retries with a
    FALLBACK_MAX_AGE_DAYS-day window and labels the section accordingly.
    """
    min_len = config.get("llm", {}).get("min_post_length", 30)
    day_count = fetch_day_post_count(db_path, stock_code, date_str)

    posts = fetch_stock_posts(db_path, stock_code, date_str, min_length=min_len)

    scope = "今日"
    if not posts:
        posts = fetch_stock_posts(
            db_path,
            stock_code,
            date_str,
            min_length=min_len,
            max_age_days=FALLBACK_MAX_AGE_DAYS,
        )
        if posts:
            scope = f"近{FALLBACK_MAX_AGE_DAYS}日"
            logger.info(
                f"  {stock_code}: 当日0帖（并集{day_count}），"
                f"回退{FALLBACK_MAX_AGE_DAYS}日窗口得{len(posts)}帖"
            )

    caliber = (
        f"- 分析帖数: {len(posts)}（{scope}窗口，过滤回复/短帖后） | "
        f"当日帖数: {day_count}（并集去重，温度计口径）"
    )

    if not posts:
        logger.info(
            f"  {stock_code}: 当日及近{FALLBACK_MAX_AGE_DAYS}日均无帖，跳过"
        )
        return {
            "section": (
                f"#### {stock_name} [{stock_code}]\n\n"
                f"{caliber}\n\n"
                f"今日及近{FALLBACK_MAX_AGE_DAYS}日无帖子数据。\n"
            ),
            "tier": TIER_FLAT,
            "takeaway": "",
        }

    trend = fetch_sentiment_trend(db_path, stock_code)
    alerts = fetch_stock_alerts(db_path, stock_code, date_str)
    hot_words = fetch_hot_words(db_path, stock_code, date_str)
    yesterday = fetch_yesterday_summary(db_path, stock_code, date_str)
    streaks = fetch_hot_word_streaks(db_path, stock_code, date_str)
    announcements = fetch_stock_announcements(db_path, stock_code, date_str)

    # v2 增量分档: 决定 prompt 模板与呈现格式 (全部股票照常调 LLM)
    tier = classify_stock_tier(stock_code, posts, alerts, announcements, config)

    prompt = _build_analysis_prompt(
        stock_name, stock_code, posts, trend, alerts, hot_words,
        yesterday=yesterday, streaks=streaks, announcements=announcements,
        scope=scope, news_details=news_details, tier=tier,
    )

    logger.info(
        f"  {stock_code}: {len(posts)}帖({scope}) 档位={tier}, "
        f"prompt={len(prompt)}字, 调用 LLM..."
    )

    try:
        client = _get_llm_client()
        model = config.get("llm", {}).get("model", "minimax-m3")
        t0 = time.time()
        response = client.chat.completions.create(
            model=model,
            max_tokens=32000,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        elapsed = time.time() - t0
        text = _normalize_llm_headings(response.choices[0].message.content or "")
        logger.info(f"  {stock_code}: LLM 完成 {elapsed:.1f}s, {len(text)}字")

        # 提取 ⭐最有价值观点 行 (deep 档, 供全场要点节挑选).
        # 精确锚定 "最有价值观点" 标题后的反引号行 —— 宽松全局匹配会抓到
        # 正文里的残缺片段 (2026-09-19 重跑实测: 要点节出现无头绪条目)。
        takeaway = ""
        m = re.search(
            r"最有价值观点[^\n]*\n+\s*`?⭐\s*([^\n`]{8,200})`?", text
        )
        if m:
            takeaway = m.group(1).strip()

        # Build section header + LLM output.
        # v2: 板块从章节级降为标注级; 档位徽标帮助读者扫读。
        sector = config.get("_sectors", {}).get(stock_code, "")
        sector_tag = f"（{sector}）" if sector else ""
        tier_badge = {"deep": "🔵", "std": "📈", "flat": ""}.get(tier, "")
        header = f"#### {tier_badge}{stock_name}{sector_tag} [{stock_code}]\n\n"
        header += f"{caliber}\n"
        header += f"- 情感分: {trend.get('today_mean', 'N/A')} | 趋势: {trend.get('trend', 'N/A')}\n\n"
        return {
            "section": header + text + "\n",
            "tier": tier,
            "takeaway": takeaway,
        }
    except Exception as e:
        logger.error(f"  {stock_code}: LLM 调用失败: {e}")
        return {
            "section": (
                f"#### {stock_name} [{stock_code}]\n\n"
                f"{caliber}\n\nLLM 分析失败: {e}\n"
            ),
            "tier": tier,
            "takeaway": "",
        }


# ════════════════════════════════════════════════════════
# Report assembly
# ════════════════════════════════════════════════════════


def _build_thermometer_section(thermometer: list, stocks_cfg: dict) -> str:
    """Build market thermometer section.

    Columns: 股票 / 名称 / 帖子数 / 情感(等权) / 情感(加权) / 加权差.
    The interaction-weighted column exposes the divergence between the
    "loud core circle" (high-engagement posts) and the "silent majority"
    (low-engagement posts) - the equal-weight average alone carries a
    systematic optimism bias on high-attention stocks (v0.7.5, note
    "舆情日报信息密度审查 v2" lever 1).
    """
    lines = ["## 一、市场温度计\n"]
    lines.append("| 股票 | 名称 | 帖子数 | 情感(等权) | 情感(加权) | 加权差 |")
    lines.append("|------|------|--------|-----------|-----------|--------|")
    for t in thermometer:
        code = t["stock_code"]
        name = stocks_cfg.get(code, {}).get("name", code)
        sent = t["sentiment"]
        sent_w = t.get("sentiment_weighted", sent)
        diff = t.get("weighted_diff", 0.0)
        # Emoji based on sentiment
        if sent > 0.1:
            emoji = "🟢"
        elif sent < -0.1:
            emoji = "🔴"
        else:
            emoji = "⚪"
        lines.append(
            f"| {code} | {name} | {t['posts']} | {emoji} {sent:+.3f} | {sent_w:+.3f} | {diff:+.3f} |"
        )

    # Summary line
    if thermometer:
        most_positive = max(thermometer, key=lambda x: x.get("sentiment_weighted", x["sentiment"]))
        most_negative = min(thermometer, key=lambda x: x.get("sentiment_weighted", x["sentiment"]))
        p_name = stocks_cfg.get(most_positive["stock_code"], {}).get(
            "name", most_positive["stock_code"]
        )
        n_name = stocks_cfg.get(most_negative["stock_code"], {}).get(
            "name", most_negative["stock_code"]
        )
        lines.append("")
        lines.append(
            f"最积极(加权): **{p_name}**({most_positive.get('sentiment_weighted', most_positive['sentiment']):+.3f}) | "
            f"最消极(加权): **{n_name}**({most_negative.get('sentiment_weighted', most_negative['sentiment']):+.3f})"
        )

    return "\n".join(lines) + "\n"

def _group_by_sector(stocks_cfg: dict) -> dict:
    """Group stocks by sector."""
    sectors: dict[str, list[str]] = {}
    for code, info in stocks_cfg.items():
        sector = info.get("sector", "其他")
        sectors.setdefault(sector, []).append(code)
    return sectors


# ════════════════════════════════════════════════════════
# v2: 增量分档 / 跨日状态 / 变化温度计 (2026-09-20)
# ════════════════════════════════════════════════════════

# 档位只决定 prompt 模板与呈现格式, 不决定是否调 LLM (全部 41 只照常分析)
TIER_DEEP = "deep"   # 深读: 新五段
TIER_STD = "std"     # 标准: 主线新进展一段
TIER_FLAT = "flat"   # 平稳: 低流量指令, 无增量时 LLM 自判输出一行


def classify_stock_tier(
    stock_code: str,
    posts: list[dict],
    alerts: list[dict],
    announcements: list[dict],
    config: dict,
) -> str:
    """增量判定三档 (v2). 硬通道优先, 其余按互动量/帖数.

    硬通道 (任一命中 → 深读):
    - 当日有 KOL 白名单作者帖 (kol_whitelist.json)
    - 当日 P0/P1 告警 (z-score 异动/高权重公告)
    - 当日高权重公告 (classify_announcement=='high')
    规则: 当日互动总量 ≥ tier.deep_engagement (默认 100) → 深读;
    当日帖 ≥ 3 → 标准; 其余 → 平稳.
    """
    from .detail_fetcher import classify_announcement

    kol = _load_kol_whitelist()
    if any((p.get("author") or "") in kol for p in posts):
        return TIER_DEEP
    if any(a.get("priority") in ("P0", "P1") for a in alerts):
        return TIER_DEEP
    if any(
        classify_announcement(a.get("title", "")) == "high" for a in announcements
    ):
        return TIER_DEEP
    engagement = sum(
        int(p.get("like_count") or 0)
        + int(p.get("comment_count") or 0)
        + int(p.get("forward_count") or 0)
        for p in posts
    )
    tier_cfg = config.get("tier", {})
    if engagement >= int(tier_cfg.get("deep_engagement", 100)):
        return TIER_DEEP
    if len(posts) >= int(tier_cfg.get("std_min_posts", 3)):
        return TIER_STD
    return TIER_FLAT


def _summary_path(output_dir: Path, date_str: str) -> Path:
    return output_dir / f"{date_str}-summary.json"


def load_prev_summary(
    output_dir: Path, date_str: str, lookback_days: int = 3
) -> Optional[dict]:
    """读最近一份 v2 summary.json (最多回看 lookback_days 天, 断更即弃)."""
    try:
        base = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None
    for i in range(1, lookback_days + 1):
        p = _summary_path(output_dir, (base - timedelta(days=i)).strftime("%Y-%m-%d"))
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                continue
    return None


def write_summary(output_dir: Path, date_str: str, data: dict) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    _summary_path(output_dir, date_str).write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def _sentiment_delta(today_weighted: float, prev: Optional[dict], code: str) -> Optional[float]:
    """今日加权 vs 昨日加权 (v2 summary 基准). 无昨日数据返回 None."""
    if not prev:
        return None
    y = (prev.get("stocks") or {}).get(code, {}).get("weighted")
    if y is None:
        return None
    return round(today_weighted - float(y), 3)


def _build_thermometer_v2(
    thermometer: list[dict],
    prev: Optional[dict],
    stocks_cfg: dict,
    deep_set: set[str],
) -> tuple[str, list[dict]]:
    """温度计 v2: 只写变化 (全场锚点 + 转暖/转冷榜), 平稳股一行列举.

    Returns (markdown, rows) — rows 带 delta 供要点节候选复用。
    """
    rows: list[dict] = []
    for t in thermometer:
        code = t["stock_code"]
        name = stocks_cfg.get(code, {}).get("name", code)
        delta = _sentiment_delta(t.get("sentiment_weighted", t["sentiment"]), prev, code)
        rows.append({**t, "name": name, "delta": delta})

    warming = sorted(
        [r for r in rows if r["delta"] is not None and r["delta"] >= 0.15],
        key=lambda r: -r["delta"],
    )
    cooling = sorted(
        [r for r in rows if r["delta"] is not None and r["delta"] <= -0.15],
        key=lambda r: r["delta"],
    )
    flat = [r for r in rows if r not in warming and r not in cooling]
    has_prev = any(r["delta"] is not None for r in rows)

    lines = ["## 二、市场温度计\n"]
    if not has_prev:
        lines.append(
            "*首日无昨日基准，「较昨日」明日启用；当前按今日加权情感排列。*\n"
        )

    # 全场锚点
    if rows:
        w_vals = [r.get("sentiment_weighted", r["sentiment"]) for r in rows]
        mkt_w = sum(w_vals) / len(w_vals)
        most_pos = max(rows, key=lambda r: r.get("sentiment_weighted", r["sentiment"]))
        most_neg = min(rows, key=lambda r: r.get("sentiment_weighted", r["sentiment"]))
        lines.append(
            f"**全场**: 加权情感 {mkt_w:+.3f} | 转暖 {len(warming)} 只 · "
            f"转冷 {len(cooling)} 只 · 平稳 {len(flat)} 只\n"
        )
        lines.append(
            f"最积极: **{most_pos['name']}**({most_pos.get('sentiment_weighted'):+.3f}) | "
            f"最消极: **{most_neg['name']}**({most_neg.get('sentiment_weighted'):+.3f})\n"
        )

    def _table(group: list[dict], title: str) -> None:
        if not group:
            return
        lines.append(f"\n### {title}\n")
        lines.append("| 股票 | 名称 | 帖数 | 情感(加权) | 较昨日 | 深读 |")
        lines.append("|------|------|------|-----------|--------|------|")
        for r in group:
            posts_disp = f"{r['posts']}+" if r["posts"] >= 100 else str(r["posts"])
            deep_mark = "🔵" if r["stock_code"] in deep_set else ""
            lines.append(
                f"| {r['stock_code']} | {r['name']} | {posts_disp} | "
                f"{r.get('sentiment_weighted', r['sentiment']):+.3f} | "
                f"{r['delta']:+.3f} | {deep_mark} |"
            )

    _table(warming, "🔺 显著转暖（Δ加权 ≥ +0.15）")
    _table(cooling, "🔻 显著转冷（Δ加权 ≤ -0.15）")

    if flat:
        flat_str = "、".join(
            f"{r['name']}({r.get('sentiment_weighted', r['sentiment']):+.2f})"
            for r in sorted(flat, key=lambda r: -r.get("sentiment_weighted", r["sentiment"]))
        )
        lines.append(f"\n**平稳**（无显著变化）: {flat_str}\n")

    return "\n".join(lines) + "\n", rows


def fetch_day_alerts(db_path: str, date_str: str) -> list[dict]:
    """当日全部 P0/P1 告警 (跨股, 供要点节候选)."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """SELECT stock_code, alert_type, priority, z_score, detail
               FROM change_alert
               WHERE date(alert_time,'unixepoch','localtime')=? AND priority IN ('P0','P1')
               ORDER BY z_score DESC LIMIT 20""",
            (date_str,),
        ).fetchall()
        return [
            {
                "stock_code": r["stock_code"], "type": r["alert_type"],
                "priority": r["priority"], "z_score": round(r["z_score"], 2),
                "detail": json.loads(r["detail"]) if r["detail"] else {},
            }
            for r in rows
        ]
    finally:
        conn.close()


def _build_highlights_section(
    db_path: str,
    date_str: str,
    stocks_cfg: dict,
    thermo_rows: list[dict],
    takeaways: list[str],
    config: dict,
) -> str:
    """今日要点 (v2): 四路候选信号 → 一次 LLM 调用凝成 3-5 条; 失败规则降级.

    候选来源: P0/P1 告警 / 高权重公告 / 温度计 Δ 显著变化 / KOL 最有价值观点。
    """
    from .detail_fetcher import classify_announcement

    def _name(code: str) -> str:
        return stocks_cfg.get(code, {}).get("name", code)

    candidates: list[str] = []
    for a in fetch_day_alerts(db_path, date_str):
        title = a["detail"].get("title") or a["type"]
        candidates.append(
            f"- [告警P{a['priority'][1]}] {_name(a['stock_code'])}: {title} (z={a['z_score']})"
        )
    conn = _connect(db_path)
    try:
        ann_rows = conn.execute(
            """SELECT a.stock_code, a.ann_title FROM announcements a
               JOIN crawl_snapshots s ON a.snapshot_id = s.id
               WHERE date(s.crawl_time,'unixepoch','localtime')=?""",
            (date_str,),
        ).fetchall()
    finally:
        conn.close()
    for r in ann_rows:
        if classify_announcement(r["ann_title"]) == "high":
            candidates.append(f"- [公告] {_name(r['stock_code'])}: {r['ann_title'][:60]}")
    for r in thermo_rows:
        if r["delta"] is not None and abs(r["delta"]) >= 0.15:
            arrow = "转暖" if r["delta"] > 0 else "转冷"
            candidates.append(
                f"- [情绪{arrow}] {r['name']}: 加权情感较昨日 {r['delta']:+.2f}"
            )
    for t in takeaways[:8]:
        candidates.append(f"- [观点] {t[:120]}")

    lines = ["## 一、今日要点\n"]
    if not candidates:
        lines.append("今日无显著异动、高权重公告或增量观点。\n")
        return "\n".join(lines)

    # 一次轻量 LLM 调用: 挑选并改写为"公司|发生了什么|为什么重要"
    prompt = (
        "以下是一组今日股票舆情候选信号（告警/公告/情绪变化/观点）。"
        "请挑选 3-5 件最值得投资者关注的，每件改写为一行：**公司｜发生了什么｜为什么重要**。"
        "优先级：重大事件告警与高权重公告 > 显著情绪反转 > 高质量观点。"
        "不要编造候选之外的信息，保留具体数字。直接输出行列表（以 - 开头），无任何标题。\n\n"
        + "\n".join(candidates[:30])
    )
    try:
        client = _get_llm_client()
        model = config.get("llm", {}).get("model", "minimax-m3")
        rsp = client.chat.completions.create(
            model=model, max_tokens=2000,
            messages=[{"role": "user", "content": prompt}], temperature=0.2,
        )
        text = (rsp.choices[0].message.content or "").strip()
        bullets = [l for l in text.splitlines() if l.strip().startswith("-")]
        if bullets:
            lines.append("\n".join(bullets[:6]) + "\n")
            return "\n".join(lines)
    except Exception as e:
        logger.warning(f"要点节 LLM 失败, 规则降级: {e}")
    # 规则降级: 原样输出 top 候选
    lines.extend(candidates[:6])
    return "\n".join(lines) + "\n"


def _build_mainlines_section(
    db_path: str, date_str: str, stocks_cfg: dict, lookback_days: int = 7
) -> str:
    """持续主线 (v2): 跨股热词聚合表, 只列今日仍在活跃的主线.

    主线 = 跨 >=2 只股票, 或单股 streak >=3 天的叙事词。
    昨日有、今日无的主线自然不出现 (停滞主线零占位)。
    """
    conn = _connect(db_path)
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d")
        end_ts = int((target + timedelta(days=1)).timestamp())
        start_ts = int((target - timedelta(days=lookback_days - 1)).timestamp())
        today_rows = conn.execute(
            """SELECT word, stock_code, MAX(tfidf_score) tf
               FROM hot_word_event
               WHERE date(event_time,'unixepoch','localtime')=?
               GROUP BY word, stock_code ORDER BY tf DESC LIMIT 400""",
            (date_str,),
        ).fetchall()
        hist_rows = conn.execute(
            """SELECT DISTINCT word, date(event_time,'unixepoch','localtime') day
               FROM hot_word_event WHERE event_time >= ? AND event_time < ?""",
            (start_ts, end_ts),
        ).fetchall()
    finally:
        conn.close()

    word_days: dict[str, set[str]] = {}
    for r in hist_rows:
        word_days.setdefault(r["word"], set()).add(r["day"])
    word_stocks: dict[str, list[str]] = {}
    word_tf: dict[str, float] = {}
    for r in today_rows:
        word_stocks.setdefault(r["word"], []).append(r["stock_code"])
        word_tf[r["word"]] = max(word_tf.get(r["word"], 0), r["tf"])

    # 复用热词节的个股名过滤 (股票名碎片不构成主线).
    # 碎片判定 (2026-09-19 重跑实测增强): 词的任一 token 命中
    # 名字 token / 代码 / 或是任一股票名的子串 → 名字碎片, 丢弃
    # ("腾讯 控股"含"腾讯"、"sk skhy"含代码前缀); 重复二元组
    # ("ai ai") 同样低信息, 丢弃。
    import jieba
    name_tokens: set[str] = set()
    name_strings: list[str] = []
    for code, info in stocks_cfg.items():
        name = info.get("name", "")
        if name:
            name_tokens |= {t.lower() for t in jieba.cut(name) if len(t.strip()) >= 2}
            name_strings.append(name.lower())
        name_tokens.add(code.lower())
        name_tokens.add(code.split(".")[0].lower())

    def _is_name_fragment(word: str) -> bool:
        tokens = [t.lower() for t in word.split() if t.strip()]
        if len(tokens) >= 2 and len(set(tokens)) == 1:
            return True  # "ai ai" 式重复组
        for t in tokens:
            if t in name_tokens:
                return True
            if len(t) >= 2 and any(t in nm for nm in name_strings):
                return True
        return False

    lines = ["## 四、持续主线（今日仍在演进）\n"]
    rows_out = []
    for word, stocks in word_stocks.items():
        wl = (word or "").lower().strip()
        if not wl or len(wl) < 2 or word.isdigit():
            continue
        if _is_name_fragment(word):
            continue
        days = len(word_days.get(word, set()))
        if len(stocks) >= 2 or days >= 3:
            rows_out.append((word, days, stocks, word_tf[word]))
    rows_out.sort(key=lambda x: (-len(x[2]), -x[1], -x[3]))
    if not rows_out:
        lines.append("今日无持续主线（均为单日新话题）。\n")
        return "\n".join(lines)
    lines.append("| 主线 | 活跃天数 | 关联 | 今日热度 |")
    lines.append("|------|----------|------|----------|")
    for word, days, stocks, tf in rows_out[:12]:
        names = "、".join(
            stocks_cfg.get(s, {}).get("name", s) for s in stocks[:4]
        ) + ("…" if len(stocks) > 4 else "")
        lines.append(f"| {word} | {days} | {names} | {tf:.1f} |")
    return "\n".join(lines) + "\n"


def enrich_details(
    db_path: str, date_str: str, config: dict
) -> tuple[dict[str, dict[str, str]], int, int]:
    """详情补全 (v2 Phase 2): news 全文 + 高权重公告详情, 供 LLM 深度分析。

    - news: 当日并集 → filter_news_posts 三道闸(时效/噪音/限量) → 智谱 reader
      抓全文 (detail_fetch_log 当日缓存, 失败标记防重试)
    - 公告: classify_announcement=='high' 且有 http 链接 → reader 抓详情
      (巨潮乱码自动降级标题搜索), 结果持久化到 announcements.ann_detail
    - 并发 detail.concurrency (默认 5); 单条失败不阻塞 —— 全程 try 守护,
      enrich 失败绝不影响日报生成。

    Returns: ({stock_code: {link: full_text}}, n_news, n_ann)
    """
    from . import detail_fetcher

    detail_cfg = config.get("detail", {})
    concurrency = int(detail_cfg.get("concurrency", 5))
    news_limit = int(detail_cfg.get("news_per_stock", 5))
    # 确保迁移到位 (ann_detail 列 / detail_fetch_log 表; 幂等)
    db.init_db(db_path)
    union = db.fetch_day_posts_union(db_path, date_str)

    # ── news 任务收集 (过滤闸后) ──
    news_tasks: list[tuple[str, str]] = []  # (stock_code, link)
    for code, posts in union.items():
        news_posts = [p for p in posts if (p.get("type") or "") == "news"]
        if not news_posts:
            continue
        for p in detail_fetcher.filter_news_posts(
            news_posts, date_str, per_stock_limit=news_limit
        ):
            link = (p.get("link") or "").strip()
            if link.startswith("http"):
                news_tasks.append((code, link))

    # ── 公告任务收集 (高权重 + 无详情 + 有链接) ──
    conn = _connect(db_path)
    try:
        ann_rows = conn.execute(
            """SELECT a.id, a.ann_title, a.ann_link, a.ann_detail
               FROM announcements a
               JOIN crawl_snapshots s ON a.snapshot_id = s.id
               WHERE date(s.crawl_time,'unixepoch','localtime')=?""",
            (date_str,),
        ).fetchall()
    finally:
        conn.close()
    ann_tasks: list[tuple[int, str, str]] = []  # (ann_id, title, link)
    for r in ann_rows:
        title, link = r["ann_title"], (r["ann_link"] or "").strip()
        if r["ann_detail"]:
            continue  # 已有详情(当日已抓或历史)
        if detail_fetcher.classify_announcement(title) != "high":
            continue
        if link.startswith("http"):
            ann_tasks.append((r["id"], title, link))

    logger.info(
        f"  详情任务: news {len(news_tasks)} 条 + 高权重公告 {len(ann_tasks)} 条"
    )

    def _fetch_news(task: tuple[str, str]) -> tuple[str, str, str]:
        code, link = task
        d = detail_fetcher.fetch_detail_cached(db_path, link)
        return code, link, d["content"] if d["status"] == "ok" else ""

    def _fetch_ann(task: tuple[int, str, str]) -> tuple[int, str]:
        ann_id, title, link = task
        return ann_id, detail_fetcher.fetch_announcement_detail(db_path, link, title)

    news_details: dict[str, dict[str, str]] = {}
    n_news = n_ann = 0
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for code, link, content in pool.map(_fetch_news, news_tasks):
            if content:
                news_details.setdefault(code, {})[link] = content
                n_news += 1
        for ann_id, detail in pool.map(_fetch_ann, ann_tasks):
            if detail:
                db.set_ann_detail(db_path, ann_id, detail)
                n_ann += 1

    logger.info(f"  详情补全完成: news 全文 {n_news} 条, 公告详情 {n_ann} 条")
    return news_details, n_news, n_ann


def generate_daily_report(
    config_path: str = "etc/config.report.json",
    date_str: Optional[str] = None,
) -> str:
    """Generate the full daily report (v2 format) and return Markdown.

    v2 结构 (2026-09-20): 今日要点 → 温度计(只写变化) → 个股深读(按增量
    排序, 平稳股紧凑化) → 持续主线 → 尾注。组织主轴是"今天与昨天比发生了
    什么", 静态描述压缩, 无增量零占位。跨日状态落 {date}-summary.json。

    Args:
        config_path: Path to report config JSON.
        date_str: Date in YYYY-MM-DD format. Defaults to today.

    Returns:
        Markdown content of the report.
    """
    cfg = load_report_config(config_path)
    db_path = cfg["db_path"]
    stocks_cfg = cfg["stocks"]
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    output_dir = Path(cfg.get("report_output_dir", "data/daily_reports"))
    prev_summary = load_prev_summary(output_dir, date_str)

    logger.info(
        f"=== 自选股舆情日报 v2 生成开始 {date_str} "
        f"(昨日summary: {'有' if prev_summary else '无'}) ==="
    )

    # 板块映射注入 config (analyze_stock 的 header 标注用, 不入配置文件)
    cfg["_sectors"] = {
        code: info.get("sector", "") for code, info in stocks_cfg.items()
    }

    # ── Section 2 data: Market thermometer (SQL only, no LLM) ──
    logger.info("[1/4] 生成市场温度计数据...")
    thermometer = fetch_market_thermometer(db_path, date_str)

    # ── Detail enrichment (v2 Phase 2): news 全文 + 高权重公告详情 ──
    news_details: dict[str, dict[str, str]] = {}
    if cfg.get("detail", {}).get("enabled", True):
        logger.info("[1.5/4] 详情补全 (news 全文 + 高权重公告详情)...")
        try:
            news_details, _, _ = enrich_details(db_path, date_str, cfg)
        except Exception as e:
            logger.error(f"详情补全失败(不阻塞日报): {e}")

    # ── Per-stock analysis (LLM, 全部股票; 档位决定呈现) ──
    logger.info("[2/4] 逐股票 LLM 分析 (增量分档)...")
    concurrency = cfg.get("llm", {}).get("concurrency", 3)
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {}
        for code, info in stocks_cfg.items():
            future = executor.submit(
                analyze_stock,
                code,
                info["name"],
                db_path,
                date_str,
                cfg,
                news_details.get(code),
            )
            futures[future] = code

        for future in as_completed(futures):
            code = futures[future]
            try:
                results[code] = future.result()
            except Exception as e:
                logger.error(f"  {code}: 分析异常: {e}")
                results[code] = {
                    "section": (
                        f"#### {stocks_cfg[code]['name']} [{code}]\n\n分析异常: {e}\n"
                    ),
                    "tier": TIER_STD,
                    "takeaway": "",
                }

    deep_set = {c for c, r in results.items() if r["tier"] == TIER_DEEP}
    takeaways = [r["takeaway"] for r in results.values() if r.get("takeaway")]

    # ── Thermometer render (需要 deep_set, 故在分析后) ──
    thermo_md, thermo_rows = _build_thermometer_v2(
        thermometer, prev_summary, stocks_cfg, deep_set
    )

    # ── Section 1: 今日要点 (LLM 一次调用, 候选来自告警/公告/Δ/观点) ──
    logger.info("[3/4] 生成今日要点...")
    try:
        highlights_md = _build_highlights_section(
            db_path, date_str, stocks_cfg, thermo_rows, takeaways, cfg
        )
    except Exception as e:
        logger.error(f"要点节失败(不阻塞): {e}")
        highlights_md = "## 一、今日要点\n\n生成失败。\n"

    # ── Section 4: 持续主线 ──
    try:
        mainlines_md = _build_mainlines_section(db_path, date_str, stocks_cfg)
    except Exception as e:
        logger.error(f"主线节失败(不阻塞): {e}")
        mainlines_md = "## 四、持续主线\n\n生成失败。\n"

    # ── Assemble: 深读 → 标准 → 平稳(紧凑) ──
    deep_codes = sorted(
        (c for c, r in results.items() if r["tier"] == TIER_DEEP),
        key=lambda c: stocks_cfg[c].get("name", c),
    )
    std_codes = sorted(
        (c for c, r in results.items() if r["tier"] == TIER_STD),
        key=lambda c: stocks_cfg[c].get("name", c),
    )
    flat_rows: list[str] = []
    flat_full: list[str] = []
    for c, r in results.items():
        if r["tier"] != TIER_FLAT:
            continue
        name = stocks_cfg.get(c, {}).get("name", c)
        section = r["section"]
        # section = "#### 标题\n\n- meta行...\n\n正文"。LLM 自判无增量时
        # 正文首行以"（无新增量）"开头 → 紧凑列表; 有实质内容的保留小节。
        # (首行判定: 早版用 body[:60] 窗口漏检 meta 行之后的正文, 全部误进
        # 完整小节 — 2026-09-19 重跑实测修复)
        parts = section.split("\n\n", 2)
        body = parts[2] if len(parts) > 2 else ""
        first_line = body.strip().splitlines()[0].strip() if body.strip() else ""
        if first_line.startswith("（无新增量）"):
            one_line = first_line[:90]
            flat_rows.append(f"- **{name}** {one_line}")
        else:
            flat_full.append(section)

    n_deep, n_std, n_flat = len(deep_codes), len(std_codes), len(flat_rows) + len(flat_full)
    stock_md = ["## 三、个股深读\n"]
    for c in deep_codes + std_codes:
        stock_md.append(results[c]["section"])
    if flat_full:
        stock_md.append("\n### 其他今日有增量的股票\n")
        stock_md.extend(flat_full)
    if flat_rows:
        stock_md.append("\n### 平稳股（无新增量，仅读数）\n")
        stock_md.extend(flat_rows)

    # ── 尾注 ──
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    notes = [
        f"今日口径: 深读 {n_deep} 只 · 标准 {n_std} 只 · 平稳 {n_flat} 只"
        f"（全部 {len(stocks_cfg)} 只均经 LLM 分析，档位只决定呈现）",
        "帖数 `100+` 为单次抓取上限截断值；标注「近7日」的股票当日无帖、已回退 7 日窗口。",
        f"news/公告详情来源: 智谱 reader（今日注入 {sum(len(v) for v in news_details.values())} 条 news 全文）；主线与热词来自 TF-IDF。",
    ]
    # 汇总当日异常涌现热词进尾注 (≤5 个)
    notes_md = "\n".join(f"- {n}" for n in notes)

    report = f"""# 📊 自选股舆情日报

**日期**: {date_str}
**生成时间**: {now_str}
**覆盖股票**: {len(stocks_cfg)} 只

---

{highlights_md}

---

{thermo_md}

---

{"".join(stock_md)}

---

{mainlines_md}

---

**尾注**

{notes_md}

*本报告由 xueqiu-monitor v2 自动生成，数据来源：雪球。LLM 分析模型：MiniMax-M3（经火山方舟 coding plan）。*
"""

    # Save report + cross-day summary (v2)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{date_str}-sentiment.md"
    output_path.write_text(report, encoding="utf-8")
    logger.info(f"日报已保存: {output_path}")

    try:
        weighted_map = {
            r["stock_code"]: r.get("sentiment_weighted", r["sentiment"])
            for r in thermometer
        }
        write_summary(
            output_dir,
            date_str,
            {
                "date": date_str,
                "stocks": {
                    c: {
                        "tier": r["tier"],
                        "weighted": weighted_map.get(c),
                    }
                    for c, r in results.items()
                },
            },
        )
    except Exception as e:
        logger.warning(f"summary.json 写入失败(不影响日报): {e}")

    return report


def _build_hot_words_section(
    db_path: str, date_str: str, stocks_cfg: dict
) -> str:
    """Build cross-stock hot words section (v0.8 rewrite).

    Filter per-stock FIRST, then aggregate across stocks. The old global
    top-30 approach let name fragments ("宁德 时代" tfidf=8.18) crowd out
    narrative words ("特斯拉" tfidf=1.74). Output has two tiers: cross-stock
    co-occurrence words and per-stock top narrative words.
    """
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """SELECT stock_code, word, tfidf_score
               FROM hot_word_event
               WHERE date(event_time,'unixepoch','localtime')=?""",
            (date_str,),
        ).fetchall()
        if not rows:
            return "## 三、今日热词\n\n今日无热词数据。\n"

        # Per-stock name token sets (jieba segmentation of names)
        import jieba
        all_name_tokens: set[str] = set()
        code_names: set[str] = set()
        for code, info in stocks_cfg.items():
            name = info.get("name", "")
            if name:
                all_name_tokens |= {t.lower() for t in jieba.cut(name) if t.strip()}
            code_names.add(code.lower())
            code_names.add(code.split(".")[0].lower())

        # Common low-signal stopwords + SEC announcement boilerplate tokens
        stopwords = {
            "ai", "股票", "投资", "市场", "今天", "今日", "现在", "可以",
            "什么", "一个", "这个", "我们", "他们", "已经", "没有", "觉得",
            "公司", "股价", "买入", "卖出", "持有", "仓位", "操作",
            "accession", "securities", "number", "size", "statement",
            "changes", "file", "form", "kb", "report", "inc", "the",
            "and", "of", "in", "for", "filed", "commission", "beneficial",
            "ownership", "statement of", "in beneficial", "of changes",
            "changes in", "securities accession", "accession number",
            "of securities", "size kb", "number size", "size number",
            "浊静 徐清",
        }

        # Filter per stock first: word -> {stock_code: max_tfidf}
        single_stock: dict[str, dict[str, float]] = {}
        for r in rows:
            word = (r["word"] or "").strip()
            wl = word.lower()
            code = r["stock_code"]
            score = float(r["tfidf_score"] or 0.0)
            if len(word) < 2 or word.isdigit():
                continue
            if wl in stopwords or word in stopwords:
                continue
            if wl in code_names or word in code_names:
                continue
            toks = [t.lower() for t in word.split()]
            is_name_variant = False
            if len(toks) > 1:
                if any(t in all_name_tokens or t in code_names for t in toks):
                    is_name_variant = True
            elif wl in all_name_tokens:
                is_name_variant = True
            if is_name_variant:
                continue
            single_stock.setdefault(word, {})[code] = max(
                single_stock.get(word, {}).get(code, 0.0), score
            )

        # Tier 1: cross-stock words (>= 2 stocks)
        cross = {w: cs for w, cs in single_stock.items() if len(cs) >= 2}
        # Tier 2: per-stock top3 narrative words
        per_stock_top: dict[str, list[tuple[str, float]]] = {}
        for w, cs in single_stock.items():
            if len(cs) >= 2:
                continue
            code, score = next(iter(cs.items()))
            per_stock_top.setdefault(code, []).append((w, score))
        for code in per_stock_top:
            per_stock_top[code].sort(key=lambda x: x[1], reverse=True)
            per_stock_top[code] = per_stock_top[code][:3]

        # Streak annotations (reuse fetch_hot_word_streaks per stock)
        streak_map: dict[str, int] = {}
        involved_codes = set()
        for cs in cross.values():
            involved_codes.update(cs.keys())
        for code in per_stock_top:
            involved_codes.add(code)
        for code in involved_codes:
            try:
                streaks = fetch_hot_word_streaks(db_path, code, date_str)
            except Exception:
                streaks = []
            for st in streaks:
                w = st.get("word")
                if w:
                    streak_map[w] = int(st.get("streak_days", 0))

        def _tag(word: str) -> str:
            days = streak_map.get(word, 0)
            if days >= 3:
                return f"📊连续{days}天"
            if days >= 2:
                return f"连续{days}天"
            return "🆕新增"

        lines = ["## 三、今日热词\n"]
        if cross:
            lines.append("### 跨股共现（多股同时讨论）\n")
            for w, cs in sorted(
                cross.items(),
                key=lambda x: (len(x[1]), max(x[1].values())),
                reverse=True,
            )[:10]:
                names = []
                for code in sorted(cs):
                    nm = stocks_cfg.get(code, {}).get("name", code)
                    names.append(nm)
                lines.append(
                    f"- **{w}** ({len(cs)}只: {', '.join(names)}) {_tag(w)}"
                )
            lines.append("")

        lines.append("### 个股热点（按 TF-IDF 信号排序）\n")
        singles: list[tuple[str, str, float]] = []
        for code, wl in per_stock_top.items():
            for w, score in wl:
                nm = stocks_cfg.get(code, {}).get("name", code)
                singles.append((w, nm, score))
        singles.sort(key=lambda x: x[2], reverse=True)
        for w, nm, score in singles[:20]:
            lines.append(f"- **{w}** ({nm}, tfidf={score:.1f}) {_tag(w)}")

        return "\n".join(lines) + "\n"
    finally:
        conn.close()

# ════════════════════════════════════════════════════════
# CLI entry point
# ════════════════════════════════════════════════════════


def main():
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config_path = sys.argv[2] if len(sys.argv) > 2 else "etc/config.report.json"
    date_str = None
    if len(sys.argv) > 1 and sys.argv[1] != "--config":
        date_str = sys.argv[1]
    elif len(sys.argv) > 4 and sys.argv[3] == "--date":
        date_str = sys.argv[4]

    report = generate_daily_report(config_path=config_path, date_str=date_str)
    print(f"\n{'='*60}")
    print(f"日报生成完成，共 {len(report)} 字")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
