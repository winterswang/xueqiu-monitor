#!/usr/bin/env python3
"""Backfill pool_history ledger from git history (v0.9 T1).

Replays every change to the ``stocks`` dict of ``etc/config.report.json``
across git history and records one add/remove row per stock per commit.
No hardcoded stock lists: the sets are computed from ``git show`` at each
commit touching the file, so replay(final) == live report pool is
structurally guaranteed (and verified after backfill).

Explicit data tables (sourced from commit messages — the only record of
these facts; kept here so the ledger is self-contained):
- VOLUME_SNAPSHOT: 14d avg daily posts at rotation time (v0.8.5/v0.8.6)
- EFFECTIVE_DATE_OVERRIDES: v0.8.6 rotation was performed 9/9 by the user
  but only committed 9/12 (3 days live-uncommitted); effective date is 9/9.
- VERSION_LABELS: human version tags for the two rotation commits.

Idempotent: UNIQUE(stock_code, effective_date, action) + INSERT OR IGNORE,
safe to re-run (new git commits → incremental rows only).

Usage:
    python3 scripts/backfill_pool_history.py [--db PATH] [--dry-run] [--verify]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import db as dbmod  # noqa: E402  (path setup must precede import)

REPORT_CONFIG = "etc/config.report.json"

# 14d 日均帖量快照（口径 8/26-9/8 posts_count）——来源：118d41a / 6c66046 commit message
VOLUME_SNAPSHOT: dict[str, dict[str, float]] = {
    # v0.8.5 (118d41a): 结构性低声量退日报池（保留爬取告警层）
    "118d41a": {"ASML.US": 3.5, "TAL.US": 3.8, "SPOT.US": 12.6},
    # v0.8.6 (6c66046): 低声量出池（9 只）
    "6c66046": {
        "NVO.US": 4.6, "RKLB.US": 5.3, "CBRS.US": 6.1, "TCOM.US": 7.9,
        "APP.US": 10.3, "ABNB.US": 12.7, "META.US": 25.7, "DUOL.US": 35.9,
        "LKNCY.US": 51.6,
    },
}

# 生效日期覆盖：轮换实际执行日 ≠ 提交日（来源：commit message 自述）
EFFECTIVE_DATE_OVERRIDES: dict[str, str] = {
    "6c66046": "2026-09-09",  # user-performed rotation on 2026-09-09, committed 09-12
}

VERSION_LABELS: dict[str, str] = {
    "118d41a": "v0.8.5",
    "6c66046": "v0.8.6",
}


def _git(root: Path, *args: str) -> str:
    """Run a git command in root, returning stdout (raises on failure)."""
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout is not None  # text=True guarantees str
    return result.stdout


def report_stocks_at(root: Path, rev: str) -> set[str]:
    """Stock codes in config.report.json's stocks dict at a given revision."""
    blob = _git(root, "show", f"{rev}:{REPORT_CONFIG}")
    return set(json.loads(blob).get("stocks", {}).keys())


def touching_commits(root: Path) -> list[tuple[str, str, str]]:
    """Commits touching REPORT_CONFIG, oldest first. Returns (hash, date, subject).

    Date = committer date (when the change landed in the repo), YYYY-MM-DD.
    """
    out = _git(
        root, "log", "--reverse", "--follow", "--format=%h|%cs|%s", "--", REPORT_CONFIG
    )
    commits = []
    for line in out.strip().splitlines():
        h, date, subject = line.split("|", 2)
        commits.append((h, date, subject))
    return commits


def diff_rows(
    prev: set[str],
    curr: set[str],
    date: str,
    version: str,
    subject: str,
    volumes: dict[str, float] | None = None,
) -> list[dict]:
    """Ledger rows for one commit's stock-set transition (pure function)."""
    volumes = volumes or {}
    rows = []
    for code in sorted(prev - curr):
        rows.append({
            "stock_code": code,
            "action": "remove",
            "effective_date": date,
            "reason": subject,
            "avg_daily_posts": volumes.get(code),
            "config_version": version,
        })
    for code in sorted(curr - prev):
        rows.append({
            "stock_code": code,
            "action": "add",
            "effective_date": date,
            "reason": subject,
            "avg_daily_posts": None,  # 入池时声量未知（新标的才有数据）
            "config_version": version,
        })
    return rows


def build_history(root: Path) -> list[dict]:
    """Full ledger: initial baseline adds + every subsequent transition."""
    commits = touching_commits(root)
    if not commits:
        return []
    rows: list[dict] = []
    prev: set[str] = set()
    for h, date, subject in commits:
        eff = EFFECTIVE_DATE_OVERRIDES.get(h, date)
        version = VERSION_LABELS.get(h, h)
        curr = report_stocks_at(root, h)
        batch = diff_rows(prev, curr, eff, version, subject, VOLUME_SNAPSHOT.get(h))
        rows.extend(batch)
        prev = curr
    return rows


def replay(rows: list[dict]) -> set[str]:
    """Final pool state after applying all rows in order (pure function)."""
    state: dict[str, bool] = {}
    for r in rows:
        state[r["stock_code"]] = r["action"] == "add"
    return {code for code, active in state.items() if active}


def insert_rows(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """INSERT OR IGNORE (idempotent via UNIQUE constraint). Returns inserted count."""
    inserted = 0
    for r in rows:
        cur = conn.execute(
            "INSERT OR IGNORE INTO pool_history "
            "(stock_code, action, effective_date, reason, avg_daily_posts, config_version) "
            "VALUES (:stock_code, :action, :effective_date, :reason, :avg_daily_posts, :config_version)",
            r,
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


def verify(db_path: str, root: Path) -> tuple[bool, set[str], set[str]]:
    """Ledger replay == live report pool? Returns (ok, ledger_only, live_only)."""
    conn = sqlite3.connect(db_path)
    try:
        db_rows = conn.execute(
            "SELECT stock_code, action FROM pool_history ORDER BY effective_date, id"
        ).fetchall()
    finally:
        conn.close()
    ledger_pool = replay([{"stock_code": c, "action": a} for c, a in db_rows])
    live_pool = report_stocks_at(root, "HEAD")
    return ledger_pool == live_pool, ledger_pool - live_pool, live_pool - ledger_pool


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--db", default=str(PROJECT_ROOT / "data" / "monitor.db"),
        help="monitor.db path (default: data/monitor.db)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print rows, write nothing")
    parser.add_argument(
        "--verify", action="store_true",
        help="after backfill, check ledger replay == live report pool",
    )
    args = parser.parse_args()

    rows = build_history(PROJECT_ROOT)
    adds = sum(1 for r in rows if r["action"] == "add")
    removes = len(rows) - adds
    print(f"[backfill] derived {len(rows)} rows from git history "
          f"({adds} add / {removes} remove)")

    if args.dry_run:
        for r in rows:
            vol = f" vol={r['avg_daily_posts']}" if r["avg_daily_posts"] is not None else ""
            print(f"  {r['effective_date']} {r['action']:6s} {r['stock_code']}{vol} [{r['config_version']}]")
        return 0

    dbmod.init_db(args.db)  # guarantees pool_history schema + dead-table migrations
    conn = sqlite3.connect(args.db)
    try:
        inserted = insert_rows(conn, rows)
    finally:
        conn.close()
    print(f"[backfill] inserted {inserted} new rows "
          f"(existing rows untouched — idempotent)")

    if args.verify:
        ok, ledger_only, live_only = verify(args.db, PROJECT_ROOT)
        if ok:
            print("[verify] OK — ledger replay == live report pool")
        else:
            print(f"[verify] MISMATCH — ledger-only: {sorted(ledger_only)}, "
                  f"live-only: {sorted(live_only)}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
