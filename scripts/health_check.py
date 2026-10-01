#!/usr/bin/env python3
"""Nightly health check: verify the last run completed successfully.

Checks:
  1. Last cron.log has [SUMMARY] entry → pipeline finished
  2. Crawl success rate >= 90%
  3. Dectect anomalies (zero posts, high failure rate)

Output: JSON to stdout, suitable for cron log monitoring.

"""

from __future__ import annotations

import datetime
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


def _load_monitor_whitelist() -> set[str] | None:
    """Union of crawler.whitelist across all group configs (etc/config*.json).

    This is the set of stocks the pipeline is *supposed* to crawl. meta rows
    for decommissioned stocks are leftovers, not health signals. Returns None
    when no config file is readable, so the caller falls back to a full-table
    scan (missing-config environments still get checked)."""
    union: set[str] = set()
    found = False
    for cfg_path in sorted((PROJECT_ROOT / "etc").glob("config*.json")):
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        found = True
        union.update(cfg.get("crawler", {}).get("whitelist", []) or [])
    return union if found else None


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


def _latest_pipeline_window(
    log_text: str,
) -> tuple[datetime.datetime | None, datetime.datetime | None]:
    """Return the newest pipeline's start and completion timestamps.

    Source failures are only meaningful when they occur inside this window.
    Using "since start" alone would also count manual smoke-test failures written
    after the scheduled pipeline had already finished.
    """
    start = None
    end = None
    timestamps: list[datetime.datetime] = []
    timestamp_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    for line in log_text.splitlines():
        match = timestamp_pattern.match(line)
        if not match:
            continue
        timestamp = datetime.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
        timestamps.append(timestamp)
        if "从 morning-brief 加载" in line:
            start = timestamp
            end = None
        elif "[SUMMARY]" in line and start is not None and end is None:
            end = timestamp

    if start is not None:
        return start, end

    # Legacy fallback: treat the first and last timestamps as one run window.
    # Note the pattern is anchored with ^ and has no MULTILINE, so it cannot be
    # run over the whole text — every timestamp must be collected line by line.
    if not timestamps:
        return None, None
    return timestamps[0], timestamps[-1]


def _read_source_failures_in_window(
    fail_log: Path,
    window: tuple[datetime.datetime | None, datetime.datetime | None],
) -> list[dict]:
    """Read source failures that occurred inside the latest pipeline run.

    When there is no usable window (no pipeline log yet), fall back to today's
    records — the previous behaviour. Returning [] here would report "no source
    failures" without any evidence, which is the silent-OK failure mode this
    whole check exists to prevent.
    """
    start, end = window
    today = datetime.date.today().isoformat()
    rows: list[dict] = []
    for line in fail_log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            ts = datetime.datetime.fromisoformat(str(rec.get("ts", "")))
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        if start is not None:
            if ts < start or (end is not None and ts > end):
                continue
        elif not str(rec.get("ts", "")).startswith(today):
            continue
        rows.append(rec)
    return rows


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
    # 默认窗口 (None, None)：没有可用日志时，下面 3.5 会退回老的「当天」口径。
    # 必须先赋值 —— _get_latest_log() 可能返回 None，否则 3.5 会 NameError。
    source_window: tuple[datetime.datetime | None, datetime.datetime | None] = (None, None)
    if log:
        text = log.read_text(encoding="utf-8", errors="replace")
        source_window = _latest_pipeline_window(text)
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

    # ── 3.5 按信息源检查抓取失败（news / notices / discussions / replies / stock）──
    # 之前只统计讨论数，资讯整源挂掉也报 healthy；这里把源级失败显式列出来。
    try:
        fail_log = LOG_DIR / "source_failures.jsonl"
        if fail_log.exists():
            rows = _read_source_failures_in_window(fail_log, source_window)
            if rows:
                agg = {}
                for rec in rows:
                    key = (rec.get("source") or "?", rec.get("reason") or "其他")
                    agg[key] = agg.get(key, 0) + 1
                detail = "; ".join(
                    "%s: %d 次（%s）" % (src_name, cnt, why)
                    for (src_name, why), cnt in sorted(agg.items(), key=lambda kv: -kv[1])
                )
                results.append({"check": "source_failures", "status": WARN,
                                "detail": "%d 次失败 → %s" % (len(rows), detail)})
                errors.append("source_failures")
            else:
                start, end = source_window
                if start is not None:
                    span = start.strftime("%Y-%m-%d %H:%M:%S")
                    if end is not None:
                        span += " → " + end.strftime("%H:%M:%S")
                    detail = "最近一轮无源级失败（窗口 %s）" % span
                else:
                    # 没有可用日志 → 用的是「当天」兜底口径，如实说明
                    detail = "今日无源级失败（未能确定 pipeline 窗口）"
                results.append({"check": "source_failures", "status": OK, "detail": detail})
        else:
            results.append({"check": "source_failures", "status": OK, "detail": "无失败记录文件"})
    except Exception as exc:
        results.append({"check": "source_failures", "status": WARN, "detail": "读取失败: %s" % exc})
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
            #     Scope = monitor whitelist union (what the pipeline is supposed
            #     to crawl), NOT the full meta table — rows for decommissioned
            #     stocks are leftover bookkeeping, not health signals. Falls
            #     back to the full table only when no config is readable.
            rows = conn.execute(
                "SELECT stock_code, last_crawl_time FROM xueqiu_monitor_meta"
            ).fetchall()
            whitelist = _load_monitor_whitelist()
            if whitelist is not None:
                rows = [r for r in rows if r[0] in whitelist]
            if rows:
                now_48h = now - 48 * 3600
                stale = [
                    (sc, lct) for sc, lct in rows
                    if lct and (now - lct) > 48 * 3600
                ]
                fresh = [lct for sc, lct in rows if lct and lct >= now_48h]
                # FAIL only when the pipeline is broadly stalled: either NO stock
                # crawled in 48h (the 8/8 accident signature) or a majority stale
                # (whitelist rotated out en masse). A few stale in-whitelist
                # stocks (e.g. one group's cron failing while others run)
                # → WARN, so normal operation never false-alarms.
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
                        "detail": f"{len(stale)}/{len(rows)} whitelist stocks not crawled in 48h (stalest: {worst[0]}, last={_time.strftime('%m-%d %H:%M', _time.localtime(worst[1]))})",
                    })
                else:
                    results.append({
                        "check": "crawl_freshness", "status": OK,
                        "detail": f"all {len(rows)} whitelisted stocks crawled within 48h",
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
