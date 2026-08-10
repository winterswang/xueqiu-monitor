"""Tests for db.py fixes from code review.

Task 1: logger not defined in db.py (line 205)
Task 2: connection leak in with _connect() blocks
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class TestDbLoggerDefined:
    """Task 1: db.py:205 referenced `logger` but it was never defined."""

    def test_db_module_has_logger_attribute(self):
        """db module should expose a `logger` attribute."""
        from src import db as db_mod
        assert hasattr(db_mod, "logger"), "db.py should define module-level logger"

    def test_insert_alerts_batch_failure_does_not_nameerror(self, tmp_path):
        """insert_alerts_batch except path must not raise NameError.

        Before fix: line 205 `logger.warning(...)` → NameError because no
        `import logging` / no `logger = ...` at module top.
        """
        from src import db as db_mod
        from src.models import ChangeAlert

        db_path = str(tmp_path / "test.db")
        db_mod.init_db(db_path)

        # Malformed alert that will cause INSERT to fail (missing required field
        # would be ideal, but ChangeAlert requires fields. Instead close the
        # connection behind db's back to force OperationalError).
        alert = ChangeAlert(
            stock_code="TEST",
            alert_type="test",
            z_score=1.0,
            magnitude=0.0,
            detail={},
            priority="P2",
        )

        # Corrupt the DB path so insert fails → triggers except branch
        bad_path = str(tmp_path / "nonexistent.db")

        # Should NOT raise NameError; should return [None] gracefully
        result = db_mod.insert_alerts_batch(bad_path, [alert])
        assert result == [None], f"Failed insert should return [None], got {result}"


class TestDbConnectionClose:
    """Task 4: with _connect() leaks connections (sqlite3 __exit__ commits but
    does not close). After fix, connections should be closed after with-block.
    """

    def test_connection_closed_after_with_block(self, tmp_path):
        """_connect should return a wrapper whose __exit__ closes the connection.

        Raw sqlite3.Connection.__exit__ only commits — it does NOT close.
        We verify by calling _connect directly and checking the connection
        is closed after the with-block.
        """
        from src import db as db_mod

        db_path = str(tmp_path / "test.db")
        db_mod.init_db(db_path)

        with db_mod._connect(db_path) as conn:
            conn.execute("SELECT 1")

        # After with-block: connection should be closed.
        # Check via the wrapper — if it exposes the underlying connection,
        # that connection should reject execute() with ProgrammingError.
        import sqlite3
        underlying = getattr(conn, '_conn', conn)
        try:
            underlying.execute("SELECT 1")
            closed = False
        except sqlite3.ProgrammingError:
            closed = True
        except Exception:
            # Proxy may already have detached
            closed = True

        assert closed, "Connection should be closed after with-block exits"
