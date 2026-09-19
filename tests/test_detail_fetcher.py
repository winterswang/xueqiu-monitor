"""Unit tests for detail_fetcher (v2 Phase 2, 2026-09-20).

Covers the news three-gate filter, announcement classification, the
garbled-PDF heuristic, the PDF front-matter (封面/目录页) skip and the EDGAR
HTML → text conversion — all pure functions, no network calls. The fetch
channels themselves (local PDF / opencli / EDGAR / zhipu fallback) were
live-verified 2026-09-19/20 and are exercised end-to-end by the daily cron,
not here.
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

    def test_eastmoney_stale_link_dropped(self):
        # 东财「相关推荐」旧闻: URL 日期在文件名里 (/a/YYYYMMDD…html)
        posts = [{"title": "半年前的旧闻", "content": "x",
                  "link": "https://finance.eastmoney.com/a/202604133703112215.html"}]
        assert df.filter_news_posts(posts, "2026-09-19") == []
        fresh = [{"title": "今日新闻", "content": "x",
                  "link": "https://finance.eastmoney.com/a/202609193875401912.html"}]
        assert len(df.filter_news_posts(fresh, "2026-09-19")) == 1

    def test_10jqka_date_directory_dropped(self):
        posts = [{"title": "四月旧文", "content": "x",
                  "link": "https://stock.10jqka.com.cn/20260414/c1.shtml"}]
        assert df.filter_news_posts(posts, "2026-09-19") == []

    def test_implausible_url_digits_not_treated_as_date(self):
        # 12 位数字目录不是日期, 不能被时效闸误杀
        posts = [{"title": "普通链接", "content": "x",
                  "link": "https://www.example.com/20260919123456/p.html"}]
        assert df.news_staleness_days(posts[0]["link"], "2026-09-19") is None
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

    def test_us_forms_classified_by_form_name(self):
        # 美股标题是英文 form 名 (无中文关键词) —— 按 form 分级, 否则
        # SEC EDGAR 通道永远收不到任务 (2026-09-20 复盘)
        assert df.classify_announcement(
            "6-K Report of foreign issuer [Rules 13a-16 and 15d-16] "
            "Accession Number: 0001046179-26-000658 Act: 34 Size: 100 KB"
        ) == "high"
        assert df.classify_announcement(
            "20-F Annual and transition report of foreign private issuers"
        ) == "high"
        assert df.classify_announcement(
            "10-Q Quarterly report [Sections 13 or 15(d)]"
        ) == "high"

    def test_us_insider_forms_not_high(self):
        assert df.classify_announcement(
            "4 Statement of changes in beneficial ownership of securities"
        ) == "other"
        assert df.classify_announcement(
            "144 Report of proposed sale of securities"
        ) == "other"


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


class TestPdfUrlDetect:
    """PDF 通道识别 (本地解析 vs opencli/reader)."""

    def test_pdf_urls(self):
        assert df._is_pdf_url("http://static.cninfo.com.cn/f/f.PDF")
        assert df._is_pdf_url("https://stockn.xueqiu.com/00700/20260918681665.pdf")
        assert df._is_pdf_url("https://x/a.pdf?x=1")

    def test_web_urls(self):
        assert not df._is_pdf_url("https://finance.sina.com.cn/jjxw/a.shtml")
        assert not df._is_pdf_url("https://xueqiu.com/S/TSM")
        assert not df._is_pdf_url("")


class TestPdfFrontMatter:
    """封面 + 目录页跳过 (否则 8000 字上限会被目录吃光)."""

    TOC = ("目錄\n公司資料\n2\n財務摘要\n4\n公司簡介\n5\n"
           "管理層討論及分析\n6\n企業管治及其他資料\n28")
    COVER = "中期報告\n2026\n股份代號：6078"
    BODY = "公司資料\n董事會\n執行董事\n朱義文先生（主席兼首席執行官）"

    def test_toc_page_detected(self):
        assert df._is_toc_page(self.TOC) is True

    def test_english_toc_page_detected(self):
        assert df._is_toc_page(
            "Content\nCORPORATE INFORMATION\n2\nFINANCIAL SUMMARY\n5\n"
            "MANAGEMENT DISCUSSION\n6\nOTHER INFORMATION\n28\n"
            "INDEPENDENT REVIEW REPORT\n38\nCONDENSED CONSOLIDATED\n39"
        ) is True

    def test_content_page_is_not_toc(self):
        assert df._is_toc_page(self.BODY + "\n2\n") is False

    def test_prose_mentioning_content_is_not_toc(self):
        # 正文里出现 content 字样 + 零星数字, 不构成目录页
        assert df._is_toc_page(
            "The content of this announcement\n2026\n" + "正文。" * 30
        ) is False

    def test_content_after_toc_kept(self):
        out = df._pdf_content([self.COVER, self.TOC, self.BODY])
        assert out.startswith("公司資料")

    def test_no_toc_keeps_everything(self):
        pages = ["证券代码：001232 嘉立创 权益分派实施公告", "正文"]
        assert df._pdf_content(pages).startswith("证券代码")


class TestJunkPageDetection:
    """opencli 抓到的废页不能当正文 (新浪 404 跳首页 / Chrome 错误页)."""

    def test_placeholder_page(self):
        assert df._looks_like_junk_page(
            "**页面没有找到 5秒钟之后将会带您进入新浪首页!**", "页面没有找到"
        )

    def test_browser_error_page(self):
        # 雪球 news 流里有 "xueqiu.com/n/<中文标题>" 畸形链接 → Chrome 错误页
        assert df._looks_like_junk_page(
            "# 无法访问此网站  **xueqiu.com** 意外终止了连接。 "
            "ERR\\_CONNECTION\\_CLOSED 请检查您的互联网连接是否正常"
        )

    def test_homepage_menu_pile_via_body(self):
        # 首页 body 2 万字 209 链接; 正常文章不足 20 个
        content = "".join(
            f"新闻标题{i}\n[详情](https://finance.sina.com.cn/x/{i}.shtml)\n"
            for i in range(100)
        )
        assert df._looks_like_junk_page(content, "新浪网", "body") is True
        assert df._looks_like_junk_page(content, "新浪网", "div.article") is False

    def test_real_article_passes(self):
        content = "（来源：电动知家）消息，9月18日微博话题登上热搜。" * 30
        content += "[宁德时代](https://finance.sina.com.cn/realstock/x.shtml)"
        assert df._looks_like_junk_page(content, "“非宁德时代不选”上热搜！") is False


class TestOpencliPageSelection:
    """正文容器按精确度优先取, 不再"取最长" (长 = 整站菜单堆)."""

    @staticmethod
    def _fake(canned: dict):
        def fake(*args, timeout=40):
            if args[0] == "open":
                return {"page": "P1"}
            if args[0] == "close":
                return {}
            return {"title": "测试标题", "content": canned.get(args[2], "")}
        return fake

    def test_first_precise_container_wins(self, monkeypatch):
        canned = {"div.article": "正文内容。" * 100,
                  ".article-content": "菜单项。" * 3000,
                  "body": "整站。" * 5000}
        calls = []
        fake = self._fake(canned)

        def spy(*args, timeout=40):
            calls.append(args)
            return fake(*args, timeout=timeout)

        monkeypatch.setattr(df, "_opencli", spy)
        r = df.fetch_page_opencli("https://finance.sina.com.cn/a.shtml")
        assert r["status"] == "ok"
        assert r["content"].startswith("正文内容")
        assert not any(len(a) > 2 and a[2] == "body" for a in calls)

    def test_falls_back_to_body_when_no_container(self, monkeypatch):
        canned = {"body": "只有 body 有内容。" * 50}
        monkeypatch.setattr(df, "_opencli", self._fake(canned))
        r = df.fetch_page_opencli("https://x.com/a")
        assert r["status"] == "ok" and r["content"].startswith("只有 body")

    def test_body_menu_pile_is_junk(self, monkeypatch):
        canned = {"body": "".join(
            f"[新闻{i}](https://x.com/{i})\n" for i in range(200)
        )}
        monkeypatch.setattr(df, "_opencli", self._fake(canned))
        r = df.fetch_page_opencli("https://x.com/a")
        assert r["status"] == "junk" and r["content"] == ""


class TestHtmlToText:
    """EDGAR filing HTML → 纯文本."""

    def test_tags_scripts_stripped(self):
        html = ("<html><head><style>a{}</style></head><body><p>Hello</p>"
                "<script>x=1</script><div>World</div></body></html>")
        out = df._html_to_text(html)
        assert "Hello" in out and "World" in out
        assert "x=1" not in out and "a{}" not in out

    def test_ix_hidden_and_header_dropped(self):
        # inline-XBRL 的机器事实块必须剥掉, 否则顶掉真正文 (TCOM 20-F 实测)
        html = ("<ix:header><ix:hidden>"
                "<ix:nonNumeric name='x'>999</ix:nonNumeric>"
                "</ix:hidden></ix:header><p>FORM 20-F 正文</p>")
        out = df._html_to_text(html)
        assert "999" not in out
        assert "FORM 20-F 正文" in out

    def test_entities_unescaped(self):
        assert "A&B" in df._html_to_text("<p>A&amp;B</p>")
