"""xueqiu-monitor: CLI entry point

Main pipeline:
  1. Load config + watchlist
  2. Init DB
  3. Decay stale weights
  4. For each stock: crawl → store → detect → filter → notify
  5. Generate daily report if any alerts
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from config import Config
import db
import crawler
import detector
import filter as rule_filter
import notifier
import feedback as fbloop
from models import (
    CrawlSnapshot, SentimentStat, ChangeAlert,
    HotWordEvent, PushHistory,
)


# ════════════════════════════════════════════════════════
# Logging setup
# ════════════════════════════════════════════════════════

def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%Y-%m-%d %H:%M:%S")


# ════════════════════════════════════════════════════════
# Pipeline
# ════════════════════════════════════════════════════════

def run_pipeline(config_path: str, dry_run: bool = False) -> dict:
    """Execute the full monitoring pipeline.

    Returns summary dict with stats.
    """
    cfg = Config.from_file(config_path)
    db_path = cfg.db_path
    os.makedirs(Path(db_path).parent, exist_ok=True)

    # Init DB
    db.init_db(db_path)
    logger = logging.getLogger(__name__)

    # Decay stale weights before each run
    fbloop.decay_stale_weights(db_path, cfg.feedback)
    logger.info("权重衰减检查完成")

    # Load watchlist
    stocks = crawler.load_watchlist({"watchlist_path": cfg.watchlist_path})
    if not stocks:
        logger.error("自选股列表为空，终止")
        return {"error": "empty_watchlist", "crawled": 0, "alerts": 0}

    # Crawl all stocks (sequential)
    logger.info(f"开始爬取 {len(stocks)} 只自选股 ...")
    start_time = time.time()
    crawl_results = crawler.crawl_watchlist(stocks, cfg.crawler["timeout_seconds"])
    crawl_elapsed = int(time.time() - start_time)
    logger.info(f"爬取完成: {crawl_elapsed}s")

    # Process each crawl result
    total_alerts = 0
    all_alerts: list[ChangeAlert] = []

    for cr in crawl_results:
        if cr["status"] != "success":
            continue

        stock_code = cr["stock_code"]
        now = int(time.time())

        # ── Store snapshot ──
        snap = CrawlSnapshot(
            stock_code=stock_code,
            crawl_time=cr["crawl_time"],
            posts_count=cr["posts_count"],
            posts_data=cr["posts_data"],
            sentiment_avg=cr["sentiment_avg"],
            status="success",
        )
        snapshot_id = db.insert_snapshot(db_path, snap)
        logger.debug(f"  {stock_code}: snapshot_id={snapshot_id}")

        # ── Detection ──
        # Get historical stats
        hist_stats = db.get_historical_stats(db_path, stock_code, cfg.detector["z_score_window_days"])
        all_hist = db.get_all_historical_stats(db_path, stock_code)

        # Cold start check
        cold = detector.is_cold_start(all_hist, cfg.cold_start["days"])

        # Detect post spikes
        spike_alert = detector.detect_post_spike(
            cr["posts_count"], hist_stats, cfg.detector["z_score_window_days"]
        )
        if spike_alert:
            spike_alert.stock_code = stock_code
            spike_alert.alert_time = now

        # Detect sentiment shift
        sent_alert = detector.detect_sentiment_shift(
            cr["sentiment_avg"], hist_stats, cfg.detector["z_score_window_days"]
        )
        if sent_alert:
            sent_alert.stock_code = stock_code
            sent_alert.alert_time = now

        alerts = [a for a in [spike_alert, sent_alert] if a is not None]

        # ── Store sentiment stat for next run ──
        today_start = now // 86400 * 86400
        stat = SentimentStat(
            stock_code=stock_code,
            stat_date=today_start,
            posts_count=cr["posts_count"],
            sentiment_mean=cr["sentiment_avg"],
            sentiment_std=0.0,  # computed over historical window
            z_score=max(a.z_score for a in alerts) if alerts else 0.0,
            z_alert=1 if alerts else 0,
        )
        db.insert_sentiment_stat(db_path, stat)

        # ── TF-IDF hot words ──
        posts_texts = [p.get("content", "") or p.get("title", "") for p in cr["posts_data"]]
        if posts_texts:
            hist_events = db.get_recent_hot_word_events(db_path, stock_code, cfg.detector["z_score_window_days"])
            hw_alerts = detector.detect_hot_word_emergence(
                stock_code, posts_texts, hist_events,
                cfg.detector["tfidf_min_df"], cfg.detector["tfidf_max_df"],
            )
            alerts.extend(hw_alerts)

            # Store hot word events
            curr_tfidf = dict(detector.compute_tfidf(posts_texts, cfg.detector["tfidf_min_df"], cfg.detector["tfidf_max_df"]))
            for word, score in curr_tfidf.items():
                db.insert_hot_word_event(db_path, HotWordEvent(
                    stock_code=stock_code,
                    word=word,
                    tfidf_score=round(score, 4),
                    event_time=now,
                    z_score=0.0,  # computed above
                ))

        # ── Filter ──
        alerts = rule_filter.filter_alerts(alerts, cr["posts_data"], cold, cfg.filter)

        # ── Store alerts ──
        for alert in alerts:
            alert_id = db.insert_alert(db_path, alert)
            total_alerts += 1
            all_alerts.append(alert)

    # ── Notification ──
    webhook = cfg.notification.get("webhook_url", "")
    if not dry_run and webhook:
        p0_alerts = [a for a in all_alerts if not a.filtered and a.priority == "P0"]
        p1_alerts = [a for a in all_alerts if not a.filtered and a.priority == "P1"]

        # P0: immediate push (one per alert)
        for alert in p0_alerts:
            stock_name = _get_stock_name(stocks, alert.stock_code)
            ok = notifier.push_immediate(alert, webhook, stock_name, cfg.notification.get("push_timeout", 5))
            db.insert_push(db_path, PushHistory(
                stock_code=alert.stock_code,
                alert_id=alert.id or 0,
                priority="P0",
                content=f"Z={alert.z_score} {alert.alert_type}",
                status="success" if ok else "failed",
            ))

        # P1: digest push
        if p1_alerts:
            ok = notifier.push_digest(p1_alerts, webhook, cfg.notification.get("push_timeout", 5))
            for alert in p1_alerts:
                db.insert_push(db_path, PushHistory(
                    stock_code=alert.stock_code,
                    alert_id=alert.id or 0,
                    priority="P1",
                    content="digest",
                    status="success" if ok else "failed",
                ))

    # ── Daily report ──
    report = notifier.generate_daily_report(all_alerts)
    report_path = Path(db_path).parent / "daily_reports" / f"{time.strftime('%Y-%m-%d')}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report)
    logger.info(f"日报已保存: {report_path}")

    summary = {
        "crawled": sum(1 for r in crawl_results if r["status"] == "success"),
        "failed": sum(1 for r in crawl_results if r["status"] != "success"),
        "total_stocks": len(stocks),
        "alerts": len(all_alerts),
        "p0": sum(1 for a in all_alerts if a.priority == "P0"),
        "p1": sum(1 for a in all_alerts if a.priority == "P1"),
        "p2": sum(1 for a in all_alerts if a.priority == "P2"),
        "filtered": sum(1 for a in all_alerts if a.filtered),
        "elapsed_seconds": crawl_elapsed,
        "report": str(report_path),
    }
    logger.info(f"Pipeline完成: {json.dumps(summary, ensure_ascii=False)}")
    return summary


def _get_stock_name(stocks: list[dict], code: str) -> str:
    for s in stocks:
        if s["stock_code"] == code:
            return s.get("stock_name", "")
    return ""


# ════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="xueqiu-monitor — 雪球舆情监控系统")
    parser.add_argument("-c", "--config", default="config/config.json",
                        help="配置文件路径 (default: config/config.json)")
    parser.add_argument("--dry-run", action="store_true",
                        help="只爬取不推送")
    parser.add_argument("--init-db", action="store_true",
                        help="仅初始化数据库")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="详细日志")
    parser.add_argument("--report", action="store_true",
                        help="仅生成日报（从已有数据）")
    args = parser.parse_args()

    setup_logging(args.verbose)

    if args.init_db:
        cfg = Config.from_file(args.config)
        db.init_db(cfg.db_path)
        print(f"数据库初始化完成: {cfg.db_path}")
        return

    if args.report:
        cfg = Config.from_file(args.config)
        alerts = db.get_today_alerts(cfg.db_path)
        report = notifier.generate_daily_report(alerts)
        print(report)
        return

    summary = run_pipeline(args.config, dry_run=args.dry_run)
    if summary.get("error"):
        sys.exit(1)
    sys.exit(0 if summary.get("crawled", 0) > 0 else 1)


if __name__ == "__main__":
    main()
