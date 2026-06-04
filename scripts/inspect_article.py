#!/usr/bin/env python3
"""Inspect xueqiu news article page to find correct content selectors."""
import sys, os, time
from pathlib import Path
sys.path.insert(0, os.environ.get(
    "XUEQIU_ANALYZER_PATH",
    str(Path(__file__).resolve().parent.parent.parent / "xueqiu-analyzer-skill" / "src")
))

from xueqiu_analyzer.crawler import XueqiuCrawler
from playwright.sync_api import sync_playwright

crawler = XueqiuCrawler({"headless": True, "delay_min": 1, "delay_max": 2})

print("🔍 爬取 SH600519 资讯列表...")
result = crawler.crawl("SH600519", max_pages=1, max_articles=0)

# Find a news item with a regular xueqiu article link
import re
news_with_links = [n for n in result.news if n.link and re.match(r'https://xueqiu\.com/\d+/\d+', n.link)]
if not news_with_links:
    # Try any news link
    news_with_links = [n for n in result.news if n.link and 'xueqiu.com' in n.link]
    
if news_with_links:
    target = news_with_links[0]
    print(f"\n📰 目标资讯: {target.title[:80]}")
    print(f"  链接: {target.link}")
    
    with sync_playwright() as p:
        browser, context = crawler._create_browser_context(p)
        page = context.new_page()
        
        url = target.link
        print(f"\n🌐 访问: {url}")
        page.goto(url, timeout=30000)
        time.sleep(3)
        crawler._close_modal(page)
        
        # Extract title
        title = page.title()
        print(f"  页面标题: {title[:100]}")
        
        # Test current selectors
        current_selectors = [
            '.article__bd__detail',
            '.detail-content',
            '.article-content',
            '[class*="article-detail"]',
            '[class*="detail_body"]',
            '.stock-news-content',
        ]
        print("\n🧪 选择器测试:")
        for sel in current_selectors:
            el = page.query_selector(sel)
            if el:
                text = el.inner_text().strip()
                print(f"  ✅ {sel}: {len(text)} chars → \"{text[:80]}...\"")
            else:
                print(f"  ❌ {sel}: not found")
        
        # Get all significant text blocks
        print("\n📦 页面所有长文本块 (>100 chars):")
        content = page.evaluate('''() => {
            const blocks = [];
            const walker = document.createTreeWalker(
                document.body, NodeFilter.SHOW_ELEMENT, null, false
            );
            let node;
            while (node = walker.nextNode()) {
                if (['SCRIPT','STYLE','NOSCRIPT','META','LINK'].includes(node.tagName)) continue;
                if (node.children.length > 0) continue; // leaf nodes only
                const text = node.textContent.trim();
                if (text.length > 50) {
                    const tagPath = node.parentElement ? 
                        node.parentElement.tagName + (node.parentElement.className ? '.' + node.parentElement.className.split(' ')[0] : '') + 
                        ' > ' + node.tagName : node.tagName;
                    blocks.push({tag: tagPath, len: text.length, text: text.substring(0, 100)});
                }
            }
            return blocks.slice(0, 20);
        }''')
        for b in content:
            print(f"  [{b['len']}c] {b['tag']}: {b['text'][:80]}...")
        
        # Save HTML for inspection
        html = page.content()
        html_path = '/tmp/xueqiu_article.html'
        with open(html_path, 'w') as f:
            f.write(html)
        print(f"\n💾 HTML 保存至: {html_path} ({len(html)} bytes)")
        
        browser.close()
else:
    print("❌ 没有找到可访问的资讯链接")
    # Show all news links
    for n in result.news:
        print(f"  [{n.title[:60]}] → {n.link}")
