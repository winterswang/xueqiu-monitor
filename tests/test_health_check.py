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
