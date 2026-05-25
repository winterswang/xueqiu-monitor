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

from models import (
    CrawlSnapshot, SentimentStat, ChangeAlert,
    HotWordDict, HotWordEvent, PushHistory,
    Comment, Announcement, ContentWeight, UserPreference,
)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str, schema_path: str | None = None) -> None:
    """Initialize database with schema."""
    if schema_path is None:
        schema_path = str(Path(__file__).parent / "schema.sql")
    conn = _connect(db_path)
    try:
        conn.executescript(Path(schema_path).read_text())
        conn.commit()
    finally:
        conn.close()


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
    d = stat.to_dict()
    del d["id"]
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO sentiment_stats (stock_code, stat_date, posts_count, sentiment_mean, sentiment_std, z_score, z_alert)
               VALUES (:stock_code, :stat_date, :posts_count, :sentiment_mean, :sentiment_std, :z_score, :z_alert)""",
            d
        )
        return cur.lastrowid


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
                """INSERT INTO comments (snapshot_id, post_id, comment_count, forward_count, like_count, sentiment_avg)
                   VALUES (:snapshot_id, :post_id, :comment_count, :forward_count, :like_count, :sentiment_avg)""",
                d
            )
            count += 1
        return count


# ════════════════════════════════════════════════════════
# announcements
# ════════════════════════════════════════════════════════

def insert_announcements(db_path: str, anns: list[Announcement]) -> int:
    with _connect(db_path) as conn:
        count = 0
        for a in anns:
            d = a.to_dict()
            del d["id"]
            conn.execute(
                """INSERT INTO announcements (snapshot_id, stock_code, ann_title, ann_date, ann_type, is_new)
                   VALUES (:snapshot_id, :stock_code, :ann_title, :ann_date, :ann_type, :is_new)""",
                d
            )
            count += 1
        return count


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


def upsert_weight(db_path: str, source: str, keyword: str, delta: float) -> float:
    """Adjust weight by delta. Returns new weight."""
    now = int(time.time())
    with _connect(db_path) as conn:
        existing = conn.execute(
            "SELECT * FROM content_weight WHERE source=? AND keyword=?",
            (source, keyword)
        ).fetchone()
        if existing:
            new_weight = max(0.0, existing["weight"] + delta)
            conn.execute(
                "UPDATE content_weight SET weight=?, updated_at=? WHERE source=? AND keyword=?",
                (new_weight, now, source, keyword)
            )
        else:
            new_weight = max(0.0, 1.0 + delta)
            conn.execute(
                "INSERT INTO content_weight (source, keyword, weight, updated_at) VALUES (?, ?, ?, ?)",
                (source, keyword, new_weight, now)
            )
        return new_weight


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
            """INSERT INTO user_preference (user_id, p0_threshold, p1_threshold, cold_start_days, notify_immediate, notify_digest, updated_at)
               VALUES (:user_id, :p0_threshold, :p1_threshold, :cold_start_days, :notify_immediate, :notify_digest, :updated_at)
               ON CONFLICT(user_id) DO UPDATE SET
               p0_threshold=:p0_threshold, p1_threshold=:p1_threshold, cold_start_days=:cold_start_days,
               notify_immediate=:notify_immediate, notify_digest=:notify_digest, updated_at=:updated_at""",
            d
        )
