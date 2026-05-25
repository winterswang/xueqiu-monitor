#!/usr/bin/env python3
"""Real E2E test for xueqiu-monitor — crawl SH600519, run full pipeline."""
import sys, os, time, json, logging
logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(message)s')

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, '/root/code/xueqiu-analyzer-skill/src')
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from xueqiu_analyzer.crawler import XueqiuCrawler
from config import Config
from db import (init_db, insert_snapshot, insert_alert,
                get_historical_stats, get_today_alerts,
                count_sentiment_days)
from models import CrawlSnapshot
from detector import is_cold_start, detect_post_spike, detect_sentiment_shift
from filter import filter_alerts, assign_priority
from notifier import generate_daily_report

cfg = Config.from_file(os.path.join(PROJECT_ROOT, 'config/config.json'))
init_db(cfg.db_path)

print("🔍 爬取 SH600519...", flush=True)
start = time.time()
crawler = XueqiuCrawler({"headless": True, "delay_min": 1, "delay_max": 2})
result = crawler.crawl("SH600519", max_pages=1, max_articles=3)
elapsed = time.time() - start
print(f"✅ {elapsed:.1f}s | {len(result.discussions)}讨论 {len(result.news)}资讯 {len(result.notices)}公告")

# Parse posts from crawl result
posts = []
for d in result.discussions:
    posts.append({"title": (d.content or "")[:100], "type": "discussion",
                  "content": d.content or "", "sentiment_score": 0.0, "author": d.author or ""})
for n in result.news:
    posts.append({"title": n.title or "", "type": "news", "content": n.content or "",
                  "sentiment_score": 0.0, "author": n.source or ""})

# Store
now = int(time.time())
snap = CrawlSnapshot(stock_code="SH600519", crawl_time=now, posts_count=len(posts),
                     posts_data=json.dumps(posts, ensure_ascii=False),
                     sentiment_avg=0.0, status="success")
sid = insert_snapshot(cfg.db_path, snap)
print(f"📊 snapshot_id={sid} | {len(posts)} posts")

# Cold start check
hist = get_historical_stats(cfg.db_path, "SH600519", 28)
cold = is_cold_start(hist, 28)
print(f"📈 历史: {len(hist)}d → cold_start={cold}")

if cold:
    print(f"⏸️  冷启动期，仅记录不分析")
else:
    spike = detect_post_spike(len(posts), hist)
    sent = detect_sentiment_shift(0.0, hist)
    alerts = [a for a in [spike, sent] if a is not None]
    print(f"📡 检测: {len(alerts)} alerts")
    for a in alerts:
        a.priority = assign_priority(a)
        insert_alert(cfg.db_path, a)
        print(f"  {'🔴' if a.priority=='P0' else '🟡'} {a.alert_type} Z={a.z_score:.1f} {a.priority}")
    filtered = filter_alerts(alerts, posts)
    print(f"🔎 过滤: {len(filtered)} alerts, {sum(1 for a in filtered if a.filtered)} filtered")
    print(f"\n📋 样本:")
    for p in posts[:3]:
        print(f"  [{p['type']}] {p['title'][:70]}")
    print(f"\n📰 早报:\n{generate_daily_report(filtered)[:400]}")

print(f"\n✅ 真实E2E完成")
