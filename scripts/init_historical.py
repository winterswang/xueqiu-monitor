#!/usr/bin/env python3
"""
Initialize historical sentiment stats for xueqiu-monitor.

Phase 1 strategy: crawl each stock once to establish a baseline, then populate
28-29 days of synthetic SentimentStat entries around that baseline. This lets us
bypass the cold-start period for immediate detection testing.

Usage:
    python3 scripts/init_historical.py                    # all stocks in config
    python3 scripts/init_historical.py SH600519           # single stock
    python3 scripts/init_historical.py --days 14          # custom window
    python3 scripts/init_historical.py --dry-run          # preview only
"""
import sys, os, time, json, random, argparse, logging

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, '/root/code/xueqiu-analyzer-skill/src')
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from config import Config
from db import (init_db, insert_sentiment_stat, get_historical_stats,
                count_sentiment_days)
from models import SentimentStat


def crawl_baseline(code: str, timeout: int = 30) -> dict:
    """Crawl a single stock to get baseline post count and sentiment.

    Returns {'posts_count': int, 'sentiment_avg': float} or None on failure.
    """
    try:
        from xueqiu_analyzer.crawler import XueqiuCrawler
        crawler = XueqiuCrawler({"headless": True, "delay_min": 1, "delay_max": 2})
        result = crawler.crawl(code, max_pages=1, max_articles=0)
        posts_count = len(result.discussions) + len(result.news)
        return {"posts_count": posts_count, "sentiment_avg": 0.0}
    except Exception as e:
        logger.error(f"Crawl failed for {code}: {e}")
        return None


def generate_historical_stats(
    code: str,
    baseline_posts: int,
    baseline_sentiment: float = 0.0,
    days: int = 29,
    noise_posts: int = 5,
    noise_sentiment: float = 0.05,
) -> list[SentimentStat]:
    """Generate synthetic historical stats around a baseline.

    Uses a normal-like distribution: most days cluster near baseline,
    with occasional outliers to make detection interesting.
    """
    now = int(time.time())
    stats = []

    for d in range(days, 0, -1):
        day_ts = now - d * 86400

        # Random walk with mean reversion
        posts_delta = random.gauss(0, noise_posts)
        if random.random() < 0.10:  # 10% chance of outlier
            posts_delta += random.choice([-1, 1]) * random.uniform(noise_posts * 3, noise_posts * 6)

        sentiment_delta = random.gauss(0, noise_sentiment)
        if random.random() < 0.08:  # 8% chance of sentiment outlier
            sentiment_delta += random.choice([-1, 1]) * random.uniform(noise_sentiment * 4, noise_sentiment * 8)

        posts = max(5, int(baseline_posts + posts_delta))
        sentiment = max(-1.0, min(1.0, baseline_sentiment + sentiment_delta))

        stats.append(SentimentStat(
            stock_code=code,
            stat_date=day_ts,
            posts_count=posts,
            sentiment_mean=round(sentiment, 4),
            sentiment_std=0.0,
            z_score=0.0,
            z_alert=0,
        ))

    return stats


def init_stock(cfg: Config, code: str, days: int, dry_run: bool) -> bool:
    """Initialize historical data for one stock."""
    existing = count_sentiment_days(cfg.db_path, code)
    if existing >= days:
        logger.info(f"  ✅ {code}: already has {existing}d history, skipping")
        return True

    # Crawl baseline
    baseline = crawl_baseline(code)
    if baseline is None:
        logger.warning(f"  ⚠️  {code}: crawl failed, using defaults")
        baseline = {"posts_count": 40, "sentiment_avg": 0.0}

    logger.info(f"  📊 {code}: baseline={baseline['posts_count']} posts")

    if dry_run:
        logger.info(f"  [DRY RUN] Would insert {days - existing} days")
        return True

    stats = generate_historical_stats(
        code,
        baseline_posts=baseline["posts_count"],
        baseline_sentiment=baseline["sentiment_avg"],
        days=days,
    )

    for stat in stats:
        insert_sentiment_stat(cfg.db_path, stat)

    final = count_sentiment_days(cfg.db_path, code)
    logger.info(f"  ✅ {code}: {existing}d → {final}d history")
    return final >= days


def main():
    parser = argparse.ArgumentParser(description="Initialize historical data for cold-start bypass")
    parser.add_argument("stocks", nargs="*", help="Stock codes to initialize (default: from config watchlist)")
    parser.add_argument("--days", type=int, default=29, help="Days of history (default: 29, min to bypass 28d cold start)")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no writes")
    parser.add_argument("-c", "--config", default=os.path.join(PROJECT_ROOT, "etc/config.json"))
    args = parser.parse_args()

    cfg = Config.from_file(args.config)
    init_db(cfg.db_path)

    # Determine stock list
    if args.stocks:
        codes = args.stocks
    else:
        # Try to read from morning-brief watchlist
        try:
            import sqlite3
            mb_db = "/root/code/morning-brief/data/morning-brief.db"
            conn = sqlite3.connect(mb_db)
            rows = conn.execute(
                "SELECT stock_code FROM watchlist WHERE is_active=1 AND is_index=0"
            ).fetchall()
            codes = [r[0] for r in rows]
            conn.close()
            logger.info(f"从 morning-brief 自选股加载 {len(codes)} 只股票")
        except Exception:
            codes = ["SH600519", "SZ000858", "SZ000651", "HK00700"]
            logger.info(f"使用默认列表: {codes}")

    if args.dry_run:
        logger.info(f"🔍 DRY RUN — 不会写入数据")

    logger.info(f"📊 冷启动初始化: {len(codes)} 只股票, 目标 {args.days} 天历史")

    success = 0
    for i, code in enumerate(codes):
        logger.info(f"[{i+1}/{len(codes)}] {code}")
        if init_stock(cfg, code, args.days, args.dry_run):
            success += 1
        # Throttle between stocks
        if i < len(codes) - 1:
            time.sleep(2)

    logger.info(f"\n✅ 完成: {success}/{len(codes)} 股票已初始化")
    if success >= len(codes):
        logger.info("🎉 冷启动期已满足，可以开始检测了！")
    else:
        logger.info("⚠️ 部分股票仍需更多历史数据")


if __name__ == "__main__":
    main()
