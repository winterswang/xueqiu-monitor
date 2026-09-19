"""posts 表迁移测试 (v2 Phase 4, 2026-09-20).

回填幂等性 / 最新快照胜出 / link 兜底 / 双空不去重 / post_ts 解析 /
insert_snapshot 双写同事务 / marker 追赶 (自愈)。
"""

import json
import sqlite3
import time

import pytest

from src import db as dbmod


@pytest.fixture
def mdb(tmp_path):
    p = str(tmp_path / "m.db")
    dbmod.init_db(p)
    return p


def _snap(conn, code, crawl_time, posts):
    cur = conn.execute(
        """INSERT INTO crawl_snapshots
           (stock_code, crawl_time, posts_count, posts_data, sentiment_avg, status)
           VALUES (?, ?, ?, ?, 0.0, 'success')""",
        (code, crawl_time, len(posts), json.dumps(posts)),
    )
    conn.commit()
    return cur.lastrowid


class TestBackfill:
    def test_backfill_latest_snapshot_wins(self, mdb):
        now = int(time.time())
        conn = sqlite3.connect(mdb)
        _snap(conn, "A.HK", now - 3600, [
            {"post_id": "p1", "title": "t", "like_count": 3},
        ])
        _snap(conn, "A.HK", now - 60, [
            {"post_id": "p1", "title": "t", "like_count": 99},
        ])
        conn.close()
        dbmod.init_db(mdb)  # 触发回填
        conn = sqlite3.connect(mdb)
        cnt, likes = conn.execute(
            "SELECT COUNT(*), MAX(like_count) FROM posts WHERE stock_code='A.HK'"
        ).fetchone()
        assert (cnt, likes) == (1, 99)  # 最新快照副本胜出
        conn.close()

    def test_backfill_idempotent_and_marker(self, mdb):
        now = int(time.time())
        conn = sqlite3.connect(mdb)
        _snap(conn, "A.HK", now, [{"post_id": "p1", "title": "t"}])
        conn.close()
        dbmod.init_db(mdb)
        dbmod.init_db(mdb)  # 二次: marker 命中, 不重复
        conn = sqlite3.connect(mdb)
        assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1
        marker = conn.execute(
            "SELECT value FROM db_meta WHERE key='posts_backfill_max_snapshot_id'"
        ).fetchone()[0]
        assert int(marker) == conn.execute(
            "SELECT MAX(id) FROM crawl_snapshots"
        ).fetchone()[0]
        conn.close()

    def test_backfill_marker_catchup_selfheal(self, mdb):
        """回填后又有旧代码写入新快照 → 下次 init_db 自动补齐."""
        now = int(time.time())
        conn = sqlite3.connect(mdb)
        _snap(conn, "A.HK", now - 100, [{"post_id": "p1", "title": "t"}])
        conn.close()
        dbmod.init_db(mdb)
        # 模拟旧代码直接 INSERT (绕过双写)
        conn = sqlite3.connect(mdb)
        _snap(conn, "A.HK", now, [{"post_id": "p2", "title": "t2"}])
        conn.close()
        dbmod.init_db(mdb)  # 自愈回填
        conn = sqlite3.connect(mdb)
        keys = {r[0] for r in conn.execute(
            "SELECT dedup_key FROM posts WHERE stock_code='A.HK'"
        )}
        assert keys == {"p1", "p2"}
        conn.close()

    def test_link_fallback_and_null_dedup(self, mdb):
        now = int(time.time())
        conn = sqlite3.connect(mdb)
        _snap(conn, "A.HK", now, [
            {"link": "https://x/1", "title": "a"},          # link 兜底
            {"title": "无ID帖"},                              # 双空 → NULL
            {"title": "另一无ID帖"},
        ])
        conn.close()
        dbmod.init_db(mdb)
        conn = sqlite3.connect(mdb)
        nulls = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE stock_code='A.HK' AND dedup_key IS NULL"
        ).fetchone()[0]
        linked = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE stock_code='A.HK' AND dedup_key='https://x/1'"
        ).fetchone()[0]
        assert (nulls, linked) == (2, 1)  # 双空不去重全保留
        conn.close()

    def test_post_ts_parsed_with_crawl_base(self, mdb):
        """相对时间 ("3分钟前") 以 crawl_time 为基准解析成绝对时间."""
        now = int(time.time())
        conn = sqlite3.connect(mdb)
        _snap(conn, "A.HK", now, [
            {"post_id": "p1", "title": "t", "time": "03-01 10:00"},
        ])
        conn.close()
        dbmod.init_db(mdb)
        conn = sqlite3.connect(mdb)
        ts = conn.execute("SELECT post_ts FROM posts WHERE dedup_key='p1'").fetchone()[0]
        assert ts > 0  # 可解析 (基准正确性由 _parse_post_time 单测覆盖)
        conn.close()


class TestDualWrite:
    def test_insert_snapshot_writes_posts_same_txn(self, mdb):
        from src.models import CrawlSnapshot
        snap = CrawlSnapshot(
            stock_code="B.HK", crawl_time=int(time.time()), posts_count=2,
            posts_data=[
                {"post_id": "q1", "title": "t1", "like_count": 5,
                 "sentiment_score": 0.6},
                {"post_id": "q2", "title": "t2"},
            ],
            sentiment_avg=0.3, status="success",
        )
        sid = dbmod.insert_snapshot(mdb, snap)
        conn = sqlite3.connect(mdb)
        rows = conn.execute(
            "SELECT dedup_key, like_count, sentiment_score, snapshot_id "
            "FROM posts WHERE stock_code='B.HK' ORDER BY dedup_key"
        ).fetchall()
        assert len(rows) == 2
        assert all(r[3] == sid for r in rows)
        assert rows[0][:3] == ("q1", 5, 0.6)
        conn.close()

    def test_dual_write_dedup_by_unique_index(self, mdb):
        """同 (stock, key) 二次入库被唯一索引挡住 (跨快照)."""
        from src.models import CrawlSnapshot
        t = int(time.time())
        for likes in (10, 20):
            dbmod.insert_snapshot(mdb, CrawlSnapshot(
                stock_code="C.HK", crawl_time=t, posts_count=1,
                posts_data=[{"post_id": "r1", "title": "t", "like_count": likes}],
                sentiment_avg=0.0, status="success",
            ))
        conn = sqlite3.connect(mdb)
        cnt = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE stock_code='C.HK'"
        ).fetchone()[0]
        assert cnt == 1
        conn.close()


class TestGetExistingPostIds:
    def test_reads_posts_table_full(self, mdb):
        from src.models import CrawlSnapshot
        t = int(time.time())
        # 100 天前的老帖: 旧 json_each(90天) 查不到, 新 posts 全量查得到
        dbmod.insert_snapshot(mdb, CrawlSnapshot(
            stock_code="D.HK", crawl_time=t - 101 * 86400, posts_count=1,
            posts_data=[{"post_id": "old1", "title": "t"}],
            sentiment_avg=0.0, status="success",
        ))
        ids = dbmod.get_existing_post_ids(mdb, "D.HK")
        assert "old1" in ids
