"""v0.8 fixes: hot-words section rewrite (filter-then-aggregate, two tiers),
KOL whitelist weighting, alert dedup (UNIQUE index + INSERT OR IGNORE).

Covers the v0.8 plan: P1 hot-words, P2 KOL, P3 alert dedup.
"""

import json
import time
from pathlib import Path

import pytest

from src import report_generator as rg
from src.models import ChangeAlert


@pytest.fixture
def tmp_db():
    """Temporary SQLite DB with full schema for each test."""
    import sqlite3
    import tempfile
    from src import db as dbmod
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "test.db"
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        dbmod.init_db(str(path))
        yield type("Tmp", (), {"path": path, "conn": conn,
                               "date_str": time.strftime("%Y-%m-%d")})()
        conn.close()


STOCKS = {
    "300750.SZ": {"name": "宁德时代"},
    "601899.SH": {"name": "紫金矿业"},
    "PDD.US": {"name": "拼多多"},
}


# ════════════════════════════════════════════════════════
# P2: KOL whitelist weighting
# ════════════════════════════════════════════════════════

class TestKolWhitelist:
    def test_prompt_tags_kol_author(self):
        """KOL author posts carry the ⭐KOL marker in the LLM prompt."""
        posts = [
            {"title": "KOL 帖子", "content": "内容", "author": "但斌",
             "like_count": 10, "comment_count": 2, "forward_count": 1, "time": "1小时前"},
            {"title": "普通帖子", "content": "内容", "author": "路人甲",
             "like_count": 5, "comment_count": 1, "forward_count": 0, "time": "2小时前"},
        ]
        trend = {"has_trend": True, "today_mean": 0.1, "avg_mean": 0.05,
                 "avg_std": 0.02, "trend": "上升", "days": 7}
        prompt = rg._build_analysis_prompt("宁德时代", "300750.SZ", posts, trend, [], [])
        assert "但斌 ⭐KOL" in prompt
        assert "路人甲 ⭐KOL" not in prompt
        assert "高影响力作者帖" in prompt

    def test_prompt_tags_media_account(self):
        """Media accounts (财联社) also get the ⭐KOL marker."""
        posts = [{"title": "财联社快讯", "content": "内容内容", "author": "财联社",
                  "like_count": 0, "comment_count": 0, "forward_count": 0, "time": "5分钟前"}]
        trend = {"has_trend": False, "days": 1}
        prompt = rg._build_analysis_prompt("拼多多", "PDD.US", posts, trend, [], [])
        assert "财联社 ⭐KOL" in prompt

    def test_load_kol_whitelist_returns_expected_names(self):
        names = rg._load_kol_whitelist()
        assert "但斌" in names
        assert "财联社" in names
        assert len(names) >= 6


# ════════════════════════════════════════════════════════
# P3: alert dedup (UNIQUE index + INSERT OR IGNORE)
# ════════════════════════════════════════════════════════

class TestAlertDedup:
    def _ann(self, stock_code, dedup_hash, alert_time=None):
        return ChangeAlert(
            stock_code=stock_code,
            alert_type="new_announcement",
            alert_time=alert_time or int(time.time()),
            z_score=0.0,
            magnitude=0.0,
            detail={"dedup_hash": dedup_hash},
            priority="P2",
        )

    def test_duplicate_announcement_ignored(self, tmp_db):
        from src import db as dbmod
        first = dbmod.insert_alert(str(tmp_db.path), self._ann("600519.SH", "h1"))
        dup = dbmod.insert_alert(str(tmp_db.path), self._ann("600519.SH", "h1"))
        assert first > 0
        assert dup == 0
        rows = tmp_db.conn.execute("SELECT COUNT(*) c FROM change_alert").fetchone()["c"]
        assert rows == 1

    def test_announcement_different_stock_or_hash_allowed(self, tmp_db):
        from src import db as dbmod
        a = dbmod.insert_alert(str(tmp_db.path), self._ann("600519.SH", "h1"))
        b = dbmod.insert_alert(str(tmp_db.path), self._ann("600519.SH", "h2"))
        c = dbmod.insert_alert(str(tmp_db.path), self._ann("300750.SZ", "h1"))
        assert (a, b, c) == (1, 2, 3)

    def test_duplicate_signal_ignored(self, tmp_db):
        from src import db as dbmod
        ts = int(time.time())
        sig = ChangeAlert(stock_code="PDD.US", alert_type="hot_word_surge",
                          alert_time=ts, z_score=3.0, magnitude=0.5,
                          detail={"word": "特斯拉"}, priority="P1")
        first = dbmod.insert_alert(str(tmp_db.path), sig)
        dup = dbmod.insert_alert(str(tmp_db.path), sig)
        assert first > 0 and dup == 0
        rows = tmp_db.conn.execute("SELECT COUNT(*) c FROM change_alert").fetchone()["c"]
        assert rows == 1

    def test_signal_different_time_allowed(self, tmp_db):
        from src import db as dbmod
        ts = int(time.time())
        def mk(t):
            return ChangeAlert(stock_code="PDD.US", alert_type="hot_word_surge",
                               alert_time=t, z_score=3.0, magnitude=0.5,
                               detail={"word": "特斯拉"}, priority="P1")
        a = dbmod.insert_alert(str(tmp_db.path), mk(ts))
        b = dbmod.insert_alert(str(tmp_db.path), mk(ts - 600))
        assert (a, b) == (1, 2)

    def test_signal_different_word_same_time_allowed(self, tmp_db):
        """v0.8.2 regression: detect_hot_word_emergence emits one alert per word,
        all sharing the same time.time() stamp. The word-blind v0.8 index would
        silently swallow every word after the first via INSERT OR IGNORE; the
        word-aware identity (detail.word in the key) must keep them all."""
        from src import db as dbmod
        ts = int(time.time())
        def mk(word):
            return ChangeAlert(stock_code="300750.SZ", alert_type="hot_word_surge",
                               alert_time=ts, z_score=3.0, magnitude=0.5,
                               detail={"word": word}, priority="P1")
        ids = [dbmod.insert_alert(str(tmp_db.path), mk(w)) for w in ("回购", "中报", "储能")]
        assert all(i > 0 for i in ids), ids
        rows = tmp_db.conn.execute(
            "SELECT COUNT(*) c FROM change_alert").fetchone()["c"]
        assert rows == 3

    def test_batch_insert_dedups(self, tmp_db):
        from src import db as dbmod
        ts = int(time.time())
        alerts = [
            self._ann("600519.SH", "h1", ts),
            self._ann("600519.SH", "h1", ts),      # dup of #1
            self._ann("600519.SH", "h2", ts),
        ]
        ids = dbmod.insert_alerts_batch(str(tmp_db.path), alerts)
        assert ids[0] > 0
        assert ids[1] == 0
        assert ids[2] > 0
        rows = tmp_db.conn.execute("SELECT COUNT(*) c FROM change_alert").fetchone()["c"]
        assert rows == 2

