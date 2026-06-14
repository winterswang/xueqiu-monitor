"""Regression tests for crawl health gate.

The historical bug: most stocks could be status=success while posts_count=0,
so the pipeline looked healthy even though the crawler/API fallback returned no data.
"""

from __future__ import annotations

import logging

from src.cli import _evaluate_crawl_health, _log_crawl_health


def _result(code: str, posts_count: int, status: str = "success") -> dict:
    return {
        "stock_code": code,
        "status": status,
        "posts_count": posts_count,
        "diagnostic": {},
    }


def test_health_gate_degraded_when_posts_coverage_below_20_percent(caplog):
    """6/9-like case: 10/60 has posts, 50/60 success+zero should be degraded."""
    results = [_result(f"OK{i}", 1) for i in range(10)]
    results += [_result(f"ZERO{i}", 0) for i in range(50)]

    health = _evaluate_crawl_health(results)

    assert health["status"] == "degraded"
    assert health["success_with_posts"] == 10
    assert health["empty_success"] == 50
    assert health["posts_coverage"] == 10 / 60

    with caplog.at_level(logging.WARNING):
        logged = _log_crawl_health(results, {"elapsed_seconds": 60}, logging.getLogger("test"))

    assert logged["status"] == "degraded"
    assert "有帖覆盖率 17%" in caplog.text
    assert "status=success 但零帖不能视为健康成功" in caplog.text


def test_health_gate_warn_when_posts_coverage_below_50_percent():
    results = [_result(f"OK{i}", 1) for i in range(25)]
    results += [_result(f"ZERO{i}", 0) for i in range(75)]

    health = _evaluate_crawl_health(results)

    assert health["status"] == "warn"
    assert health["posts_coverage"] == 0.25


def test_health_gate_healthy_when_at_least_half_have_posts():
    results = [_result(f"OK{i}", 1) for i in range(30)]
    results += [_result(f"ZERO{i}", 0) for i in range(30)]

    health = _evaluate_crawl_health(results)

    assert health["status"] == "healthy"
    assert health["posts_coverage"] == 0.5
