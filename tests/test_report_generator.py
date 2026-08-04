"""Unit tests for report_generator module.

Tests SQL data fetching, prompt building, and report assembly logic.
LLM calls are mocked — no real API calls in tests.
"""

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from src import report_generator as rg


class TestFetchStockPosts:
    """Test fetch_stock_posts filtering logic."""

    def test_filters_reply_posts(self, tmp_db):
        """Reply posts (回复@ prefix) must be filtered out."""
        posts = [
            {"title": "正常帖子，有足够长度的标题内容", "content": "这是一个正常的帖子内容，用于测试过滤逻辑", "author": "A"},
            {"title": "回复@某人: 哈哈", "content": "回复内容", "author": "B"},
            {"title": "正常讨论帖，标题也够长", "content": "另一个正常帖子内容，足够通过过滤", "author": "C"},
        ]
        tmp_db.insert_snapshot("TEST.HK", posts)
        result = rg.fetch_stock_posts(str(tmp_db.path), "TEST.HK", tmp_db.date_str, min_length=10)
        assert len(result) == 2
        assert all(not p["title"].startswith("回复@") for p in result)

    def test_filters_short_posts(self, tmp_db):
        """Posts shorter than min_length must be filtered."""
        posts = [
            {"title": "短", "content": "很短"},
            {"title": "正常长度的帖子标题", "content": "这是一个有足够内容的帖子，用于测试过滤逻辑"},
        ]
        tmp_db.insert_snapshot("TEST.HK", posts)
        result = rg.fetch_stock_posts(str(tmp_db.path), "TEST.HK", tmp_db.date_str, min_length=30)
        assert len(result) == 1
        assert "正常" in result[0]["title"]

    def test_empty_snapshot(self, tmp_db):
        """No snapshot → empty list."""
        result = rg.fetch_stock_posts(str(tmp_db.path), "NOEXIST.US", tmp_db.date_str)
        assert result == []

    def test_sorted_by_engagement(self, tmp_db):
        """Posts with unknown time sort by engagement desc (stable fallback)."""
        posts = [
            {"title": "低互动", "content": "内容内容内容内容", "like_count": 0, "forward_count": 0, "comment_count": 1},
            {"title": "高互动", "content": "内容内容内容内容", "like_count": 100, "forward_count": 50, "comment_count": 20},
            {"title": "中互动", "content": "内容内容内容内容", "like_count": 10, "forward_count": 5, "comment_count": 5},
        ]
        tmp_db.insert_snapshot("TEST.HK", posts)
        result = rg.fetch_stock_posts(str(tmp_db.path), "TEST.HK", tmp_db.date_str, min_length=5)
        # All posts have no time field → fall back to engagement sort
        assert result[0]["title"] == "高互动"
        assert result[1]["title"] == "中互动"
        assert result[2]["title"] == "低互动"

    def test_filters_old_posts_by_time(self, tmp_db):
        """Posts with an old MM-DD time must be filtered out."""
        posts = [
            {"title": "今日新帖足够长度", "content": "这是今天发的帖子内容", "time": "2小时前"},
            {"title": "历史热门帖足够长度", "content": "这是五月发的旧帖子内容", "time": "05-27 14:30",
             "like_count": 999},
        ]
        tmp_db.insert_snapshot("TEST.HK", posts)
        result = rg.fetch_stock_posts(str(tmp_db.path), "TEST.HK", tmp_db.date_str, min_length=5)
        # Only today's post survives, even though the old one has higher engagement
        assert len(result) == 1
        assert result[0]["title"] == "今日新帖足够长度"

    def test_fail_open_unparseable_time_kept(self, tmp_db):
        """Posts with unparseable time must be kept (fail-open)."""
        posts = [
            {"title": "无时间字段的帖子内容", "content": "这个帖子没有time字段但内容够长", "time": ""},
            {"title": "时间格式奇怪的帖子", "content": "这个帖子的时间格式无法解析但内容够长", "time": "garbage!!"},
        ]
        tmp_db.insert_snapshot("TEST.HK", posts)
        result = rg.fetch_stock_posts(str(tmp_db.path), "TEST.HK", tmp_db.date_str, min_length=5)
        # Both kept because time can't be parsed (fail-open)
        assert len(result) == 2

    def test_sorted_newest_first(self, tmp_db):
        """Posts with known time sort newest first."""
        posts = [
            {"title": "较早的今日帖", "content": "内容内容内容内容", "time": "3小时前",
             "like_count": 100},
            {"title": "最新的今日帖", "content": "内容内容内容内容", "time": "5分钟前",
             "like_count": 0},
        ]
        tmp_db.insert_snapshot("TEST.HK", posts)
        result = rg.fetch_stock_posts(str(tmp_db.path), "TEST.HK", tmp_db.date_str, min_length=5)
        # Newest (5分钟前) first despite lower engagement
        assert result[0]["title"] == "最新的今日帖"
        assert result[1]["title"] == "较早的今日帖"

    def test_internal_ts_key_stripped(self, tmp_db):
        """Internal _ts sort key must not leak into returned dicts."""
        tmp_db.insert_snapshot("TEST.HK", [
            {"title": "测试帖子内容足够长", "content": "内容内容内容内容", "time": "1小时前"}
        ])
        result = rg.fetch_stock_posts(str(tmp_db.path), "TEST.HK", tmp_db.date_str, min_length=5)
        assert result and "_ts" not in result[0]


class TestSentimentTrend:
    """Test sentiment trend calculation."""

    def test_insufficient_data(self, tmp_db):
        """<2 data points → has_trend=False."""
        tmp_db.insert_sentiment_stat("TEST.HK", sentiment_mean=0.1)
        result = rg.fetch_sentiment_trend(str(tmp_db.path), "TEST.HK")
        assert result["has_trend"] is False

    def test_valid_trend(self, tmp_db):
        """>=2 data points with non-zero means → has_trend=True."""
        tmp_db.insert_sentiment_stat("TEST.HK", sentiment_mean=0.1, days_ago=2)
        tmp_db.insert_sentiment_stat("TEST.HK", sentiment_mean=0.2, days_ago=1)
        result = rg.fetch_sentiment_trend(str(tmp_db.path), "TEST.HK")
        assert result["has_trend"] is True
        assert "trend" in result
        assert result["days"] >= 2


class TestBuildPrompt:
    """Test LLM prompt construction."""

    def test_prompt_contains_all_sections(self):
        """Prompt must reference all data sources."""
        posts = [{"title": "测试帖子", "content": "内容", "author": "A",
                   "like_count": 0, "forward_count": 0, "comment_count": 0, "link": "", "time": ""}]
        trend = {"has_trend": True, "today_mean": 0.1, "avg_mean": 0.05,
                 "avg_std": 0.02, "trend": "上升", "days": 7}
        alerts = [{"type": "hot_word_surge", "priority": "P1", "z_score": 4.5, "detail": {}}]
        hot_words = ["财报", "增长"]

        prompt = rg._build_analysis_prompt("测试股", "TEST.US", posts, trend, alerts, hot_words)

        assert "测试股" in prompt
        assert "TEST.US" in prompt
        assert "0.1" in prompt
        assert "上升" in prompt
        assert "hot_word_surge" in prompt
        assert "财报" in prompt
        assert "讨论焦点" in prompt
        assert "多空分歧" in prompt
        assert "风险提示" in prompt
        assert "情感解读" in prompt

    def test_prompt_includes_post_time(self):
        """Prompt must surface each post's time so LLM can tell new from old."""
        posts = [{"title": "测试帖子", "content": "内容", "author": "A",
                  "like_count": 1, "forward_count": 0, "comment_count": 0,
                  "link": "", "time": "3小时前"}]
        trend = {"has_trend": True, "today_mean": 0.1, "avg_mean": 0.05,
                 "avg_std": 0.02, "trend": "上升", "days": 7}
        prompt = rg._build_analysis_prompt("测试股", "T.US", posts, trend, [], [])
        assert "3小时前" in prompt
        # Posts sorted by time, not engagement
        assert "按发帖时间倒序" in prompt

    def test_prompt_with_no_trend(self):
        """Prompt handles missing trend gracefully."""
        posts = []
        trend = {"has_trend": False, "mean": None, "std": None, "days": 1}
        prompt = rg._build_analysis_prompt("测试", "T.US", posts, trend, [], [])
        assert "数据积累中" in prompt

    def test_prompt_includes_yesterday_delta(self):
        """Prompt includes yesterday comparison when provided."""
        posts = [{"title": "测试", "content": "内容", "author": "A",
                   "like_count": 0, "forward_count": 0, "comment_count": 0}]
        trend = {"has_trend": True, "today_mean": 0.15, "avg_mean": 0.05,
                 "avg_std": 0.02, "trend": "上升", "days": 7}
        yesterday = {
            "has_data": True,
            "yesterday_str": "2026-07-23",
            "sentiment": 0.05,
            "posts_count": 60,
            "top_hot_words": ["储能", "电池"],
        }
        prompt = rg._build_analysis_prompt("测试", "T.US", posts, trend, [], [], yesterday=yesterday)
        assert "昨日对比" in prompt
        assert "0.05" in prompt
        assert "储能" in prompt
        assert "↑" in prompt  # 0.15 - 0.05 = 0.10 > 0.05

    def test_prompt_includes_streaks_annotation(self):
        """Prompt annotates hot words with streak counts."""
        posts = [{"title": "测试", "content": "内容", "author": "A",
                   "like_count": 0, "forward_count": 0, "comment_count": 0}]
        trend = {"has_trend": True, "today_mean": 0.0, "avg_mean": 0.0,
                 "avg_std": 0.01, "trend": "平稳", "days": 7}
        streaks = [
            {"word": "储能", "today_tfidf": 3.5, "streak_days": 5, "is_persistent": True},
            {"word": "新话题", "today_tfidf": 1.2, "streak_days": 1, "is_persistent": False},
        ]
        prompt = rg._build_analysis_prompt("测试", "T.US", posts, trend, [], [],
                                           streaks=streaks)
        assert "连续5天" in prompt
        assert "🆕新增" in prompt
        assert "话题连续性" in prompt

    def test_prompt_handles_missing_yesterday(self):
        """Prompt shows 'no data' when yesterday context absent."""
        posts = []
        trend = {"has_trend": False, "days": 1}
        prompt = rg._build_analysis_prompt("测试", "T.US", posts, trend, [], [])
        assert "无昨日数据" in prompt


class TestYesterdaySummary:
    """Test fetch_yesterday_summary."""

    def test_returns_has_data_when_yesterday_exists(self, tmp_db):
        """Yesterday sentiment_stats present → has_data=True."""
        tmp_db.insert_sentiment_stat("TEST.HK", sentiment_mean=0.15, days_ago=1)
        result = rg.fetch_yesterday_summary(str(tmp_db.path), "TEST.HK", tmp_db.date_str)
        assert result["has_data"] is True
        assert result["sentiment"] == 0.15

    def test_returns_no_data_when_missing(self, tmp_db):
        """No yesterday data → has_data=False."""
        result = rg.fetch_yesterday_summary(str(tmp_db.path), "NOEXIST.US", tmp_db.date_str)
        assert result["has_data"] is False


class TestHotWordStreaks:
    """Test fetch_hot_word_streaks."""

    def test_persistent_vs_new(self, tmp_db):
        """Words appearing 3+ days → persistent; 1 day → new."""
        import sqlite3
        conn = sqlite3.connect(str(tmp_db.path))
        now = int(time.time())
        # Insert hot words across 4 days
        for days_ago in range(4):
            ts = now - days_ago * 86400
            conn.execute(
                "INSERT INTO hot_word_event (stock_code, word, tfidf_score, event_time, z_score) VALUES (?, ?, ?, ?, 0)",
                ("TEST.HK", "持续词", 3.0, ts)
            )
        # New word today only
        conn.execute(
            "INSERT INTO hot_word_event (stock_code, word, tfidf_score, event_time, z_score) VALUES (?, ?, ?, ?, 0)",
            ("TEST.HK", "新词", 1.5, now)
        )
        conn.commit()
        conn.close()

        streaks = rg.fetch_hot_word_streaks(str(tmp_db.path), "TEST.HK", tmp_db.date_str)
        assert len(streaks) >= 2
        by_word = {s["word"]: s for s in streaks}
        assert by_word["持续词"]["is_persistent"] is True
        assert by_word["持续词"]["streak_days"] >= 3
        assert by_word["新词"]["is_persistent"] is False


class TestThermometerSection:
    """Test market thermometer Markdown generation."""

    def test_thermometer_table_format(self):
        """Thermometer generates valid markdown table."""
        thermo = [
            {"stock_code": "AAA.US", "posts": 100, "sentiment": 0.15},
            {"stock_code": "BBB.HK", "posts": 80, "sentiment": -0.05},
        ]
        stocks_cfg = {"AAA.US": {"name": "股票A"}, "BBB.HK": {"name": "股票B"}}
        md = rg._build_thermometer_section(thermo, stocks_cfg)
        assert "| 股票 |" in md
        assert "股票A" in md
        assert "股票B" in md
        assert "最积极" in md


class TestAnalyzeStockNoPosts:
    """Test analyze_stock when no posts available."""

    def test_returns_no_data_message(self, tmp_db):
        """Empty posts → graceful 'no data' message."""
        config = {"llm": {"min_post_length": 30}}
        result = rg.analyze_stock("NOEXIST.US", "不存在", str(tmp_db.path), tmp_db.date_str, config)
        assert "无帖子数据" in result


# ════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════

class TmpDB:
    """Temporary SQLite DB helper for tests."""
    def __init__(self, path: str, date_str: str):
        self.path = Path(path)
        self.date_str = date_str
        import sqlite3
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        # Init schema
        from src import db as dbmod
        dbmod.init_db(str(self.path))

    def insert_snapshot(self, stock_code: str, posts: list[dict]):
        """Insert a crawl snapshot with posts_data."""
        import json
        ts = int(time.time())
        self.conn.execute(
            """INSERT INTO crawl_snapshots
               (stock_code, crawl_time, posts_count, posts_data, sentiment_avg, status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (stock_code, ts, len(posts), json.dumps(posts), 0.0, "success")
        )
        self.conn.commit()

    def insert_sentiment_stat(self, stock_code: str, sentiment_mean: float = 0.0,
                               sentiment_std: float = 0.01, days_ago: int = 0):
        """Insert a sentiment_stats row."""
        ts = int(time.time()) - days_ago * 86400
        # Normalize to midnight
        ts = ts // 86400 * 86400
        self.conn.execute(
            """INSERT OR REPLACE INTO sentiment_stats
               (stock_code, stat_date, posts_count, sentiment_mean, sentiment_std, z_score, z_alert)
               VALUES (?, ?, ?, ?, ?, 0.0, 0)""",
            (stock_code, ts, 50, sentiment_mean, sentiment_std)
        )
        self.conn.commit()

    def insert_announcement(self, stock_code: str, title: str,
                            ann_link: str = "", notice_type: str = ""):
        """Insert a crawl snapshot + linked announcement row."""
        import json
        ts = int(time.time())
        cur = self.conn.execute(
            """INSERT INTO crawl_snapshots
               (stock_code, crawl_time, posts_count, posts_data, sentiment_avg, status)
               VALUES (?, ?, 0, '[]', 0.0, 'success')""",
            (stock_code, ts)
        )
        snapshot_id = cur.lastrowid
        self.conn.execute(
            """INSERT OR IGNORE INTO announcements
               (snapshot_id, stock_code, ann_title, ann_date, ann_type, ann_link, is_new)
               VALUES (?, ?, ?, ?, ?, ?, 0)""",
            (snapshot_id, stock_code, title, ts, notice_type, ann_link)
        )
        self.conn.commit()

    def cleanup(self):
        self.conn.close()


@pytest.fixture
def tmp_db():
    """Create a temporary DB for each test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = TmpDB(str(db_path), time.strftime("%Y-%m-%d"))
        yield db
        db.cleanup()


# ════════════════════════════════════════════════════════
# Announcement URL passthrough tests
# ════════════════════════════════════════════════════════


class TestAnnouncementUrlPassthrough:
    """Verify announcement detail-page URLs survive the full pipeline:
    crawler -> DB -> fetch_stock_announcements -> LLM prompt."""

    def test_fetch_stock_announcements_returns_link(self, tmp_db):
        """fetch_stock_announcements must include ann_link in each row."""
        tmp_db.insert_announcement(
            "TEST.HK", "季度财报公告",
            ann_link="https://xueqiu.com/announcement/12345",
            notice_type="财报",
        )
        result = rg.fetch_stock_announcements(
            str(tmp_db.path), "TEST.HK", tmp_db.date_str
        )
        assert len(result) == 1
        assert result[0]["link"] == "https://xueqiu.com/announcement/12345"
        assert result[0]["title"] == "季度财报公告"

    def test_fetch_stock_announcements_empty_link(self, tmp_db):
        """Announcements without URL still surface (link='')."""
        tmp_db.insert_announcement("TEST.HK", "无链接公告", ann_link="")
        result = rg.fetch_stock_announcements(
            str(tmp_db.path), "TEST.HK", tmp_db.date_str
        )
        assert len(result) == 1
        assert result[0]["link"] == ""

    def test_prompt_includes_announcement_section_with_link(self, tmp_db):
        """_build_analysis_prompt must render announcement titles + links."""
        tmp_db.insert_announcement(
            "TEST.HK", "回购股份公告",
            ann_link="https://xueqiu.com/announcement/99999",
        )
        anns = rg.fetch_stock_announcements(
            str(tmp_db.path), "TEST.HK", tmp_db.date_str
        )
        prompt = rg._build_analysis_prompt(
            "测试股", "TEST.HK",
            posts=[{"title": "t", "content": "c", "author": "a",
                    "like_count": 0, "forward_count": 0, "comment_count": 0,
                    "link": "", "time": ""}],
            trend={"has_trend": False, "days": 0},
            alerts=[], hot_words=[],
            announcements=anns,
        )
        assert "今日公告" in prompt
        assert "回购股份公告" in prompt
        assert "https://xueqiu.com/announcement/99999" in prompt

    def test_prompt_announcement_section_empty_when_none(self):
        """No announcements -> prompt shows placeholder text."""
        prompt = rg._build_analysis_prompt(
            "测试股", "TEST.HK",
            posts=[{"title": "t", "content": "c", "author": "a",
                    "like_count": 0, "forward_count": 0, "comment_count": 0,
                    "link": "", "time": ""}],
            trend={"has_trend": False, "days": 0},
            alerts=[], hot_words=[],
            announcements=[],
        )
        assert "今日无新公告" in prompt
