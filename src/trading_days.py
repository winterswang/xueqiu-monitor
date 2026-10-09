"""交易日历与按交易日采样 (审计 2026-10-09 休市日感知, 方案 C: longbridge).

Z 基线此前按日历日取 14 天, 假期 (国庆/春节/美股感恩节等) 的 0 样本日
稀释基线, 节后首日的补涨被误判为突变。现按市场取真实交易日采样:

- 数据源: longbridge CLI (trading days), 与财报日历同源
- 缓存: 进程内按 (market, start, end) 只取一次 — 一轮 pipeline 复用
- 失败即回退: longbridge 不可用/超时/解析失败 → None → 调用方退回
  日历日窗口 (旧行为), 绝不因日历源故障阻塞 pipeline

北京爬取日与美东交易日的对齐规则 (US 市场):
  北京 B 日爬到的是美东 B-1 (隔夜) 或 B (北京时间晚间=美东早盘) 的讨论,
  故 B 为「交易日观测」当且仅当 B-1 或 B 是美东交易日。CN/HK 市场无时差,
  直接用 B 本身。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

LB_BIN = os.path.expanduser("~/.cargo/bin/longbridge")

_cache: dict[tuple[str, str, str], Optional[set[str]]] = {}


def market_of(stock_code: str) -> str:
    """从股票代码推市场: 后缀优先 (LULU.US/700.HK/600519.SH), 无后缀默认 CN."""
    code = stock_code.strip().upper()
    if code.endswith(".US"):
        return "US"
    if code.endswith(".HK"):
        return "HK"
    if code.endswith((".SH", ".SZ")) or code.startswith(("SH", "SZ")):
        return "CN"
    return "CN"


def get_trading_days(market: str, start: str, end: str) -> Optional[set[str]]:
    """取 [start, end] 内 market 的交易日集合 (ISO 日期字符串).

    Returns: {"YYYY-MM-DD", ...}; 数据不可用时 None (调用方回退旧行为).
    """
    mkt = market.upper()
    if mkt not in ("CN", "HK", "US"):
        mkt = "CN"
    key = (mkt, start, end)
    if key in _cache:
        return _cache[key]
    days: Optional[set[str]] = None
    try:
        result = subprocess.run(
            [LB_BIN, "trading", "days", mkt,
             "--start", start, "--end", end, "--format", "json"],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            parsed = {d for d in (data.get("trading_days") or [])}
            if parsed:
                days = parsed
    except Exception as e:  # noqa: BLE001 — 日历源故障必须回退而非阻塞
        logger.warning("trading days 获取失败 (%s %s): %s", mkt, start, e)
    _cache[key] = days
    return days


def filter_trading_day_stats(
    stats: Sequence,
    market: str,
    window_days: int,
    today: Optional[date] = None,
) -> Optional[list]:
    """按交易日采样基线窗口; 交易日数据不可用 → None (调用方回退).

    stats: SentimentStat 列表 (须按 stat_date 升序或任意序, 内部自排序),
    stat_date 为 UTC 当日零点 epoch (即北京日历日). 取最后一个 window_days
    条交易日观测。
    """
    if not stats:
        return None
    now = today or datetime.now(timezone.utc).date()
    start_iso = (now - timedelta(days=window_days * 2 + 10)).isoformat()
    end_iso = now.isoformat()
    trading = get_trading_days(market, start_iso, end_iso)
    if trading is None:
        return None

    def _is_observation(day: date) -> bool:
        iso = day.isoformat()
        if market in ("CN", "HK"):
            return iso in trading
        # US: 北京 B 日的观测对齐美东 B-1 或 B 的交易时段
        return (day - timedelta(days=1)).isoformat() in trading or iso in trading

    kept = [
        s for s in stats
        if _is_observation(
            datetime.fromtimestamp(s.stat_date, tz=timezone.utc).date()
        )
    ]
    return kept[-window_days:]
