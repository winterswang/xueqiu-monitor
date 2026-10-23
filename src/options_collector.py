"""options-sense (方向 8): 美股期权日频采集器。

数据源 (2026-10-15 实测, 见 plans/options-sense/PLAN.md §2):
- CBOE 公开延迟快照 (主通道): 单请求整条链, 每合约 bid/ask/volume/OI/iv/Greeks
- Longbridge CLI option volume daily: 20 天滚动 PCR, 首日回填 + 第二源交叉校验

CBOE 快照只有当日, 历史必须每天落库自建; Longbridge 窗口滚动同理。
采集失败必须显式报错, 不得用旧数据充数 (用户 2026-10-13 明确要求)。
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import db
from .db import local_day_start
from .models import OptionDaily

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{}.json"

# daily_matrix.py 踩坑沉淀的三道报价过滤闸 (防占位价/空挂价污染指标):
# 收租率 ATM call 的最短到期天数: 排除 0DTE/隔日链的衰减噪声, 让年化跨股可比
MIN_YIELD_DTE = 5

_MIN_BID = 0.0
_MIN_ASK = 0.03
_MAX_SPREAD_RATIO = 0.60


def _et_now() -> datetime:
    return datetime.now(ET)


def _us_stat_date(now: datetime | None = None) -> int:
    """美股交易日 → 统一日键。

    日键 = (ET 交易日 + 1 天) 的北京午夜。CBOE 快照在 ET 收盘后采集
    (北京上午), 它覆盖的交易日 T 落在北京日历 T+1; Longbridge 日频
    stats 的 timestamp 键在 ET 交易日 T (北京午夜或 UTC 午夜, 两者经
    +1 天归一后等价)。统一后双源同键, 交叉校验才不会静默比错日期。
    """
    et = now or _et_now()
    trade_date = et.date() if et.hour >= 16 else et.date() - timedelta(days=1)
    anchor = datetime(trade_date.year, trade_date.month, trade_date.day,
                      16, tzinfo=ET)  # ET 16:00 = 北京次日凌晨 4/5 点
    return local_day_start(anchor.timestamp() + 12 * 3600)


def _parse_cboe_symbol(sym: str, root: str) -> tuple[str, str, float] | None:
    """PDD261016C00080000 -> (2026-10-16, 'C', 80.0)。root 前缀剥离后定长解析。"""
    body = sym[len(root):]
    m = re.fullmatch(r"(\d{6})([CP])(\d{8})", body)
    if not m:
        return None
    ymd, cp, strike = m.groups()
    try:
        expiry = datetime.strptime(ymd, "%y%m%d").date().isoformat()
    except ValueError:
        return None
    return expiry, cp, int(strike) / 1000.0


def fetch_cboe(symbol: str, timeout: int = 30, retries: int = 2) -> dict:
    """拉取 CBOE 延迟快照。307 重定向由 HTTPRedirectHandler 跟随。"""
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                CBOE_URL.format(symbol),
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:  # noqa: BLE001 — 网络/JSON 错误统一重试
            last_err = e
            if attempt < retries:
                time.sleep(3)
    raise RuntimeError(f"CBOE {symbol} 拉取失败: {last_err}") from last_err


def is_quotable(opt: dict) -> bool:
    """三道过滤闸 (来源: ~/pdd-options/daily_matrix.py 踩坑沉淀)。"""
    bid = opt.get("bid") or 0
    ask = opt.get("ask") or 0
    if bid <= _MIN_BID or ask < _MIN_ASK:
        return False
    mid = (ask + bid) / 2
    return mid > 0 and (ask - bid) / mid <= _MAX_SPREAD_RATIO


def _max_pain(calls: list[dict], puts: list[dict]) -> float | None:
    """到期时全市场期权内在价值总和最小的行权价。"""
    oi_rows = [(c["strike"], c.get("oi") or 0, "C") for c in calls]
    oi_rows += [(p["strike"], p.get("oi") or 0, "P") for p in puts]
    oi_rows = [(k, oi, cp) for k, oi, cp in oi_rows if oi > 0]
    if len(oi_rows) < 3:
        return None
    strikes = sorted({k for k, _, _ in oi_rows})
    best, best_pain = None, None
    for s in strikes:
        pain = sum(oi * max(0.0, s - k) if cp == "C" else oi * max(0.0, k - s)
                   for k, oi, cp in oi_rows)
        if best_pain is None or pain < best_pain:
            best, best_pain = s, pain
    return best


def aggregate_chain(raw: dict, symbol: str) -> dict | None:
    """CBOE 快照 → 聚合指标 + 合约行。无未到期合约时返回 None。"""
    data = raw.get("data") or {}
    spot = float(data.get("current_price") or 0)
    if spot <= 0:
        return None
    today_et = _et_now().date()

    calls: list[dict] = []
    puts: list[dict] = []
    contract_rows: list[dict] = []
    contracts_seen = 0
    for opt in data.get("options") or []:
        parsed = _parse_cboe_symbol(opt.get("option", ""), symbol)
        if parsed is None:
            continue
        expiry, cp, strike = parsed
        if datetime.fromisoformat(expiry).date() < today_et:
            continue
        row = {
            "expiry": expiry,
            "strike": strike,
            "cp": cp,
            "bid": float(opt.get("bid") or 0),
            "ask": float(opt.get("ask") or 0),
            "volume": int(opt.get("volume") or 0),
            "oi": int(opt.get("open_interest") or 0),
            "iv": float(opt["iv"]) if opt.get("iv") else None,
            "delta": _f(opt.get("delta")),
            "gamma": _f(opt.get("gamma")),
            "theta": _f(opt.get("theta")),
            "vega": _f(opt.get("vega")),
        }
        contracts_seen += 1
        if is_quotable(opt):
            (calls if cp == "C" else puts).append(row)
            mid = (row["bid"] + row["ask"]) / 2
            contract_rows.append({
                "contract_id": opt["option"],
                "expiry": expiry,
                "strike": strike,
                "cp": cp,
                "last": _f(opt.get("last_trade_price")),
                "bid": row["bid"],
                "ask": row["ask"],
                "volume": row["volume"],
                "oi": row["oi"],
                "iv": row["iv"],
                "delta": row["delta"],
                "gamma": row["gamma"],
                "theta": row["theta"],
                "vega": row["vega"],
                "premium_pct": round(mid / spot * 100, 4) if mid > 0 else None,
                "moneyness": round((strike / spot - 1) * 100, 4),
                "option_currency": "USD",
            })

    if not calls and not puts:
        return None

    call_vol = sum(c["volume"] for c in calls)
    put_vol = sum(p["volume"] for p in puts)
    call_oi = sum(c["oi"] for c in calls)
    put_oi = sum(p["oi"] for p in puts)

    # 期限结构统计使用同一条 OI 最大到期链，避免跨到期混算。
    near = [r for r in contract_rows
            if abs(r["moneyness"]) <= 20 and (r["oi"] or 0) > 0]
    by_expiry: dict[str, list[dict]] = {}
    for r in near:
        by_expiry.setdefault(r["expiry"], []).append(r)
    chain_expiry = max(by_expiry, key=lambda e: sum(r["oi"] for r in by_expiry[e])) if by_expiry else None
    chain_rows = by_expiry.get(chain_expiry, [])
    wall_call = _max_oi_strike(chain_rows, "C")
    wall_put = _max_oi_strike(chain_rows, "P")
    mp = _max_pain([r for r in chain_rows if r["cp"] == "C"],
                   [r for r in chain_rows if r["cp"] == "P"])

    # ATM call 权利金率年化: 最近到期里离现价最近的过闸 call, 按实际 DTE 折算
    # (premium_pct / DTE × 365)。必须限定到期 —— 不限定会把远期 LEAPS 当基数。
    # 旧算法 ×12 隐含「当月合约」: 同样的权利金, 周期权被低估 ~30 倍、45 天合约
    # 被高估 1.5 倍 (2026-10-18 修)。DTE 基准 = ET 交易日; dte 随值落库供报告标注。
    # 另排除当日到期 (0DTE) 链并要求 dte >= MIN_YIELD_DTE: 收盘后的 0DTE 权利金是
    # 衰减噪声不是可收的租 —— 2026-10-17 PDD 的「76%」就是 ×12 乘在 0DTE 上的产物。
    # 排除当日到期 (0DTE) 链: 收盘后它的权利金是衰减噪声, 不是可收的租 ——
    # 2026-10-17 PDD 的「76%」就是 ×12 乘在 0DTE 权利金上的产物。
    pool = []
    for r in contract_rows:
        if r["cp"] != "C" or not r["iv"] or r["expiry"] <= today_et.isoformat():
            continue
        d = (datetime.fromisoformat(r["expiry"]).date() - today_et).days
        if d >= MIN_YIELD_DTE:
            pool.append((r, d))
    premium_yield = None
    premium_yield_dte = None
    if pool:
        front = min(d for _, d in pool)
        atm, dte = min(
            (rd for rd in pool if rd[1] == front),
            key=lambda rd: abs(rd[0]["strike"] / spot - 1),
        )
        if atm["premium_pct"] and atm["premium_pct"] > 0:
            premium_yield = round(atm["premium_pct"] / dte * 365, 3)
            premium_yield_dte = dte

    iv30 = _f(data.get("iv30"))
    return {
        "spot": spot,
        "stat_date": _us_stat_date(),
        "call_volume": call_vol,
        "put_volume": put_vol,
        "call_oi": call_oi,
        "put_oi": put_oi,
        "pcr_volume": round(put_vol / call_vol, 4) if call_vol else None,
        "pcr_oi": round(put_oi / call_oi, 4) if call_oi else None,
        "atm_iv": iv30,
        "premium_yield": premium_yield,
        "premium_yield_dte": premium_yield_dte,
        "max_pain": mp,
        "chain_expiry": chain_expiry,
        "option_currency": "USD",
        "underlying_currency": "USD",
        "contracts_seen": contracts_seen,
        "contracts_valid": len(contract_rows),
        "quality_rule": "bid>0;ask>=0.03;spread/mid<=0.60",
        "wall_call": wall_call,
        "wall_put": wall_put,
        "contracts": contract_rows,
    }


def _max_oi_strike(rows: list[dict], cp: str) -> float | None:
    oi_by_strike: dict[float, int] = {}
    for r in rows:
        if r["cp"] != cp:
            continue
        oi_by_strike[r["strike"]] = oi_by_strike.get(r["strike"], 0) + (r["oi"] or 0)
    if not oi_by_strike:
        return None
    return max(oi_by_strike, key=lambda k: oi_by_strike[k])


def _f(v) -> float | None:
    try:
        f = float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
    return f


def collect_us_symbol(db_path: str, symbol: str) -> dict:
    """单只美股: CBOE 快照 → 落库。返回摘要 dict 供日志/汇报。"""
    raw = fetch_cboe(symbol)
    agg = aggregate_chain(raw, symbol)
    if agg is None:
        raise RuntimeError(f"{symbol} CBOE 快照无未到期合约")
    code = f"{symbol}.US"
    contracts = [{**row, "stock_code": code, "snap_date": agg["stat_date"]}
                 for row in agg["contracts"]]
    db.insert_option_contracts(db_path, contracts)
    stat = OptionDaily(
        stock_code=code,
        stat_date=agg["stat_date"],
        call_volume=agg["call_volume"],
        put_volume=agg["put_volume"],
        call_oi=agg["call_oi"],
        put_oi=agg["put_oi"],
        pcr_volume=agg["pcr_volume"],
        pcr_oi=agg["pcr_oi"],
        atm_iv=agg["atm_iv"],
        premium_yield=agg["premium_yield"],
        premium_yield_dte=agg.get("premium_yield_dte"),
        max_pain=agg["max_pain"],
        chain_expiry=agg.get("chain_expiry"),
        wall_call=agg["wall_call"],
        wall_put=agg["wall_put"],
        source="cboe",
        option_currency=agg["option_currency"],
        underlying_currency=agg["underlying_currency"],
        contracts_seen=agg["contracts_seen"],
        contracts_valid=agg["contracts_valid"],
        quality_rule=agg["quality_rule"],
    )
    db.upsert_option_daily(db_path, stat)
    return {k: v for k, v in agg.items() if k != "contracts"} | {
        "n_contracts": len(contracts), "symbol": symbol,
    }


def backfill_longbridge(db_path: str, symbol: str, cli: str = "longbridge") -> int:
    """Longbridge 20 天 PCR 回填 (INSERT OR IGNORE, 只补缺)。返回新增行数。"""
    r = subprocess.run(
        [cli, "option", "volume", "daily", f"{symbol}.US", "--format", "json"],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(f"longbridge CLI {symbol} 失败: {r.stderr.strip()[:200]}")
    stats = (json.loads(r.stdout) or {}).get("stats") or []
    added = 0
    for s in stats:
        ts = int(s["timestamp"])
        stat_date = local_day_start(ts + 86400)
        call_vol = int(float(s.get("total_call_volume") or 0))
        put_vol = int(float(s.get("total_put_volume") or 0))
        call_oi = int(float(s.get("total_call_open_interest") or 0))
        put_oi = int(float(s.get("total_put_open_interest") or 0))
        if db.backfill_option_daily(db_path, OptionDaily(
            stock_code=f"{symbol}.US",
            stat_date=stat_date,
            call_volume=call_vol,
            put_volume=put_vol,
            call_oi=call_oi,
            put_oi=put_oi,
            pcr_volume=round(put_vol / call_vol, 4) if call_vol else None,
            pcr_oi=round(put_oi / call_oi, 4) if call_oi else None,
            source="longbridge",
        )):
            added += 1
    return added


def cross_validate_pcr(db_path: str, symbol: str, tolerance: float = 0.20) -> str | None:
    """双源 PCR 交叉校验: 最近一个双源日, pcr_oi 偏差超容忍度时返回告警文案。"""
    hist = db.get_option_daily_history(db_path, f"{symbol}.US", limit=30)
    by_date: dict[int, dict[str, OptionDaily]] = {}
    for h in hist:
        by_date.setdefault(h.stat_date, {})[h.source] = h
    overlaps = [d for d, src in by_date.items()
                if "cboe" in src and "longbridge" in src
                and src["cboe"].pcr_oi and src["longbridge"].pcr_oi]
    if not overlaps:
        return None
    day = max(overlaps)
    a = by_date[day]["cboe"].pcr_oi
    b = by_date[day]["longbridge"].pcr_oi
    if b and abs(a - b) / b > tolerance:
        dstr = datetime.fromtimestamp(day).strftime("%Y-%m-%d")
        return (f"{symbol}.US {dstr} 双源 PCR-OI 偏差超 {tolerance:.0%}: "
                f"cboe={a:.3f} longbridge={b:.3f}")
    return None


def collect_options(db_path: str, us_symbols: list[str], skip_backfill: bool = False) -> dict:
    """编排入口: 逐股采集 + 回填 + 校验。单股失败不阻塞其他股票。"""
    if _et_now().weekday() >= 5:
        # ET 周六/日无交易; 北京周一早上的轮次会拿到周五旧快照,
        # 若不打周末守卫会以新日键重复入库污染序列
        logger.info("[options] ET 周末, 跳过本次采集")
        return {"results": {}, "warnings": [], "skipped": "et_weekend"}
    results: dict[str, dict] = {}
    warnings: list[str] = []
    for sym in us_symbols:
        try:
            summary = collect_us_symbol(db_path, sym)
        except Exception as e:  # noqa: BLE001 — 单股失败不阻塞
            logger.error(f"[options] {sym} 采集失败: {e}")
            results[sym] = {"error": str(e)}
            continue
        if skip_backfill:
            results[sym] = summary
            continue
        try:
            summary["backfill_added"] = backfill_longbridge(db_path, sym)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[options] {sym} Longbridge 回填失败(不阻塞): {e}")
        warn = cross_validate_pcr(db_path, sym)
        if warn:
            warnings.append(warn)
        results[sym] = summary
    return {"results": results, "warnings": warnings}
