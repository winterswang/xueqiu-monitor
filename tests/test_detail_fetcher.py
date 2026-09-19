"""Unit tests for detail_fetcher (v2 Phase 2, 2026-09-20).

Covers the news three-gate filter, announcement classification and the
garbled-PDF heuristic — all pure functions, no network calls. The zhipu
reader/search channels were live-verified 2026-09-19 (see module docstring)
and are exercised end-to-end by the daily cron, not here.
"""

from src import detail_fetcher as df


class TestFilterNewsPosts:
    """news 三道过滤闸: 时效 → 噪音 → 限量."""

    def test_stale_news_dropped(self):
        posts = [
            {"title": "旧闻事件", "content": "x",
             "link": "https://finance.sina.com.cn/jjxw/2026-09-01/doc-a.shtml"},
            {"title": "今日事件", "content": "y",
             "link": "https://finance.sina.com.cn/jjxw/2026-09-19/doc-b.shtml"},
        ]
        kept = df.filter_news_posts(posts, "2026-09-19")
        assert [p["title"] for p in kept] == ["今日事件"]

    def test_yesterday_kept_within_default_window(self):
        posts = [
            {"title": "昨日事件", "content": "y",
             "link": "https://finance.sina.com.cn/jjxw/2026-09-18/doc-b.shtml"},
        ]
        kept = df.filter_news_posts(posts, "2026-09-19")
        assert len(kept) == 1

    def test_url_without_date_passes_time_gate(self):
        # URL 无日期 → 不受时效闸 (交由噪音闸与 LLM 甄别)
        posts = [{"title": "无日期链接的事件", "content": "y",
                  "link": "https://www.example.com/news/xyz"}]
        assert len(df.filter_news_posts(posts, "2026-09-19")) == 1

    def test_noise_titles_dropped(self):
        posts = [
            {"title": "公司获融资买入1.2亿元", "content": "x", "link": ""},
            {"title": "衍生权证补充上市文件", "content": "x", "link": ""},
            {"title": "股价盘中涨超5%", "content": "x", "link": ""},
            {"title": "公司宣布签订重大合作协议", "content": "x", "link": ""},
        ]
        kept = df.filter_news_posts(posts, "2026-09-19")
        assert [p["title"] for p in kept] == ["公司宣布签订重大合作协议"]

    def test_announcement_repost_dropped(self):
        # 新浪 AIGC 公告转载与 announcements 表重叠
        posts = [{"title": "中远海能(01138)公告及通告 - [股息或分派]", "content": "x",
                  "link": "https://finance.sina.com.cn/x/2026-09-19/a.shtml"}]
        assert df.filter_news_posts(posts, "2026-09-19") == []

    def test_per_stock_limit(self):
        posts = [
            {"title": f"事件新闻{i}", "content": "x" * 50,
             "link": f"https://finance.sina.com.cn/jjxw/2026-09-19/doc{i}.shtml",
             "like_count": 10 - i}
            for i in range(8)
        ]
        kept = df.filter_news_posts(posts, "2026-09-19", per_stock_limit=5)
        assert len(kept) == 5
        # 互动量高的排前
        assert kept[0]["title"] == "事件新闻0"


class TestClassifyAnnouncement:
    """公告分级: high / routine / other, high 优先."""

    def test_high_value(self):
        assert df.classify_announcement("关于回购公司股份的进展公告") == "high"
        assert df.classify_announcement("2026年中期报告") == "high"
        assert df.classify_announcement("拟收购某公司股权的公告") == "high"

    def test_routine(self):
        assert df.classify_announcement("翌日披露报表") == "routine"
        assert df.classify_announcement("持续督导跟踪报告") == "routine"

    def test_other(self):
        assert df.classify_announcement("关于召开业绩说明会的通知") == "other"

    def test_high_beats_routine_when_both_match(self):
        # 同时命中 high 与 routine 词 → high 优先 (宁可多抓)
        assert df.classify_announcement("翌日披露报表-回购股份") == "high"


class TestGarbledDetection:
    """巨潮 PDF 乱码启发式."""

    def test_normal_chinese_passes(self):
        text = "这是腾讯控股的翌日披露报表正文。" * 30
        assert df._looks_like_garbled(text) is False

    def test_garbled_detected(self):
        text = "FF305 W ֻ∉܋∉ ၵರ஼ೝḸі ٺܢứྛದˢˢ༰" * 30
        assert df._looks_like_garbled(text) is True

    def test_short_text_skips_check(self):
        assert df._looks_like_garbled("FF305 W ֻ∉") is False


class TestStripMenuJunk:
    """东财/新浪页面导航菜单清洗."""

    def test_leading_menu_stripped(self):
        text = "- 财经\n- 焦点\n- 股票\n- 新股\n- 期指\n- 期权\n\n华住集团Q1业绩：营收60亿元。正文开始。" + "正文内容。" * 20
        out = df._strip_menu_junk(text)
        assert out.startswith("华住集团")

    def test_normal_text_untouched(self):
        text = "这是一段正常正文，没有菜单。" * 10
        assert df._strip_menu_junk(text) == text

    def test_short_menu_below_threshold_kept(self):
        # 少于 5 行短行不视为菜单
        text = "财经\n股票\n\n正文从这里开始。" + "内容。" * 10
        assert df._strip_menu_junk(text) == text
