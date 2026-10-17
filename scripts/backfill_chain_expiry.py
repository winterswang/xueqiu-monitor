#!/usr/bin/env python3
"""一次性回填 options_daily.chain_expiry（2026-10-16 加列之前的历史行）。

chain_expiry = MaxPain 所属到期链 = |moneyness|<=20、oi>0 的过闸合约里 OI 最
集中的到期 —— 与采集端 aggregate 的 near 口径一致。options_contracts 里已存
了过闸行与 moneyness，可直接复算。

只填 NULL，不覆盖已有值；当日无合约快照的行留空（重跑采集会自然补上）。

用法: python3 scripts/backfill_chain_expiry.py [-c data/monitor.db]
"""

from __future__ import annotations

import argparse
import collections
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db  # noqa: E402


def backfill(db_path: str) -> tuple[int, int]:
    """返回 (回填行数, 无合约快照跳过行数)。只填 NULL。"""
    # 先确保 schema 已迁移 (chain_expiry 列由 init_db 的 _run_migrations 加, 幂等)
    db.init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, stock_code, stat_date FROM options_daily WHERE chain_expiry IS NULL"
    ).fetchall()
    fixed = missed = 0
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
        by: collections.Counter[str] = collections.Counter()
        for x in cs:
            by[x["expiry"]] += x["oi"]
        chain = max(by, key=lambda e: by[e])
        conn.execute(
            "UPDATE options_daily SET chain_expiry=? WHERE id=?", (chain, r["id"])
        )
        fixed += 1
    conn.commit()
    conn.close()
    return fixed, missed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-c", "--db", default="data/monitor.db")
    args = ap.parse_args()
    fixed, missed = backfill(args.db)
    # 人读输出走 stderr, stdout 只留 cindy-script/1 帧 —— 与仓库其他脚本同一纪律,
    # 免得哪天被挂成 script 模式又被判 failed。
    print(f"回填 {fixed} 行, 无合约快照跳过 {missed} 行", file=sys.stderr)
    print('{"protocol": "cindy-script/1", "type": "complete"}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
