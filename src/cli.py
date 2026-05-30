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

from .config import Config
from . import db
from . import crawler
from . import detector
from . import filter as rule_filter
from . import notifier
from . import feedback as fbloop
from .models import (
    CrawlSnapshot, SentimentStat, ChangeAlert,
    HotWordEvent, PushHistory, Comment, Announcement,
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

    # Load watchlist (pass crawler config for morning_brief_db override)
    watchlist_cfg = {"watchlist_path": cfg.watchlist_path, **cfg.crawler}
    stocks = crawler.load_watchlist(watchlist_cfg)
    if not stocks:
        logger.error("自选股列表为空，终止")
        return {"error": "empty_watchlist", "crawled": 0, "alerts": 0}

    # Whitelist filter
    whitelist = cfg.crawler.get("whitelist", [])
    if whitelist:
        before = len(stocks)
        stocks = [s for s in stocks if s["stock_code"] in whitelist]
        skipped = set(whitelist) - {s["stock_code"] for s in stocks}
        logger.info(f"白名单模式: {len(stocks)}/{before} 只 (配置 {len(whitelist)} 只)")
        if skipped:
            logger.warning(f"白名单中以下股票不在 watchlist: {skipped}")
        if not stocks:
            logger.error("白名单过滤后为空，终止")
            return {"error": "empty_whitelist", "crawled": 0, "alerts": 0}

    # Crawl all stocks (parallel if concurrency > 1)
    concurrency = cfg.crawler.get("concurrency", 1)
    logger.info(f"开始爬取 {len(stocks)} 只自选股 (concurrency={concurrency}) ...")
    start_time = time.time()
    crawl_results = crawler.crawl_watchlist(
        stocks, cfg.crawler["timeout_seconds"], cfg.db_path, concurrency=concurrency
    )
    crawl_elapsed = int(time.time() - start_time)
    logger.info(f"爬取完成: {crawl_elapsed}s")

    # Process each crawl result
    total_alerts = 0
    all_alerts: list[ChangeAlert] = []
    stock_extra: dict[str, dict] = {}  # supplemental data for push_immediate key_data

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

        # ── Store comments ──
        comments_list: list[Comment] = []
        for post in cr["posts_data"]:
            comments_list.append(Comment(
                snapshot_id=snapshot_id,
                post_id=post.get("post_id", ""),
                comment_count=post.get("comment_count", 0),
                forward_count=post.get("forward_count", 0),
                like_count=post.get("like_count", 0),
            ))
        if comments_list:
            n_comments = db.insert_comments(db_path, comments_list)
            logger.debug(f"  {stock_code}: 写入 {n_comments} 条评论")

        # ── Store announcements ──
        anns_list: list[Announcement] = []
        for ann in cr.get("announcements", []):
            anns_list.append(Announcement(
                snapshot_id=snapshot_id,
                stock_code=stock_code,
                ann_title=ann.get("title", ""),
                ann_date=int(time.time()),
                ann_type=ann.get("notice_type", ""),
                is_new=0,
            ))
        if anns_list:
            n_anns = db.insert_announcements(db_path, anns_list)
            logger.debug(f"  {stock_code}: 写入 {n_anns} 条公告")

        # ── Announcement detection ──
        prev_snap_anns = []
        if snapshot_id > 1:
            prev_snap = db.get_previous_snapshot(db_path, stock_code, cr["crawl_time"])
            if prev_snap and prev_snap.id is not None:
                prev_snap_anns = db.get_announcements_by_snapshot(db_path, prev_snap.id)
        curr_anns = cr.get("announcements", [])
        ann_alerts = detector.detect_new_announcement(
            stock_code, curr_anns, prev_snap_anns
        )
        for a in ann_alerts:
            a.alert_time = now

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

        # Detect sentiment shift (with two-period direct threshold)
        prev_snap = db.get_previous_snapshot(db_path, stock_code, cr["crawl_time"])
        prev_snap_sentiment = prev_snap.sentiment_avg if prev_snap else None
        sent_alert = detector.detect_sentiment_shift(
            cr["sentiment_avg"], hist_stats, cfg.detector["z_score_window_days"],
            prev_snapshot_sentiment=prev_snap_sentiment,
        )
        if sent_alert:
            sent_alert.stock_code = stock_code
            sent_alert.alert_time = now

        alerts = [a for a in [spike_alert, sent_alert] if a is not None] + ann_alerts

        # Compute sample std from historical sentiment means
        sentiment_values = [s.sentiment_mean for s in hist_stats if s.sentiment_mean != 0.0]
        if len(sentiment_values) >= 2:
            n = len(sentiment_values)
            mean_s = sum(sentiment_values) / n
            variance_s = sum((x - mean_s) ** 2 for x in sentiment_values) / (n - 1)
            computed_std = variance_s ** 0.5
        else:
            computed_std = 0.0

        # ── Store sentiment stat for next run ──
        today_start = now // 86400 * 86400
        stat = SentimentStat(
            stock_code=stock_code,
            stat_date=today_start,
            posts_count=cr["posts_count"],
            sentiment_mean=cr["sentiment_avg"],
            sentiment_std=computed_std,
            z_score=max(a.z_score for a in alerts) if alerts else 0.0,
            z_alert=1 if alerts else 0,
        )
        db.insert_sentiment_stat(db_path, stat)

        # ── TF-IDF hot words ──
        posts_texts = [p.get("content", "") or p.get("title", "") for p in cr["posts_data"]]
        curr_tfidf = {}
        if posts_texts:
            hist_events = db.get_recent_hot_word_events(db_path, stock_code, cfg.detector["z_score_window_days"])
            hw_alerts = detector.detect_hot_word_emergence(
                stock_code, posts_texts, hist_events,
                cfg.detector["tfidf_min_df"], cfg.detector["tfidf_max_df"],
            )
            alerts.extend(hw_alerts)

            # Store hot word events + update hot_word_dict
            curr_tfidf = dict(detector.compute_tfidf(posts_texts, cfg.detector["tfidf_min_df"], cfg.detector["tfidf_max_df"]))
            for word, score in curr_tfidf.items():
                db.insert_hot_word_event(db_path, HotWordEvent(
                    stock_code=stock_code,
                    word=word,
                    tfidf_score=round(score, 4),
                    event_time=now,
                    z_score=0.0,  # computed above
                ))
                db.upsert_hot_word(db_path, word, now)

        # ── Collect supplemental data for push key_data (§2.5)
        prev_sent_mean = hist_stats[0].sentiment_mean if hist_stats else 0.0
        prev_posts = hist_stats[0].posts_count if hist_stats else 0
        hot_words_top = sorted(curr_tfidf.items(), key=lambda x: x[1], reverse=True)[:3] if curr_tfidf else []
        post_titles_top = [p.get("title", "") for p in cr["posts_data"] if p.get("title")][:3]

        stock_extra[stock_code] = {
            "stock_name": _get_stock_name(stocks, stock_code),
            "sentiment_avg": cr["sentiment_avg"],
            "sentiment_shift": cr["sentiment_avg"] - prev_sent_mean,
            "posts_count": cr["posts_count"],
            "posts_count_delta": cr["posts_count"] - prev_posts,
            "hot_words": [w for w, _ in hot_words_top],
            "post_titles": post_titles_top,
            "trigger_time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        }

        # ── Filter ──
        alerts = rule_filter.filter_alerts(alerts, cr["posts_data"], cold, cfg.filter)

        # ── Store alerts ──
        for alert in alerts:
            alert_id = db.insert_alert(db_path, alert)
            alert.id = alert_id
            total_alerts += 1
            all_alerts.append(alert)

    # ── Notification ──
    pending_path = cfg.notification.get("pending_path", "/tmp/xueqiu_monitor_pending.json")
    p0_alerts = [a for a in all_alerts if not a.filtered and a.priority == "P0"]
    p1_alerts = [a for a in all_alerts if not a.filtered and a.priority == "P1"]

    pending_messages: list[str] = []

    if not dry_run:
        # P0: format immediate alert messages (one per alert)
        for alert in p0_alerts:
            extra = stock_extra.get(alert.stock_code, {})
            key_data = {
                "stock_code": alert.stock_code,
                "stock_name": extra.get("stock_name", ""),
                "alert_type": alert.alert_type,
                "z_score": alert.z_score,
                "sentiment_avg": extra.get("sentiment_avg", 0.0),
                "sentiment_shift": extra.get("sentiment_shift", 0.0),
                "posts_count": extra.get("posts_count", 0),
                "posts_count_delta": extra.get("posts_count_delta", 0),
                "hot_words": extra.get("hot_words", []),
                "post_titles": extra.get("post_titles", []),
                "magnitude": alert.magnitude,
                "priority": alert.priority,
                "trigger_time": extra.get("trigger_time", ""),
            }
            msg = notifier.format_immediate_alert_message(alert, key_data)
            pending_messages.append(msg)
            db.insert_push(db_path, PushHistory(
                stock_code=alert.stock_code,
                alert_id=alert.id or 0,
                priority="P0",
                content=f"Z={alert.z_score} {alert.alert_type}",
                status="pending",
            ))

        # P1: format digest message
        if p1_alerts:
            msg = notifier.format_digest_message(p1_alerts)
            if msg:
                pending_messages.append(msg)
            for alert in p1_alerts:
                db.insert_push(db_path, PushHistory(
                    stock_code=alert.stock_code,
                    alert_id=alert.id or 0,
                    priority="P1",
                    content="digest",
                    status="pending",
                ))

        # Dispatch messages via lark-cli or file fallback
        if pending_messages:
            notifier.dispatch_messages(
                pending_messages, pending_path,
                mode=cfg.notification.get("mode", "auto"),
                lark_chat_id=cfg.notification.get("lark_chat_id") or None,
            )

    # ── Daily report ──
    # Build posts_data_map for report
    posts_data_map = {}
    for cr in crawl_results:
        if cr["status"] == "success" and cr.get("posts_data"):
            posts_data_map[cr["stock_code"]] = cr["posts_data"]

    report = notifier.generate_daily_report(all_alerts, posts_data_map)
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

    # ── Crawl Health Report ──
    _log_crawl_health(crawl_results, summary, logger)

    # ── Health alert: success rate < 98% (§2.1) ──
    total_stocks = len(crawl_results)
    if not dry_run and total_stocks > 0:
        success_count = summary["crawled"]
        success_rate = success_count / total_stocks
        if success_rate < 0.98:
            failed_count = total_stocks - success_count
            health_msg = (
                f"⚠️ **爬取健康告警**\n\n"
                f"成功率 {success_rate:.0%}（{failed_count}/{total_stocks} 失败）\n\n"
                f"请检查网络状态和 xueqiu-analyzer 日志。"
            )
            notifier.dispatch_messages(
                [health_msg], pending_path,
                mode=cfg.notification.get("mode", "auto"),
                lark_chat_id=cfg.notification.get("lark_chat_id") or None,
            )
            logger.warning(
                f"爬取成功率 {success_rate:.0%}（{failed_count}/{total_stocks} 失败）→ 已写入待发送消息"
            )

    return summary


def _get_stock_name(stocks: list[dict], code: str) -> str:
    for s in stocks:
        if s["stock_code"] == code:
            return s.get("stock_name", "")
    return ""


def _log_crawl_health(
    crawl_results: list[dict], summary: dict, logger: logging.Logger
) -> None:
    """Log a crawl health report: posts coverage, zero-post stocks, timeout ratio.

    Helps distinguish "stock genuinely has no content" from "crawler is broken".
    """
    total = len(crawl_results)
    if total == 0:
        return

    # ── 1. Posts coverage ──
    stocks_with_posts = [
        r for r in crawl_results
        if r.get("posts_count", 0) > 0
    ]
    stocks_success_zero = [
        r for r in crawl_results
        if r["status"] == "success" and r.get("posts_count", 0) == 0
    ]
    stocks_timeout = [r for r in crawl_results if r["status"] == "timeout"]
    stocks_failed = [r for r in crawl_results if r["status"] == "failed"]

    posts_ratio = len(stocks_with_posts) / total * 100

    logger.info(
        "═" * 50
    )
    logger.info("爬取健康报告")
    logger.info(
        "═" * 50
    )
    logger.info(
        f"有帖股票数: {len(stocks_with_posts)}/{total} ({posts_ratio:.0f}%)"
    )
    logger.info(
        f"成功但零帖: {len(stocks_success_zero)} | "
        f"超时: {len(stocks_timeout)} | "
        f"失败: {len(stocks_failed)}"
    )

    # ── 2. WARNING: ALL stocks have zero posts ──
    if len(stocks_with_posts) == 0:
        logger.warning(
            "⚠ 所有股票 posts_count=0 —— 爬虫可能已失效或雪球无数据返回"
        )

    # ── 3. WARNING: >50% timeout ──
    timeout_ratio = len(stocks_timeout) / total * 100
    if timeout_ratio > 50:
        logger.warning(
            f"⚠ 超时率 {timeout_ratio:.0f}%（>{50}%）—— "
            f"建议增大 crawler.timeout_seconds（当前 {summary.get('elapsed_seconds', 0)//max(total,1)}s/股）"
        )

    # ── 4. List suspicious zero-post stocks (success but no content) ──
    if stocks_success_zero:
        zero_codes = [r["stock_code"] for r in stocks_success_zero]
        logger.warning(
            f"⚠ 以下股票爬取成功但帖数为0（可能爬虫对该股票失效）: "
            f"{', '.join(zero_codes[:20])}"
            + (f" ...及其他 {len(zero_codes) - 20} 只" if len(zero_codes) > 20 else "")
        )

    # ── 5. Per-stock diagnostic drill-down (DEBUG level) ──
    for r in crawl_results:
        diag = r.get("diagnostic", {})
        if diag:
            logger.debug(
                f"  {r['stock_code']}: diag="
                f"timed_out={diag.get('timed_out')}, "
                f"err={diag.get('error_type')}, "
                f"dur={diag.get('crawl_duration_ms')}ms, "
                f"d={diag.get('discussions_count')}/"
                f"n={diag.get('news_count')}/"
                f"a={diag.get('articles_count')}/"
                f"nt={diag.get('notices_count')}"
            )

    logger.info(
        "═" * 50
    )


# ════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="xueqiu-monitor — 雪球舆情监控系统")
    parser.add_argument("-c", "--config", default="etc/config.json",
                        help="配置文件路径 (default: etc/config.json)")
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