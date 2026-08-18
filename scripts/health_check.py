#!/usr/bin/env python3
"""Nightly health check: verify the last run completed successfully.

Checks:
  1. Last cron.log has [SUMMARY] entry → pipeline finished
  2. Crawl success rate >= 90%
  3. Dectect anomalies (zero posts, high failure rate)

Output: JSON to stdout, suitable for cron log monitoring.

"""

from __future__ import annotations

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

    # ── 5. Data freshness sentinel (v0.7 F2) — semantic checks ──
    #    Catches silent degradation (e.g. 8/8 ISO-8601 time-format drift that
    #    went unnoticed for 5 days): stale last_crawl_time, high post-time parse
    #    failure rate, and stale-post pileup.
    db_path = PROJECT_ROOT / "data" / "monitor.db"
    if db_path.exists():
        import sqlite3 as _sqlite3
        import time as _time
        # Lazy import: crawler pulls xueqiu-analyzer path — keep module load light.
        sys.path.insert(0, str(PROJECT_ROOT))
        try:
            from src.crawler import _parse_post_time
        except Exception:
            _parse_post_time = None
        conn = _sqlite3.connect(str(db_path))
        try:
            now = _time.time()
            # 5a. last_crawl_time freshness: any stock not crawled for 48h → FAIL
            rows = conn.execute(
                "SELECT stock_code, last_crawl_time FROM xueqiu_monitor_meta"
            ).fetchall()
            if rows:
                now_48h = now - 48 * 3600
                stale = [
                    (sc, lct) for sc, lct in rows
                    if lct and (now - lct) > 48 * 3600
                ]
                fresh = [lct for sc, lct in rows if lct and lct >= now_48h]
                # FAIL only when the pipeline is broadly stalled: either NO stock
                # crawled in 48h (the 8/8 accident signature) or a majority stale
                # (watchlist rotated out en masse). A few legacy rows that were
                # decommissioned (removed from configs) are expected → WARN, so
                # normal operation never false-alarms.
                if not fresh or len(stale) > len(rows) / 2:
                    results.append({
                        "check": "crawl_freshness", "status": FAIL,
                        "detail": f"{len(stale)}/{len(rows)} stocks not crawled in 48h (freshest: {_time.strftime('%m-%d %H:%M', _time.localtime(max(fresh or [0])))})",
                    })
                    errors.append("stale_last_crawl_time")
                elif stale:
                    worst = max(stale, key=lambda x: x[1])
                    results.append({
                        "check": "crawl_freshness", "status": WARN,
                        "detail": f"{len(stale)}/{len(rows)} stocks not crawled in 48h (legacy: {worst[0]}, last={_time.strftime('%m-%d %H:%M', _time.localtime(worst[1]))})",
                    })
                else:
                    results.append({
                        "check": "crawl_freshness", "status": OK,
                        "detail": f"all {len(rows)} stocks crawled within 48h",
                    })
            else:
                results.append({
                    "check": "crawl_freshness", "status": WARN,
                    "detail": "xueqiu_monitor_meta empty — no crawl bookkeeping",
                })

            # 5b/5c. Post-time health on the latest snapshot per stock:
            #       parse-failure rate > 30% → FAIL (the 8/8 accident signature);
            #       > 50% posts older than 7 days → WARN (stale pileup).
            if _parse_post_time is not None:
                snap_rows = conn.execute(
                    """SELECT cs.stock_code, cs.posts_data, cs.crawl_time
                       FROM crawl_snapshots cs
                       WHERE cs.id IN (
                           SELECT MAX(id) FROM crawl_snapshots
                           WHERE status='success' AND posts_data IS NOT NULL
                           GROUP BY stock_code
                       )"""
                ).fetchall()
                total = fail_parse = old_posts = 0
                old_cutoff = now - 7 * 86400
                for _sc, posts_data, _ct in snap_rows:
                    try:
                        posts = json.loads(posts_data)
                    except Exception:
                        continue
                    for p in posts:
                        total += 1
                        ts = _parse_post_time(p.get("time", "") or "", now)
                        if ts <= 0:
                            fail_parse += 1
                        elif ts < old_cutoff:
                            old_posts += 1
                if total:
                    fail_rate = fail_parse / total
                    old_ratio = old_posts / total
                    if fail_rate > 0.30:
                        results.append({
                            "check": "time_parse_rate", "status": FAIL,
                            "detail": f"{fail_parse}/{total} posts time-parse failed ({fail_rate:.0%})",
                        })
                        errors.append("high_time_parse_failure")
                    else:
                        results.append({
                            "check": "time_parse_rate", "status": OK,
                            "detail": f"{fail_parse}/{total} posts time-parse failed ({fail_rate:.0%})",
                        })
                    if old_ratio > 0.50:
                        results.append({
                            "check": "post_freshness", "status": WARN,
                            "detail": f"{old_posts}/{total} posts older than 7d ({old_ratio:.0%})",
                        })
                    else:
                        results.append({
                            "check": "post_freshness", "status": OK,
                            "detail": f"{old_posts}/{total} posts older than 7d ({old_ratio:.0%})",
                        })
                else:
                    results.append({
                        "check": "time_parse_rate", "status": WARN,
                        "detail": "no posts_data to evaluate",
                    })
        finally:
            conn.close()

    # ── Output ──
    status = FAIL if any(e in ["db_missing", "watchlist_missing", "stale_last_crawl_time", "high_time_parse_failure"] for e in errors) else \
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
