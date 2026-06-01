"""
Daily Sentiment Report — 64 stocks overview from xueqiu-monitor DB.

Reads:   monitor.db (crawl_snapshots, change_alert, hot_word_dict)
Outputs: data/reports/{date}_sentiment.md
Pushes:  lark CLI (optional, when --push flag used)

Usage:
    python scripts/daily_sentiment_report.py
    python scripts/daily_sentiment_report.py --push
    python scripts/daily_sentiment_report.py --date 2026-06-01
"""

import json
import sqlite3
import datetime
import os
import sys
import argparse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, 'data', 'monitor.db')
SECTORS_PATH = os.path.join(BASE_DIR, 'etc', 'sectors.json')
REPORTS_DIR = os.path.join(BASE_DIR, 'data', 'reports')
WATCHLIST_PATH = os.path.join(BASE_DIR, 'data', 'watchlist.json')


def load_sectors() -> dict:
    """Load sector mapping: stock_code -> {name, sector, market}."""
    if os.path.exists(SECTORS_PATH):
        with open(SECTORS_PATH) as f:
            return json.load(f)
    return {}


def load_watchlist() -> list:
    """Load watchlist."""
    if os.path.exists(WATCHLIST_PATH):
        with open(WATCHLIST_PATH) as f:
            return json.load(f)
    return []


def get_stock_info(stock_code: str, sectors: dict) -> tuple:
    """Return (name, sector) for a given stock code."""
    info = sectors.get(stock_code, {})
    return info.get('name', stock_code), info.get('sector', '其他')


def aggregate_posts(posts_data: str) -> dict:
    """Parse posts_data JSON and compute sentiment stats per stock.

    Returns dict with:
        total_posts, avg_score, positive, negative, neutral counts/ratios.
    """
    try:
        posts = json.loads(posts_data) if isinstance(posts_data, str) else posts_data
    except (json.JSONDecodeError, TypeError):
        return {"total": 0, "avg_score": 0.0, "positive": 0,
                "negative": 0, "neutral": 0}

    scores = []
    for p in posts:
        s = p.get('sentiment_score', None)
        if s is not None:
            scores.append(float(s))

    total = len(scores)
    if total == 0:
        return {"total": 0, "avg_score": 0.0, "positive": 0,
                "negative": 0, "neutral": 0,
                "positive_ratio": 0.0, "negative_ratio": 0.0,
                "neutral_ratio": 0.0}

    positive = sum(1 for s in scores if s > 0.1)
    negative = sum(1 for s in scores if s < -0.1)
    neutral = total - positive - negative

    return {
        "total": total,
        "avg_score": round(sum(scores) / total, 3),
        "positive": positive,
        "negative": negative,
        "neutral": neutral,
        "positive_ratio": round(positive / total, 3) if total else 0,
        "negative_ratio": round(negative / total, 3) if total else 0,
        "neutral_ratio": round(neutral / total, 3) if total else 0,
    }


def load_snapshots(db, cutoff: int) -> list:
    """Load latest crawl_snapshots since cutoff."""
    rows = db.execute(
        "SELECT id, stock_code, crawl_time, posts_count, posts_data, sentiment_avg, status "
        "FROM crawl_snapshots WHERE crawl_time >= ? AND status='success'",
        (cutoff,)
    ).fetchall()
    results = []
    for r in rows:
        stats = aggregate_posts(r[4]) if r[4] and len(r[4]) > 10 else {
            "total": r[3] or 0, "avg_score": r[5] or 0.0, "positive": 0,
            "negative": 0, "neutral": 0, "positive_ratio": 0.0,
            "negative_ratio": 0.0, "neutral_ratio": 0.0
        }
        results.append({
            "stock_code": r[1],
            "crawl_time": r[2],
            "posts_count": max(r[3], stats["total"]),
            "sentiment_avg": stats["avg_score"] if stats["avg_score"] != 0 else r[5],
            "positive": stats["positive"],
            "negative": stats["negative"],
            "neutral": stats["neutral"],
            "positive_ratio": stats["positive_ratio"],
            "negative_ratio": stats["negative_ratio"],
            "neutral_ratio": stats["neutral_ratio"],
        })
    return results


def get_top_stocks(stocks: list, sectors: dict, top_n: int = 5) -> list:
    """Sort by posts_count desc, return top N with name+sector."""
    sorted_stocks = sorted(stocks, key=lambda x: x['posts_count'], reverse=True)
    for s in sorted_stocks:
        name, sector = get_stock_info(s['stock_code'], sectors)
        s['name'] = name
        s['sector'] = sector
    return sorted_stocks[:top_n]


def aggregate_by_sector(stocks: list, sectors: dict) -> list:
    """Aggregate sentiment by sector."""
    sec_map = {}
    for s in stocks:
        _, sec = get_stock_info(s['stock_code'], sectors)
        if sec not in sec_map:
            sec_map[sec] = {"count": 0, "total_score": 0.0, "total_posts": 0}
        sec_map[sec]["count"] += 1
        sec_map[sec]["total_score"] += s['sentiment_avg']
        sec_map[sec]["total_posts"] += s['posts_count']

    results = []
    for sec, data in sorted(sec_map.items(), key=lambda x: -x[1]["total_posts"]):
        avg = round(data["total_score"] / data["count"], 3) if data["count"] else 0
        if avg > 0.03:
            label = "偏多"
        elif avg < -0.03:
            label = "偏空"
        else:
            label = "中性"
        results.append({
            "sector": sec,
            "count": data["count"],
            "total_posts": data["total_posts"],
            "avg_score": avg,
            "label": label,
        })
    return results


def load_alerts(db, cutoff: int) -> list:
    """Load significant change_alerts (Z > 2.0) since cutoff."""
    rows = db.execute(
        "SELECT stock_code, z_score, priority, detail, alert_time "
        "FROM change_alert "
        "WHERE alert_time >= ? AND filtered=0 AND z_score > 2.0 "
        "ORDER BY z_score DESC LIMIT 10",
        (cutoff,)
    ).fetchall()
    results = []
    for r in rows:
        results.append({
            "stock_code": r[0],
            "z_score": r[1],
            "priority": r[2],
            "detail": (r[3] or '')[:500],
            "alert_time": r[4],
        })
    return results


# Common noise words to filter from hot words
_NOISE_WORDS = frozenset([
    '网页链接', 'hk', 'sh', 'us', 'of', 'app', 'accession', '万股',
    '日消息', '美元', '港元', '公告', '公司', '披露', '文件',
])


def load_hotwords(db) -> dict:
    """Load top meaningful hot words, filtering noise."""
    rows = db.execute(
        "SELECT word, frequency FROM hot_word_dict WHERE frequency > 1 "
        "ORDER BY frequency DESC LIMIT 30"
    ).fetchall()
    filtered = [
        {"word": r[0], "frequency": r[1]} for r in rows
        if r[0] not in _NOISE_WORDS and len(r[0]) > 1
    ]
    return filtered[:15]


def render_report(date_str: str, stocks: list, sectors: dict,
                  top5: list, sector_scores: list, alerts: list,
                  hotwords: list, total_stocks: int) -> str:
    """Render Markdown report from aggregated data."""
    lines = []
    lines.append(f"# ☀️ 自选股情绪日报 — {date_str}")
    lines.append(f"")
    lines.append(f"> 覆盖: {total_stocks} 只 | 有数据: {len(stocks)} 只 | "
                 f"总帖子: {sum(s['posts_count'] for s in stocks):,} 条")
    lines.append(f"")

    # ── Overall sentiment ──
    all_scores = [s['sentiment_avg'] for s in stocks]
    avg = round(sum(all_scores) / len(all_scores), 3) if all_scores else 0
    total_pos = sum(s['positive'] for s in stocks)
    total_neg = sum(s['negative'] for s in stocks)
    total_neu = sum(s['neutral'] for s in stocks)
    total_all = total_pos + total_neg + total_neu
    if total_all > 0:
        pos_pct = round(total_pos / total_all * 100)
        neg_pct = round(total_neg / total_all * 100)
        neu_pct = round(total_neu / total_all * 100)
    else:
        pos_pct = neg_pct = neu_pct = 0

    summary = ""
    if avg > 0.03:
        summary = "整体偏多"
    elif avg < -0.03:
        summary = "整体偏空"
    else:
        summary = "整体中性"

    lines.append(f"## 整体概况")
    lines.append(f"")
    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 看多 | {pos_pct}% |")
    lines.append(f"| 看空 | {neg_pct}% |")
    lines.append(f"| 中性 | {neu_pct}% |")
    lines.append(f"| 情感均值 | {avg:.3f} ({summary}) |")
    lines.append(f"")

    # ── Top 5 ──
    lines.append(f"## 🔥 今日热度 TOP5")
    lines.append(f"")
    lines.append(f"| # | 名称 | 股票代码 | 帖子数 | 情感值 | 行业 |")
    lines.append(f"|---|------|---------|-------|--------|------|")
    for i, s in enumerate(top5, 1):
        emoji = "🟢" if s['sentiment_avg'] > 0.03 else ("🔴" if s['sentiment_avg'] < -0.03 else "🟡")
        lines.append(f"| {i} | {s['name']} | {s['stock_code']} | {s['posts_count']:,} | "
                     f"{emoji} {s['sentiment_avg']:.3f} | {s['sector']} |")
    lines.append(f"")

    # ── Sector scores ──
    lines.append(f"## 📊 行业板块情绪")
    lines.append(f"")
    lines.append(f"| 行业 | 覆盖 | 帖子数 | 情感值 | 判断 |")
    lines.append(f"|------|------|--------|--------|------|")
    for sec in sector_scores:
        emoji = "🟢" if sec['avg_score'] > 0.03 else ("🔴" if sec['avg_score'] < -0.03 else "🟡")
        lines.append(f"| {sec['sector']} | {sec['count']} | {sec['total_posts']:,} | "
                     f"{emoji} {sec['avg_score']:.3f} | {sec['label']} |")
    lines.append(f"")

    # ── Alerts ──
    if alerts:
        lines.append(f"## ⚡ 异常信号 (Z > 2.0)")
        lines.append(f"")
        for a in alerts:
            name, sec = get_stock_info(a['stock_code'], sectors)
            priority = "🚨 P0" if a['priority'] == "P0" else ("⚠️ P1" if a['priority'] == "P1" else "P2")
            lines.append(f"- **{name}** ({a['stock_code']}) — Z={a['z_score']:.1f} {priority}")
            if a['detail']:
                detail = a['detail']
                # Try to extract cleaner summary from JSON
                if detail.startswith('{'):
                    try:
                        parsed = json.loads(detail)
                        title = parsed.get('title', '') or parsed.get('text', '') or ''
                        detail = title[:100] if title else '公告事件'
                    except json.JSONDecodeError:
                        detail = detail[:100]
                lines.append(f"  > {detail}")
        lines.append(f"")

    # ── Hot words ──
    if hotwords:
        lines.append(f"## 💬 热门话题词")
        lines.append(f"")
        # Render as a simple horizontal tag bar
        max_freq = max(w['frequency'] for w in hotwords) if hotwords else 1
        tags = "  ".join(
            f"**{w['word']}** ({w['frequency']})"
            for w in hotwords if w['frequency'] > 1
        )
        if tags:
            lines.append(f"{tags}")
        lines.append(f"")

    # ── Footer ──
    lines.append(f"---")
    lines.append(f"*生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}*")
    lines.append(f"*数据来源: xueqiu-monitor*")

    return '\n'.join(lines)


def push_report(markdown: str, date_str: str):
    """Push report to Feishu via lark CLI."""
    # Write to file for manual review
    os.makedirs(REPORTS_DIR, exist_ok=True)
    report_path = os.path.join(REPORTS_DIR, f"{date_str}_sentiment.md")
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(markdown)
    print(f"Report saved: {report_path}")

    # Optionally push to lark
    import subprocess
    try:
        # Use lark-cli to send message
        result = subprocess.run(
            ["lark", "im", "message", "send",
             "--receive-id-type", "chat_id",
             "--receive-id", os.environ.get("LARK_CHAT_ID", ""),
             "--msg-type", "post",
             "--content", json.dumps({
                 "zh_cn": {
                     "title": f"☀️ 自选股情绪日报 {date_str}",
                     "content": [[{"tag": "text", "text": "已生成，查看完整日报文件。"}]]
                 }
             }, ensure_ascii=False)],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            print("Pushed to Feishu")
        else:
            print(f"Push failed: {result.stderr[:200]}")
    except Exception as e:
        print(f"Push error (non-fatal): {e}")


def main():
    parser = argparse.ArgumentParser(description="Daily sentiment report for watchlist stocks")
    parser.add_argument('--date', help='Date string (YYYY-MM-DD), default: today')
    parser.add_argument('--push', action='store_true', help='Push to Feishu')
    args = parser.parse_args()

    if args.date:
        date_str = args.date
        today = datetime.datetime.strptime(date_str, '%Y-%m-%d')
    else:
        today = datetime.datetime.now()
        date_str = today.strftime('%Y-%m-%d')

    # Date range: last 24h
    cutoff = today.timestamp()

    # Load data
    sectors = load_sectors()
    watchlist = load_watchlist()
    all_codes = {s['stock_code'] for s in watchlist}

    if not os.path.exists(DB_PATH):
        print(f"DB not found: {DB_PATH}")
        sys.exit(1)

    db = sqlite3.connect(DB_PATH)
    try:
        # Load snapshots
        snapshots = load_snapshots(db, cutoff)
        print(f"Loaded {len(snapshots)} snapshots")

        if not snapshots:
            print("No snapshots found in the last 24h. Try running xueqiu-monitor first.")
            sys.exit(0)

        # Only keep stocks that are in watchlist (or all if watchlist empty)
        if all_codes:
            snapshots = [s for s in snapshots if s['stock_code'] in all_codes]

        # Get top5 by post count
        top5 = get_top_stocks(snapshots, sectors)
        print(f"Top 5: {[(s['name'], s['posts_count']) for s in top5]}")

        # Aggregate by sector
        sector_scores = aggregate_by_sector(snapshots, sectors)

        # Load alerts
        alerts = load_alerts(db, cutoff)
        print(f"Alerts: {len(alerts)}")

        # Load hot words
        hotwords = load_hotwords(db)

        # Render
        markdown = render_report(
            date_str=date_str,
            stocks=snapshots,
            sectors=sectors,
            top5=top5,
            sector_scores=sector_scores,
            alerts=alerts,
            hotwords=hotwords,
            total_stocks=len(watchlist) if watchlist else len(snapshots),
        )

        # Save and optionally push
        os.makedirs(REPORTS_DIR, exist_ok=True)
        report_path = os.path.join(REPORTS_DIR, f"{date_str}_sentiment.md")
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(markdown)
        print(f"Report written: {report_path} ({len(markdown)} chars)")

        if args.push:
            push_report(markdown, date_str)

    finally:
        db.close()


if __name__ == '__main__':
    main()
