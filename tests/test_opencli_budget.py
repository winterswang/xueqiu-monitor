"""`OpencliBudget` 的行为测试。

它是「一只股票的 opencli 时间上界」的载体：不设上界时，预取（2 次调用 + 一次 30s 重试）
与回复正文（最多 replies_per_stock 次）叠起来能吃掉十几分钟，而 `crawler.timeout_seconds`
**管不到**这些调用（那个超时只包住 Playwright 回退）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from opencli_budget import (  # noqa: E402
    DEFAULT_OPENCLI_BUDGET_SECONDS,
    OpencliBudget,
)


def test_default_budget_can_fit_one_long_pause():
    """默认预算要能容下**一次**长停（插件层最坏 87s），否则一遇长停就把整段跳光。

    长停 = 取锁 2s + 45–75s 长停 + 执行 ≈ 87s（见 xueqiu-crawler 的
    opencli-adapters/lib/ratelimit.js 与 opencli_extractor.py 的注释）。
    """
    assert DEFAULT_OPENCLI_BUDGET_SECONDS >= 90, (
        "默认预算低于一次长停的最坏等待，会把正常的长停误判成超预算"
    )


def test_limited_budget_expires_and_reports_remaining():
    budget = OpencliBudget(0.02)
    assert budget.limited is True
    assert budget.expired() is False
    assert budget.remaining() is not None and budget.remaining() > 0

    time.sleep(0.03)

    assert budget.expired() is True
    assert budget.remaining() == 0.0, "超时后剩余应为 0，不能是负数"
    assert budget.spent() >= 0.02


def test_zero_or_negative_means_unlimited():
    """`<= 0` 表示不限（保留旧行为，便于回滚）。"""
    for value in (0, -1, None):
        budget = OpencliBudget(value)
        assert budget.limited is False, f"{value!r} 应表示不限"
        assert budget.expired() is False, f"{value!r} 不该判成超预算"
        assert budget.remaining() is None


def test_unlimited_budget_never_expires_even_after_waiting():
    budget = OpencliBudget(0)
    time.sleep(0.01)
    assert budget.expired() is False
