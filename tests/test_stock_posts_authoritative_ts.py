"""帖子发布时间必须以 ``posts.post_ts`` 为准，不能按当前时刻重解析相对时间串。

## 漂移是什么

``crawl_snapshots.posts_data`` 存的是**原始抓取副本**，时间字段是相对串
（``"3小时前"`` / ``"昨天 10:20"``）。抓取当时是对的，之后再拿**当前时刻**
去解析就会漂：

    11-24 18:00 抓到的 "昨天 10:20"  → 指 11-23 10:20（正确）
    11-25 19:00 跑日报再解析         → "昨天" = 11-24 → 落进 11-24 的当日窗口

结果：一条 11-23 发的帖，在 11-24 的日报里被当成「当日帖」。

``posts.post_ts`` 是**入库时**用爬取时刻算好落库的，不随时间漂移，应当是唯一
时间来源（2026-11-25 实测：UBER.US 当日 0 帖被算成 1 帖，修后归零）。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import db as dbmod  # noqa: E402
from src import report_generator as rg  # noqa: E402

DATE = "2026-11-24"


def _mkdb(tmp_path):
    p = tmp_path / "t.db"
    dbmod.init_db(str(p))
    return str(p)


def _seed(db_path, post_ts, time_text="昨天 10:20",
          crawl_ts=None, post_id="p1"):
    """插一条快照（原始副本）+ 一条 posts（带权威 post_ts）。

    快照落在 DATE 当天 18:00，posts.post_ts 由调用方指定。
    """
    if crawl_ts is None:
        crawl_ts = int(datetime(2026, 11, 24, 18, 0).timestamp())
    posts = [{
        "post_id": post_id, "title": "某公司发布公告",
        "content": "正文" * 60, "time": time_text,
        "like_count": 3, "comment_count": 1, "forward_count": 0,
        "link": f"https://xueqiu.com/{post_id}",
    }]
    import sqlite3
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        """INSERT INTO crawl_snapshots
           (stock_code, crawl_time, posts_count, posts_data, sentiment_avg, status)
           VALUES (?,?,?,?,?,?)""",
        ("AAA.US", crawl_ts, len(posts), json.dumps(posts, ensure_ascii=False), 0.0, "success"),
    )
    snap_id = cur.lastrowid
    conn.execute(
        """INSERT INTO posts
           (stock_code, dedup_key, post_id, post_type, title, content, author,
            time_text, post_ts, like_count, comment_count, forward_count,
            sentiment_score, link, snapshot_id, first_seen_ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("AAA.US", post_id, post_id, "discussion", "某公司发布公告", "正文" * 60,
         "作者", time_text, int(post_ts), 3, 1, 0, 0.0,
         f"https://xueqiu.com/{post_id}", snap_id, crawl_ts),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def read_at_next_evening(monkeypatch):
    """把「现在」拨到 DATE 的次日 19:00 —— 漂移正是在这个时点显现。"""
    t = datetime(2026, 11, 25, 19, 0).timestamp()
    monkeypatch.setattr(time, "time", lambda: t)


def test_old_post_is_excluded_by_authoritative_ts(tmp_path, read_at_next_evening):
    """11-23 发的帖，即使相对时间串在次日会重解析成 11-24，也必须被排除。"""
    db_path = _mkdb(tmp_path)
    _seed(db_path, datetime(2026, 11, 23, 10, 20).timestamp())

    got = rg.fetch_stock_posts(db_path, "AAA.US", DATE, min_length=0, max_age_days=1)
    assert got == [], (
        "相对时间串 '昨天 10:20' 在 11-25 重解析会落到 11-24 当日窗口；"
        "必须用 posts.post_ts（11-23）判定，否则旧帖冒充当日帖"
    )


def test_today_post_is_kept_by_authoritative_ts(tmp_path, read_at_next_evening):
    """反向：真的 11-24 发的帖不能被误杀。"""
    db_path = _mkdb(tmp_path)
    _seed(db_path, datetime(2026, 11, 24, 10, 20).timestamp())

    got = rg.fetch_stock_posts(db_path, "AAA.US", DATE, min_length=0, max_age_days=1)
    assert len(got) == 1


def test_falls_back_to_text_when_no_authoritative_ts(tmp_path, read_at_next_evening):
    """拿不到 posts 行（回填缺口/解析失败=0）时退回文本解析，不因缺数据整段清空。"""
    db_path = _mkdb(tmp_path)
    _seed(db_path, 0)  # post_ts=0 → 视为未知

    got = rg.fetch_stock_posts(db_path, "AAA.US", DATE, min_length=0, max_age_days=1)
    assert len(got) == 1, "缺权威时间时保持原有的 fail-open 行为"


def test_ts_map_lookup(tmp_path):
    db_path = _mkdb(tmp_path)
    _seed(db_path, datetime(2026, 11, 23, 10, 20).timestamp())

    m = dbmod.fetch_post_ts_map(db_path, "AAA.US", ["p1", "missing", ""])
    assert "p1" in m and m["p1"] > 0
    assert "missing" not in m
    assert dbmod.fetch_post_ts_map(db_path, "AAA.US", []) == {}


def test_ts_map_scoped_to_stock(tmp_path):
    """别把同 post_id 的其它票时间串过来。"""
    db_path = _mkdb(tmp_path)
    _seed(db_path, datetime(2026, 11, 23, 10, 20).timestamp())
    assert dbmod.fetch_post_ts_map(db_path, "BBB.US", ["p1"]) == {}
