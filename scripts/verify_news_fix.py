#!/usr/bin/env python3
"""Verify news content extraction fix — check if content is richer now."""
import sys, os
from pathlib import Path
sys.path.insert(0, os.environ.get(
    "XUEQIU_ANALYZER_PATH",
    str(Path(__file__).resolve().parent.parent.parent / "xueqiu-analyzer-skill" / "src")
))

from xueqiu_analyzer.crawler import XueqiuCrawler

crawler = XueqiuCrawler({"headless": True, "delay_min": 1, "delay_max": 2})
result = crawler.crawl("SH600519", max_pages=1, max_articles=0)

print(f"爬取结果: {len(result.discussions)}讨论 {len(result.news)}资讯 {len(result.notices)}公告")

# Check news content
print("\n📰 资讯内容抽样:")
for i, n in enumerate(result.news[:5]):
    content_len = len(n.content) if n.content else 0
    has_link = bool(n.link)
    is_article = has_link and n.link and '/S/' not in n.link
    status = "✅ 有正文" if content_len > 100 else "⚠️ 正文短" if content_len > 0 else "❌ 无正文"
    print(f"  [{i+1}] {status} | {content_len}c | link={n.link[:60] if n.link else 'None'}")
    if n.content:
        print(f"       {n.content[:120]}...")
    print()
