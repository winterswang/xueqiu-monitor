"""options-sense: 港股期权日频采集器 (HKEX DTOP 通道)。

数据源 (2026-10-15 实测, 见 plans/options-sense/PLAN.md §2):
- HKEX DTOP 日报 (主通道): 每个交易日约 19:24 (香港时间) 发布一个 zip,
  内含全市场所有股票期权类别的逐合约明细 (``*_opt_dtl_all.raw``, 干净 CSV) ——
  一次请求即覆盖全部自选港股, 免费、无账号。
- 类代码映射: 产品页挂的 The-List-of-Stock-Option-Classes-*.xlsx
  (SEHK code ↔ HKATS code, 如 981→SMC / 9992→POP)。
- 正股价: Longbridge CLI ``quote`` (DTOP 不含标的价格, 备兑收益率需要它做分母)。

口径边界: DTOP 没有 IV 也没有盘口 (bid/ask), 故港股为 PCR(仓/量) + 期权墙 +
Max Pain + 备兑收益率年化, ``atm_iv`` 留空; 流动性闸由 bid/ask 改为 settle>0 且
(oi>0 或 volume>0)。OI 是当日收盘清算值 (非 T-1)。

采集失败必须显式报错, 不得用旧数据充数 (用户 2026-10-13 明确要求)。
唯一例外是类代码表 —— 它是低频变更的参考数据, 拉不到时用旧缓存并告警。
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import subprocess
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

from . import db
from .db import local_day_start
from .models import OptionDaily
from .options_collector import MIN_YIELD_DTE, _f, _max_oi_strike, _max_pain

logger = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
PRODUCT_PAGE = (
    "https://www.hkex.com.hk/Products/Listed-Derivatives/Single-Stock/Stock-Options"
    "?sc_lang=en"
)
DTOP_URL = "https://www.hkex.com.hk/eng/stat/dmstat/oi/DTOP_O_{}.zip"
CLASS_XLSX_RE = re.compile(
    r"href=\"([^\"]*The-List-of-Stock-Option-Classes-Available-for-Trading_(\d{8})\.xlsx)\""
)
CLASS_CACHE_DAYS = 7
_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "JAN",
            "FEB",
            "MAR",
            "APR",
            "MAY",
            "JUN",
            "JUL",
            "AUG",
            "SEP",
            "OCT",
            "NOV",
            "DEC",
        ],
        1,
    )
}
# DTOP 每行的字段位 (见 *_opt_dtl_all.raw / readme.rpt)
_F_STRIKE, _F_C_OI, _F_C_TO, _F_C_SETTLE = 7, 8, 11, 13
_F_P_OI, _F_P_TO, _F_P_SETTLE = 15, 18, 20
_MIN_FIELDS = 22

# options_contracts 的列 (settle 只服务于聚合, 不落库)
_CONTRACT_COLS = (
    "contract_id",
    "expiry",
    "strike",
    "cp",
    "last",
    "bid",
    "ask",
    "volume",
    "oi",
    "iv",
    "delta",
    "gamma",
    "theta",
    "vega",
    "premium_pct",
    "moneyness",
    "option_currency",
)


def class_cache_path(db_path: str) -> str:
    """类代码表与库同目录 (data/), 日报端也要读它。"""
    return str(Path(db_path).parent / "hk_option_classes.json")


def _sehk_code(symbol: str) -> int | None:
    """'9992.HK' -> 9992; 非港股或不可解析返回 None。"""
    head = symbol.split(".")[0].strip()
    return int(head) if head.isdigit() else None


def _http_get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ── 类代码表 ──────────────────────────────────────────────


def parse_class_xlsx(data: bytes) -> dict[int, str]:
    """官方类代码表 xlsx -> {SEHK code: HKATS code}。"""
    import openpyxl  # 局部引入: 只有走港股通道时才需要

    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    try:
        ws = wb["Eng"] if "Eng" in wb.sheetnames else wb[wb.sheetnames[0]]
        out: dict[int, str] = {}
        for row in ws.iter_rows(min_row=6, values_only=True):
            if len(row) < 4 or row[1] is None or row[3] is None:
                continue
            try:
                code = int(row[1])
            except (TypeError, ValueError):
                continue
            out[code] = str(row[3]).strip().upper()
        return out
    finally:
        wb.close()


def load_class_cache(cache_path: str) -> dict | None:
    """读类代码表缓存 {as_of, fetched_at, map}。缺失/损坏返回 None。

    报告端 (report_generator) 也要用它判断「哪些港股根本没有上市期权」,
    所以是公开契约 —— 缓存路径与结构只在这里定义一次。
    """
    try:
        return json.loads(Path(cache_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _cache_fresh(cache: dict, max_age_days: int) -> bool:
    try:
        fetched = datetime.fromisoformat(cache["fetched_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return (datetime.now() - fetched) < timedelta(days=max_age_days)


def fetch_class_map(
    cache_path: str, page_url: str = PRODUCT_PAGE, max_age_days: int = CLASS_CACHE_DAYS
) -> dict[int, str]:
    """SEHK code → HKATS code。缓存新鲜则直接用; 拉不到时回退旧缓存 (告警)。"""
    cache = load_class_cache(cache_path)
    if cache and _cache_fresh(cache, max_age_days) and cache.get("map"):
        return {int(k): v for k, v in cache["map"].items()}

    try:
        html = _http_get(page_url).decode("utf-8", "replace")
        m = CLASS_XLSX_RE.search(html)
        if not m:
            raise RuntimeError("产品页未找到类代码表 xlsx 链接")
        mapping = parse_class_xlsx(_http_get(urljoin(page_url, m.group(1))))
        if not mapping:
            raise RuntimeError("类代码表解析为空")
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        Path(cache_path).write_text(
            json.dumps(
                {
                    "as_of": m.group(2),
                    "fetched_at": datetime.now().isoformat(timespec="seconds"),
                    "map": {str(k): v for k, v in mapping.items()},
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        return mapping
    except (
        urllib.error.URLError,
        OSError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
    ) as e:
        # zipfile.BadZipFile / openpyxl InvalidFileException: 站点返回 HTML 错误页时
        # 内容不是 zip —— 必须走「旧缓存降级」, 不能让它冒出去炸掉整轮采集
        if cache and cache.get("map"):
            logger.warning(
                f"类代码表拉取失败({e}), 沿用旧缓存 as_of={cache.get('as_of')}"
            )
            return {int(k): v for k, v in cache["map"].items()}
        raise


# ── DTOP 日报 ─────────────────────────────────────────────


def fetch_dtop(trade_day: date, timeout: int = 90) -> bytes | None:
    """取某日 DTOP zip; 404 (非交易日/未发布) 返回 None。"""
    url = DTOP_URL.format(trade_day.strftime("%Y%m%d"))
    try:
        return _http_get(url, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def latest_dtop(
    trade_day: date | None = None, max_lookback: int = 7
) -> tuple[date, bytes] | None:
    """从 trade_day 往前找最近一份可用的 DTOP (跨周末/假期/未发布)。"""
    start = trade_day or datetime.now().date()
    for back in range(max_lookback + 1):
        day = start - timedelta(days=back)
        data = fetch_dtop(day)
        if data is not None:
            return day, data
    return None


def extract_dtop_raw(zip_bytes: bytes) -> str | None:
    """从 zip 里取出全市场期权明细 CSV (只解这一个成员)。"""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            if name.endswith("_opt_dtl_all.raw"):
                return zf.read(name).decode("utf-8", "replace")
    return None


def parse_dtop_raw(text: str, wanted_classes: set[str]) -> dict[str, list[dict]]:
    """DTOP CSV -> {HKATS code: [合约行]}，只保留 wanted_classes。"""
    out: dict[str, list[dict]] = {c: [] for c in wanted_classes}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < _MIN_FIELDS or row[0] != "01":
            continue
        cls = row[3]
        if cls not in out:
            continue
        parsed = _parse_dtop_row(row, cls)
        if parsed:
            out[cls].extend(parsed)
    return out


def _parse_dtop_row(row: list[str], cls: str) -> list[dict]:
    try:
        expiry = date(
            2000 + int(row[6]), _MONTHS[row[5].upper()], int(row[4])
        ).isoformat()
        strike = float(row[_F_STRIKE])
        legs = (
            ("C", int(row[_F_C_OI]), int(row[_F_C_TO]), float(row[_F_C_SETTLE])),
            ("P", int(row[_F_P_OI]), int(row[_F_P_TO]), float(row[_F_P_SETTLE])),
        )
    except (ValueError, KeyError, IndexError):
        return []
    return [
        {
            "contract_id": f"{cls}-{expiry}-{cp}{strike:g}",
            "expiry": expiry,
            "strike": strike,
            "cp": cp,
            "last": None,
            "bid": None,
            "ask": None,
            "volume": vol,
            "oi": oi,
            "iv": None,
            "delta": None,
            "gamma": None,
            "theta": None,
            "vega": None,
            "settle": settle,
        }
        for cp, oi, vol, settle in legs
    ]


# ── 正股价 ────────────────────────────────────────────────


def fetch_spot(symbols: list[str], cli: str = "longbridge") -> dict[str, float]:
    """Longbridge 批量取正股价。失败返回 {} (备兑收益率降级为 None, 不阻塞采集)。"""
    if not symbols:
        return {}
    try:
        r = subprocess.run(
            [cli, "quote", *symbols, "--format", "json"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if r.returncode != 0:
            logger.warning(
                f"longbridge quote 退出码 {r.returncode}: {r.stderr.strip()[:200]}"
            )
            return {}
        data = json.loads(r.stdout)
        out: dict[str, float] = {}
        for item in data:
            price = _f(item.get("last"))
            if price and price > 0:
                out[item["symbol"]] = price
        return out
    except (
        OSError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
        AttributeError,
        KeyError,
        TypeError,
    ) as e:
        logger.warning(f"longbridge quote 失败: {e}")
        return {}


# ── 聚合 ──────────────────────────────────────────────────


def aggregate_hk_chain(
    contracts: list[dict], spot: float | None, stat_date: int
) -> dict | None:
    """DTOP 合约行 → 聚合指标。无合约返回 None (与 aggregate_chain 同形)。"""
    if not contracts:
        return None
    px = spot if (spot or 0) > 0 else None
    gated = [
        _gate_row(c, px)
        for c in contracts
        if (c["settle"] or 0) > 0 and ((c["oi"] or 0) > 0 or (c["volume"] or 0) > 0)
    ]
    calls = [c for c in gated if c["cp"] == "C"]
    puts = [c for c in gated if c["cp"] == "P"]
    call_vol = sum(c["volume"] or 0 for c in calls)
    put_vol = sum(c["volume"] or 0 for c in puts)
    call_oi = sum(c["oi"] or 0 for c in calls)
    put_oi = sum(c["oi"] or 0 for c in puts)
    # 无正股价时**不**做墙/MaxPain: 缺了 ±20% 价内外带, 全链取 OI 最大会落到
    # 远离现价的远期行权价上, 读数看着正常其实是脏值 —— 宁可留空。
    near = (
        [r for r in gated if r["moneyness"] is not None and abs(r["moneyness"]) <= 20]
        if px
        else []
    )
    _hk_mp, _hk_chain = _hk_max_pain(near)
    chain_rows = [r for r in near if r["expiry"] == _hk_chain] if _hk_chain else []
    _py, _py_dte = _hk_premium_yield(near, px, stat_date) if px else (None, None)
    by_expiry: dict[str, list[dict]] = {}
    for row in gated:
        by_expiry.setdefault(row["expiry"], []).append(row)
    expiry_metrics = []
    for expiry, chain in by_expiry.items():
        calls_e = [r for r in chain if r["cp"] == "C"]
        puts_e = [r for r in chain if r["cp"] == "P"]
        coi, poi = sum(r["oi"] or 0 for r in calls_e), sum(r["oi"] or 0 for r in puts_e)
        strike_oi: dict[float, int] = {}
        for row in chain:
            strike_oi[row["strike"]] = strike_oi.get(row["strike"], 0) + (row["oi"] or 0)
        expiry_metrics.append({
            "expiry": expiry,
            "dte": max(0, (date.fromisoformat(expiry) - datetime.fromtimestamp(stat_date).date()).days),
            "spot": px,
            "call_volume": sum(r["volume"] or 0 for r in calls_e),
            "put_volume": sum(r["volume"] or 0 for r in puts_e),
            "call_oi": coi, "put_oi": poi,
            "pcr_volume": round(sum(r["volume"] or 0 for r in puts_e) / sum(r["volume"] or 0 for r in calls_e), 4) if sum(r["volume"] or 0 for r in calls_e) else None,
            "pcr_oi": round(poi / coi, 4) if coi else None,
            "call_oi_wall": _max_oi_strike(chain, "C"),
            "put_oi_wall": _max_oi_strike(chain, "P"),
            "top_strike_oi_share": round(max(strike_oi.values()) / (coi + poi), 4) if coi + poi and strike_oi else None,
            "valid_contracts": len(chain), "iv_contracts": 0, "delta_contracts": 0,
            "option_currency": "HKD", "quality_rule": "settle>0;(oi>0 or volume>0)",
        })
    return {
        "spot": spot,
        "stat_date": stat_date,
        "call_volume": call_vol,
        "put_volume": put_vol,
        "call_oi": call_oi,
        "put_oi": put_oi,
        "pcr_volume": round(put_vol / call_vol, 4) if call_vol else None,
        "pcr_oi": round(put_oi / call_oi, 4) if call_oi else None,
        "atm_iv": None,  # DTOP 无 IV
        "premium_yield": _py,
        "premium_yield_dte": _py_dte,
        "max_pain": _hk_mp,
        "chain_expiry": _hk_chain,
        "wall_call": _max_oi_strike(chain_rows, "C"),
        "wall_put": _max_oi_strike(chain_rows, "P"),
        "contracts_seen": len(contracts),
        "contracts_valid": len(gated),
        "quality_rule": "settle>0;(oi>0 or volume>0)",
        "option_currency": "HKD",
        "underlying_currency": "HKD",
        "contracts": gated,
        "expiry_metrics": expiry_metrics,
    }


def _gate_row(c: dict, px: float | None) -> dict:
    """过闸合约行: 补 moneyness / premium_pct (与美股同口径, 港股用结算价代 mid)。"""
    return {
        **c,
        "moneyness": (c["strike"] / px - 1) * 100 if px else None,
        "premium_pct": (c["settle"] or 0) / px * 100 if px else None,
        "option_currency": "HKD",
    }


def _hk_premium_yield(
    near: list[dict], spot: float, stat_date: int
) -> tuple[float | None, int | None]:
    """ATM call 权利金率按实际 DTE 年化 (premium_pct / DTE × 365), 连同 DTE 返回。

    只取**最近到期**: 同一个行权价在多个到期月都有, 不限定会把远期 LEAPS 当近月。
    旧算法 ×12 隐含「当月合约」, 周期权被低估 ~30 倍 (2026-10-18 修)。
    DTE 基准 = stat_date 当天 (香港与北京同日)。
    """
    base = datetime.fromtimestamp(stat_date).date()
    day_iso = base.isoformat()
    pool = []
    for r in near:
        if r["cp"] != "C" or (r["premium_pct"] or 0) <= 0 or r["expiry"] <= day_iso:
            continue
        d = (date.fromisoformat(r["expiry"]) - base).days
        if d >= MIN_YIELD_DTE:
            pool.append((r, d))
    if not pool:
        return None, None
    front = min(d for _, d in pool)
    atm, dte = min(
        (rd for rd in pool if rd[1] == front),
        key=lambda rd: abs(rd[0]["strike"] - spot),
    )
    return round(atm["premium_pct"] / dte * 365, 3), dte


def _hk_max_pain(near: list[dict]) -> tuple[float | None, str | None]:
    """OI 最集中的到期日 (通常是近月) 上的 Max Pain, 连同该到期一起返回。

    到期要随值一起落库 (chain_expiry): 报告端据此标注静态 OI 计算所属的到期链。
    """
    by_expiry: dict[str, list[dict]] = {}
    for r in near:
        by_expiry.setdefault(r["expiry"], []).append(r)
    if not by_expiry:
        return None, None
    big = max(by_expiry, key=lambda e: sum(r["oi"] or 0 for r in by_expiry[e]))
    rows = by_expiry[big]
    mp = _max_pain(
        [r for r in rows if r["cp"] == "C"], [r for r in rows if r["cp"] == "P"]
    )
    return mp, big


# ── 编排 ──────────────────────────────────────────────────


def to_contract_rows(sym: str, stat_date: int, agg: dict) -> list[dict]:
    """聚合结果 → options_contracts 行 (只保留表内列; settle 是内部字段)。"""
    return [
        {k: r.get(k) for k in _CONTRACT_COLS}
        | {"stock_code": sym, "snap_date": stat_date}
        for r in agg["contracts"]
    ]


def _to_option_row(sym: str, stat_date: int, agg: dict) -> OptionDaily:
    return OptionDaily(
        stock_code=sym,
        stat_date=stat_date,
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
        source="hkex",
        option_currency="HKD",
        underlying_currency="HKD",
        contracts_seen=agg["contracts_seen"],
        contracts_valid=agg["contracts_valid"],
        quality_rule=agg["quality_rule"],
    )


def _map_symbols(
    hk_symbols: list[str], class_map: dict[int, str]
) -> tuple[dict[str, str], list[str]]:
    """→ ({HKATS code: '形式symbol'}, 无上市期权的 symbol 列表)。"""
    by_class: dict[str, str] = {}
    unmapped: list[str] = []
    for sym in hk_symbols:
        code = _sehk_code(sym)
        hkats = class_map.get(code) if code is not None else None
        if hkats:
            by_class.setdefault(hkats, sym)
        else:
            unmapped.append(sym)
    return by_class, unmapped


def collect_hk(
    db_path: str,
    hk_symbols: list[str],
    trade_day: date | None = None,
    dry_run: bool = False,
    cli: str = "longbridge",
) -> dict:
    """港股自选池 → DTOP → 聚合落库。返回 {results, warnings, unmapped}。"""
    class_map = fetch_class_map(class_cache_path(db_path))
    by_class, unmapped = _map_symbols(hk_symbols, class_map)
    if not by_class:
        return {"results": {}, "warnings": [], "unmapped": unmapped}

    found = latest_dtop(trade_day)
    if found is None:
        return {
            "results": {},
            "warnings": ["最近 7 天无 DTOP 数据 (非交易日或尚未发布)"],
            "unmapped": unmapped,
            "skipped": "no_dtop",
        }
    day, zip_bytes = found
    expected = trade_day or datetime.now().date()
    if day != expected:
        # 回退到更早的一天必须看得见 —— 否则「今天还没发布 → 静默再采一遍昨天」
        # 会表现成「跑了、退 0、库里没有今天的数据」，事后无从判断。
        logger.warning(
            f"DTOP 回退: 期望 {expected.isoformat()} 的实际取到 {day.isoformat()}"
            f"（当日尚未发布或非交易日），本轮按 {day.isoformat()} 采集"
        )
    logger.info(f"DTOP 交易日 = {day.isoformat()} ({len(zip_bytes)} bytes)")
    raw = extract_dtop_raw(zip_bytes)
    if raw is None:
        raise RuntimeError("DTOP zip 内未找到 *_opt_dtl_all.raw")

    chains = parse_dtop_raw(raw, set(by_class))
    spots = fetch_spot(list(by_class.values()), cli=cli)
    stat_date = local_day_start(datetime(day.year, day.month, day.day).timestamp())

    results: dict[str, dict] = {}
    for hkats, sym in sorted(by_class.items()):
        agg = aggregate_hk_chain(chains.get(hkats) or [], spots.get(sym), stat_date)
        if agg is None:
            results[sym] = {"error": "DTOP 中该类别无合约"}
            continue
        if not dry_run:
            db.insert_option_contracts(db_path, to_contract_rows(sym, stat_date, agg))
            db.upsert_option_expiry_daily(db_path, [
                {**metric, "stock_code": sym, "stat_date": stat_date, "source": "hkex"}
                for metric in agg["expiry_metrics"]
            ])
            db.upsert_option_daily(db_path, _to_option_row(sym, stat_date, agg))
        results[sym] = {k: v for k, v in agg.items() if k != "contracts"} | {
            "symbol": sym,
            "hkats": hkats,
            "n_contracts": len(agg["contracts"]),
            "spot": agg["spot"],
        }
    return {
        "results": results,
        "warnings": [],
        "unmapped": unmapped,
        "trade_date": day.isoformat(),
    }
