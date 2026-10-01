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

# ─────────────────────────────────────────────────────────────
# 资讯维度（2026-10-01 事故回归）
#
# 事故形态：资讯整源失效（被风控验证页挡住 / 取数静默失败），
# 但 posts_count = 讨论+公告+资讯 是聚合计数，被讨论量稀释后仍 > 0，
# 于是 health 报 healthy、日报照发 —— 失效完全不可见。
# 因此健康判定必须把资讯当成独立维度，按「错误率」判定。
# ─────────────────────────────────────────────────────────────


def _result_with_news(code: str, news_status: str, posts_count: int = 50) -> dict:
    return {
        "stock_code": code,
        "status": "success",
        "posts_count": posts_count,
        "news_status": news_status,
        "diagnostic": {},
    }


def test_news_total_failure_degrades_health_even_when_posts_are_healthy():
    """讨论全部正常、资讯全部异常 → 必须 degraded（这就是被稀释的情形）。"""
    results = [_result_with_news(f"S{i}", "error") for i in range(10)]

    health = _evaluate_crawl_health(results)

    assert health["posts_coverage"] == 1.0, "前提：讨论覆盖是满的"
    assert health["news_errors"] == 10
    assert health["news_error_rate"] == 1.0
    assert health["status"] == "degraded"
    assert health["news_error_codes"] == [f"S{i}" for i in range(10)]


def test_news_partial_failure_downgrades_healthy_to_warn():
    """资讯错误率 30% → 从 healthy 降为 warn。"""
    results = [_result_with_news(f"A{i}", "ok") for i in range(7)]
    results += [_result_with_news(f"B{i}", "error") for i in range(3)]

    health = _evaluate_crawl_health(results)

    assert health["posts_coverage"] == 1.0
    assert abs(health["news_error_rate"] - 0.3) < 1e-9
    assert health["status"] == "warn"


def test_news_all_ok_keeps_healthy():
    """资讯正常时不应改变原有判定（回归保护）。"""
    results = [_result_with_news(f"C{i}", "ok") for i in range(10)]

    health = _evaluate_crawl_health(results)

    assert health["status"] == "healthy"
    assert health["news_coverage"] == 1.0
    assert health["news_errors"] == 0


def test_news_empty_is_not_error():
    """干净返回空（真的没有资讯）不算异常，不应拉低健康状态。"""
    results = [_result_with_news(f"D{i}", "empty") for i in range(10)]

    health = _evaluate_crawl_health(results)

    assert health["news_errors"] == 0
    assert health["status"] == "healthy"


def test_news_dimension_absent_keeps_legacy_behaviour():
    """没有 news_status 字段时（旧调用方 / 未启用 fetch_news）按原逻辑判定。"""
    results = [_result(f"E{i}", 1) for i in range(10)]

    health = _evaluate_crawl_health(results)

    assert health["news_tracked"] == 0
    assert health["status"] == "healthy"
