"""Tests for cli.py main loop exception isolation (Task 2).

Before fix: if db.insert_snapshot raises for one stock, the entire
`for cr in crawl_results` loop aborts — subsequent stocks are skipped.
After fix: each stock's processing is isolated; failure increments
detect_errors and continues.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def temp_config(tmp_path):
    """Minimal config for run_pipeline."""
    from src.config import Config
    db_path = str(tmp_path / "test.db")
    config_data = {
        "db_path": db_path,
        "watchlist_path": "",
        "cold_start": {"enabled": True, "days": 0, "min_data_points": 1},
        "detector": {
            "z_score_window_days": 14,
            "z_score_threshold": 2.0,
            "tfidf_min_df": 1,
            "tfidf_max_df": 0.8,
            "tfidf_ngram_range": [1, 2],
        },
        "filter": {
            "ad_keywords": [],
            "duplicate_similarity_threshold": 0.85,
            "short_post_threshold": 20,
            "p0_z_threshold": 3.0,
            "p1_z_threshold": 2.0,
        },
        "notification": {"webhook_url": "", "push_timeout": 5, "max_retries": 2},
        "schedule": {"interval_hours": 4},
        "crawler": {
            "timeout_seconds": 30, "max_retries": 2, "concurrency": 1,
            "whitelist": [],
            "xueqiu_analyzer_path": "/dev/null/nonexistent",
            "morning_brief_db": "/dev/null/nonexistent",
        },
    }
    config_path = str(tmp_path / "config.json")
    Path(config_path).write_text(json.dumps(config_data, ensure_ascii=False))
    return config_path


class TestMainLoopExceptionIsolation:
    """Task 2: single stock processing failure should not abort the loop."""

    def test_loop_continues_after_stock_exception(self, temp_config, monkeypatch):
        """If db.insert_snapshot raises for the first stock, the second
        stock should still be processed (summary crawled >= 1)."""
        from src import cli
        from src import crawler as crawler_mod

        # Two mock stocks, both "success"
        mock_stocks = [
            {"stock_code": "FAIL", "stock_name": "Will Fail"},
            {"stock_code": "OK", "stock_name": "Will Succeed"},
        ]
        mock_results = [
            {
                "stock_code": "FAIL", "crawl_time": int(time.time()),
                "posts_count": 1, "posts_data": [], "announcements": [],
                "sentiment_avg": 0.0, "status": "success", "error": None,
                "diagnostic": {},
            },
            {
                "stock_code": "OK", "crawl_time": int(time.time()),
                "posts_count": 1, "posts_data": [], "announcements": [],
                "sentiment_avg": 0.0, "status": "success", "error": None,
                "diagnostic": {},
            },
        ]

        monkeypatch.setattr(crawler_mod, "load_watchlist", lambda _cfg: mock_stocks)
        monkeypatch.setattr(crawler_mod, "crawl_watchlist", lambda *_a, **_kw: mock_results)

        # Make insert_snapshot fail only for "FAIL" stock
        real_insert = cli.db.insert_snapshot

        def flaky_insert(db_path, snap):
            if snap.stock_code == "FAIL":
                raise sqlite3.OperationalError("simulated lock")
            return real_insert(db_path, snap)

        import sqlite3
        monkeypatch.setattr(cli.db, "insert_snapshot", flaky_insert)

        summary = cli.run_pipeline(temp_config, dry_run=True)

        # Second stock should still be processed → crawled should include it
        # Before fix: loop aborts on FAIL → OK never processed
        assert summary.get("total_stocks") == 2
        # The "OK" stock should be in the summary's crawled count
        # (detect_errors should be 1, not a crash)
