"""xueqiu-monitor: SQLite storage layer (CRUD for all 10 tables)

All functions accept/return dataclass models from models.py.
No ORM — raw sqlite3 with parameterized queries.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .models import (
    CrawlSnapshot, SentimentStat, ChangeAlert,
    HotWordDict, HotWordEvent, PushHistory,
    Comment, Announcement, ContentWeight, UserPreference,
)


def _connect(db_path: str) -> sqlite3.Connection:
    """Open SQLite connection with WAL mode, busy timeout, and retry.

    Per §3.4: waits 3s (busy_timeout=3000ms) and retries up to 3 times.
    """
    for attempt in range(3):
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=3000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            return conn
        except sqlite3.OperationalError as e:
            if attempt < 2:
                time.sleep(1)
                continue
            raise


def init_db(db_path: str, schema_path: str | None = None) -> None:
    """Initialize database with schema, then run idempotent migrations."""
    if schema_path is None:
        schema_path = str(Path(__file__).parent / "schema.sql")
    conn = _connect(db_path)
    try:
        conn.executescript(Path(schema_path).read_text())
        _run_migrations(conn)
        conn.commit()
    finally:
        conn.close()


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Idempotent schema migrations for legacy databases.

    Safe to run on every init_db(): each step inspects current state first.
    """
    import logging
    log = logging.getLogger(__name__)

    # Drop dead column: cold_start_days was never read at runtime (the cold-start
    # window is always sourced from global config), so it accumulated as a legacy
    # NOT NULL DEFAULT 28 column. Remove it from pre-existing databases.
    cols = conn.execute("PRAGMA table_info(user_preference)").fetchall()
    if any(c[1] == "cold_start_days" for c in cols):
        conn.execute("ALTER TABLE user_preference DROP COLUMN cold_start_days")
        log.info("[migrate] dropped dead column cold_start_days from user_preference")


# ════════════════════════════════════════════════════════
# crawl_snapshots
# ════════════════════════════════════════════════════════

def insert_snapshot(db_path: str, snap: CrawlSnapshot) -> int:
    d = snap.to_dict()
    del d["id"]
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO crawl_snapshots (stock_code, crawl_time, posts_count, posts_data, sentiment_avg, status)
               VALUES (:stock_code, :crawl_time, :posts_count, :posts_data, :sentiment_avg, :status)""",
            d
        )
        return cur.lastrowid


def get_latest_snapshot(db_path: str, stock_code: str) -> CrawlSnapshot | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM crawl_snapshots WHERE stock_code=? ORDER BY crawl_time DESC LIMIT 1",
            (stock_code,)
        ).fetchone()
        return CrawlSnapshot.from_row(row) if row else None


def get_previous_snapshot(db_path: str, stock_code: str, before_time: int) -> CrawlSnapshot | None:
    """Get the snapshot immediately before the given time."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM crawl_snapshots WHERE stock_code=? AND crawl_time < ? ORDER BY crawl_time DESC LIMIT 1",
            (stock_code, before_time)
        ).fetchone()
        return CrawlSnapshot.from_row(row) if row else None


# ════════════════════════════════════════════════════════
# sentiment_stats
# ════════════════════════════════════════════════════════

def insert_sentiment_stat(db_path: str, stat: SentimentStat) -> int:
    """Upsert sentiment stat: INSERT new row or UPDATE on (stock_code, stat_date) conflict."""
    d = stat.to_dict()
    del d["id"]
    with _connect(db_path) as conn:
        # Ensure unique constraint exists (idempotent — safe for both new and existing databases)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_senti_unique "
            "ON sentiment_stats(stock_code, stat_date)"
        )
        cur = conn.execute(
            """INSERT INTO sentiment_stats (stock_code, stat_date, posts_count, sentiment_mean, sentiment_std, z_score, z_alert)
               VALUES (:stock_code, :stat_date, :posts_count, :sentiment_mean, :sentiment_std, :z_score, :z_alert)
               ON CONFLICT(stock_code, stat_date) DO UPDATE SET
               posts_count    = excluded.posts_count,
               sentiment_mean = excluded.sentiment_mean,
               sentiment_std  = excluded.sentiment_std,
               z_score        = excluded.z_score,
               z_alert        = excluded.z_alert
               RETURNING id""",
            d
        )
        row = cur.fetchone()
        conn.commit()
        return row["id"] if row else cur.lastrowid


def get_historical_stats(db_path: str, stock_code: str, days: int = 14) -> list[SentimentStat]:
    """Get last N days of sentiment stats for Z-score calculation."""
    cutoff = int(time.time()) - days * 86400
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM sentiment_stats WHERE stock_code=? AND stat_date >= ? ORDER BY stat_date DESC",
            (stock_code, cutoff)
        ).fetchall()
        return [SentimentStat.from_row(r) for r in rows]


def get_all_historical_stats(db_path: str, stock_code: str) -> list[SentimentStat]:
    """Get ALL historical stats (cold-start fallback)."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM sentiment_stats WHERE stock_code=? ORDER BY stat_date ASC",
            (stock_code,)
        ).fetchall()
        return [SentimentStat.from_row(r) for r in rows]


def count_sentiment_days(db_path: str, stock_code: str) -> int:
    """Count days of data (for cold-start check)."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM sentiment_stats WHERE stock_code=?",
            (stock_code,)
        ).fetchone()
        return row["cnt"] if row else 0


# ════════════════════════════════════════════════════════
# change_alert
# ════════════════════════════════════════════════════════

def insert_alert(db_path: str, alert: ChangeAlert) -> int:
    d = alert.to_dict()
    del d["id"]
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO change_alert (stock_code, alert_time, alert_type, z_score, magnitude, detail, priority, filtered, filter_reason)
               VALUES (:stock_code, :alert_time, :alert_type, :z_score, :magnitude, :detail, :priority, :filtered, :filter_reason)""",
            d
        )
        return cur.lastrowid


def insert_alerts_batch(db_path: str, alerts: list[ChangeAlert]) -> list[int]:
    """Batch insert multiple alerts in one connection.

    Returns list of inserted ids (None for rows that failed).
    """
    if not alerts:
        return []
    rows = []
    with _connect(db_path) as conn:
        for alert in alerts:
            d = alert.to_dict()
            del d["id"]
            try:
                cur = conn.execute(
                    """INSERT INTO change_alert (stock_code, alert_time, alert_type, z_score, magnitude, detail, priority, filtered, filter_reason)
                       VALUES (:stock_code, :alert_time, :alert_type, :z_score, :magnitude, :detail, :priority, :filtered, :filter_reason)""",
                    d
                )
                rows.append(cur.lastrowid)
            except Exception as e:
                logger.warning(f"insert_alert failed: stock={alert.stock_code} error={e}")
                rows.append(None)
    return rows


def get_pending_alerts(db_path: str, priority: str | None = None) -> list[ChangeAlert]:
    """Get unfiltered alerts, optionally filtered by priority."""
    with _connect(db_path) as conn:
        if priority:
            rows = conn.execute(
                "SELECT * FROM change_alert WHERE filtered=0 AND priority=? ORDER BY alert_time DESC",
                (priority,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM change_alert WHERE filtered=0 ORDER BY alert_time DESC"
            ).fetchall()
        return [ChangeAlert.from_row(r) for r in rows]


def mark_alert_filtered(db_path: str, alert_id: int, reason: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE change_alert SET filtered=1, filter_reason=? WHERE id=?",
            (reason, alert_id)
        )


def get_today_alerts(db_path: str) -> list[ChangeAlert]:
    """Get today's alerts (filtered=0)."""
    today_start = int(time.time()) // 86400 * 86400
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM change_alert WHERE alert_time >= ? AND filtered=0 ORDER BY priority, alert_time DESC",
            (today_start,)
        ).fetchall()
        return [ChangeAlert.from_row(r) for r in rows]


# ════════════════════════════════════════════════════════
# hot_word_dict / hot_word_event
# ════════════════════════════════════════════════════════

def upsert_hot_word(db_path: str, word: str, now_ts: int | None = None) -> None:
    now = now_ts or int(time.time())
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO hot_word_dict (word, frequency, last_seen)
               VALUES (?, 1, ?) ON CONFLICT(word) DO UPDATE SET
               frequency = frequency + 1, last_seen = MAX(last_seen, ?)""",
            (word, now, now)
        )


def insert_hot_word_event(db_path: str, event: HotWordEvent) -> int:
    d = event.to_dict()
    del d["id"]
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO hot_word_event (stock_code, word, tfidf_score, event_time, z_score)
               VALUES (:stock_code, :word, :tfidf_score, :event_time, :z_score)""",
            d
        )
        # Auto-update hot_word_dict via trigger
        return cur.lastrowid


def get_recent_hot_word_events(db_path: str, stock_code: str, days: int = 14) -> list[HotWordEvent]:
    cutoff = int(time.time()) - days * 86400
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM hot_word_event WHERE stock_code=? AND event_time >= ? ORDER BY event_time DESC",
            (stock_code, cutoff)
        ).fetchall()
        return [HotWordEvent.from_row(r) for r in rows]


# ════════════════════════════════════════════════════════
# push_history
# ════════════════════════════════════════════════════════

def insert_push(db_path: str, push: PushHistory) -> int:
    d = push.to_dict()
    del d["id"]
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO push_history (stock_code, alert_id, push_time, priority, content, status)
               VALUES (:stock_code, :alert_id, :push_time, :priority, :content, :status)""",
            d
        )
        return cur.lastrowid


def get_push_by_id(db_path: str, push_id: int) -> PushHistory | None:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM push_history WHERE id=?", (push_id,)).fetchone()
        return PushHistory.from_row(row) if row else None


# ════════════════════════════════════════════════════════
# comments
# ════════════════════════════════════════════════════════

def insert_comments(db_path: str, comments: list[Comment]) -> int:
    with _connect(db_path) as conn:
        count = 0
        for c in comments:
            d = c.to_dict()
            del d["id"]
            conn.execute(
                """INSERT OR IGNORE INTO comments (snapshot_id, post_id, comment_count, forward_count, like_count, sentiment_avg)
                   VALUES (:snapshot_id, :post_id, :comment_count, :forward_count, :like_count, :sentiment_avg)""",
                d
            )
            count += 1
        return count


# ════════════════════════════════════════════════════════
# announcements
# ════════════════════════════════════════════════════════

def insert_announcements(db_path: str, anns: list[Announcement]) -> int:
    inserted = 0
    with _connect(db_path) as conn:
        for a in anns:
            d = a.to_dict()
            del d["id"]
            cur = conn.execute(
                """INSERT OR IGNORE INTO announcements (snapshot_id, stock_code, ann_title, ann_date, ann_type, is_new)
                   VALUES (:snapshot_id, :stock_code, :ann_title, :ann_date, :ann_type, :is_new)""",
                d
            )
            if cur.rowcount > 0:
                inserted += 1
        return inserted


def get_announcements_by_snapshot(db_path: str, snapshot_id: int) -> list[dict]:
    """Get announcements for a given snapshot_id (for change detection)."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT ann_title, ann_date, ann_type FROM announcements WHERE snapshot_id=?",
            (snapshot_id,)
        ).fetchall()
        return [
            {"title": r["ann_title"], "time": str(r["ann_date"]), "notice_type": r["ann_type"]}
            for r in rows
        ]


def get_recent_announcement_alerts(
    db_path: str, stock_code: str, title: str, days: int = 7
) -> list[dict]:
    """Check if an announcement was already alerted within N days.

    Returns existing alerts matching stock_code + announcement title hash.
    Used for deduplication in detect_new_announcement.
    """
    import hashlib
    title_hash = hashlib.md5(title.encode()).hexdigest()
    cutoff = int(time.time()) - days * 86400
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT id, detail FROM change_alert
               WHERE stock_code=? AND alert_type='new_announcement'
               AND alert_time >= ?""",
            (stock_code, cutoff)
        ).fetchall()
        matches = []
        for r in rows:
            try:
                detail = json.loads(r["detail"]) if r["detail"] else {}
            except (json.JSONDecodeError, TypeError):
                detail = {}
            if detail.get("title_hash") == title_hash:
                matches.append({"id": r["id"], "title": detail.get("title", "")})
        return matches


def get_historical_new_announcement_counts(
    db_path: str, stock_code: str, window_days: int = 14
) -> list[float]:
    """Get daily new_announcement alert counts for Z-score baseline.

    Returns list of daily counts (one value per day with alerts) for
    computing Z-score on new announcement volume.
    """
    cutoff = int(time.time()) - window_days * 86400
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT COUNT(*) as cnt FROM change_alert
               WHERE stock_code=? AND alert_type='new_announcement'
               AND alert_time >= ?
               GROUP BY (alert_time / 86400)""",
            (stock_code, cutoff)
        ).fetchall()
        return [float(r["cnt"]) for r in rows]


# ════════════════════════════════════════════════════════
# content_weight
# ════════════════════════════════════════════════════════

def get_weight(db_path: str, source: str, keyword: str) -> ContentWeight | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM content_weight WHERE source=? AND keyword=?",
            (source, keyword)
        ).fetchone()
        return ContentWeight.from_row(row) if row else None


def upsert_weight(db_path: str, source: str, keyword: str, delta: float,
                  preference_delta: float = 0.0) -> float:
    """Adjust weight by delta (and optional preference_level).
    Uses ON CONFLICT to avoid TOCTOU race.
    Returns new weight.
    """
    now = int(time.time())
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO content_weight (source, keyword, weight, preference_level, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(source, keyword) DO UPDATE SET
               weight = MAX(0.0, content_weight.weight + ?),
               preference_level = MAX(0.0, MIN(2.0, content_weight.preference_level + ?)),
               updated_at = ?
               RETURNING weight""",
            (source, keyword, max(0.0, 1.0 + delta), max(0.0, 1.0 + preference_delta), now,
             delta, preference_delta, now)
        )
        row = cur.fetchone()
        return float(row["weight"]) if row else max(0.0, 1.0 + delta)


def decay_stale_weights(db_path: str, days: int = 7, decay: float = 0.05, floor: float = 0.3) -> int:
    """Decay weights that haven't been updated in N days. Returns count of decayed rows."""
    cutoff = int(time.time()) - days * 86400
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE content_weight SET weight = MAX(?, weight - ?), updated_at = ? WHERE updated_at < ? AND weight > ?",
            (floor, decay, int(time.time()), cutoff, floor)
        )
        return cur.rowcount


# ════════════════════════════════════════════════════════
# user_preference
# ════════════════════════════════════════════════════════

def get_user_preference(db_path: str, user_id: str) -> UserPreference | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM user_preference WHERE user_id=?",
            (user_id,)
        ).fetchone()
        return UserPreference.from_row(row) if row else None


def upsert_user_preference(db_path: str, pref: UserPreference) -> None:
    d = pref.to_dict()
    d["updated_at"] = int(time.time())
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO user_preference (user_id, p0_threshold, p1_threshold, notify_immediate, notify_digest, updated_at)
               VALUES (:user_id, :p0_threshold, :p1_threshold, :notify_immediate, :notify_digest, :updated_at)
               ON CONFLICT(user_id) DO UPDATE SET
               p0_threshold=:p0_threshold, p1_threshold=:p1_threshold,
               notify_immediate=:notify_immediate, notify_digest=:notify_digest, updated_at=:updated_at""",
            d
        )


# ════════════════════════════════════════════════════════
# xueqiu_monitor_meta — incremental crawl metadata
# ════════════════════════════════════════════════════════

def get_last_crawl_time(db_path: str, stock_code: str) -> float:
    """返回该股票上次的 last_post_time，首次返回 0"""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT last_post_time FROM xueqiu_monitor_meta WHERE stock_code=?",
            (stock_code,)
        ).fetchone()
        return float(row["last_post_time"]) if row else 0.0


def get_existing_post_ids(db_path: str, stock_code: str, window_days: int = 30) -> set:
    """返回该股票最近 window_days 天内已存储的 post_id 集合，用于过滤去重。

    comments 表通过 snapshot_id → crawl_snapshots 间接关联 stock_code，
    因此用 JOIN 查询而非直接 comments.stock_code（该列不存在）。
    """
    cutoff = int(time.time()) - window_days * 86400
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT c.post_id FROM comments c
               JOIN crawl_snapshots cs ON c.snapshot_id = cs.id
               WHERE cs.stock_code = ? AND cs.crawl_time > ?""",
            (stock_code, cutoff)
        ).fetchall()
        return {r[0] for r in rows}


def update_last_crawl_time(db_path: str, stock_code: str, last_post_time: float) -> None:
    """记录本次爬取时间和帖子最新时间"""
    now = time.time()
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO xueqiu_monitor_meta (stock_code, last_crawl_time, last_post_time)
               VALUES (?, ?, ?)
               ON CONFLICT(stock_code) DO UPDATE SET
               last_crawl_time=excluded.last_crawl_time,
               last_post_time=excluded.last_post_time""",
            (stock_code, now, last_post_time)
        )