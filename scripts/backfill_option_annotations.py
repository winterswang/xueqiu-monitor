#!/usr/bin/env python3
"""一次性回填 options_daily 的派生标注: chain_expiry 与 premium_yield(+DTE)。

- chain_expiry (2026-10-16 加列): MaxPain 所属到期链 = |moneyness|<=20、oi>0 的
  过闸合约里 OI 最集中的到期。只填 NULL, 不覆盖已有值。
- premium_yield / premium_yield_dte (2026-10-18 加列): 收租率按实际 DTE 年化
  (premium_pct / DTE × 365)。历史值是 ×12 的月化假设、与实际到期无关 —— 对有
  合约快照的行**重算覆盖**。DTE 基准: US = snap_date − 1 天 (ET 交易日),
  HK = snap_date 当天。longbridge-only 行无合约快照, 跳过 (保持 NULL)。

用法: python3 scripts/backfill_option_annotations.py [-c data/monitor.db]
"""

from __future__ import annotations

import argparse
import collections
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db  # noqa: E402
from src.options_collector import MIN_YIELD_DTE  # noqa: E402


def _yield_from_contracts(
    conn: sqlite3.Connection, r: sqlite3.Row
) -> tuple[float, int] | None:
    """从合约快照复算 (premium_yield, dte)。无可用 call 返回 None。"""
    cs = conn.execute(
        """SELECT expiry, cp, moneyness, premium_pct, iv FROM options_contracts
           WHERE stock_code=? AND snap_date=?""",
        (r["stock_code"], r["stat_date"]),
    ).fetchall()
    if not cs:
        return None
    base = datetime.fromtimestamp(r["stat_date"]).date() - timedelta(
        days=1 if r["source"] == "cboe" else 0
    )
    base_iso = base.isoformat()
    calls = [
        x
        for x in cs
        if x["cp"] == "C"
        and (x["premium_pct"] or 0) > 0
        and x["moneyness"] is not None
        and (x["iv"] is not None if r["source"] == "cboe" else True)
        and x["expiry"] > base_iso  # 排除当日到期 (0DTE) 链
        and (date.fromisoformat(x["expiry"]) - base).days >= MIN_YIELD_DTE
    ]
    if not calls:
        return None
    front = min(x["expiry"] for x in calls)
    atm = min(
        (x for x in calls if x["expiry"] == front), key=lambda x: abs(x["moneyness"])
    )
    dte = max((date.fromisoformat(atm["expiry"]) - base).days, 1)
    return round(atm["premium_pct"] / dte * 365, 3), dte


def backfill(db_path: str) -> tuple[int, int, int]:
    """返回 (chain 回填数, yield 重算数, 无合约快照跳过数)。"""
    # 先确保 schema 已迁移 (两个列都由 init_db 的 _run_migrations 加, 幂等)
    db.init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, stock_code, stat_date, source, chain_expiry FROM options_daily"
    ).fetchall()
    n_chain = n_yield = missed = 0
    for r in rows:
        cs = conn.execute(
            """SELECT expiry, oi FROM options_contracts
               WHERE stock_code=? AND snap_date=? AND moneyness IS NOT NULL
                 AND ABS(moneyness)<=20 AND oi>0""",
            (r["stock_code"], r["stat_date"]),
        ).fetchall()
        if not cs:
            missed += 1
            continue
        if r["chain_expiry"] is None:
            by: collections.Counter[str] = collections.Counter()
            for x in cs:
                by[x["expiry"]] += x["oi"]
            conn.execute(
                "UPDATE options_daily SET chain_expiry=? WHERE id=?",
                (max(by, key=lambda e: by[e]), r["id"]),
            )
            n_chain += 1
        y = _yield_from_contracts(conn, r)
        if y is not None:
            conn.execute(
                "UPDATE options_daily SET premium_yield=?, premium_yield_dte=? WHERE id=?",
                (y[0], y[1], r["id"]),
            )
            n_yield += 1
    conn.commit()
    conn.close()
    return n_chain, n_yield, missed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-c", "--db", default="data/monitor.db")
    args = ap.parse_args()
    n_chain, n_yield, missed = backfill(args.db)
    # 人读输出走 stderr, stdout 只留 cindy-script/1 帧 —— 与仓库其他脚本同一纪律,
    # 免得哪天被挂成 script 模式又被判 failed。
    print(
        f"chain_expiry 回填 {n_chain} 行; premium_yield 重算 {n_yield} 行; "
        f"无合约快照跳过 {missed} 行",
        file=sys.stderr,
    )
    print('{"protocol": "cindy-script/1", "type": "complete"}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
