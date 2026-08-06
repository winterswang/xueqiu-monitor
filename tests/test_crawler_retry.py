"""Tests for crawler immediate retry logic.

Tests _crawl_with_retry wrapper which retries transient failures
(browser handshake errors, etc.) but NOT timeouts (time budget exhausted).

Run from project root:
    python -m pytest tests/test_crawler_retry.py -v --tb=short
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import crawler as crawler_mod


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _make_success_result():
    """Simulate a successful _crawl_with_timeout return."""
    class FakeCrawlResult:
        discussions = []
        news = []
        articles = []
        notices = []
    return {
        "result": FakeCrawlResult(),
        "diagnostic": {
            "timed_out": False,
            "error_type": None,
            "error_message": None,
            "crawl_duration_ms": 5000,
            "discussions_count": 0,
            "news_count": 0,
            "articles_count": 0,
            "notices_count": 0,
        },
    }


def _make_failure_result(error_msg="Failed to connect to browser"):
    """Simulate a transient failure (e.g. nodriver handshake error)."""
    return {
        "result": None,
        "diagnostic": {
            "timed_out": False,
            "error_type": "ConnectionError",
            "error_message": error_msg,
            "crawl_duration_ms": 21500,
            "discussions_count": 0,
            "news_count": 0,
            "articles_count": 0,
            "notices_count": 0,
        },
    }


def _make_timeout_result():
    """Simulate a timeout (time budget exhausted, should NOT retry)."""
    return {
        "result": None,
        "diagnostic": {
            "timed_out": True,
            "error_type": "timeout",
            "error_message": "爬取超时（600s）",
            "crawl_duration_ms": 600000,
            "discussions_count": 0,
            "news_count": 0,
            "articles_count": 0,
            "notices_count": 0,
        },
    }


# ═══════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════

class TestCrawlWithRetry:
    """Test _crawl_with_retry retry logic."""

    def test_success_first_try_no_retry(self, monkeypatch):
        """max_retries=2 but succeeds on first try → only 1 call."""
        call_count = 0

        def fake_crawl(stock_code, timeout):
            nonlocal call_count
            call_count += 1
            return _make_success_result()

        monkeypatch.setattr(crawler_mod, "_crawl_with_timeout", fake_crawl)

        result = crawler_mod._crawl_with_retry("9992.HK", timeout=600, max_retries=2)

        assert call_count == 1, f"Should call once, got {call_count}"
        assert result["result"] is not None

    def test_failure_then_success_retries_once(self, monkeypatch):
        """max_retries=2, fails first then succeeds → 2 calls, success."""
        responses = [_make_failure_result(), _make_success_result()]
        call_count = 0

        def fake_crawl(stock_code, timeout):
            nonlocal call_count
            idx = min(call_count, len(responses) - 1)
            call_count += 1
            return responses[idx]

        monkeypatch.setattr(crawler_mod, "_crawl_with_timeout", fake_crawl)
        # Patch sleep to avoid real delay in tests
        monkeypatch.setattr(crawler_mod.time, "sleep", lambda _: None)

        result = crawler_mod._crawl_with_retry("9992.HK", timeout=600, max_retries=2)

        assert call_count == 2, f"Should retry once (2 calls), got {call_count}"
        assert result["result"] is not None

    def test_timeout_not_retried(self, monkeypatch):
        """max_retries=2 but timeout → NO retry (time budget exhausted)."""
        call_count = 0

        def fake_crawl(stock_code, timeout):
            nonlocal call_count
            call_count += 1
            return _make_timeout_result()

        monkeypatch.setattr(crawler_mod, "_crawl_with_timeout", fake_crawl)
        monkeypatch.setattr(crawler_mod.time, "sleep", lambda _: None)

        result = crawler_mod._crawl_with_retry("9992.HK", timeout=600, max_retries=2)

        assert call_count == 1, f"Timeout should NOT retry, got {call_count} calls"
        assert result["result"] is None
        assert result["diagnostic"]["timed_out"] is True

    def test_all_failures_exhausts_retries(self, monkeypatch):
        """max_retries=2, all fail → 3 calls (1 initial + 2 retries), returns failure."""
        call_count = 0

        def fake_crawl(stock_code, timeout):
            nonlocal call_count
            call_count += 1
            return _make_failure_result(f"Error attempt {call_count}")

        monkeypatch.setattr(crawler_mod, "_crawl_with_timeout", fake_crawl)
        monkeypatch.setattr(crawler_mod.time, "sleep", lambda _: None)

        result = crawler_mod._crawl_with_retry("9992.HK", timeout=600, max_retries=2)

        assert call_count == 3, f"1 initial + 2 retries = 3 calls, got {call_count}"
        assert result["result"] is None
        assert result["diagnostic"]["timed_out"] is False
        assert "Error attempt 3" in result["diagnostic"]["error_message"]

    def test_max_retries_zero_single_attempt(self, monkeypatch):
        """max_retries=0 → exactly 1 attempt, no retry (preserves current behavior)."""
        call_count = 0

        def fake_crawl(stock_code, timeout):
            nonlocal call_count
            call_count += 1
            return _make_failure_result()

        monkeypatch.setattr(crawler_mod, "_crawl_with_timeout", fake_crawl)
        monkeypatch.setattr(crawler_mod.time, "sleep", lambda _: None)

        result = crawler_mod._crawl_with_retry("9992.HK", timeout=600, max_retries=0)

        assert call_count == 1, f"max_retries=0 should be 1 attempt, got {call_count}"
        assert result["result"] is None


class TestCrawlSingleStockRetry:
    """Test that crawl_single_stock passes max_retries through."""

    def test_crawl_single_stock_accepts_max_retries(self, monkeypatch):
        """crawl_single_stock should accept and use max_retries param."""
        # Mock _crawl_with_retry to verify it's called with max_retries
        captured_kwargs = {}

        def fake_retry(stock_code, timeout, max_retries=0):
            captured_kwargs["max_retries"] = max_retries
            return _make_success_result()

        monkeypatch.setattr(crawler_mod, "_crawl_with_retry", fake_retry)
        # Mock opencli to be unavailable → force Playwright path
        monkeypatch.setattr(crawler_mod, "_ensure_xueqiu_analyzer_path", lambda: "/fake")

        result = crawler_mod.crawl_single_stock(
            "9992.HK", timeout=30, db_path=None, max_retries=3
        )

        assert captured_kwargs.get("max_retries") == 3

    def test_crawl_single_stock_defaults_max_retries_zero(self, monkeypatch):
        """Without max_retries param, crawl_single_stock should default to 0."""
        captured_kwargs = {}

        def fake_retry(stock_code, timeout, max_retries=0):
            captured_kwargs["max_retries"] = max_retries
            return _make_success_result()

        monkeypatch.setattr(crawler_mod, "_crawl_with_retry", fake_retry)
        monkeypatch.setattr(crawler_mod, "_ensure_xueqiu_analyzer_path", lambda: "/fake")

        result = crawler_mod.crawl_single_stock("9992.HK", timeout=30)

        assert captured_kwargs.get("max_retries") == 0
