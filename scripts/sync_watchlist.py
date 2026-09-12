#!/usr/bin/env python3
"""Sync watchlist from Longbridge to data/watchlist.json.

Thin wrapper around the `longbridge` CLI which manages OAuth + token refresh
internally. We just call `longbridge watchlist --format json`, flatten the
groups, and write the project-canonical schema {stock_code, stock_name}.

v0.9 T4: after syncing, verifies the monitor pool (etc/config*.json
whitelist union) is covered by the morning-brief DB active set — the real
crawl source (crawler.py loads morning-brief first, config whitelist is the
group slice). Prints a loud warning + non-zero hint when the pool drifts
out of coverage; exits 0 either way (sync itself succeeded — the drift is
the rotation tool's check #5 domain, this is the runtime early-warning).
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = PROJECT_ROOT / "data" / "watchlist.json"
MB_DB_DEFAULT = PROJECT_ROOT.parent / "morning-brief" / "data" / "morning-brief.db"


class WatchlistError(Exception):
    """Non-recoverable error during watchlist fetch."""


def fetch_watchlist() -> list[dict]:
    """Run `longbridge watchlist --format json` and flatten to [{stock_code, stock_name}]."""
    cli = shutil.which("longbridge")
    if not cli:
        raise WatchlistError(
            "未找到 longbridge CLI，请先 `cargo install --path .` 或 `brew install`"
        )

    try:
        result = subprocess.run(
            [cli, "watchlist", "--format", "json"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise WatchlistError("longbridge CLI 超时 (30s)")

    if result.returncode != 0:
        raise WatchlistError(
            f"longbridge CLI 失败 (exit={result.returncode}): {result.stderr.strip()[:200]}"
        )

    try:
        groups = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise WatchlistError("longbridge CLI 输出非 JSON 格式")

    if not isinstance(groups, list):
        raise WatchlistError(
            f"longbridge CLI 输出格式异常（期望数组，实际 {type(groups).__name__}）"
        )

    return [
        {
            "stock_code": sec.get("symbol", ""),
            "stock_name": (
                sec.get("name", "")
                or sec.get("name_cn", "")
                or sec.get("name_en", "")
            ),
        }
        for g in groups
        for sec in g.get("securities", [])
    ]


def check_pool_coverage() -> tuple[bool, str]:
    """Monitor pool ⊆ morning-brief active set? (v0.9 T4 runtime guard)

    Returns (covered, message). Soft-fails: unreadable configs / missing DB
    report as (True, note) — this is an early-warning, not a hard gate.
    """
    try:
        mb_db = Path(os.environ.get("MORNING_BRIEF_DB", str(MB_DB_DEFAULT)))
        pool: set[str] = set()
        for cfg_path in sorted((PROJECT_ROOT / "etc").glob("config*.json")):
            if cfg_path.name == "config.report.json":
                continue
            data = json.loads(cfg_path.read_text())
            pool.update(data.get("crawler", {}).get("whitelist", []) or [])
        if not pool:
            return True, "no monitor pool configured — coverage check skipped"
        if not mb_db.exists():
            return True, f"morning-brief DB not found ({mb_db}) — coverage check skipped"
        conn = sqlite3.connect(f"file:{mb_db}?mode=ro", uri=True)
        try:
            active = {
                r[0] for r in conn.execute(
                    "SELECT stock_code FROM watchlist WHERE is_active = 1"
                )
            }
        finally:
            conn.close()
        uncovered = pool - active
        if uncovered:
            return False, (
                f"⚠️ 监控池 {len(pool)} 只中 {len(uncovered)} 只不在 morning-brief active 集"
                f"（爬取真源）——这些股票将不被爬取: {sorted(uncovered)}。"
                "请跑 scripts/rotate_pool.py --check 定位，或在 morning-brief 中激活。"
            )
        return True, f"监控池 {len(pool)} 只全部被 morning-brief active 集覆盖（{len(active)} active）"
    except (json.JSONDecodeError, OSError, sqlite3.Error) as e:
        return True, f"coverage check soft-skipped: {e}"


def main() -> None:
    try:
        stocks = fetch_watchlist()
    except WatchlistError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)
    if not stocks:
        print("⚠️  watchlist 为空")
        sys.exit(1)

    # Deduplicate (preserve first-seen order)
    seen: set[str] = set()
    unique: list[dict] = []
    for s in stocks:
        if s["stock_code"] not in seen:
            seen.add(s["stock_code"])
            unique.append(s)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(unique, ensure_ascii=False, indent=2) + "\n")
    print(f"✅ 自选股同步完成: {len(unique)} 只 → {OUTPUT_PATH}")
    for s in unique[:20]:
        print(f"   {s['stock_code']:20s} {s['stock_name']}")
    if len(unique) > 20:
        print(f"   ... 等 {len(unique)} 只")

    covered, msg = check_pool_coverage()
    print(msg)
    if not covered:
        print("⚠️  [POOL_COVERAGE_ALERT] 监控池与爬取真源漂移 — 见上行详情", file=sys.stderr)


if __name__ == "__main__":
    main()
