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

    def test_retry_still_empty_keeps_notices_as_partial(self, monkeypatch):
        """重试仍空 → **不能**落 failed，要落 partial 并保留已抓到的公告。

        原先这里是「落 failed, posts_count=0」并当作正确行为 —— 但 cli.py 的状态门
        只放行 success/partial，failed 会让这批公告**整批不入库、不产生告警**，
        等于把已经拿到的数据丢掉。见 issue #76。
        """
        fake = _fake_opencli_module([[], []], [[_NOTICE_ITEM]])
        sleeps: list = []
        _install(monkeypatch, fake, sleeps)

        result = crawler_mod.crawl_single_stock("300866.SZ", timeout=30, db_path=None)

        assert result["status"] == "partial", "公告已拿到，不能按 failed 丢掉"
        assert result["posts_count"] == 0
        assert len(result["announcements"]) == 1
        assert result["announcements"][0]["title"] == _NOTICE_ITEM["title"]
        assert result["announcements"][0]["link"] == _NOTICE_ITEM["url"]
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
        """重试调用本身抛异常 → 吃掉，不影响其他股票；公告仍按 partial 保留。

        原断言是 `failed`，但那样 cli 的状态门会把**已经拿到的公告**一起丢掉
        （见 issue #76）。异常被吃掉、流程不崩 —— 这才是本用例要钉的东西。
        """
        fake = _fake_opencli_module([[]], [[_NOTICE_ITEM]])
        sleeps: list = []
        _install(monkeypatch, fake, sleeps)

        def _boom(code, limit=100):
            if fake.fetch_discussions.call_count >= 2:
                raise RuntimeError("opencli bridge died")
            return []

        fake.fetch_discussions = MagicMock(side_effect=_boom)

        result = crawler_mod.crawl_single_stock("300866.SZ", timeout=30, db_path=None)

        assert result["status"] == "partial"
        assert result["announcements"], "公告已在手，不能按 failed 丢"
        assert sleeps == [30]


# ═══════════════════════════════════════════════════════════════
# 预取阶段的 opencli 预算
# ═══════════════════════════════════════════════════════════════

class _FakeBudget:
    """预算替身：第 `free_checks` 次 `expired()` 之前返回 False，之后恒 True。

    这个 harness 会把 `time.sleep` 全局打桩，没法「真的睡到预算耗尽」，所以直接替换
    协作者（真实类由 tests/test_opencli_budget.py 覆盖）。
    """

    free_checks = 0

    def __init__(self, *_args, **_kwargs):
        self.seconds = 0.01
        self._checks = 0

    def limited(self):
        return True

    def expired(self):
        self._checks += 1
        return self._checks > self.free_checks

    def spent(self):
        return 999.0

    def remaining(self):
        return 0.0


class _AlwaysExpiredBudget(_FakeBudget):
    """一上来就超时：用来验证「公告被跳过」。"""

    free_checks = 0


class _ExpiredAfterFirstCheck(_FakeBudget):
    """第一次检查放行、之后超时 —— 复现真实形态。

    预取里有**两处**守卫：公告前、限流重试前。若替身一上来就超时，公告会被跳过 →
    公告为空 → 「讨论空但公告正常」这个前提不成立 → 重试分支天然不走，于是
    「重试守卫真的会跳过」这件事**根本没被测到**（review 指出的假覆盖）。
    让第一次检查放行，公告就抓到了，第二次检查才超时 —— 这才是要覆盖的分支。
    """

    free_checks = 1


class TestPrefetchBudget:
    """预取阶段（讨论 + 公告 + 限流重试）受 per-stock 预算约束。

    不设上界时这几步最坏叠起来 ≈390s（单次最坏 87s），而 crawler.timeout_seconds 管不到
    opencli 预取 —— 一组 8 只串行时会顶穿 1 小时的 cron 窗口、与下一组重叠。
    """

    def test_notices_are_skipped_when_budget_exhausted(self, monkeypatch):
        fake = _fake_opencli_module([[], [_DISC_ITEM]], [[_NOTICE_ITEM]])
        sleeps: list = []
        _install(monkeypatch, fake, sleeps)
        monkeypatch.setattr(crawler_mod, "OpencliBudget", _AlwaysExpiredBudget)

        crawler_mod.crawl_single_stock(
            "300866.SZ", timeout=30, db_path=None, opencli_budget_seconds=0.01
        )

        assert fake.fetch_notices.call_count == 0, "预算已用尽仍去抓了公告"
        assert fake.fetch_discussions.call_count == 1, "预算是「跳过剩下的」，不是一开始就不抓"
        assert sleeps == []

    def test_retry_is_skipped_when_budget_expires_after_notices(self, monkeypatch):
        """公告抓到了、随后预算用尽 → 不做那个 30s sleep + 重试（这才是最费时间的一步）。"""
        fake = _fake_opencli_module([[], [_DISC_ITEM]], [[_NOTICE_ITEM]])
        sleeps: list = []
        _install(monkeypatch, fake, sleeps)
        monkeypatch.setattr(crawler_mod, "OpencliBudget", _ExpiredAfterFirstCheck)

        crawler_mod.crawl_single_stock(
            "300866.SZ", timeout=30, db_path=None, opencli_budget_seconds=0.01
        )

        assert fake.fetch_notices.call_count == 1, "第一次检查应放行，公告要抓到"
        assert sleeps == [], "预算用尽后不该再 sleep 30s 去重试"
        assert fake.fetch_discussions.call_count == 1, "重试那次讨论调用不该发生"

    def test_budget_disabled_keeps_previous_behaviour(self, monkeypatch):
        """`opencli_budget_seconds=0` 表示不限 → 公告照抓、重试照做（旧行为）。"""
        fake = _fake_opencli_module([[], [_DISC_ITEM]], [[_NOTICE_ITEM]])
        sleeps: list = []
        _install(monkeypatch, fake, sleeps)

        result = crawler_mod.crawl_single_stock(
            "300866.SZ", timeout=30, db_path=None, opencli_budget_seconds=0
        )

        assert fake.fetch_notices.call_count == 1, "不限预算时公告必须照抓"
        assert sleeps == [30], "不限预算时该重试仍要重试"
        assert result["posts_count"] == 1


class TestCooldownSkipsPlaywright:
    """熔断冷却期**不**回退 Playwright。

    那条通道不经插件钩子、**不限速**；站点正因为被打才开的冷却，回退回去等于继续打它。
    `_install` 里的 `_crawl_with_retry` 一旦被调用就抛断言 —— 所以这条既能证明「没回退」，
    也会在守卫被挖掉时变红（先前 monitor 侧这半个改动 538 个用例一个都盖不到）。
    """

    def test_cooldown_skips_playwright(self, monkeypatch):
        fake = _fake_opencli_module([[]], [[]])  # opencli 空手而归
        _install(monkeypatch, fake, [])
        monkeypatch.setattr(crawler_mod, "_in_cooldown", lambda: True)

        result = crawler_mod.crawl_single_stock("300866.SZ", timeout=30, db_path=None)

        assert result["diagnostic"]["error_type"] == "cooldown"
        assert "熔断冷却" in (result["error"] or ""), "原因不能被藏成 None"

    def test_no_cooldown_keeps_the_fallback(self, monkeypatch):
        """没冷却时行为不变 —— 这条判断只该在熔断期生效。

        （`_crawl_with_retry` 会抛断言，所以这里只断言「它被调用了」。）
        """
        sleeps = []
        fake = _fake_opencli_module([[]], [[]])
        _install(monkeypatch, fake, sleeps)
        monkeypatch.setattr(crawler_mod, "_in_cooldown", lambda: False)
        called = []
        monkeypatch.setattr(
            crawler_mod,
            "_crawl_with_retry",
            lambda *a, **k: called.append(1) or {"result": None, "diagnostic": {}},
        )

        crawler_mod.crawl_single_stock("300866.SZ", timeout=30, db_path=None)

        assert called, "没冷却时该照旧回退"
