"""xueqiu-monitor: SQLite storage layer (CRUD for all 10 tables)

All functions accept/return dataclass models from models.py.
No ORM — raw sqlite3 with parameterized queries.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

from .models import (
    CrawlSnapshot, SentimentStat, ChangeAlert,
    HotWordEvent, PushHistory,
    Comment, Announcement,
)

logger = logging.getLogger(__name__)


class _ClosingConnection:
    """Wrapper around sqlite3.Connection that closes on __exit__.

    sqlite3.Connection's context manager only commits — it does NOT close.
    This proxy delegates attribute access to the underlying connection but
    implements __exit__ to call both commit() and close(), preventing the
    connection leaks that affected all 30+ `with _connect() as conn:` sites.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __getattr__(self, name):
        """Delegate attribute access to the underlying connection."""
        return getattr(self._conn, name)

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None:
                self._conn.commit()
        finally:
            self._conn.close()
        return False


def _connect(db_path: str) -> _ClosingConnection:
    """Open SQLite connection with WAL mode, busy timeout, and retry.

    Per §3.4: waits 3s (busy_timeout=3000ms) and retries up to 3 times.
    Returns a _ClosingConnection wrapper so `with _connect(...) as conn:`
    commits AND closes (raw sqlite3 only commits).
    """
    for attempt in range(3):
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=3000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            return _ClosingConnection(conn)
        except sqlite3.OperationalError:
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
    # Add ann_link column to announcements (schema added 2026-08-04).
    # Old databases predate the column; ADD COLUMN is idempotent-guarded by PRAGMA.
    ann_cols = conn.execute("PRAGMA table_info(announcements)").fetchall()
    if not any(c[1] == "ann_link" for c in ann_cols):
        conn.execute(
            "ALTER TABLE announcements ADD COLUMN ann_link TEXT NOT NULL DEFAULT ''"
        )
        logger.info("[migrate] added column ann_link to announcements")

    # v0.7 F4: purge generic/noise words from hot_word_dict. Before v0.7 the
    # storage path stored every TF-IDF token without the alert path's stopword
    # filter, polluting the dict with words like ai/市场/这个/就是. This removes
    # them once (idempotent — re-running finds nothing to delete). The noise set
    # reuses detector._CN_STOPWORDS so storage and alert paths stay consistent.
    # v0.8.1: announcement dedup identity changed from title-only
    # (title_hash) to title+time (dedup_hash). Old databases carry the
    # over-broad title-only unique index, which permanently swallows
    # generic titles ("财报披露") that recur on different dates. Drop it
    # and rebuild on the new identity so legacy DBs are healed in place.
    # Idempotent: guarded by PRAGMA index_list.
    idxs = conn.execute("PRAGMA index_list(change_alert)").fetchall()
    if any(i[1] == "uq_change_alert_announcement" for i in idxs):
        # Expression indexes hide the expression in PRAGMA index_info, so read
        # the DDL from sqlite_master to tell old (title_hash) from new
        # (dedup_hash).
        row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND name='uq_change_alert_announcement'"
        ).fetchone()
        ddl = (row[0] if row and row[0] else "") or ""
        if "title_hash" in ddl:
            conn.execute("DROP INDEX uq_change_alert_announcement")
            conn.execute(
                "CREATE UNIQUE INDEX uq_change_alert_announcement "
                "ON change_alert(stock_code, json_extract(detail, '$.dedup_hash'))"
            )
            logger.info(
                "[migrate] rebuilt uq_change_alert_announcement on dedup_hash"
            )

    # v0.8.2: the original v0.8 signal index ignored detail.word, so it could
    # neither build on legacy DBs (distinct-word hot_word_surge alerts share
    # the same second) nor keep the 2nd+ word of a run. If the word-blind
    # variant somehow exists, rebuild it on the word-aware identity.
    if any(i[1] == "uq_change_alert_signal" for i in idxs):
        row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND name='uq_change_alert_signal'"
        ).fetchone()
        ddl = (row[0] if row and row[0] else "") or ""
        if "detail" not in ddl:
            conn.execute("DROP INDEX uq_change_alert_signal")
            conn.execute(
                "CREATE UNIQUE INDEX uq_change_alert_signal "
                "ON change_alert(stock_code, alert_type, "
                "COALESCE(json_extract(detail, '$.word'), ''), alert_time) "
                "WHERE alert_type != 'new_announcement'"
            )
            logger.info(
                "[migrate] rebuilt uq_change_alert_signal on word-aware identity"
            )

    # Drop feedback-loop tables (v0.7.3): content_weight / user_preference held
    # 0 rows for 3 months — feedback.py and its decay path were removed with no
    # remaining consumers. Idempotent: guarded by sqlite_master existence check.
    # v0.9 T3: xueqiu_monitor_meta_backup_0825 joins the dead-table list —
    # 8/25 手工备份残留（4 行，与活表同构），src/scripts/etc 零引用，git 历史可追溯。
    dead_tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('content_weight', 'user_preference', "
        "'xueqiu_monitor_meta_backup_0825')"
    ).fetchall()
    if dead_tables:
        conn.execute("DROP TABLE IF EXISTS content_weight")
        conn.execute("DROP TABLE IF EXISTS user_preference")
        conn.execute("DROP TABLE IF EXISTS xueqiu_monitor_meta_backup_0825")
        logger.info(
            "[migrate] dropped dead tables: %s",
            ", ".join(sorted(r[0] for r in dead_tables)),
        )

    try:
        from . import detector as _detector
        noise = _detector._CN_STOPWORDS
        placeholder = ",".join("?" * len(noise))
        cur = conn.execute(
            f"DELETE FROM hot_word_dict WHERE word IN ({placeholder})",
            tuple(noise),
        )
        if cur.rowcount:
            logger.info("[migrate] purged %d noise words from hot_word_dict", cur.rowcount)
    except Exception as e:  # detector import must never block init_db
        logger.warning("[migrate] hot_word_dict noise purge skipped: %s", e)


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
            """INSERT OR IGNORE INTO change_alert (stock_code, alert_time, alert_type, z_score, magnitude, detail, priority, filtered, filter_reason)
               VALUES (:stock_code, :alert_time, :alert_type, :z_score, :magnitude, :detail, :priority, :filtered, :filter_reason)""",
            d
        )
        if cur.rowcount == 0:
            return 0
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
                    """INSERT OR IGNORE INTO change_alert (stock_code, alert_time, alert_type, z_score, magnitude, detail, priority, filtered, filter_reason)
                       VALUES (:stock_code, :alert_time, :alert_type, :z_score, :magnitude, :detail, :priority, :filtered, :filter_reason)""",
                    d
                )
                rows.append(0 if cur.rowcount == 0 else cur.lastrowid)
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
                """INSERT OR IGNORE INTO announcements (snapshot_id, stock_code, ann_title, ann_date, ann_type, ann_link, is_new)
                   VALUES (:snapshot_id, :stock_code, :ann_title, :ann_date, :ann_type, :ann_link, :is_new)""",
                d
            )
            if cur.rowcount > 0:
                inserted += 1
        return inserted


def get_announcements_by_snapshot(db_path: str, snapshot_id: int) -> list[dict]:
    """Get announcements for a given snapshot_id (for change detection)."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT ann_title, ann_date, ann_type, ann_link FROM announcements WHERE snapshot_id=?",
            (snapshot_id,)
        ).fetchall()
        return [
            {"title": r["ann_title"], "time": str(r["ann_date"]), "notice_type": r["ann_type"], "link": r["ann_link"]}
            for r in rows
        ]


def get_recent_announcement_alerts(
    db_path: str, stock_code: str, dedup_hash: str, days: int = 7
) -> list[dict]:
    """Check if an announcement was already alerted within N days.

    Returns existing alerts matching stock_code + announcement dedup hash
    (title+time identity). Used for deduplication in detect_new_announcement.
    """
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
            if detail.get("dedup_hash") == dedup_hash:
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
