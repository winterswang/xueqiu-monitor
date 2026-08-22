"""Announcement within-batch dedup regression tests.

Validates Stage 0 dedup: when the crawler returns duplicate announcement
titles (e.g. daily share-buyback reports appearing N times in the API
response), detect_new_announcement must only emit one alert per unique title.

Historical case: 2026-07-04 2400.HK (心动公司) — the same "翌日披露报表 -
[股份购回] 翌日披露报表" title appeared 17 times in curr_announcements,
producing 17 identical alerts (title_hash=b0b30aa8...) in a single run.
"""

import hashlib
import tempfile
import time
from pathlib import Path

from src import detector
from src import db
from src.models import ChangeAlert


class TestAnnouncementWithinBatchDedup:
    """Same-title duplicates within one crawl batch must collapse to 1 alert."""

    def test_identical_titles_collapse_to_one(self):
        """17 identical buyback reports → exactly 1 alert."""
        dup_title = "心动公司 翌日披露报表 - [股份购回] 翌日披露报表"
        curr_anns = [{"title": dup_title, "time": "2026-07-04", "notice_type": "公告"}] * 17
        prev_anns: list[dict] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_monitor.db")
            db.init_db(db_path)

            alerts = detector.detect_new_announcement(
                stock_code="2400.HK",
                curr_announcements=curr_anns,
                prev_announcements=prev_anns,
                db_path=db_path,
            )

        assert len(alerts) == 1, f"Expected 1 alert for 17 dups, got {len(alerts)}"
        assert alerts[0].detail["title"] == dup_title

    def test_distinct_titles_all_pass(self):
        """Different announcement titles should each get their own alert."""
        curr_anns = [
            {"title": f"公告标题_{i}", "time": "2026-07-04", "notice_type": "公告"}
            for i in range(5)
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_monitor.db")
            db.init_db(db_path)

            alerts = detector.detect_new_announcement(
                stock_code="TEST.HK",
                curr_announcements=curr_anns,
                prev_announcements=[],
                db_path=db_path,
            )

        assert len(alerts) == 5

        # All dedup_hashes unique
        hashes = [a.detail["dedup_hash"] for a in alerts]
        assert len(set(hashes)) == 5

    def test_partial_duplicates(self):
        """Mix of unique and duplicate titles: dedup only the dups."""
        curr_anns = [
            {"title": "独特公告A", "time": "", "notice_type": ""},
            {"title": "重复公告B", "time": "", "notice_type": ""},
            {"title": "重复公告B", "time": "", "notice_type": ""},
            {"title": "重复公告B", "time": "", "notice_type": ""},
            {"title": "独特公告C", "time": "", "notice_type": ""},
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_monitor.db")
            db.init_db(db_path)

            alerts = detector.detect_new_announcement(
                stock_code="TEST.HK",
                curr_announcements=curr_anns,
                prev_announcements=[],
                db_path=db_path,
            )

        assert len(alerts) == 3
        titles = [a.detail["title"] for a in alerts]
        assert "独特公告A" in titles
        assert "重复公告B" in titles
        assert "独特公告C" in titles

    def test_empty_announcements(self):
        """No announcements → no alerts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_monitor.db")
            db.init_db(db_path)

            alerts = detector.detect_new_announcement(
                stock_code="TEST.HK",
                curr_announcements=[],
                prev_announcements=[],
                db_path=db_path,
            )

        assert len(alerts) == 0

    def test_same_title_different_time_survives(self):
        """Same title with different ann times are distinct announcements.

        Real case: SPCX.US had 6 "财报披露" entries on different dates
        (06-27, 06-24, 06-22, 06-18, 06-16, 06-16) — all legitimate
        distinct announcements sharing a generic title. Dedup key must
        include ann time so these survive.
        """
        curr_anns = [
            {"title": "财报披露", "time": "06-30 19:45", "notice_type": ""},
            {"title": "财报披露", "time": "06-25 18:55", "notice_type": ""},
            {"title": "财报披露", "time": "06-12 04:15", "notice_type": ""},
            {"title": "财报披露", "time": "06-30 19:45", "notice_type": ""},  # true dup
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_monitor.db")
            db.init_db(db_path)

            alerts = detector.detect_new_announcement(
                stock_code="SPCX.US",
                curr_announcements=curr_anns,
                prev_announcements=[],
                db_path=db_path,
            )

        # 3 distinct dates + 1 exact dup collapsed = 3 alerts
        assert len(alerts) == 3
        # Same title but distinct dates → distinct dedup_hashes
        hashes = {a.detail["dedup_hash"] for a in alerts}
        assert len(hashes) == 3, "Same title on different dates must get distinct dedup hashes"


class TestAnnouncementDedupPersistence:
    """v0.8.1: DB-level dedup must use title+time identity (dedup_hash).

    The v0.8 unique index keyed on title-only (title_hash) permanently
    swallowed generic titles ("财报披露") that legitimately recur on
    different dates. These tests lock in the corrected behavior:
    - same title+time → deduped (permanent)
    - same title, different time → both persist (new announcement)
    """

    def _alert(self, title, time_str):
        import hashlib
        dedup_hash = hashlib.md5(f"{title}|{time_str}".encode()).hexdigest()
        return ChangeAlert(
            stock_code="SPCX.US",
            alert_type="new_announcement",
            alert_time=int(time.time()),
            z_score=0.0,
            magnitude=0.0,
            detail={"title": title, "dedup_hash": dedup_hash, "time": time_str},
            priority="P2",
        )

    def test_same_title_same_time_deduped(self):
        import tempfile
        from pathlib import Path
        from src import db
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "t.db")
            db.init_db(db_path)
            a = db.insert_alert(db_path, self._alert("财报披露", "06-30 19:45"))
            b = db.insert_alert(db_path, self._alert("财报披露", "06-30 19:45"))
            assert a > 0
            assert b == 0

    def test_same_title_different_time_both_persist(self):
        import tempfile
        from pathlib import Path
        from src import db
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "t.db")
            db.init_db(db_path)
            a = db.insert_alert(db_path, self._alert("财报披露", "06-30 19:45"))
            b = db.insert_alert(db_path, self._alert("财报披露", "06-25 18:55"))
            assert a > 0
            assert b > 0

    def test_legacy_title_hash_index_rebuilt(self):
        """Old title-only index is migrated to dedup_hash in place."""
        import tempfile
        from pathlib import Path
        from src import db
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "t.db")
            # Build old schema: use a title_hash-based index directly.
            import sqlite3
            conn = sqlite3.connect(db_path)
            conn.executescript(
                "CREATE TABLE change_alert ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "stock_code TEXT NOT NULL, alert_time INTEGER NOT NULL,"
                "alert_type TEXT NOT NULL, z_score REAL NOT NULL DEFAULT 0.0,"
                "magnitude REAL NOT NULL DEFAULT 0.0,"
                "detail TEXT NOT NULL DEFAULT '{}', priority TEXT NOT NULL DEFAULT 'P2',"
                "filtered INTEGER NOT NULL DEFAULT 0, filter_reason TEXT DEFAULT NULL);"
                "CREATE UNIQUE INDEX uq_change_alert_announcement "
                "ON change_alert(stock_code, json_extract(detail, '$.title_hash'));"
            )
            conn.commit()
            conn.close()
            # init_db should detect + rebuild the index on dedup_hash.
            db.init_db(db_path)
            conn = sqlite3.connect(db_path)
            idx = conn.execute(
                "PRAGMA index_list(change_alert)"
            ).fetchall()
            names = [r[1] for r in idx]
            assert "uq_change_alert_announcement" in names
            row = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='index' AND name='uq_change_alert_announcement'"
            ).fetchone()
            ddl = (row[0] if row and row[0] else "") or ""
            assert "dedup_hash" in ddl
            assert "title_hash" not in ddl
            conn.close()
