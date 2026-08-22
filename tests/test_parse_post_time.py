"""Unit tests for _parse_post_time() — Xueqiu time-string parser.

Regression coverage for the YYYY-MM-DD format gap (2026-07-29):
posts with absolute dates (e.g. LKNCY 2020-2022 news articles) previously
parsed to 0.0, which caused the incremental filter to always keep them.
"""

from datetime import datetime, timedelta

from src.crawler import _parse_post_time


# ════════════════════════════════════════════════════════
# Relative formats (existing behaviour, must not regress)
# ════════════════════════════════════════════════════════

class TestRelativeFormats:
    def test_minutes_ago(self):
        now = datetime(2026, 7, 29, 12, 0, 0).timestamp()
        ts = _parse_post_time("5分钟前", now)
        assert abs(ts - (now - 5 * 60)) < 1

    def test_hours_ago(self):
        now = datetime(2026, 7, 29, 12, 0, 0).timestamp()
        ts = _parse_post_time("3小时前", now)
        assert abs(ts - (now - 3 * 3600)) < 1

    def test_seconds_ago(self):
        now = datetime(2026, 7, 29, 12, 0, 0).timestamp()
        ts = _parse_post_time("30秒前", now)
        assert abs(ts - (now - 30)) < 1


# ════════════════════════════════════════════════════════
# "昨天 HH:MM"
# ════════════════════════════════════════════════════════

class TestYesterdayFormat:
    def test_yesterday_with_time(self):
        now = datetime(2026, 7, 29, 12, 0, 0).timestamp()
        ts = _parse_post_time("昨天 15:30", now)
        expected = datetime(2026, 7, 28, 15, 30, 0).timestamp()
        assert abs(ts - expected) < 1


# ════════════════════════════════════════════════════════
# "MM-DD HH:MM" / "MM-DD"
# ════════════════════════════════════════════════════════

class TestMonthDayFormats:
    def test_mmdd_hhmm(self):
        now = datetime(2026, 7, 29, 12, 0, 0).timestamp()
        ts = _parse_post_time("07-28 09:15", now)
        expected = datetime(2026, 7, 28, 9, 15, 0).timestamp()
        assert abs(ts - expected) < 1

    def test_mmdd_only(self):
        now = datetime(2026, 7, 29, 12, 0, 0).timestamp()
        ts = _parse_post_time("07-28", now)
        expected = datetime(2026, 7, 28, 0, 0, 0).timestamp()
        assert abs(ts - expected) < 1


# ════════════════════════════════════════════════════════
# "HH:MM" (today)
# ════════════════════════════════════════════════════════

class TestTodayFormat:
    def test_hhmm_today(self):
        now = datetime(2026, 7, 29, 12, 0, 0).timestamp()
        ts = _parse_post_time("09:30", now)
        expected = datetime(2026, 7, 29, 9, 30, 0).timestamp()
        assert abs(ts - expected) < 1


# ════════════════════════════════════════════════════════
# NEW: "YYYY-MM-DD" — the bug fix (was returning 0.0)
# ════════════════════════════════════════════════════════

class TestYyyyMmDdFormat:
    """Posts from previous years use absolute YYYY-MM-DD dates.

    Before the fix these returned 0.0, causing the incremental filter to
    always keep them (fail-open). LKNCY.US had 6 of 7 posts from 2020-2022
    re-analyzed every single day.
    """

    def test_yyyymmdd_basic(self):
        """2022-02-05 must parse to the correct timestamp, not 0.0."""
        now = datetime(2026, 7, 29, 12, 0, 0).timestamp()
        ts = _parse_post_time("2022-02-05", now)
        expected = datetime(2022, 2, 5, 0, 0, 0).timestamp()
        assert abs(ts - expected) < 1

    def test_yyyymmdd_does_not_return_zero(self):
        """The core regression: must NOT return 0.0 for valid YYYY-MM-DD."""
        now = datetime(2026, 7, 29, 12, 0, 0).timestamp()
        assert _parse_post_time("2020-10-13", now) > 0
        assert _parse_post_time("2021-03-17", now) > 0
        assert _parse_post_time("2022-02-05", now) > 0

    def test_yyyymmdd_old_post_is_old(self):
        """A 2020 post must produce a timestamp older than a 2026 'now'."""
        now = datetime(2026, 7, 29, 12, 0, 0).timestamp()
        old_ts = _parse_post_time("2020-10-13", now)
        # Should be ~6 years in the past
        assert (now - old_ts) > 365 * 5 * 86400

    def test_yyyymmdd_invalid_date(self):
        """Invalid calendar date should return 0.0, not raise."""
        now = datetime(2026, 7, 29, 12, 0, 0).timestamp()
        assert _parse_post_time("2022-02-30", now) == 0.0  # Feb 30 doesn't exist
        assert _parse_post_time("2022-13-01", now) == 0.0  # month 13


class TestYyyyMmDdHhMmFormat:
    """YYYY-MM-DD HH:MM — absolute date with time component."""

    def test_yyyymmdd_hhmm_basic(self):
        now = datetime(2026, 7, 29, 12, 0, 0).timestamp()
        ts = _parse_post_time("2022-02-05 14:30", now)
        expected = datetime(2022, 2, 5, 14, 30, 0).timestamp()
        assert abs(ts - expected) < 1

    def test_yyyymmdd_hhmm_single_digit_hour(self):
        now = datetime(2026, 7, 29, 12, 0, 0).timestamp()
        ts = _parse_post_time("2021-12-14 9:05", now)
        expected = datetime(2021, 12, 14, 9, 5, 0).timestamp()
        assert abs(ts - expected) < 1

    def test_yyyymmdd_hhmm_does_not_return_zero(self):
        now = datetime(2026, 7, 29, 12, 0, 0).timestamp()
        assert _parse_post_time("2022-01-29 10:00", now) > 0


class TestIso8601Format:
    """ISO 8601 format from opencli (since 2026-08-08).

    opencli scrapes xueqiu.com DOM which returns time as
    '2026-08-12T04:57:37.000Z'. Before this fix, _parse_post_time returned 0.0,
    causing ALL posts to be marked "时间无法解析" and last_crawl_time to freeze
    at 8/7 (never updated because all_ts list was empty).
    """

    def test_iso8601_with_millis_z(self):
        """The exact format opencli returns. Z = UTC, converted to Beijing (+8)."""
        now = datetime(2026, 8, 12, 12, 0, 0).timestamp()
        ts = _parse_post_time("2026-08-12T04:57:37.000Z", now)
        expected = datetime(2026, 8, 12, 12, 57, 37).timestamp()
        assert abs(ts - expected) < 1

    def test_iso8601_without_millis(self):
        now = datetime(2026, 8, 12, 12, 0, 0).timestamp()
        ts = _parse_post_time("2026-08-12T04:57:37Z", now)
        expected = datetime(2026, 8, 12, 12, 57, 37).timestamp()
        assert abs(ts - expected) < 1

    def test_iso8601_does_not_return_zero(self):
        """The core regression: must NOT return 0.0 for valid ISO 8601."""
        now = datetime(2026, 8, 12, 12, 0, 0).timestamp()
        assert _parse_post_time("2026-08-12T04:57:37.000Z", now) > 0
        assert _parse_post_time("2026-08-08T00:00:01.000Z", now) > 0

    def test_iso8601_with_timezone_offset(self):
        """ISO 8601 with +08:00 timezone offset."""
        now = datetime(2026, 8, 12, 12, 0, 0).timestamp()
        ts = _parse_post_time("2026-08-12T12:57:37+08:00", now)
        expected = datetime(2026, 8, 12, 12, 57, 37).timestamp()
        assert abs(ts - expected) < 1

    def test_iso8601_invalid_date(self):
        """Invalid calendar date in ISO format should return 0.0."""
        now = datetime(2026, 8, 12, 12, 0, 0).timestamp()
        assert _parse_post_time("2026-02-30T04:57:37.000Z", now) == 0.0


# ════════════════════════════════════════════════════════
# Edge cases & unparseable strings
# ════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_empty_string(self):
        now = datetime(2026, 7, 29, 12, 0, 0).timestamp()
        assert _parse_post_time("", now) == 0.0

    def test_none(self):
        now = datetime(2026, 7, 29, 12, 0, 0).timestamp()
        assert _parse_post_time(None, now) == 0.0

    def test_whitespace_only(self):
        now = datetime(2026, 7, 29, 12, 0, 0).timestamp()
        assert _parse_post_time("   ", now) == 0.0

    def test_truly_unparseable(self):
        """Genuinely malformed strings should still return 0.0."""
        now = datetime(2026, 7, 29, 12, 0, 0).timestamp()
        assert _parse_post_time("just some text", now) == 0.0
        assert _parse_post_time("2026年7月29日", now) == 0.0  # CN date not supported
