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
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

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
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def fetch_stock_posts(
    db_path: str, stock_code: str, date_str: str, min_length: int = 30
) -> list[dict]:
    """Fetch today's posts for a stock, filtered from the latest snapshot.

    The snapshot from 雪球 is a mixed stream of latest posts + historical
    hot posts. This function filters to keep only posts authored on
    ``date_str``, so the LLM analyzes today's discussion instead of
    rehashing old high-engagement posts.

    Filters out:
    - Reply posts ("回复@" prefix in title)
    - Posts shorter than min_length chars
    - Posts whose parsed time falls outside ``date_str`` (fail-open:
      posts with unparseable time are kept)

    Sort: newest first (by timestamp), ties broken by engagement desc.
    Posts with unknown time sort last, ordered by engagement.
    """
    conn = _connect(db_path)
    try:
        # Get the latest snapshot for this stock on this date
        row = conn.execute(
            """SELECT posts_data FROM crawl_snapshots
               WHERE stock_code=? AND date(crawl_time,'unixepoch','localtime')=?
               ORDER BY crawl_time DESC LIMIT 1""",
            (stock_code, date_str),
        ).fetchone()

        if not row or not row["posts_data"]:
            return []

        posts = json.loads(row["posts_data"])

        # Compute the [day_start, day_end) window for date_str (local time)
        target_date = datetime.strptime(date_str, "%Y-%m-%d")
        day_start = int(target_date.timestamp())
        day_end = day_start + 86400

        # Lazy import to avoid a module-load-time circular dependency
        from .crawler import _parse_post_time

        now = time.time()

        filtered = []
        for p in posts:
            title = (p.get("title") or "")[:200]
            content = p.get("content") or ""
            # Skip reply posts
            if title.startswith("回复@") or content.startswith("回复@"):
                continue
            # Skip very short posts
            full_text = f"{title} {content}".strip()
            if len(full_text) < min_length:
                continue
            # Filter out posts not authored on date_str.
            # Fail-open: unparseable time (ts == 0) is kept, so missing
            # time data never blanks out a stock's entire feed.
            post_time_str = p.get("time", "")
            post_ts = _parse_post_time(post_time_str, now)
            if post_ts > 0 and not (day_start <= post_ts < day_end):
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
        # Stable two-pass sort: engagement desc, then timestamp desc.
        # Result: newest first; same-timestamp ties keep engagement order;
        # unknown-time posts (ts=0) sink to the end in engagement order.
        filtered.sort(
            key=lambda p: p["like_count"]
            + p["forward_count"]
            + p["comment_count"],
            reverse=True,
        )
        filtered.sort(key=lambda p: p["_ts"], reverse=True)
        for p in filtered:
            p.pop("_ts", None)
        return filtered
    finally:
        conn.close()


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
    """Fetch sentiment overview for all stocks on a given date."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """SELECT s.stock_code, s.posts_count, s.sentiment_avg
               FROM crawl_snapshots s
               WHERE date(s.crawl_time,'unixepoch','localtime')=?
               GROUP BY s.stock_code
               ORDER BY s.sentiment_avg DESC""",
            (date_str,),
        ).fetchall()
        return [
            {
                "stock_code": r["stock_code"],
                "posts": r["posts_count"],
                "sentiment": round(r["sentiment_avg"], 3) if r["sentiment_avg"] else 0.0,
            }
            for r in rows
        ]
    finally:
        conn.close()


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
) -> str:
    """Build the LLM prompt for per-stock analysis.

    Args:
        yesterday: Yesterday's sentiment + hot words for delta comparison.
        streaks: Hot words with consecutive-day streak counts, used to
            distinguish persistent narratives from new topics.
    """
    # Format posts
    posts_text = ""
    for i, p in enumerate(posts, 1):
        engagement = (
            f"❤️{p['like_count']} 💬{p['comment_count']} 🔄{p['forward_count']}"
        )
        time_str = p.get("time", "") or "时间未知"
        posts_text += f"\n---\n[{i}] {p['author']} | 🕐{time_str} | ({engagement})\n{p['title']}\n{p['content']}\n"
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

    # Format hot words with streak annotations
    if streaks:
        hw_parts = []
        for s in streaks[:10]:
            tag = f"📊连续{s['streak_days']}天" if s["is_persistent"] else "🆕新增"
            hw_parts.append(f"{s['word']}({s['today_tfidf']}) {tag}")
        hot_words_text = "\n".join(f"- {w}" for w in hw_parts)
    else:
        hot_words_text = ", ".join(hot_words) if hot_words else "无"

    return f"""你是雪球舆情分析师。请分析以下「{stock_name}（{stock_code}）」今日的雪球讨论。

## 情感数据
{trend_text}

## 昨日对比
{delta_text}

## 今日异常信号
{alert_text}

## 今日热词（TF-IDF top，已标注连续天数）
{hot_words_text}

## 今日讨论帖（共 {len(posts)} 帖，按发帖时间倒序排列）
{posts_text}

---

请输出结构化分析（Markdown 格式），包含以下五个部分：

### 讨论焦点
提炼 3-5 条今日核心讨论观点（每条一句话概括，附代表性帖子编号）

### 多空分歧
看多 vs 看空的主要论据（如分歧不明显则说明）

### 风险提示
帖子中提到的关键风险（如无则标注"暂无明显风险讨论"）

### 话题连续性
区分以下两类内容（如全部为新增则说明"今日无持续叙事"）：
- **📊 持续叙事**：与昨日/近期重复的话题，简要标注已持续天数，重点说**今日有何新进展或新角度**
- **🆕 今日新增**：昨日未出现的新话题、新事件、新观点

### 情感解读
结合情感数据和帖子内容，一句话总结今日市场情绪"""


def analyze_stock(
    stock_code: str,
    stock_name: str,
    db_path: str,
    date_str: str,
    config: dict,
) -> str:
    """Run LLM analysis for a single stock. Returns Markdown section."""
    min_len = config.get("llm", {}).get("min_post_length", 30)
    posts = fetch_stock_posts(db_path, stock_code, date_str, min_length=min_len)

    if not posts:
        logger.info(f"  {stock_code}: 今日无帖子，跳过")
        return f"#### {stock_code} {stock_name}\n\n今日无帖子数据。\n"

    trend = fetch_sentiment_trend(db_path, stock_code)
    alerts = fetch_stock_alerts(db_path, stock_code, date_str)
    hot_words = fetch_hot_words(db_path, stock_code, date_str)
    yesterday = fetch_yesterday_summary(db_path, stock_code, date_str)
    streaks = fetch_hot_word_streaks(db_path, stock_code, date_str)

    prompt = _build_analysis_prompt(
        stock_name, stock_code, posts, trend, alerts, hot_words,
        yesterday=yesterday, streaks=streaks,
    )

    logger.info(
        f"  {stock_code}: {len(posts)}帖, "
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
        text = response.choices[0].message.content or ""
        logger.info(f"  {stock_code}: LLM 完成 {elapsed:.1f}s, {len(text)}字")

        # Build section header + LLM output
        header = f"#### {stock_code} {stock_name}\n\n"
        header += f"- 帖子数: {len(posts)} | 情感分: {trend.get('today_mean', 'N/A')} | 趋势: {trend.get('trend', 'N/A')}\n\n"
        return header + text + "\n"
    except Exception as e:
        logger.error(f"  {stock_code}: LLM 调用失败: {e}")
        return f"#### {stock_code} {stock_name}\n\nLLM 分析失败: {e}\n"


# ════════════════════════════════════════════════════════
# Report assembly
# ════════════════════════════════════════════════════════


def _build_thermometer_section(thermometer: list, stocks_cfg: dict) -> str:
    """Build market thermometer section."""
    lines = ["## 一、市场温度计\n"]
    lines.append("| 股票 | 名称 | 帖子数 | 情感分 |")
    lines.append("|------|------|--------|--------|")
    for t in thermometer:
        code = t["stock_code"]
        name = stocks_cfg.get(code, {}).get("name", code)
        sent = t["sentiment"]
        # Emoji based on sentiment
        if sent > 0.1:
            emoji = "🟢"
        elif sent < -0.1:
            emoji = "🔴"
        else:
            emoji = "⚪"
        lines.append(
            f"| {code} | {name} | {t['posts']} | {emoji} {sent:+.3f} |"
        )

    # Summary line
    if thermometer:
        most_positive = max(thermometer, key=lambda x: x["sentiment"])
        most_negative = min(thermometer, key=lambda x: x["sentiment"])
        p_name = stocks_cfg.get(most_positive["stock_code"], {}).get(
            "name", most_positive["stock_code"]
        )
        n_name = stocks_cfg.get(most_negative["stock_code"], {}).get(
            "name", most_negative["stock_code"]
        )
        lines.append("")
        lines.append(
            f"最积极: **{p_name}**({most_positive['sentiment']:+.3f}) | "
            f"最消极: **{n_name}**({most_negative['sentiment']:+.3f})"
        )

    return "\n".join(lines) + "\n"


def _group_by_sector(stocks_cfg: dict) -> dict:
    """Group stocks by sector."""
    sectors: dict[str, list[str]] = {}
    for code, info in stocks_cfg.items():
        sector = info.get("sector", "其他")
        sectors.setdefault(sector, []).append(code)
    return sectors


def generate_daily_report(
    config_path: str = "etc/config.report.json",
    date_str: Optional[str] = None,
) -> str:
    """Generate the full daily report and return Markdown content.

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

    logger.info(f"=== 自选股舆情日报生成开始 {date_str} ===")

    # ── Section 1: Market thermometer (SQL only, no LLM) ──
    logger.info("[1/3] 生成市场温度计...")
    thermometer = fetch_market_thermometer(db_path, date_str)
    thermo_md = _build_thermometer_section(thermometer, stocks_cfg)

    # ── Section 2: Per-stock analysis (LLM) ──
    logger.info("[2/3] 逐股票 LLM 分析...")
    sectors = _group_by_sector(stocks_cfg)
    concurrency = cfg.get("llm", {}).get("concurrency", 3)

    # Build all stock analysis tasks
    stock_results: dict[str, str] = {}
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
            )
            futures[future] = code

        for future in as_completed(futures):
            code = futures[future]
            try:
                stock_results[code] = future.result()
            except Exception as e:
                logger.error(f"  {code}: 分析异常: {e}")
                stock_results[code] = (
                    f"#### {code} {stocks_cfg[code]['name']}\n\n分析异常: {e}\n"
                )

    # Assemble per-sector sections
    sector_emojis = {
        "消费": "🛍️",
        "AI/科技": "🤖",
        "互联网/游戏": "🎮",
        "出行/酒旅": "✈️",
        "新能源": "🔋",
        "医疗器械": "🏥",
        "矿业": "⛏️",
        "航天": "🚀",
        "其他": "📊",
    }
    stock_sections = ["## 二、个股深度分析\n"]
    for sector, codes in sectors.items():
        emoji = sector_emojis.get(sector, "📊")
        stock_sections.append(f"\n### {emoji} {sector}\n")
        for code in codes:
            stock_sections.append(stock_results.get(code, f"#### {code}\n\n无数据\n"))

    # ── Section 3: Hot words cross-stock ──
    logger.info("[3/3] 生成热词云...")
    hot_words_md = _build_hot_words_section(db_path, date_str, stocks_cfg)

    # ── Assemble full report ──
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    report = f"""# 📊 自选股舆情日报

**日期**: {date_str}
**生成时间**: {now_str}
**覆盖股票**: {len(stocks_cfg)} 只（按板块分组）

---

{thermo_md}

---

{"".join(stock_sections)}

---

{hot_words_md}

---

*本报告由 xueqiu-monitor 自动生成，数据来源：雪球。LLM 分析模型：MiniMax-M3（经火山方舟 coding plan）。*
"""

    # Save to file
    output_dir = Path(cfg.get("report_output_dir", "data/daily_reports"))
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{date_str}-sentiment.md"
    output_path.write_text(report, encoding="utf-8")
    logger.info(f"日报已保存: {output_path}")

    return report


def _build_hot_words_section(
    db_path: str, date_str: str, stocks_cfg: dict
) -> str:
    """Build cross-stock hot words section."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """SELECT stock_code, word, tfidf_score
               FROM hot_word_event
               WHERE date(event_time,'unixepoch','localtime')=?
               ORDER BY tfidf_score DESC LIMIT 30""",
            (date_str,),
        ).fetchall()
        if not rows:
            return "## 三、今日热词\n\n今日无热词数据。\n"

        lines = ["## 三、今日热词\n"]
        # Build stock-name blocklist (short names, codes, lowercased)
        stock_blocklist = set()
        for code, info in stocks_cfg.items():
            stock_blocklist.add(info.get("name", ""))
            stock_blocklist.add(code.lower())
            stock_blocklist.add(code.split(".")[0].lower())
        # Common low-signal stopwords
        stopwords = {
            "ai", "股票", "投资", "市场", "今天", "今日", "现在", "可以",
            "什么", "一个", "这个", "我们", "他们", "已经", "没有", "觉得",
            "公司", "股价", "买入", "卖出", "持有", "仓位", "操作",
        }

        # Aggregate by word across stocks (filter out noise)
        word_counts: dict[str, list[str]] = {}
        for r in rows:
            word = r["word"]
            wl = word.lower().strip()
            # Skip stock names, codes, and stopwords
            if wl in stock_blocklist or word in stock_blocklist or wl in stopwords:
                continue
            # Skip pure numbers or single chars
            if len(word) < 2 or word.isdigit():
                continue
            stock_code = r["stock_code"]
            stock_name = stocks_cfg.get(stock_code, {}).get("name", stock_code)
            word_counts.setdefault(word, []).append(stock_name)

        # Sort by number of stocks mentioning (cross-stock relevance)
        sorted_words = sorted(
            word_counts.items(), key=lambda x: len(x[1]), reverse=True
        )
        for word, stocks_list in sorted_words[:15]:
            stocks_str = ", ".join(stocks_list)
            lines.append(f"- **{word}** ({len(stocks_list)}只: {stocks_str})")

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
