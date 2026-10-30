#!/usr/bin/env python3
"""options-sense 每日采集入口。

标的来源: ``etc/config.report.json`` 的 ``stocks`` —— 与舆情日报**同源**,
加/减自选股自动跟上 (2026-10-15 修正: 此前只读 etc/config.json 的
crawler.whitelist, 只覆盖到 3 只美股, 漏采了散在 g1..g5 分组里的 16 只)。

两个市场通道时间不同, 分两条 cron 跑:
  --market us  (默认) 北京 07:05, 美股收盘后:  CBOE 逐合约 → Longbridge 回填+交叉校验
  --market hk         北京 20:00, 港股清算后:  HKEX DTOP 日报 → (无 IV, 备兑收益率需 Longbridge spot)

单股失败不阻塞其他股票; 全市场全失败时 exit 1 (cron 可见)。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db, options_collector, options_detector, options_hk  # noqa: E402
from src.config import Config  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("collect_options")

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_REPORT_CONFIG = "etc/config.report.json"


def load_report_stocks(report_config: str) -> list[str]:
    """读日报的股票池 (唯一真源, 与 report_generator 同源)。

    路径按 **项目根** 解析而非 cwd —— cron 在仓库外起进程时, 裸相对路径会
    静默落空并退回旧白名单 (正是 2026-10-15 修掉的那个 3 只美股漏采)。
    """
    path = Path(report_config)
    if not path.is_absolute():
        path = PROJECT_DIR / path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"读取股票池 {path} 失败: {e}")
        return []
    return list((data.get("stocks") or {}).keys())


def _report_config_path(cfg: Config) -> str:
    return (cfg.options or {}).get("report_config") or DEFAULT_REPORT_CONFIG


def derive_us_symbols(cfg: Config, stocks: list[str]) -> list[str]:
    """美股标的: 优先级 us_symbols 覆盖 > 日报股票池。池空则返回 [] (调用方报错)。"""
    custom = (cfg.options or {}).get("us_symbols") or []
    if custom:
        return [s.upper().removesuffix(".US") for s in custom]
    # 去掉末尾 .US，但保留 ticker 内部的点（BRK.B.US → BRK.B）
    return [s[:-3] for s in stocks if s.upper().endswith(".US")]


def derive_hk_symbols(stocks: list[str]) -> list[str]:
    return [s for s in stocks if s.upper().endswith(".HK")]


def _log_us_summary(out: dict, symbols: list[str]) -> int:
    for sym, r in sorted(out["results"].items()):
        if "error" in r:
            logger.error(f"{sym}: {r['error']}")
        else:
            logger.info(
                f"{sym}: S0={r['spot']:.2f} PCR-仓={r['pcr_oi']} PCR-量={r['pcr_volume']} "
                f"iv30={r['atm_iv']} 备兑收益率={r['premium_yield']}% "
                f"墙 C{r['wall_call']}/P{r['wall_put']} MaxPain={r['max_pain']} "
                f"合约={r['n_contracts']} 回填+{r.get('backfill_added', 0)}"
            )
    for w in out["warnings"]:
        logger.warning(f"[cross-check] {w}")
    ok = [s for s in symbols if "error" not in out["results"].get(s, {"error": 1})]
    failed = [s for s in symbols if "error" in out["results"].get(s, {})]
    logger.info(f"美股完成: 成功 {len(ok)}/{len(symbols)}")
    return 1 if not ok and failed else 0


def _run_us(cfg: Config, stocks: list[str], skip_backfill: bool, dry_run: bool) -> int:
    symbols = derive_us_symbols(cfg, stocks)
    if not symbols:
        # 池空 = 日报配置没读到, 不是「今天没有美股」。静默跳过会让 16 只自选股
        # 无声无息地没有期权数据 —— 显式失败, 让 cron 的健康检查看得见。
        logger.error("美股股票池为空 (日报配置未读到), 本轮失败")
        return 1
    logger.info(f"采集美股期权 ({len(symbols)} 只): {symbols}")
    db.init_db(cfg.db_path)
    out = options_collector.collect_options(
        cfg.db_path,
        symbols,
        skip_backfill=skip_backfill or (cfg.options or {}).get("skip_backfill", False),
    )
    n_alerts = 0
    for sym in symbols:
        for alert in options_detector.detect_options_signals(cfg.db_path, f"{sym}.US"):
            if not dry_run:
                db.insert_alert(cfg.db_path, alert)
            n_alerts += 1
            logger.info(
                f"[alert] {alert.stock_code} {alert.alert_type} P={alert.priority}"
            )
    rc = _log_us_summary(out, symbols)
    logger.info(f"美股新告警 {n_alerts} 条")
    return rc


def _run_hk(cfg: Config, stocks: list[str], dry_run: bool) -> int:
    symbols = derive_hk_symbols(stocks)
    if not symbols:
        logger.error("港股股票池为空 (日报配置未读到), 本轮失败")
        return 1
    logger.info(f"采集港股期权 (池内 {len(symbols)} 只): {symbols}")
    db.init_db(cfg.db_path)
    out = options_hk.collect_hk(cfg.db_path, symbols, dry_run=dry_run)

    for sym, r in sorted(out["results"].items()):
        if "error" in r:
            logger.error(f"{sym}: {r['error']}")
        else:
            logger.info(
                f"{sym}({r['hkats']}): S0={r['spot']} PCR-仓={r['pcr_oi']} "
                f"PCR-量={r['pcr_volume']} 备兑收益率={r['premium_yield']}% "
                f"墙 C{r['wall_call']}/P{r['wall_put']} MaxPain={r['max_pain']} "
                f"合约={r['n_contracts']}"
            )
    n_alerts = 0
    for sym in sorted(out["results"]):
        if "error" in out["results"][sym]:
            continue
        for alert in options_detector.detect_options_signals(cfg.db_path, sym):
            if not dry_run:
                db.insert_alert(cfg.db_path, alert)
            n_alerts += 1
            logger.info(
                f"[alert] {alert.stock_code} {alert.alert_type} P={alert.priority}"
            )
    for w in out["warnings"]:
        logger.warning(f"[hk] {w}")
    if out.get("skipped") == "no_dtop":
        # DTOP 每个港股交易日都发; 工作日拿不到才值得报错
        weekday = datetime.now().weekday() < 5
        logger.warning(f"无 DTOP 数据 (工作日={weekday}), 跳过本轮")
        return 1 if weekday else 0
    logger.info(
        f"港股完成: {len(out['results'])} 只, 无上市期权 {len(out['unmapped'])} 只, "
        f"新告警 {n_alerts} 条"
    )
    return (
        1
        if out["results"] and all("error" in r for r in out["results"].values())
        else 0
    )


def emit_complete(ok: bool, error: str = "") -> None:
    """输出 cindy-script/1 协议帧 —— script 模式的 stdout 只认这个。

    缺了它调度器会判「script exited without a complete frame」并把本轮记成
    failed，哪怕活儿其实干成了。所有诊断输出必须走 stderr（logging 默认即
    如此），stdout 上只允许这一行。
    """
    frame: dict = {"protocol": "cindy-script/1", "type": "complete"}
    if not ok:
        frame["ok"] = False
        frame["error"] = error or "failed"
    print(json.dumps(frame, ensure_ascii=False), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-c", "--config", default="etc/config.json")
    ap.add_argument("--market", choices=["us", "hk", "both"], default="us")
    ap.add_argument("--symbols", default="", help="逗号分隔覆盖 (仅美股), 如 PDD,META")
    ap.add_argument("--skip-backfill", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只算不落库")
    args = ap.parse_args()

    cfg = Config.from_file(args.config)
    if not (cfg.options or {}).get("enabled", True):
        logger.info("options.enabled=false, 跳过")
        emit_complete(True)
        return 0

    stocks = load_report_stocks(_report_config_path(cfg))
    if not stocks:
        logger.error(f"股票池为空 ({_report_config_path(cfg)})")

    rc = 0
    if args.market in ("us", "both"):
        if args.symbols:
            symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            cfg.options = {**(cfg.options or {}), "us_symbols": symbols}
        rc |= _run_us(cfg, stocks, args.skip_backfill, args.dry_run)
    if args.market in ("hk", "both"):
        rc |= _run_hk(cfg, stocks, args.dry_run)
    emit_complete(rc == 0, "" if rc == 0 else f"options {args.market} 采集未全部成功")
    return rc


if __name__ == "__main__":
    sys.exit(main())
