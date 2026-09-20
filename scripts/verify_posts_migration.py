#!/usr/bin/env python3
"""posts 表迁移对账 (v2 Phase 4, 2026-09-20).

观察期每天跑一次（可挂在 health_check 之后 / 手动）。三类检查:

① 集合等值: 每股 (当日快照并集的 distinct key) vs posts 表 — 回填完整性
② 双写缺口: 有帖快照却无任何 posts 行 (marker 之后的快照漏写)
③ 内容抽查: 随机 N 行逐字段比对最新快照 JSON 里的副本

退出码: 0 全绿 / 1 有差异 (差异详情打 stdout)。
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB = sys.argv[1] if len(sys.argv) > 1 else "data/monitor.db"


def check_set_equality(conn: sqlite3.Connection) -> list[str]:
    """① 每股集合等值 (只对 marker 覆盖范围内的快照有意义; 双写期近似)."""
    problems = []
    rows = conn.execute(
        """SELECT cs.stock_code,
                  COUNT(DISTINCT COALESCE(NULLIF(json_extract(p.value,'$.post_id'),''),
                                          NULLIF(json_extract(p.value,'$.link'),'')))
           FROM crawl_snapshots cs, json_each(cs.posts_data) p
           WHERE cs.posts_data != '[]'
           GROUP BY cs.stock_code"""
    ).fetchall()
    posts_counts = dict(
        conn.execute(
            "SELECT stock_code, COUNT(*) FROM posts "
            "WHERE dedup_key IS NOT NULL GROUP BY stock_code"
        ).fetchall()
    )
    for code, json_cnt in rows:
        p_cnt = posts_counts.get(code, 0)
        if p_cnt < json_cnt:
            problems.append(f"① {code}: posts {p_cnt} < 快照并集 {json_cnt}")
    return problems


def check_dualwrite_gap(conn: sqlite3.Connection) -> list[str]:
    """② 快照的帖未全部落入 posts 表 (双写真实缺口).

    注: "快照无专属 posts 行"不等于漏写 —— 快照里全是重复帖时
    INSERT OR IGNORE 全部被忽略是正常语义; 真缺口 = 快照的 key
    在该股 posts 表里找不到。
    """
    problems = []
    rows = conn.execute(
        """SELECT cs.id, cs.stock_code, cs.posts_data FROM crawl_snapshots cs
           LEFT JOIN posts p ON p.snapshot_id = cs.id
           WHERE cs.posts_data != '[]' AND p.id IS NULL
           LIMIT 50"""
    ).fetchall()
    for snap_id, code, posts_json in rows:
        try:
            posts = json.loads(posts_json)
        except (ValueError, TypeError):
            continue
        keys = {
            (p.get("post_id") or "").strip() or (p.get("link") or "").strip()
            for p in posts
        }
        keys.discard("")
        if not keys:
            continue
        have = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE stock_code=? AND dedup_key IN "
            f"({','.join('?' * len(keys))})",
            (code, *keys),
        ).fetchone()[0]
        if have < len(keys):
            missing = len(keys) - have
            problems.append(
                f"② 快照 {snap_id} ({code}): {missing}/{len(keys)} 个 key "
                "不在 posts 表 (真实双写缺口)"
            )
    return problems


def check_content_sample(conn: sqlite3.Connection, n: int = 20) -> list[str]:
    """③ 随机抽 n 行比对最新快照 JSON 副本的关键字段."""
    random.seed()  # 每次抽样不同
    problems = []
    rows = conn.execute(
        "SELECT stock_code, dedup_key, snapshot_id FROM posts "
        "WHERE dedup_key IS NOT NULL ORDER BY RANDOM() LIMIT ?", (n,)
    ).fetchall()
    for code, key, snap_id in rows:
        snap = conn.execute(
            "SELECT posts_data FROM crawl_snapshots WHERE id=?", (snap_id,)
        ).fetchone()
        if not snap or not snap[0]:
            continue
        try:
            posts = json.loads(snap[0])
        except (ValueError, TypeError):
            continue
        match = None
        for p in posts:
            if ((p.get("post_id") or "").strip() or (p.get("link") or "").strip()) == key:
                match = p
                break
        if match is None:
            problems.append(f"③ {code}/{key[:40]}: snapshot {snap_id} JSON 中找不到")
            continue
        prow = conn.execute(
            "SELECT like_count, comment_count, forward_count, sentiment_score "
            "FROM posts WHERE stock_code=? AND dedup_key=?",
            (code, key),
        ).fetchone()
        if prow and (
            prow[0] != int(match.get("like_count") or 0)
            or prow[1] != int(match.get("comment_count") or 0)
            or prow[2] != int(match.get("forward_count") or 0)
        ):
            problems.append(f"③ {code}/{key[:40]}: 互动数字段不一致")
    return problems


def main() -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    problems = []
    problems += check_set_equality(conn)
    problems += check_dualwrite_gap(conn)
    problems += check_content_sample(conn)
    if problems:
        print(f"❌ 对账发现 {len(problems)} 处差异:")
        for p in problems:
            print(" ", p)
        return 1
    print("✅ posts 迁移对账全绿 (集合等值 / 双写无缺口 / 内容抽查一致)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
