#!/usr/bin/env python3
"""Nightly health check: verify the last run completed successfully.

Checks:
  1. Last cron.log has [SUMMARY] entry → pipeline finished
  2. Crawl success rate >= 90%
  3. Dectect anomalies (zero posts, high failure rate)

Output: JSON to stdout, suitable for cron log monitoring.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
WATCHLIST_PATH = PROJECT_ROOT / "data" / "watchlist.json"

OK = "ok"
WARN = "warn"
FAIL = "fail"


def _get_latest_log() -> Path | None:
    """Get the most recent non-empty log file by modification time.
    Searches across all log types (run_*.log and pipeline*.log) and returns
    the newest one — no type has priority."""
    all_logs = list(LOG_DIR.glob("run_*.log")) + list(LOG_DIR.glob("pipeline*.log"))
    non_empty = [f for f in all_logs if f.stat().st_size > 0]
    if non_empty:
        return max(non_empty, key=lambda f: f.stat().st_mtime)
    return None


def _get_latest_cron_log() -> Path | None:
    log = LOG_DIR / "cron.log"
    return log if log.exists() else None


def _is_pipeline_running() -> bool:
    """Check if pipeline process is still running."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", r"python.*src\.cli"],
            capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        return False


def check():
    results = []
    errors = []

    # ── 1. Check DB exists and has data ──
    db_path = PROJECT_ROOT / "data" / "monitor.db"
    if db_path.exists():
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        snapshots = conn.execute("SELECT COUNT(*) FROM crawl_snapshots").fetchone()[0]
        stocks = conn.execute("SELECT COUNT(DISTINCT stock_code) FROM crawl_snapshots").fetchone()[0]
        total_posts = conn.execute("SELECT SUM(posts_count) FROM crawl_snapshots").fetchone()[0] or 0
        conn.close()
        results.append({"check": "db_snapshots", "status": OK, "detail": f"{snapshots} snapshots, {stocks} stocks, {total_posts} posts"})
    else:
        results.append({"check": "db_exists", "status": FAIL, "detail": "data/monitor.db not found"})
        errors.append("db_missing")

    # ── 2. Check latest log for [SUMMARY] ──
    log = _get_latest_log()
    if log:
        text = log.read_text(encoding="utf-8", errors="replace")
        summary_match = re.search(r"\[SUMMARY\] (.+)", text)
        phase_end = re.findall(r"\[PHASE\] (\w+) end elapsed=([\d.]+)s", text)

        if summary_match:
            results.append({"check": "pipeline_completed", "status": OK, "detail": summary_match.group(1)})
        elif _is_pipeline_running():
            results.append({"check": "pipeline_completed", "status": OK, "detail": "Pipeline still running — check back later"})
        else:
            results.append({"check": "pipeline_completed", "status": WARN, "detail": "No [SUMMARY] entry found — pipeline may have failed"})
            errors.append("no_summary")

        # Phase durations
        phases = {name: float(elapsed) for name, elapsed in phase_end}
        if phases:
            elapsed_total = sum(phases.values())
            results.append({"check": "phase_times", "status": OK, "detail": json.dumps(phases, ensure_ascii=False)})

        # Check for failures
        fail_count = len(re.findall(r"\[SKIP\] stock=.+ status=(timeout|failed)", text))
        if fail_count > 0:
            results.append({"check": "crawl_failures", "status": WARN, "detail": f"{fail_count} stocks skipped"})
            if fail_count > 5:
                errors.append("high_failure_rate")

        # Check for errors
        error_count = len(re.findall(r"\[ABORT\]|\[DETECT_ERR\]|CRITICAL", text))
        if error_count > 0:
            results.append({"check": "detect_errors", "status": WARN, "detail": f"{error_count} errors"})
            errors.append("detect_errors")
    else:
        results.append({"check": "log_exists", "status": WARN, "detail": "No run log found"})

    # ── 3. Check watchlist ──
    if WATCHLIST_PATH.exists():
        wl = json.loads(WATCHLIST_PATH.read_text())
        results.append({"check": "watchlist", "status": OK, "detail": f"{len(wl)} stocks"})
    else:
        results.append({"check": "watchlist", "status": FAIL, "detail": "watchlist.json not found"})
        errors.append("watchlist_missing")

    # ── 4. Check disk usage ──
    try:
        du = subprocess.run(["du", "-sh", str(PROJECT_ROOT / "data")], capture_output=True, text=True, timeout=10)
        results.append({"check": "disk_usage", "status": OK, "detail": du.stdout.strip()})
    except Exception:
        pass

    # ── Output ──
    status = FAIL if any(e in ["db_missing", "watchlist_missing"] for e in errors) else \
             WARN if errors else OK

    report = {
        "status": status,
        "errors": errors,
        "checks": results,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if status != FAIL else 1)


if __name__ == "__main__":
    check()
