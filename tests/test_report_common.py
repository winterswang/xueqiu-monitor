"""Unit tests for src/report_common.py — shared recency post filter (v0.7 F3).

Covers the single source of truth for recency-based post filtering that the
report / export paths share, introduced to fix R1 (export_csv no time filter)
and R2 (daily_sentiment_report full-100-post aggregation).
"""

import time

from src.report_common import filter_posts_by_recency


def _post(time_str: str, post_id: str = "p") -> dict:
    return {"time": time_str, "post_id": post_id, "title": "t"}


def test_keeps_recent_iso_posts():
    """Posts with ISO 8601 time within the window are kept."""
    now = time.time()
    recent = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.localtime(now - 3600))
    out = filter_posts_by_recency([_post(recent)], now=now, max_age_days=1)
    assert len(out) == 1


def test_drops_old_posts():
    """Posts older than max_age_days are dropped."""
    now = time.time()
    old = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.localtime(now - 30 * 86400))
    out = filter_posts_by_recency([_post(old)], now=now, max_age_days=1)
    assert out == []


def test_respects_max_age_days():
    """max_age_days widens the window (KB scenario uses 7d)."""
    now = time.time()
    week_old = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.localtime(now - 5 * 86400))
    # 5 days > 1d window → dropped
    assert filter_posts_by_recency([_post(week_old)], now=now, max_age_days=1) == []
    # 5 days < 7d window → kept
    assert len(filter_posts_by_recency([_post(week_old)], now=now, max_age_days=7)) == 1


def test_fail_open_keeps_unparseable():
    """Unparseable time (ts==0) is kept by default (fail-open policy)."""
    out = filter_posts_by_recency([_post("无法解析的时间")], now=time.time())
    assert len(out) == 1


def test_fail_closed_drops_unparseable():
    """fail_open=False drops unparseable posts."""
    out = filter_posts_by_recency(
        [_post("无法解析的时间")], now=time.time(), fail_open=False
    )
    assert out == []


def test_relative_time_parsed():
    """Relative times (X分钟前) are parsed by the default crawler parser."""
    out = filter_posts_by_recency([_post("5分钟前")], now=time.time())
    assert len(out) == 1


def test_does_not_mutate_input():
    """The input list/dicts are not mutated."""
    now = time.time()
    old = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.localtime(now - 30 * 86400))
    posts = [_post(old, "a"), _post("5分钟前", "b")]
    _ = filter_posts_by_recency(posts, now=now)
    assert len(posts) == 2
    assert posts[0]["time"] == old
