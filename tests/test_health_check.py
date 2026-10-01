"""Unit tests for scripts/health_check.py — data freshness sentinel (v0.7 F2).

Verifies the three semantic checks added in v0.7:
- crawl_freshness: last_crawl_time > 48h → FAIL when pipeline broadly stalled
- time_parse_rate: post time parse failure > 30% → FAIL (8/8 accident signature)
- post_freshness: > 50% posts older than 7d → WARN

crawl_freshness scope = whitelist union of etc/config*.json when configs are
readable (v0.8.4: decommissioned meta rows no longer warn); falls back to the
full meta table in bare environments (tmp roots without etc/).

Runs against a temp project root so the real monitor.db is never touched.
"""

import json
import datetime
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent))  # project root for `from src import ...`

import health_check


@pytest.fixture
def fake_project(tmp_path: Path):
    """Build a temp project root with a monitor.db seeded for tests."""
    db = tmp_path / "data" / "monitor.db"
    db.parent.mkdir(parents=True)
    import sqlite3

    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE crawl_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, stock_code TEXT,
            crawl_time REAL, posts_count INTEGER, posts_data TEXT,
            sentiment_avg REAL, status TEXT
        );
        CREATE TABLE xueqiu_monitor_meta (
            stock_code TEXT UNIQUE, last_crawl_time REAL, last_post_time REAL
        );
        """
    )
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "cron.log").write_text(
        "[SUMMARY] stocks=1/1 success", encoding="utf-8"
    )
    (tmp_path / "data" / "watchlist.json").write_text("[]", encoding="utf-8")
    conn.commit()
    return conn, tmp_path


def _run_check(tmp_path: Path) -> dict:
    """Run health_check.check() against a fake project root, return parsed report."""
    with mock.patch.object(health_check, "PROJECT_ROOT", tmp_path), \
         mock.patch.object(health_check, "LOG_DIR", tmp_path / "logs"), \
         mock.patch.object(health_check, "WATCHLIST_PATH", tmp_path / "data" / "watchlist.json"):
        import io
        from contextlib import redirect_stdout

        out = io.StringIO()
        with redirect_stdout(out):
            with pytest.raises(SystemExit):
                health_check.check()
        return json.loads(out.getvalue())


def test_latest_pipeline_window_uses_run_marker_and_summary():
    """Source-failure window must end at SUMMARY, excluding later manual smoke tests."""
    text = "\n".join([
        "2026-10-01 13:00:00 [INFO] src.crawler: 从 morning-brief 加载 69 只自选股",
        "2026-10-01 13:11:20 [INFO] __main__: [SUMMARY] stocks=7/7 success=7",
        "2026-10-01 19:00:00 [INFO] manual smoke test",
    ])
    assert health_check._latest_pipeline_window(text) == (
        datetime.datetime(2026, 10, 1, 13, 0, 0),
        datetime.datetime(2026, 10, 1, 13, 11, 20),
    )


def test_source_failures_before_latest_run_are_ignored(tmp_path):
    """A morning failure must not keep the afternoon health check warned."""
    log = tmp_path / "source_failures.jsonl"
    records = [
        {"ts": "2026-10-01T11:00:00", "source": "news", "reason": "风控验证页"},
        {"ts": "2026-10-01T13:01:00", "source": "news", "reason": "风控验证页"},
    ]
    log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")

    rows = health_check._read_source_failures_in_window(
        log,
        (
            datetime.datetime(2026, 10, 1, 13, 0, 0),
            datetime.datetime(2026, 10, 1, 13, 11, 20),
        ),
    )

    assert len(rows) == 1
    assert rows[0]["ts"] == "2026-10-01T13:01:00"


def test_source_failure_window_falls_back_to_first_and_last_timestamps():
    text = "2026-10-01 13:00:00 [INFO] legacy startup\n2026-10-01 13:05:00 [INFO] done"
    assert health_check._latest_pipeline_window(text) == (
        datetime.datetime(2026, 10, 1, 13, 0, 0),
        datetime.datetime(2026, 10, 1, 13, 5, 0),
    )


def test_no_window_falls_back_to_today_not_empty(tmp_path):
    """No pipeline log → fall back to today's records (old behaviour).

    Returning [] would report "no source failures" with no evidence behind it —
    exactly the silent-OK mode this check exists to catch.
    """
    log = tmp_path / "source_failures.jsonl"
    today = datetime.date.today().isoformat()
    old = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
    records = [
        {"ts": "%sT09:00:00" % today, "source": "news", "reason": "风控验证页"},
        {"ts": "%sT09:00:00" % old, "source": "news", "reason": "风控验证页"},
    ]
    log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")

    rows = health_check._read_source_failures_in_window(log, (None, None))

    assert len(rows) == 1
    assert rows[0]["ts"].startswith(today)


def test_no_latest_log_does_not_crash(tmp_path):
    """_get_latest_log() can return None; source_failures must not NameError."""
    assert health_check._latest_pipeline_window("") == (None, None)


def test_no_pipeline_log_does_not_report_read_failure(fake_project):
    """Regression: source_window used to be bound only inside `if log:`.

    With no pipeline log, section 3.5 raised NameError; the blanket except turned
    that into a permanent WARN "读取失败: ..." on every run.
    """
    conn, tmp_path = fake_project
    report = _run_check(tmp_path)
    checks = {c["check"]: c for c in report["checks"]}
    assert "source_failures" in checks
    assert not checks["source_failures"]["detail"].startswith("读取失败"), checks["source_failures"]


def test_fresh_db_status_ok(fake_project):
    conn, tmp_path = fake_project
    now = time.time()
    conn.execute(
        "INSERT INTO xueqiu_monitor_meta VALUES ('AAA.HK', ?, 0)", (now - 3600,)
    )
    posts = json.dumps(
        [{"time": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.localtime(now - 600))}]
    )
    conn.execute(
        "INSERT INTO crawl_snapshots VALUES (1, 'AAA.HK', ?, 1, ?, 0.1, 'success')",
        (now - 3600, posts),
    )
    conn.commit()
    report = _run_check(tmp_path)
    checks = {c["check"]: c["status"] for c in report["checks"]}
    assert checks["crawl_freshness"] == "ok"
    assert checks["time_parse_rate"] == "ok"
    assert checks["post_freshness"] == "ok"
    assert report["status"] == "ok"


def test_stale_crawl_time_fails(fake_project):
    conn, tmp_path = fake_project
    now = time.time()
    # Both stocks stale 5 days → pipeline broadly stalled → FAIL
    for sc in ("AAA.HK", "BBB.US"):
        conn.execute(
            "INSERT INTO xueqiu_monitor_meta VALUES (?, ?, 0)", (sc, now - 5 * 86400)
        )
    posts = json.dumps([{"time": "2026-08-12T04:57:37.000Z"}])
    conn.execute(
        "INSERT INTO crawl_snapshots VALUES (1, 'AAA.HK', ?, 1, ?, 0.1, 'success')",
        (now - 3600, posts),
    )
    conn.commit()
    report = _run_check(tmp_path)
    checks = {c["check"]: c["status"] for c in report["checks"]}
    assert checks["crawl_freshness"] == "fail"
    assert "stale_last_crawl_time" in report["errors"]
    assert report["status"] == "fail"


def test_legacy_stale_stock_is_warn_not_fail(fake_project):
    """A few decommissioned rows must WARN, not FAIL (no false alarm)."""
    conn, tmp_path = fake_project
    now = time.time()
    # 1 fresh + 1 stale → majority fresh → WARN
    conn.execute("INSERT INTO xueqiu_monitor_meta VALUES ('AAA.HK', ?, 0)", (now - 3600,))
    conn.execute("INSERT INTO xueqiu_monitor_meta VALUES ('BBB.US', ?, 0)", (now - 5 * 86400,))
    posts = json.dumps([{"time": "2026-08-12T04:57:37.000Z"}])
    conn.execute(
        "INSERT INTO crawl_snapshots VALUES (1, 'AAA.HK', ?, 1, ?, 0.1, 'success')",
        (now - 3600, posts),
    )
    conn.commit()
    report = _run_check(tmp_path)
    checks = {c["check"]: c["status"] for c in report["checks"]}
    assert checks["crawl_freshness"] == "warn"
    assert "stale_last_crawl_time" not in report["errors"]
    assert report["status"] == "ok"


def test_high_time_parse_failure_fails(fake_project):
    """Garbage time fields (>30% unparseable) trigger FAIL — 8/8 signature."""
    conn, tmp_path = fake_project
    now = time.time()
    conn.execute("INSERT INTO xueqiu_monitor_meta VALUES ('AAA.HK', ?, 0)", (now - 3600,))
    posts = json.dumps([{"time": f"garbage-{i}"} for i in range(10)])
    conn.execute(
        "INSERT INTO crawl_snapshots VALUES (1, 'AAA.HK', ?, 10, ?, 0.1, 'success')",
        (now - 3600, posts),
    )
    conn.commit()
    report = _run_check(tmp_path)
    checks = {c["check"]: c["status"] for c in report["checks"]}
    assert checks["time_parse_rate"] == "fail"
    assert "high_time_parse_failure" in report["errors"]
    assert report["status"] == "fail"


# ── v0.8.4: crawl_freshness scopes to the whitelist union, not the full table ──


@pytest.fixture
def with_config(tmp_path: Path):
    """Make _load_monitor_whitelist() resolve: tmp etc/ with one group config."""
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "config.json").write_text(
        json.dumps({"crawler": {"whitelist": ["AAA.HK", "ZZZ.US"]}}), encoding="utf-8"
    )
    return tmp_path


def test_decommissioned_meta_rows_do_not_warn(fake_project, with_config):
    """Stale rows for stocks outside the whitelist are invisible to the check.

    This is the 2026-08-25 incident: 4 legacy rows (06-08) kept WARNing forever
    because the check scanned the full meta table."""
    conn, tmp_path = fake_project
    now = time.time()
    conn.execute("INSERT INTO xueqiu_monitor_meta VALUES ('AAA.HK', ?, 0)", (now - 3600,))
    # 3 decommissioned stocks — stale, but outside the whitelist union
    for sc in ("BBB.US", "CCC.SZ", "DDD.HK"):
        conn.execute("INSERT INTO xueqiu_monitor_meta VALUES (?, ?, 0)", (sc, now - 60 * 86400))
    posts = json.dumps([{"time": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.localtime(now - 600))}])
    conn.execute(
        "INSERT INTO crawl_snapshots VALUES (1, 'AAA.HK', ?, 1, ?, 0.1, 'success')",
        (now - 3600, posts),
    )
    conn.commit()
    report = _run_check(tmp_path)
    checks = {c["check"]: c["status"] for c in report["checks"]}
    assert checks["crawl_freshness"] == "ok"
    detail = next(c["detail"] for c in report["checks"] if c["check"] == "crawl_freshness")
    assert "1 whitelisted" in detail
    assert report["status"] == "ok"


def test_whitelisted_stale_stock_still_warns(fake_project, with_config):
    """The fix must not mask real signals: a stale IN-whitelist stock still WARNs
    (single group's cron failing while others run)."""
    conn, tmp_path = fake_project
    now = time.time()
    conn.execute("INSERT INTO xueqiu_monitor_meta VALUES ('AAA.HK', ?, 0)", (now - 5 * 86400,))
    conn.execute("INSERT INTO xueqiu_monitor_meta VALUES ('ZZZ.US', ?, 0)", (now - 3600,))
    posts = json.dumps([{"time": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.localtime(now - 600))}])
    conn.execute(
        "INSERT INTO crawl_snapshots VALUES (1, 'ZZZ.US', ?, 1, ?, 0.1, 'success')",
        (now - 3600, posts),
    )
    conn.commit()
    report = _run_check(tmp_path)
    checks = {c["check"]: c["status"] for c in report["checks"]}
    assert checks["crawl_freshness"] == "warn"
    detail = next(c["detail"] for c in report["checks"] if c["check"] == "crawl_freshness")
    assert "AAA.HK" in detail
    assert report["status"] == "ok"
