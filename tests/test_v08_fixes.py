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


def insert_hot_word(tmp_db, stock_code, word, score, days_ago=0):
    ts = int(time.time()) - days_ago * 86400
    tmp_db.conn.execute(
        "INSERT INTO hot_word_event (stock_code, word, tfidf_score, event_time, z_score)"
        " VALUES (?, ?, ?, ?, 0)",
        (stock_code, word, score, ts),
    )
    tmp_db.conn.commit()


STOCKS = {
    "300750.SZ": {"name": "宁德时代"},
    "601899.SH": {"name": "紫金矿业"},
    "PDD.US": {"name": "拼多多"},
}


# ════════════════════════════════════════════════════════
# P1: hot-words section rewrite
# ════════════════════════════════════════════════════════

class TestHotWordsFilterBeforeAggregate:
    """Name fragments must be filtered per-stock BEFORE cross-stock ranking."""

    def test_name_fragments_removed(self, tmp_db):
        """'宁德 时代' / '紫金 矿业' (jieba fragments of stock names) must not
        appear in the section even with huge TF-IDF scores."""
        insert_hot_word(tmp_db, "300750.SZ", "宁德 时代", 8.18)
        insert_hot_word(tmp_db, "601899.SH", "紫金 矿业", 7.50)
        md = rg._build_hot_words_section(str(tmp_db.path), tmp_db.date_str, STOCKS)
        assert "宁德 时代" not in md
        assert "紫金 矿业" not in md

    def test_narrative_words_kept(self, tmp_db):
        """Real narrative words (马斯克) must survive the name-token filter."""
        insert_hot_word(tmp_db, "300750.SZ", "马斯克", 1.74)
        md = rg._build_hot_words_section(str(tmp_db.path), tmp_db.date_str, STOCKS)
        assert "马斯克" in md

    def test_sec_boilerplate_removed(self, tmp_db):
        """SEC announcement boilerplate fragments ('size kb', 'accession number')
        must not surface as hot words."""
        insert_hot_word(tmp_db, "PDD.US", "size kb", 5.0)
        insert_hot_word(tmp_db, "PDD.US", "accession number", 4.0)
        md = rg._build_hot_words_section(str(tmp_db.path), tmp_db.date_str, STOCKS)
        assert "size kb" not in md
        assert "accession number" not in md

    def test_cross_stock_aggregation_tier(self, tmp_db):
        """A word discussed on 2+ stocks lands in the cross-stock tier."""
        insert_hot_word(tmp_db, "300750.SZ", "特斯拉", 3.0)
        insert_hot_word(tmp_db, "PDD.US", "特斯拉", 2.0)
        md = rg._build_hot_words_section(str(tmp_db.path), tmp_db.date_str, STOCKS)
        assert "跨股共现" in md
        assert "特斯拉" in md
        assert "2只" in md

    def test_single_stock_words_in_tier2(self, tmp_db):
        """A word on one stock only appears under 个股热点, not 跨股共现."""
        insert_hot_word(tmp_db, "300750.SZ", "动力电池", 4.0)
        md = rg._build_hot_words_section(str(tmp_db.path), tmp_db.date_str, STOCKS)
        assert "跨股共现" not in md
        assert "个股热点" in md
        assert "动力电池" in md

    def test_streak_annotation(self, tmp_db):
        """A word present 3+ days gets 📊连续N天; a 1-day word gets 🆕新增."""
        insert_hot_word(tmp_db, "300750.SZ", "持续词", 3.0, days_ago=3)
        insert_hot_word(tmp_db, "300750.SZ", "持续词", 3.0, days_ago=2)
        insert_hot_word(tmp_db, "300750.SZ", "持续词", 3.0, days_ago=1)
        insert_hot_word(tmp_db, "300750.SZ", "持续词", 3.0, days_ago=0)
        insert_hot_word(tmp_db, "300750.SZ", "新词", 1.5, days_ago=0)
        md = rg._build_hot_words_section(str(tmp_db.path), tmp_db.date_str, STOCKS)
        assert "📊连续" in md and "连续4天" in md
        assert "新词" in md and "🆕新增" in md

    def test_no_data(self, tmp_db):
        md = rg._build_hot_words_section(str(tmp_db.path), tmp_db.date_str, STOCKS)
        assert "今日无热词数据" in md


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

