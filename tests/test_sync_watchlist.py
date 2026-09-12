"""Unit tests for scripts/sync_watchlist.py check_pool_coverage (v0.9 T4).

Strategy: monkeypatch PROJECT_ROOT/MB_DB via module attributes to point at a
tmp directory with synthetic etc/config*.json + morning-brief.db, then call
check_pool_coverage() directly. fetch_watchlist is NOT tested here (it shells
out to the longbridge CLI — covered by manual runs).

Covers:
- full coverage → (True, counts message)
- pool drift (1 symbol not active) → (False, alert names the symbol)
- pool drift (symbol entirely absent from DB) → (False, alert)
- empty pool → (True, skipped)
- missing DB → (True, skipped note)
- bad JSON config → (True, soft-skip)
- config.report.json excluded from pool union
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import sync_watchlist as sw  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Synthetic project root with etc/ + morning-brief DB; returns helpers."""
    etc = tmp_path / "etc"
    etc.mkdir()
    mb_dir = tmp_path / "mb"
    mb_dir.mkdir()
    mb_db = mb_dir / "morning-brief.db"

    conn = sqlite3.connect(mb_db)
    conn.execute(
        "CREATE TABLE watchlist (stock_code TEXT PRIMARY KEY, is_active INTEGER)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(sw, "PROJECT_ROOT", tmp_path, raising=False)
    monkeypatch.setattr(sw, "MB_DB_DEFAULT", mb_db, raising=False)
    monkeypatch.delenv("MORNING_BRIEF_DB", raising=False)

    def add_config(name: str, whitelist: list[str]) -> None:
        (etc / name).write_text(
            json.dumps({"crawler": {"whitelist": whitelist}})
        )

    def set_active(codes: list[str]) -> None:
        conn = sqlite3.connect(mb_db)
        conn.execute("DELETE FROM watchlist")
        conn.executemany(
            "INSERT INTO watchlist (stock_code, is_active) VALUES (?, 1)",
            [(c,) for c in codes],
        )
        conn.commit()
        conn.close()

    return type("Env", (), {
        "add_config": staticmethod(add_config),
        "set_active": staticmethod(set_active),
        "mb_db": mb_db,
    })()


def test_full_coverage(env):
    env.add_config("config.g1.json", ["NVDA.US", "AAPL.US"])
    env.add_config("config.g2.json", ["TSLA.US"])
    env.add_config("config.report.json", ["GHOST.US"])  # excluded from union
    env.set_active(["NVDA.US", "AAPL.US", "TSLA.US", "EXTRA.US"])
    covered, msg = sw.check_pool_coverage()
    assert covered is True
    assert "3" in msg and "4" in msg


def test_drift_symbol_inactive(env):
    env.add_config("config.g1.json", ["NVDA.US", "AAPL.US"])
    env.set_active(["NVDA.US", "AAPL.US", "ORPHAN.US"])
    # ORPHAN active but NOT in pool → fine; make AAPL inactive instead:
    conn = sqlite3.connect(env.mb_db)
    conn.execute("UPDATE watchlist SET is_active = 0 WHERE stock_code = 'AAPL.US'")
    conn.commit()
    conn.close()
    covered, msg = sw.check_pool_coverage()
    assert covered is False
    assert "AAPL.US" in msg


def test_drift_symbol_absent(env):
    env.add_config("config.g1.json", ["NVDA.US", "MISSING.US"])
    env.set_active(["NVDA.US"])
    covered, msg = sw.check_pool_coverage()
    assert covered is False
    assert "MISSING.US" in msg


def test_empty_pool_skipped(env):
    covered, msg = sw.check_pool_coverage()
    assert covered is True
    assert "no monitor pool" in msg


def test_missing_db_skipped(env, monkeypatch):
    env.add_config("config.g1.json", ["NVDA.US"])
    monkeypatch.setattr(sw, "MB_DB_DEFAULT", env.mb_db.with_name("nope.db"), raising=False)
    covered, msg = sw.check_pool_coverage()
    assert covered is True
    assert "not found" in msg


def test_bad_json_soft_skip(env):
    (sw.PROJECT_ROOT / "etc" / "config.g1.json").write_text("{not json")
    covered, msg = sw.check_pool_coverage()
    assert covered is True
    assert "soft-skipped" in msg


def test_report_pool_excluded_from_union(env):
    # report pool alone (no group configs) → empty union → skipped
    env.add_config("config.report.json", ["GHOST.US"])
    covered, msg = sw.check_pool_coverage()
    assert covered is True
    assert "no monitor pool" in msg
