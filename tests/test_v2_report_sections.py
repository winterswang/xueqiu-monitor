"""v2 tests: thermometer-only-changes render, mainlines name-fragment filter,
prev-summary delta plumbing. Ported from the deleted v1 section renderers
(_build_thermometer_section / _build_hot_words_section) whose behavioral
guarantees still apply to their v2 successors."""

import json
import time
from pathlib import Path

import pytest

from src import report_generator as rg


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


class TestThermometerV2:
    """温度计 v2: 只写变化 (转暖/转冷榜 + 平稳一行), 首日无基准降级."""

    def _thermo(self, posts_by_stock):
        rows = []
        for code, posts in posts_by_stock.items():
            equal, weighted, used = rg._sentiment_aggregates(posts)
            rows.append({
                "stock_code": code, "posts": len(posts),
                "sentiment": round(equal, 3) if equal is not None else 0.0,
                "sentiment_weighted": round(weighted, 3) if weighted is not None else 0.0,
            })
        rows.sort(key=lambda x: x["sentiment"], reverse=True)
        return rows

    def test_first_day_without_prev_summary(self):
        thermo = self._thermo({
            "AAA.US": [{"sentiment_score": 0.5, "like_count": 2}],
            "BBB.HK": [{"sentiment_score": -0.3, "like_count": 1}],
        })
        md, rows = rg._build_thermometer_v2(thermo, None, STOCKS, set())
        assert "首日无昨日基准" in md
        assert "显著转暖" not in md and "显著转冷" not in md  # 无 delta 不出榜
        assert "平稳" in md  # 全部进平稳列举
        assert "最积极" in md and "AAA.US" in md  # 锚点行照常
        assert all(r["delta"] is None for r in rows)

    def test_warming_cooling_tables_and_flat_line(self):
        today = self._thermo({
            "300750.SZ": [{"sentiment_score": 0.6, "like_count": 2}],
            "PDD.US": [{"sentiment_score": -0.5, "like_count": 1}],
            "601899.SH": [{"sentiment_score": 0.05, "like_count": 1}],
        })
        prev = {"stocks": {
            "300750.SZ": {"weighted": 0.1},
            "PDD.US": {"weighted": 0.1},
            "601899.SH": {"weighted": 0.05},
        }}
        md, rows = rg._build_thermometer_v2(today, prev, STOCKS, set())
        assert "显著转暖" in md and "宁德时代" in md   # 0.6-0.1=+0.5
        assert "显著转冷" in md and "拼多多" in md     # -0.5-0.1=-0.6
        assert "平稳" in md and "紫金矿业" in md       # delta 0.0
        assert "| 股票 |" in md and "较昨日" in md
        # deep 标记列
        md2, _ = rg._build_thermometer_v2(today, prev, STOCKS, {"300750.SZ"})
        assert "🔵" in md2

    def test_posts_100plus_display(self):
        today = self._thermo({
            "300750.SZ": [{"sentiment_score": 0.3, "like_count": 0}] * 120,
        })
        prev = {"stocks": {"300750.SZ": {"weighted": 0.0}}}
        md, _ = rg._build_thermometer_v2(today, prev, STOCKS, set())
        assert "120+" in md  # >=100 的帖数带 "+" 后缀 (至少语义)


class TestMainlinesNameFragmentFilter:
    """主线表的名字碎片过滤 (v1 热词节 filter-then-aggregate 的 v2 后继)."""

    def test_name_fragments_removed(self, tmp_db):
        # 跨 2 只股票仍会被名字碎片规则拦下 ("宁德 时代" 含 token "宁德")
        # —— 但两只不同股票的名字碎片拼不成跨股共现, 直接用单股 streak 验证
        for d in range(4):
            insert_hot_word(tmp_db, "300750.SZ", "宁德 时代", 8.18, days_ago=d)
        md = rg._build_mainlines_section(str(tmp_db.path), tmp_db.date_str, STOCKS)
        assert "宁德 时代" not in md
        assert "今日无持续主线" in md

    def test_narrative_word_survives_via_cross_stock(self, tmp_db):
        insert_hot_word(tmp_db, "300750.SZ", "特斯拉", 3.0)
        insert_hot_word(tmp_db, "PDD.US", "特斯拉", 2.0)
        md = rg._build_mainlines_section(str(tmp_db.path), tmp_db.date_str, STOCKS)
        assert "特斯拉" in md
        assert "宁德时代、拼多多" in md

    def test_single_stock_streak_3days_promotes(self, tmp_db):
        for d in range(3):
            insert_hot_word(tmp_db, "300750.SZ", "固态电池", 2.0, days_ago=d)
        md = rg._build_mainlines_section(str(tmp_db.path), tmp_db.date_str, STOCKS)
        assert "固态电池" in md

    def test_single_day_single_stock_word_not_mainline(self, tmp_db):
        insert_hot_word(tmp_db, "300750.SZ", "单日词", 5.0)
        md = rg._build_mainlines_section(str(tmp_db.path), tmp_db.date_str, STOCKS)
        assert "单日词" not in md
        assert "今日无持续主线" in md


class TestPrevSummaryDelta:
    """跨日 summary 读写与 delta 推导."""

    def test_load_prev_summary_lookback(self, tmp_path):
        base = time.strftime("%Y-%m-%d")
        from datetime import datetime, timedelta
        d1 = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        d2 = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        rg.write_summary(tmp_path, d2, {"date": d2, "stocks": {"A.HK": {"weighted": 0.1}}})
        got = rg.load_prev_summary(tmp_path, base)
        assert got is not None and got["date"] == d2  # 断更一天仍可回看
        rg.write_summary(tmp_path, d1, {"date": d1, "stocks": {"A.HK": {"weighted": 0.2}}})
        got = rg.load_prev_summary(tmp_path, base)
        assert got["date"] == d1  # 优先取最近

    def test_delta_none_without_prev(self):
        assert rg._sentiment_delta(0.3, None, "A.HK") is None
        assert rg._sentiment_delta(0.3, {"stocks": {}}, "A.HK") is None
        assert rg._sentiment_delta(0.3, {"stocks": {"A.HK": {}}}, "A.HK") is None
        assert rg._sentiment_delta(0.3, {"stocks": {"A.HK": {"weighted": 0.1}}}, "A.HK") == 0.2
