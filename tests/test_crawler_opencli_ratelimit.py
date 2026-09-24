"""Regression tests for the opencli comments rate-limit retry (2026-09-24).

Failure shape (observed 9/22-9/23, 2423.HK & 300866.SZ, also 981.HK 9/23):
    opencli comments → []   (rate-limited)
    opencli stock-notices → 50 条 (healthy)
    → "notices 非空" satisfied the skip-Playwright condition → stock failed
      with zero posts, no snapshot row.

Patch: posts empty AND notices non-empty → sleep 30s → retry fetch_discussions
once → only then accept the empty result (existing failed path).

Run from project root:
    python -m pytest tests/test_crawler_opencli_ratelimit.py -v --tb=short
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import crawler as crawler_mod


# ═══════════════════════════════════════════════════════════════
# Fixtures / helpers
# ═══════════════════════════════════════════════════════════════

_DISC_ITEM = {
    "id": "410439499",
    "author": "自由的赚钱小猎豹",
    "text": "$安克创新(SZ300866)$ 要绿一个月么",
    "likes": 0,
    "replies": 2,
    "retweets": 0,
    "created_at": "2026-09-24T01:58:48.000Z",
    "url": "https://xueqiu.com/5436372535/410439499",
}

_NOTICE_ITEM = {
    "title": "2026年半年度A股权益分派实施公告",
    "type": "公司公告",
    "created_at": "2026-09-23",
    "url": "https://xueqiu.com/info/SZ300866/announcement-detail",
}


def _fake_opencli_module(disc_results, notice_results):
    """Build a fake xueqiu_analyzer.fetcher_opencli module.

    disc_results / notice_results: lists returned on successive calls
    (last value repeats if exhausted — convenient for "always empty").
    """
    mod = MagicMock()
    mod.is_available = MagicMock(return_value=True)

    def _sequencer(results):
        return MagicMock(side_effect=list(results))

    mod.fetch_discussions = _sequencer(disc_results)
    mod.fetch_notices = _sequencer(notice_results)
    mod.fetch_news = MagicMock(return_value=[])
    mod.fetch_replies = MagicMock(return_value=[])
    return mod


def _install(monkeypatch, fake_mod, sleeps):
    monkeypatch.setattr(crawler_mod, "_ensure_xueqiu_analyzer_path", lambda: "/fake")
    monkeypatch.setitem(sys.modules, "xueqiu_analyzer.fetcher_opencli", fake_mod)
    # record sleep durations instead of actually waiting 30s
    real_sleep = crawler_mod.time.sleep
    monkeypatch.setattr(
        crawler_mod.time, "sleep", lambda s: sleeps.append(s) or real_sleep(0)
    )
    # never call Playwright from these tests; record if we do
    def _no_playwright(stock_code, timeout, max_retries=0):
        raise AssertionError("Playwright fallback must not run in these tests")

    monkeypatch.setattr(crawler_mod, "_crawl_with_retry", _no_playwright)
    # no LLM calls
    monkeypatch.setattr(
        crawler_mod.sentiment, "analyze_sentiment_batch", lambda posts: [0.0] * len(posts)
    )


# ═══════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════

class TestRateLimitRetry:
    """posts 空且 notices 非空 → sleep 30s → 重试一次."""

    def test_retry_recovers_after_rate_limit(self, monkeypatch):
        """首次 comments 空(限流) + notices 正常 → 30s 后重试拿到帖子 → success."""
        fake = _fake_opencli_module([[], [_DISC_ITEM]], [[_NOTICE_ITEM]])
        sleeps: list = []
        _install(monkeypatch, fake, sleeps)

        result = crawler_mod.crawl_single_stock("300866.SZ", timeout=30, db_path=None)

        assert result["status"] == "success"
        assert result["posts_count"] == 1
        assert result["posts_data"][0]["post_id"] == _DISC_ITEM["url"]
        assert fake.fetch_discussions.call_count == 2, \
            "fetch_discussions must be called exactly twice"
        assert sleeps == [30], f"expected one 30s sleep, got {sleeps}"

    def test_retry_still_empty_falls_to_failed(self, monkeypatch):
        """重试仍空 → 维持原行为: 落 failed, posts_count=0."""
        fake = _fake_opencli_module([[], []], [[_NOTICE_ITEM]])
        sleeps: list = []
        _install(monkeypatch, fake, sleeps)

        result = crawler_mod.crawl_single_stock("300866.SZ", timeout=30, db_path=None)

        assert result["status"] == "failed"
        assert result["posts_count"] == 0
        assert fake.fetch_discussions.call_count == 2
        assert sleeps == [30]

    def test_no_retry_when_notices_also_empty(self, monkeypatch):
        """两路都空 → 不 sleep, 不重试(整站故障走 Playwright 回退语义)."""
        fake = _fake_opencli_module([[]], [[]])
        sleeps: list = []
        # allow Playwright fallback to run (mocked) so the flow completes
        _install(monkeypatch, fake, sleeps)
        monkeypatch.setattr(
            crawler_mod, "_crawl_with_retry",
            lambda sc, timeout, max_retries=0: {
                "result": None,
                "diagnostic": {
                    "timed_out": False, "error_type": None,
                    "error_message": "pw-mock-no-data", "crawl_duration_ms": 1,
                    "discussions_count": 0, "news_count": 0,
                    "articles_count": 0, "notices_count": 0,
                },
            },
        )

        result = crawler_mod.crawl_single_stock("300866.SZ", timeout=30, db_path=None)

        assert fake.fetch_discussions.call_count == 1, \
            "no retry expected when notices also empty"
        assert sleeps == [], "no sleep expected when notices also empty"
        assert result["status"] == "failed"

    def test_no_retry_when_first_fetch_succeeds(self, monkeypatch):
        """首抓即有帖 → 不 sleep 不重试(不惩罚正常路径)."""
        fake = _fake_opencli_module([[_DISC_ITEM]], [[_NOTICE_ITEM]])
        sleeps: list = []
        _install(monkeypatch, fake, sleeps)

        result = crawler_mod.crawl_single_stock("300866.SZ", timeout=30, db_path=None)

        assert result["status"] == "success"
        assert result["posts_count"] == 1
        assert fake.fetch_discussions.call_count == 1
        assert sleeps == []

    def test_retry_exception_does_not_crash(self, monkeypatch):
        """重试调用本身抛异常 → 吃掉, 维持 failed, 不影响其他股票."""
        fake = _fake_opencli_module([[]], [[_NOTICE_ITEM]])
        sleeps: list = []
        _install(monkeypatch, fake, sleeps)

        def _boom(code, limit=100):
            if fake.fetch_discussions.call_count >= 2:
                raise RuntimeError("opencli bridge died")
            return []

        fake.fetch_discussions = MagicMock(side_effect=_boom)

        result = crawler_mod.crawl_single_stock("300866.SZ", timeout=30, db_path=None)

        assert result["status"] == "failed"
        assert sleeps == [30]
