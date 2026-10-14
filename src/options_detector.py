"""options-sense (方向 8): 期权信号检测 (L1 聚合层)。

复用 sentiment 侧的 Z-score 思路, 但口径独立:
- 冷启动 30 天 (IV 分位需 60 天) — 只积累不告警
- PCR-OI 突变 (put 侧) = 对冲盘异动; PCR-Vol / 总量突增 = 事件博弈
所有告警是环境参考, 不是买卖指令 (推送文案由 report/notifier 侧统一带后缀)。
"""

from __future__ import annotations

import logging
import time

import numpy as np

from . import db
from .detector import compute_z_score
from .models import ChangeAlert

logger = logging.getLogger(__name__)

COLD_START_DAYS = 30
IV_PERCENTILE_MIN_DAYS = 60
Z_WINDOW = 60


def _dedup_today(alerts: list[ChangeAlert], existing: list[ChangeAlert]) -> list[ChangeAlert]:
    """同一股票同一类型当日只留一条 (震荡期防刷屏, 计划 §4.5)。"""
    today_start = time.time() - (time.time() % 86400)
    seen = {
        (a.stock_code, a.alert_type)
        for a in existing if a.alert_time >= today_start
    }
    out, seen_new = [], set()
    for a in alerts:
        key = (a.stock_code, a.alert_type)
        if key in seen or key in seen_new:
            continue
        seen_new.add(key)
        out.append(a)
    return out


def detect_options_signals(
    db_path: str,
    stock_code: str,
    z_window: int = Z_WINDOW,
    cold_start_days: int = COLD_START_DAYS,
) -> list[ChangeAlert]:
    """L1 聚合信号: PCR-OI 突变 / 总量异动。冷启动期返回 []。"""
    hist = db.get_option_daily_history(db_path, stock_code, limit=z_window + 1)
    if len(hist) < cold_start_days:
        logger.debug(f"[options] {stock_code} 冷启动 {len(hist)}/{cold_start_days} 天, 不告警")
        return []

    # 当日可能双源两行: 取 stat_date 最大的那行, 源优先级 cboe > longbridge
    latest_date = max(h.stat_date for h in hist)
    todays = [h for h in hist if h.stat_date == latest_date]
    prio = {"cboe": 0, "longbridge": 1, "hkex": 2}
    curr = min(todays, key=lambda h: prio.get(h.source, 9))
    history = [h for h in hist if h.stat_date < latest_date][-z_window:]

    alerts: list[ChangeAlert] = []
    z_pcr = z_vol = None

    if curr.pcr_oi is not None:
        hist_pcr = [h.pcr_oi for h in history if h.pcr_oi is not None]
        z_pcr = compute_z_score(curr.pcr_oi, hist_pcr)
        if z_pcr >= 3.0:
            alerts.append(ChangeAlert(
                stock_code=stock_code,
                alert_type="options_pcr_spike",
                z_score=round(z_pcr, 2),
                magnitude=round(curr.pcr_oi - float(np.mean(hist_pcr)), 4),
                priority="P0",
                detail={
                    "signal": "put 对冲仓位异动 (PCR-OI 突增)",
                    "pcr_oi": curr.pcr_oi,
                    "pcr_mean": round(float(np.mean(hist_pcr)), 4),
                    "source": curr.source,
                    "note": "环境参考, 不构成操作建议",
                },
            ))
        elif z_pcr >= 2.0:
            alerts.append(ChangeAlert(
                stock_code=stock_code,
                alert_type="options_pcr_spike",
                z_score=round(z_pcr, 2),
                magnitude=round(curr.pcr_oi - float(np.mean(hist_pcr)), 4),
                priority="P1",
                detail={
                    "signal": "PCR-OI 偏离历史区间",
                    "pcr_oi": curr.pcr_oi,
                    "pcr_mean": round(float(np.mean(hist_pcr)), 4),
                    "source": curr.source,
                    "note": "环境参考, 不构成操作建议",
                },
            ))
        elif z_pcr <= -2.0:
            alerts.append(ChangeAlert(
                stock_code=stock_code,
                alert_type="options_pcr_call_surge",
                z_score=round(z_pcr, 2),
                magnitude=round(curr.pcr_oi - float(np.mean(hist_pcr)), 4),
                priority="P1",
                detail={
                    "signal": "call 侧仓位/成交显著占优 (PCR 骤降)",
                    "pcr_oi": curr.pcr_oi,
                    "pcr_mean": round(float(np.mean(hist_pcr)), 4),
                    "source": curr.source,
                    "note": "环境参考, 不构成操作建议",
                },
            ))

    if curr.call_volume is not None and curr.put_volume is not None:
        total_vol = curr.call_volume + curr.put_volume
        hist_vol = [h.call_volume + h.put_volume for h in history
                    if h.call_volume is not None and h.put_volume is not None]
        z_vol = compute_z_score(total_vol, hist_vol)
        if z_vol >= 3.0:
            alerts.append(ChangeAlert(
                stock_code=stock_code,
                alert_type="options_volume_spike",
                z_score=round(z_vol, 2),
                magnitude=float(total_vol - np.mean(hist_vol)),
                priority="P1",
                detail={
                    "signal": "期权总成交量异动 — 有资金为某事下注 (方向看同日 PCR)",
                    "total_volume": total_vol,
                    "pcr_volume": curr.pcr_volume,
                    "source": curr.source,
                    "note": "环境参考, 不构成操作建议",
                },
            ))

    iv_pct = None
    iv_hist = [h.atm_iv for h in hist if h.atm_iv is not None]
    if curr.atm_iv is not None and len(iv_hist) >= IV_PERCENTILE_MIN_DAYS:
        iv_pct = round(
            sum(1 for v in iv_hist if v <= curr.atm_iv) / len(iv_hist) * 100, 1
        )

    db.update_option_daily_stats(
        db_path, curr.id, z_pcr_oi=z_pcr, z_volume=z_vol, iv_percentile=iv_pct
    )
    return _dedup_today(alerts, db.get_today_alerts(db_path))
