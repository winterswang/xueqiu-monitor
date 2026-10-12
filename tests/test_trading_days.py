"""交易日历采样 — 审计 2026-10-09 休市日感知 (方案 C: longbridge) 回归."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

import pytest

from src import trading_days as td
from src.models import SentimentStat


@pytest.fixture(autouse=True)
def _clear_cache():
    td._cache.clear()
    yield
    td._cache.clear()


def _stat(stat_date: str, mean: float = 0.1) -> SentimentStat:
    epoch = int(
        datetime.fromisoformat(stat_date).replace(tzinfo=timezone.utc).timestamp()
    )
    return SentimentStat(stock_code="X", stat_date=epoch, sentiment_mean=mean)


def test_market_of():
    assert td.market_of("LULU.US") == "US"
    assert td.market_of("0700.HK") == "HK"
    assert td.market_of("600519.SH") == "CN"
    assert td.market_of("SH600519") == "CN"
    assert td.market_of("unknown") == "CN"


def test_get_trading_days_parses_and_caches(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout=json.dumps({"trading_days": ["2026-10-08", "2026-10-09"],
                               "half_trading_days": []}),
            stderr="",
        )

    monkeypatch.setattr(td.subprocess, "run", fake_run)
    first = td.get_trading_days("CN", "2026-10-01", "2026-10-12")
    second = td.get_trading_days("CN", "2026-10-01", "2026-10-12")
    assert first == {"2026-10-08", "2026-10-09"}
    assert second is first
    assert len(calls) == 1, "同 key 必须命中进程内缓存"


def test_get_trading_days_failure_returns_none(monkeypatch):
    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 20)

    monkeypatch.setattr(td.subprocess, "run", boom)
    assert td.get_trading_days("CN", "2026-10-01", "2026-10-12") is None


def test_filter_keeps_trading_days_and_caps_window(monkeypatch):
    trading = {"2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"}
    monkeypatch.setattr(td, "get_trading_days", lambda m, s, e: trading)
    stats = [
        _stat("2026-10-05"),  # 假期 (国庆) → 剔除
        _stat("2026-10-06"),
        _stat("2026-10-07"),
        _stat("2026-10-08"),
        _stat("2026-10-09"),
    ]
    kept = td.filter_trading_day_stats(stats, "CN", 14)
    assert [s.stat_date for s in kept] == [
        _stat(d).stat_date for d in
        ["2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]
    ]


def test_filter_us_aligns_prev_beijing_day(monkeypatch):
    """US: 北京 B 日观测对齐美东 B-1 或 B — 周末无交易 → 剔除."""
    # 美东交易日: 10-06~09 (周二~周五) + 10-12/13 (周一/周二)
    trading = {"2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09",
               "2026-10-12", "2026-10-13"}
    monkeypatch.setattr(td, "get_trading_days", lambda m, s, e: trading)
    stats = [
        _stat("2026-10-10"),  # 北京周六, 美东周五 (B-1) → 保留
        _stat("2026-10-11"),  # 北京周日, 美东周六/周日 → 剔除
        _stat("2026-10-12"),  # 北京周一, 美东周一 (B) → 保留
        _stat("2026-10-13"),  # 北京周二, 美东周一 (B-1) → 保留
    ]
    kept = td.filter_trading_day_stats(stats, "US", 14)
    assert len(kept) == 3


def test_filter_returns_none_for_fallback(monkeypatch):
    monkeypatch.setattr(td, "get_trading_days", lambda m, s, e: None)
    assert td.filter_trading_day_stats([_stat("2026-10-08")], "CN", 14) is None


def test_filter_empty_stats_returns_none():
    assert td.filter_trading_day_stats([], "CN", 14) is None


def test_stat_date_is_read_as_local_midnight(monkeypatch):
    """`stat_date` 是**本地**自然日零点；按 UTC 解释会整错一天（issue #77）。

    构造一个「本地日 D 是交易日、D-1 不是」的日历。若读取端仍按 UTC 解释，
    本地午夜（= D-1 16:00 UTC）会被算成 D-1 → 这个观测被丢掉 → 用例红。

    这是 `8a857ba` 把写入端改成本地午夜后，读取端没跟着改的那个缺口：
    写本地、读 UTC，两边差一格。
    """
    trading_day = "2026-10-08"      # 本地交易日
    # 前一日本地休市 —— 所以"被误算成前一天"会立刻表现为丢观测
    monkeypatch.setattr(td, "get_trading_days", lambda *a, **k: [trading_day])

    # 本地午夜（naive → 宿主本地时区）
    epoch = int(datetime.fromisoformat(trading_day).timestamp())
    stat = SentimentStat(stock_code="600519.SH", stat_date=epoch, sentiment_mean=0.1)

    kept = td.filter_trading_day_stats([stat], "CN", 14)

    assert kept == [stat], "本地午夜的 stat_date 必须被解析为当天，而不是前一天"
