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

    def test_sorted_engagement_first(self, tmp_db):
        """Posts sort by engagement desc first (v0.7.5), not time."""
        posts = [
            {"title": "较早的高互动帖", "content": "内容内容内容内容", "time": "3小时前",
             "like_count": 100},
            {"title": "最新的零互动帖", "content": "内容内容内容内容", "time": "5分钟前",
             "like_count": 0},
        ]
        tmp_db.insert_snapshot("TEST.HK", posts)
        result = rg.fetch_stock_posts(str(tmp_db.path), "TEST.HK", tmp_db.date_str, min_length=5)
        # High-engagement post first, even though older (note lever 2).
        assert result[0]["title"] == "较早的高互动帖"
        assert result[1]["title"] == "最新的零互动帖"

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
        # 2026-09-20: 告警渲染成人话 (热词飙升 + 具体词), 不再输出裸英文类型名
        assert "热词飙升" in prompt

    def test_prompt_alert_carries_hot_word(self):
        """热词告警必须把具体词带进 prompt (否则 LLM 只能写空话)."""
        posts = [{"title": "测试帖子", "content": "内容", "author": "A",
                  "like_count": 0, "forward_count": 0, "comment_count": 0,
                  "link": "", "time": ""}]
        trend = {"has_trend": True, "today_mean": 0.1, "avg_mean": 0.05,
                 "avg_std": 0.02, "trend": "上升", "days": 7}
        alerts = [{"type": "hot_word_surge", "priority": "P1", "z_score": 4.5,
                   "detail": {"word": "gemini", "curr_tfidf": 2.6,
                              "hist_mean": 1.1}}]
        prompt = rg._build_analysis_prompt(
            "测试股", "TEST.US", posts, trend, alerts, ["财报"]
        )
        assert "gemini" in prompt
        assert "财报" in prompt
        assert "今日新增" in prompt  # v2 deep 档五段
        assert "多空交锋" in prompt
        assert "值得关注的判断" in prompt
        assert "新增风险" in prompt
        assert "情感读数" in prompt

    def test_prompt_includes_post_time(self):
        """Prompt must surface each post's time so LLM can tell new from old."""
        posts = [{"title": "测试帖子", "content": "内容", "author": "A",
                  "like_count": 1, "forward_count": 0, "comment_count": 0,
                  "link": "", "time": "3小时前"}]
        trend = {"has_trend": True, "today_mean": 0.1, "avg_mean": 0.05,
                 "avg_std": 0.02, "trend": "上升", "days": 7}
        prompt = rg._build_analysis_prompt("测试股", "T.US", posts, trend, [], [])
        assert "3小时前" in prompt
        # Posts sorted by engagement (v0.7.5), prompt says so
        assert "按互动量排序" in prompt

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
        assert "今日新增" in prompt  # v2: 持续/新增由 🆕段承接

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
        """Empty posts → graceful 'no data' message with dual calibers."""
        config = {"llm": {"min_post_length": 30}}
        result = rg.analyze_stock("NOEXIST.US", "不存在", str(tmp_db.path), tmp_db.date_str, config)
        section = result["section"]  # v2: 返回 {section, tier, takeaway}
        assert result["tier"] == rg.TIER_FLAT
        assert "无帖子数据" in section
        assert "分析帖数: 0" in section
        assert "当日帖数: 0" in section  # 2026-09-20 改并集口径


# ════════════════════════════════════════════════════════
# v0.7.4 report-credibility tests (P0-1 / P0-2)
# ════════════════════════════════════════════════════════


class TestFetchStockPostsWindow:
    """max_age_days window semantics (v0.7.4)."""

    def test_default_window_is_today_only(self, tmp_db):
        """max_age_days=1 must behave exactly like the old single-day filter."""
        posts = [
            {"title": "今日新帖足够长度", "content": "这是今天发的帖子内容", "time": "2小时前"},
            {"title": "历史热门帖足够长度", "content": "这是五月发的旧帖子内容", "time": "05-27 14:30",
             "like_count": 999},
        ]
        tmp_db.insert_snapshot("TEST.HK", posts)
        result = rg.fetch_stock_posts(str(tmp_db.path), "TEST.HK", tmp_db.date_str, min_length=5)
        assert len(result) == 1
        assert result[0]["title"] == "今日新帖足够长度"

    def test_widened_window_includes_recent_history(self, tmp_db):
        """max_age_days=8 picks up posts 05-27..08-19 when today is empty (P0-2 pattern)."""
        posts = [
            {"title": "三天前的热帖内容足够长", "content": "低流量股票的历史热帖", "time": "3天前",
             "like_count": 50},
            {"title": "两个月前的旧帖内容足够长", "content": "窗口外的更早旧帖", "time": "60天前"},
        ]
        tmp_db.insert_snapshot("TEST.HK", posts)
        result = rg.fetch_stock_posts(
            str(tmp_db.path), "TEST.HK", tmp_db.date_str, min_length=5, max_age_days=8
        )
        assert len(result) == 1
        assert result[0]["title"] == "三天前的热帖内容足够长"


class TestAnalyzeStockFallback:
    """No-today-posts fallback to a 7-day window (P0-2)."""

    def test_fallback_picks_up_recent_posts(self, tmp_db):
        """Posts only in the past week → analyzed under 近7日 scope, not 'no data'."""
        posts = [
            {"title": "五天前的帖子内容足够长", "content": "低流量股票的历史热帖，内容补充到足够通过三十字的最小长度过滤线",
             "time": "5天前", "like_count": 30},
        ]
        tmp_db.insert_snapshot("TEST.HK", posts)
        config = {"llm": {"min_post_length": 30}}
        result = rg.analyze_stock("TEST.HK", "测试股", str(tmp_db.path), tmp_db.date_str, config)
        section = result["section"]  # v2: 返回 {section, tier, takeaway}
        assert "无帖子数据" not in section
        assert "近7日" in section
        assert "分析帖数: 1" in section

    def test_fallback_not_triggered_when_today_has_posts(self, tmp_db):
        """Today's posts present → scope stays 今日, old post excluded."""
        posts = [
            {"title": "今日新帖足够长度", "content": "今天的内容", "time": "2小时前"},
            {"title": "五天前的帖子内容足够长", "content": "历史热帖", "time": "5天前"},
        ]
        tmp_db.insert_snapshot("TEST.HK", posts)
        config = {"llm": {"min_post_length": 10}}
        result = rg.analyze_stock("TEST.HK", "测试股", str(tmp_db.path), tmp_db.date_str, config)
        section = result["section"]
        assert "近7日" not in section
        assert "分析帖数: 1" in section

    def test_snapshot_count_surfaced_in_header(self, tmp_db):
        """Dual calibers: snapshot size vs filtered count both in header (P0-1)."""
        posts = [
            {"title": "今日新帖足够长度", "content": "今天的内容", "time": "2小时前"},
            {"title": "历史热门帖足够长度", "content": "旧帖子", "time": "05-27 14:30"},
            {"title": "回复@某人: 哈哈", "content": "回复内容"},
        ]
        tmp_db.insert_snapshot("TEST.HK", posts)
        config = {"llm": {"min_post_length": 5}}
        result = rg.analyze_stock("TEST.HK", "测试股", str(tmp_db.path), tmp_db.date_str, config)
        section = result["section"]
        # Day-union has 3 posts (single snapshot), only 1 survives filtering
        assert "当日帖数: 3" in section  # 2026-09-20 改并集口径
        assert "分析帖数: 1" in section
        assert "温度计口径" in section


class TestNormalizeLlmHeadings:
    """Heading-level normalization of LLM output (v0.7.4 template rule)."""

    def test_h1_h2_h3_demoted_to_h5(self):
        """# / ## / ### headlines → ##### so they stay below #### stock headers."""
        raw = "# 大标题\n内容\n## 中标题\n### 小标题\n#### 已是四级\n"
        out = rg._normalize_llm_headings(raw)
        import re as _re
        # Every heading line must start with exactly 5 #'s
        levels = [len(m.group(1)) for m in _re.finditer(r"^(#+) ", out, _re.MULTILINE)]
        assert levels == [5, 5, 5, 5]
        assert "大标题" in out and "已是四级" in out

    def test_h5_h6_untouched(self):
        raw = "##### 五级\n###### 六级\n"
        out = rg._normalize_llm_headings(raw)
        assert "##### 五级" in out
        assert "###### 六级" in out

    def test_indented_hash_not_heading(self):
        """Indented # (list item or code block content) must not be rewritten."""
        raw = "  # 缩进的井号不是标题\n- 列表 # 带井号\n"
        out = rg._normalize_llm_headings(raw)
        assert "  # 缩进的井号不是标题" in out


class TestPromptScope:
    """scope label injection into prompts (v0.7.4)."""

    def test_scope_injected_in_fallback_prompt(self):
        posts = [{"title": "t", "content": "c", "author": "a",
                  "like_count": 0, "forward_count": 0, "comment_count": 0,
                  "link": "", "time": ""}]
        prompt = rg._build_analysis_prompt(
            "测试股", "T.US", posts,
            trend={"has_trend": False, "days": 0}, alerts=[], hot_words=[],
            scope="近7日",
        )
        assert "近7日的雪球讨论" in prompt
        assert "近7日讨论帖" in prompt
        assert "近7日新增" in prompt  # v2 deep 档
        # New formatting rules must be present
        assert "严禁" in prompt
        assert "4 个 #" in prompt

    def test_default_scope_is_today(self):
        posts = []
        prompt = rg._build_analysis_prompt(
            "测试股", "T.US", posts,
            trend={"has_trend": False, "days": 0}, alerts=[], hot_words=[],
        )
        assert "今日的雪球讨论" in prompt


# ════════════════════════════════════════════════════════
# Day-union semantics (2026-09-20 修: 一天多快照并集去重)
# ════════════════════════════════════════════════════════


class TestFetchDayPostsUnion:
    """db.fetch_day_posts_union: 当日全部快照并集 + post_id/link 去重."""

    def test_multi_snapshot_union_no_loss(self, tmp_db):
        """两个不相交快照 → 并集全部保留 (修复: 只读最新快照会丢早间帖)."""
        now = int(time.time())
        tmp_db.insert_snapshot("A.HK", [
            {"post_id": "p1", "title": "早间帖", "content": "x"},
        ], ts=now - 3600)
        tmp_db.insert_snapshot("A.HK", [
            {"post_id": "p2", "title": "午后帖", "content": "y"},
        ], ts=now - 60)
        union = rg.db.fetch_day_posts_union(str(tmp_db.path), tmp_db.date_str, "A.HK")
        assert len(union["A.HK"]) == 2

    def test_dup_keeps_latest_snapshot_copy(self, tmp_db):
        """同 post_id 出现在两个快照 → 保留最新快照的副本 (互动数更新)."""
        now = int(time.time())
        tmp_db.insert_snapshot("A.HK", [
            {"post_id": "p1", "title": "同帖", "content": "x", "like_count": 3},
        ], ts=now - 3600)
        tmp_db.insert_snapshot("A.HK", [
            {"post_id": "p1", "title": "同帖", "content": "x", "like_count": 99},
        ], ts=now - 60)
        union = rg.db.fetch_day_posts_union(str(tmp_db.path), tmp_db.date_str, "A.HK")
        assert len(union["A.HK"]) == 1
        assert union["A.HK"][0]["like_count"] == 99

    def test_link_fallback_dedup(self, tmp_db):
        """post_id 为空时用 link 去重."""
        tmp_db.insert_snapshot("A.HK", [{"link": "https://x/1", "title": "a", "content": "x"}])
        tmp_db.insert_snapshot("A.HK", [{"link": "https://x/1", "title": "a", "content": "x"}])
        union = rg.db.fetch_day_posts_union(str(tmp_db.path), tmp_db.date_str, "A.HK")
        assert len(union["A.HK"]) == 1

    def test_empty_key_posts_all_kept(self, tmp_db):
        """post_id 与 link 都为空 → 不去重, 全部保留 (天然过不了下游过滤)."""
        tmp_db.insert_snapshot("A.HK", [{"title": "无ID帖", "content": "x"}, {"title": "无ID帖2", "content": "y"}])
        union = rg.db.fetch_day_posts_union(str(tmp_db.path), tmp_db.date_str, "A.HK")
        assert len(union["A.HK"]) == 2

    def test_stock_filter_and_cross_stock(self, tmp_db):
        """stock_code 过滤生效; 不传则返回全部股票."""
        tmp_db.insert_snapshot("A.HK", [{"post_id": "p1", "title": "a", "content": "x"}])
        tmp_db.insert_snapshot("B.HK", [{"post_id": "p2", "title": "b", "content": "y"}])
        only_a = rg.db.fetch_day_posts_union(str(tmp_db.path), tmp_db.date_str, "A.HK")
        assert list(only_a.keys()) == ["A.HK"]
        both = rg.db.fetch_day_posts_union(str(tmp_db.path), tmp_db.date_str)
        assert set(both.keys()) == {"A.HK", "B.HK"}

    def test_single_snapshot_day_thermometer_matches(self, tmp_db):
        """单快照日: 温度计并集口径 == 快照原值 (回归锚点)."""
        posts = [
            {"post_id": "p1", "title": "a", "content": "x", "sentiment_score": 0.6,
             "like_count": 3, "comment_count": 1, "forward_count": 0},
            {"post_id": "p2", "title": "b", "content": "y", "sentiment_score": -0.2,
             "like_count": 0, "comment_count": 0, "forward_count": 0},
        ]
        tmp_db.insert_snapshot("A.HK", posts)
        thermo = rg.fetch_market_thermometer(str(tmp_db.path), tmp_db.date_str)
        assert len(thermo) == 1
        assert thermo[0]["posts"] == 2
        # equal = (0.6 + -0.2)/2 = 0.2; weighted = (0.6*5 + -0.2*1)/6 = 0.4667
        assert thermo[0]["sentiment"] == pytest.approx(0.2, abs=1e-3)
        assert thermo[0]["sentiment_weighted"] == pytest.approx(0.4667, abs=1e-3)


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

    def insert_snapshot(self, stock_code: str, posts: list[dict], ts: int | None = None):
        """Insert a crawl snapshot with posts_data."""
        import json
        if ts is None:
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


# ════════════════════════════════════════════════════════
# v2 增量分档 (2026-09-20)
# ════════════════════════════════════════════════════════


class TestClassifyStockTier:
    """三档判定: KOL/告警/高权重公告硬通道, 互动量/帖数规则."""

    def test_kol_post_forces_deep(self, tmp_db, monkeypatch):
        monkeypatch.setattr(rg, "_load_kol_whitelist", lambda: {"但斌"})
        posts = [{"title": "t", "content": "c", "author": "但斌"}]
        tier = rg.classify_stock_tier("A.HK", posts, [], [], {})
        assert tier == rg.TIER_DEEP

    def test_p1_alert_forces_deep(self):
        posts = [{"title": "t", "content": "c", "author": "路人"}]
        alerts = [{"type": "hot_word_surge", "priority": "P1", "z_score": 3.0, "detail": {}}]
        assert rg.classify_stock_tier("A.HK", posts, alerts, [], {}) == rg.TIER_DEEP

    def test_high_value_announcement_forces_deep(self):
        posts = [{"title": "t", "content": "c", "author": "路人"}]
        anns = [{"title": "2026年中期报告"}]
        assert rg.classify_stock_tier("A.HK", posts, [], anns, {}) == rg.TIER_DEEP

    def test_engagement_threshold_deep(self):
        posts = [
            {"title": "t", "content": "c", "author": "x",
             "like_count": 40, "comment_count": 10, "forward_count": 0}
            for _ in range(3)
        ]
        assert rg.classify_stock_tier("A.HK", posts, [], [], {}) == rg.TIER_DEEP

    def test_std_and_flat(self):
        std_posts = [{"title": "t", "content": "c", "author": "x"}] * 3
        assert rg.classify_stock_tier("A.HK", std_posts, [], [], {}) == rg.TIER_STD
        few = [{"title": "t", "content": "c", "author": "x"}]
        assert rg.classify_stock_tier("A.HK", few, [], [], {}) == rg.TIER_FLAT


class TestTakeawayExtraction:
    """⭐最有价值观点 精确锚定提取 (修复残缺片段)."""

    def test_extracts_after_marker(self):
        text = "##### 情感读数\n读数内容\n\n**⭐最有价值观点：**\n`⭐ [SK海力士] 存储周期顶部论 [31]`"
        # 模拟 analyze_stock 内部逻辑
        import re as _re
        m = _re.search(r"最有价值观点[^\n]*\n+\s*`?⭐\s*([^\n`]{8,200})`?", text)
        assert m and "存储周期顶部论" in m.group(1)

    def test_ignores_body_star(self):
        text = "正文提到 ⭐KOL 某人发言很长一段没有标记行"
        import re as _re
        m = _re.search(r"最有价值观点[^\n]*\n+\s*`?⭐\s*([^\n`]{8,200})`?", text)
        assert m is None


class TestAlertSummary:
    """告警 detail → 人话摘要 (2026-09-20: 此前只有裸英文类型名)."""

    def test_hot_word_surge_names_the_word(self):
        out = rg._alert_summary({
            "type": "hot_word_surge",
            "detail": {"word": "gemini", "curr_tfidf": 2.6657, "hist_mean": 1.1009},
        })
        assert "gemini" in out and "2.7" in out and "1.1" in out

    def test_post_spike_carries_counts(self):
        out = rg._alert_summary({
            "type": "post_spike",
            "detail": {"curr_count": 31, "historical_mean": 4.9},
        })
        assert "31" in out and "4.9" in out

    def test_sentiment_shift_carries_both_sides(self):
        out = rg._alert_summary({
            "type": "sentiment_shift",
            "detail": {"curr_sentiment": -0.35, "prev_snapshot_sentiment": 0.4},
        })
        assert "-0.35" in out and "+0.40" in out

    def test_announcement_uses_title(self):
        out = rg._alert_summary({
            "type": "new_announcement", "detail": {"title": "完成配售新H股"},
        })
        assert out == "完成配售新H股"

    def test_missing_detail_degrades_gracefully(self):
        assert rg._alert_summary({"type": "post_spike", "detail": {}}) != ""
        assert rg._alert_summary({"type": "unknown_kind", "detail": {}}) == "unknown_kind"


class TestNoDataStock:
    """0 帖股票必须进"平稳股"紧凑行, 不能占一个"有增量"小节."""

    def test_zero_post_stock_is_compact_flat(self, tmp_db):
        res = rg.analyze_stock(
            "TSLA.US", "特斯拉", str(tmp_db.path), tmp_db.date_str,
            {"llm": {"min_post_length": 30}},
        )
        assert res["tier"] == rg.TIER_FLAT
        assert res["analyzed"] is False
        body = res["section"].split("\n\n", 2)[2]
        # 组装层按此前缀把股票归入紧凑列表 (2026-09-19 实测特斯拉被误放进
        # "其他今日有增量的股票" 完整小节)
        assert body.strip().startswith("（无新增量）")

    def test_compact_row_uses_the_marker(self, tmp_db):
        res = rg.analyze_stock(
            "TSLA.US", "特斯拉", str(tmp_db.path), tmp_db.date_str,
            {"llm": {"min_post_length": 30}},
        )
        body = res["section"].split("\n\n", 2)[2]
        first_line = body.strip().splitlines()[0].strip()
        assert first_line.startswith("（无新增量）")
        assert len(first_line) < 88

