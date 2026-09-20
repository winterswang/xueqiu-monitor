"""v0.7.5 fixes: thermometer latest-snapshot, interaction-weighted sentiment,
engagement-first post sort. Independent of the legacy
test_report_generator.py assertions on old sort order."""

import json
import time
from pathlib import Path

import pytest

from src import report_generator as rg


@pytest.fixture
def tmp_db():
    """Create a temporary DB for each test (same helper as legacy tests)."""
    import tempfile
    import sqlite3
    from src import db as dbmod
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "test.db"
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        dbmod.init_db(str(path))
        yield type("Tmp", (), {"path": path, "conn": conn, "date_str": time.strftime("%Y-%m-%d")})()
        conn.close()


class TestEngagementFirstSort:
    """fetch_stock_posts must sort by engagement desc first (v0.7.5)."""

    def test_high_engagement_first_even_if_older(self, tmp_db):
        posts = [
            {"title": "较早的高互动帖", "content": "内容内容内容内容", "time": "3小时前",
             "like_count": 100},
            {"title": "最新的零互动帖", "content": "内容内容内容内容", "time": "5分钟前",
             "like_count": 0},
        ]
        insert_snapshot(tmp_db, "TEST.HK", posts)
        result = rg.fetch_stock_posts(str(tmp_db.path), "TEST.HK", tmp_db.date_str, min_length=5)
        assert result[0]["title"] == "较早的高互动帖"
        assert result[1]["title"] == "最新的零互动帖"

    def test_engagement_tie_breaks_by_time(self, tmp_db):
        posts = [
            {"title": "较早的同互动帖", "content": "内容内容内容内容", "time": "3小时前",
             "like_count": 5},
            {"title": "较新的同互动帖", "content": "内容内容内容内容", "time": "5分钟前",
             "like_count": 5},
        ]
        insert_snapshot(tmp_db, "TEST.HK", posts)
        result = rg.fetch_stock_posts(str(tmp_db.path), "TEST.HK", tmp_db.date_str, min_length=5)
        assert result[0]["title"] == "较新的同互动帖"
        assert result[1]["title"] == "较早的同互动帖"


class TestThermometerLatestSnapshot:
    """fetch_market_thermometer must use the LATEST snapshot per stock.

    Regression for the GROUP BY bug: with two snapshots on the same day,
    the old SQL picked an arbitrary row (PDD 6-08 showed 0 posts while
    the latest snapshot had 97).
    """

    def test_uses_latest_snapshot_when_multiple(self, tmp_db):
        now = int(time.time())
        # Older snapshot: 0 posts
        tmp_db.conn.execute(
            "INSERT INTO crawl_snapshots (stock_code, crawl_time, posts_count, posts_data, sentiment_avg, status) VALUES (?, ?, 0, '[]', 0.0, 'success')"
            , ("TEST.HK", now - 3600))
        # Newer snapshot: 5 posts, sentiment 0.3
        posts2 = [{"title": f"p{i}", "content": "c", "sentiment_score": 0.3, "like_count": 1} for i in range(5)]
        tmp_db.conn.execute(
            "INSERT INTO crawl_snapshots (stock_code, crawl_time, posts_count, posts_data, sentiment_avg, status) VALUES (?, ?, 5, ?, 0.3, 'success')"
            , ("TEST.HK", now, json.dumps(posts2)))
        tmp_db.conn.commit()
        result = rg.fetch_market_thermometer(str(tmp_db.path), tmp_db.date_str)
        row = [r for r in result if r["stock_code"] == "TEST.HK"]
        assert row, "TEST.HK missing from thermometer"
        assert row[0]["posts"] == 5
        assert row[0]["sentiment"] == 0.3


def insert_snapshot(tmp_db, stock_code, posts):
    tmp_db.conn.execute(
        "INSERT INTO crawl_snapshots (stock_code, crawl_time, posts_count, posts_data, sentiment_avg, status) VALUES (?, ?, ?, ?, 0.0, 'success')"
        , (stock_code, int(time.time()), len(posts), json.dumps(posts)))
    tmp_db.conn.commit()
