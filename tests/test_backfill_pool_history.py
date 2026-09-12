"""Unit tests for scripts/backfill_pool_history.py (v0.9 T1).

Strategy: build a throwaway git repo in tmp, craft config.report.json
history (baseline → drop/add → drop/add), run the backfill functions
against it, and assert the ledger replays to the live pool.

Covers:
- initial baseline stocks → add rows
- subsequent transitions → remove/add rows with date & version
- idempotency: second run inserts 0 rows (UNIQUE constraint)
- replay(final rows) == live report pool at HEAD
- effective-date override applies (commit date ≠ effective date)
- volume snapshot attached to removes, None for adds
- dead-table migration drops xueqiu_monitor_meta_backup_0825
"""

import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent))

import backfill_pool_history as bph  # noqa: E402
from src import db as dbmod  # noqa: E402


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=check
    )


def _commit_report(root: Path, stocks: dict, message: str) -> str:
    """Write etc/config.report.json with given stocks and commit it."""
    cfg_dir = root / "etc"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "config.report.json").write_text(
        json.dumps({"stocks": stocks}, ensure_ascii=False, indent=2)
    )
    _git(root, "add", "etc/config.report.json")
    _git(root, "commit", "-m", message, "--allow-empty" if not stocks else "--no-verify")
    out = _git(root, "rev-parse", "--short", "HEAD")
    return out.stdout.strip()


@pytest.fixture
def repo():
    """Throwaway git repo with 3-commit rotation history.

    baseline {A,B,C} → v2 drop B add D → v3 drop C add E
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "t@t")
        _git(root, "config", "user.name", "t")
        _commit_report(root, {"A.US": {}, "B.US": {}, "C.US": {}}, "baseline 3 stocks")
        _commit_report(root, {"A.US": {}, "C.US": {}, "D.US": {}}, "v2: drop B, add D")
        _commit_report(root, {"A.US": {}, "D.US": {}, "E.US": {}}, "v3: drop C, add E")
        yield root


def test_diff_rows_transitions(repo):
    rows = bph.build_history(repo)
    # baseline 3 adds + v2 (1 rm + 1 add) + v3 (1 rm + 1 add) = 7 rows
    assert len(rows) == 7
    actions = [(r["effective_date"] and r["action"], r["stock_code"]) for r in rows]
    assert ("add", "A.US") in actions and ("add", "B.US") in actions and ("add", "C.US") in actions
    assert ("remove", "B.US") in actions and ("add", "D.US") in actions
    assert ("remove", "C.US") in actions and ("add", "E.US") in actions


def test_replay_equals_live_pool(repo):
    rows = bph.build_history(repo)
    assert bph.replay(rows) == {"A.US", "D.US", "E.US"}
    live = bph.report_stocks_at(repo, "HEAD")
    assert live == {"A.US", "D.US", "E.US"}


def test_replay_ordering_with_churn(repo):
    """A stock dropped then re-added later must end up IN the pool."""
    _commit_report(repo, {"A.US": {}, "D.US": {}, "E.US": {}, "C.US": {}}, "v4: re-add C")
    rows = bph.build_history(repo)
    assert "C.US" in bph.replay(rows)


def test_effective_date_override(repo, monkeypatch):
    """Override table maps commit → effective date (rotation done before commit)."""
    short = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
    monkeypatch.setattr(bph, "EFFECTIVE_DATE_OVERRIDES", {short: "2020-01-01"})
    rows = bph.build_history(repo)
    v3_rows = [r for r in rows if r["stock_code"] == "E.US"]
    assert v3_rows and v3_rows[0]["effective_date"] == "2020-01-01"


def test_volume_snapshot_on_remove_only(repo, monkeypatch):
    short = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
    monkeypatch.setattr(
        bph, "VOLUME_SNAPSHOT",
        {short: {"C.US": 7.9}},
    )
    rows = bph.build_history(repo)
    rm_c = [r for r in rows if r["stock_code"] == "C.US" and r["action"] == "remove"]
    add_e = [r for r in rows if r["stock_code"] == "E.US" and r["action"] == "add"]
    assert rm_c and rm_c[0]["avg_daily_posts"] == 7.9
    assert add_e and add_e[0]["avg_daily_posts"] is None


def test_insert_idempotent(repo):
    rows = bph.build_history(repo)
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "t.db")
        dbmod.init_db(db_path)
        conn = sqlite3.connect(db_path)
        try:
            first = bph.insert_rows(conn, rows)
            second = bph.insert_rows(conn, rows)
        finally:
            conn.close()
        assert first == 7
        assert second == 0  # UNIQUE swallowed everything on re-run


def test_schema_has_pool_history_with_unique():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "t.db")
        dbmod.init_db(db_path)
        conn = sqlite3.connect(db_path)
        try:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "pool_history" in tables
            dup = conn.execute(
                "INSERT INTO pool_history (stock_code, action, effective_date) "
                "VALUES ('X.US','add','2026-01-01')"
            )
            conn.commit()
            assert dup.rowcount == 1
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO pool_history (stock_code, action, effective_date) "
                    "VALUES ('X.US','add','2026-01-01')"
                )
        finally:
            conn.close()


def test_migration_drops_backup_0825():
    """init_db on a legacy DB carrying the backup table must drop it (idempotent)."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "legacy.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE xueqiu_monitor_meta_backup_0825 ("
            "stock_code TEXT, last_crawl_time REAL, last_post_time REAL)"
        )
        conn.execute(
            "INSERT INTO xueqiu_monitor_meta_backup_0825 VALUES ('002241.SZ', 1.0, 2.0)"
        )
        conn.commit()
        conn.close()
        dbmod.init_db(db_path)  # runs migrations incl. dead-table drop
        conn = sqlite3.connect(db_path)
        try:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "xueqiu_monitor_meta_backup_0825" not in tables
            assert "pool_history" in tables  # migration also provisions ledger
        finally:
            conn.close()


def test_real_repo_replay_if_available():
    """Smoke: on the real repo, derived history replays to the live 41 pool.

    Skipped automatically when the git history is unavailable (e.g. CI
    checkout without full clone).
    """
    real_root = SCRIPTS_DIR.parent
    try:
        rows = bph.build_history(real_root)
    except subprocess.CalledProcessError:
        pytest.skip("real repo git history unavailable")
    live = bph.report_stocks_at(real_root, "HEAD")
    assert bph.replay(rows) == live, "ledger replay must equal live pool"
    # v0.8.6 rotation: 9 removes + 20 adds on the effective date 2026-09-09
    v086 = [r for r in rows if r["config_version"] == "v0.8.6"]
    adds = sum(1 for r in v086 if r["action"] == "add")
    removes = sum(1 for r in v086 if r["action"] == "remove")
    assert (adds, removes) == (20, 9)
    # every v0.8.6 remove carries a volume snapshot from the commit message
    assert all(
        r["avg_daily_posts"] is not None
        for r in v086 if r["action"] == "remove"
    )
