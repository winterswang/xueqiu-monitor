-- =============================================================================
-- xueqiu-monitor: SQLite 数据库 DDL
-- 10 张核心表 + 索引
-- 时间戳: INTEGER (unix), 情感值/Z-score: REAL
-- =============================================================================

PRAGMA foreign_keys = ON;

-- 1. crawl_snapshots — 爬取快照表
CREATE TABLE IF NOT EXISTS crawl_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code    TEXT    NOT NULL,
    crawl_time    INTEGER NOT NULL,
    posts_count   INTEGER NOT NULL DEFAULT 0,
    posts_data    TEXT    NOT NULL DEFAULT '[]',       -- JSON array
    sentiment_avg REAL    NOT NULL DEFAULT 0.0,
    status        TEXT    NOT NULL DEFAULT 'pending'   -- success/failed/timeout
);

-- 2. sentiment_stats — 情感统计表（按日聚合）
CREATE TABLE IF NOT EXISTS sentiment_stats (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code     TEXT    NOT NULL,
    stat_date      INTEGER NOT NULL,                  -- unix timestamp at 00:00:00
    posts_count    INTEGER NOT NULL DEFAULT 0,
    sentiment_mean REAL    NOT NULL DEFAULT 0.0,
    sentiment_std  REAL    NOT NULL DEFAULT 0.0,
    z_score        REAL    NOT NULL DEFAULT 0.0,
    z_alert        INTEGER NOT NULL DEFAULT 0         -- 0/1
);

-- 3. change_alert — 变化告警表
CREATE TABLE IF NOT EXISTS change_alert (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT    NOT NULL,
    alert_time INTEGER NOT NULL,
    alert_type TEXT    NOT NULL,                       -- sentiment_shift/hot_word_surge/post_spike/new_announcement
    z_score    REAL    NOT NULL DEFAULT 0.0,
    magnitude  REAL    NOT NULL DEFAULT 0.0,
    detail     TEXT    NOT NULL DEFAULT '{}',          -- JSON
    priority   TEXT    NOT NULL DEFAULT 'P2',          -- P0/P1/P2
    filtered   INTEGER NOT NULL DEFAULT 0,            -- 0/1
    filter_reason TEXT DEFAULT NULL
);

-- 4. hot_word_dict — 热词词典
CREATE TABLE IF NOT EXISTS hot_word_dict (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    word      TEXT    NOT NULL UNIQUE,
    frequency INTEGER NOT NULL DEFAULT 1,
    last_seen INTEGER NOT NULL
);

-- 5. hot_word_event — 热词事件
CREATE TABLE IF NOT EXISTS hot_word_event (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code  TEXT    NOT NULL,
    word        TEXT    NOT NULL,
    tfidf_score REAL    NOT NULL DEFAULT 0.0,
    event_time  INTEGER NOT NULL,
    z_score     REAL    NOT NULL DEFAULT 0.0
);

-- 6. push_history — 推送历史
CREATE TABLE IF NOT EXISTS push_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT    NOT NULL,
    alert_id   INTEGER NOT NULL,
    push_time  INTEGER NOT NULL,
    priority   TEXT    NOT NULL DEFAULT 'P2',
    content    TEXT    NOT NULL DEFAULT '',
    status     TEXT    NOT NULL DEFAULT 'pending',    -- success/failed/pending
    FOREIGN KEY (alert_id) REFERENCES change_alert(id)
);

-- 7. comments — 评论快照表
CREATE TABLE IF NOT EXISTS comments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id    INTEGER NOT NULL,
    post_id        TEXT    NOT NULL,
    comment_count  INTEGER NOT NULL DEFAULT 0,
    forward_count  INTEGER NOT NULL DEFAULT 0,
    like_count     INTEGER NOT NULL DEFAULT 0,
    sentiment_avg  REAL    NOT NULL DEFAULT 0.0,
    FOREIGN KEY (snapshot_id) REFERENCES crawl_snapshots(id)
);

-- 8. announcements — 公告快照表
CREATE TABLE IF NOT EXISTS announcements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL,
    stock_code  TEXT    NOT NULL,
    ann_title   TEXT    NOT NULL DEFAULT '',
    ann_date    INTEGER NOT NULL,
    ann_type    TEXT    NOT NULL DEFAULT '',
    is_new      INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (snapshot_id) REFERENCES crawl_snapshots(id)
);

-- 9. content_weight — 内容权重（反馈闭环）
CREATE TABLE IF NOT EXISTS content_weight (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT    NOT NULL,                      -- stock_code/author_id
    keyword     TEXT    NOT NULL,
    weight      REAL    NOT NULL DEFAULT 1.0,
    updated_at  INTEGER NOT NULL
);

-- 10. user_preference — 用户偏好
CREATE TABLE IF NOT EXISTS user_preference (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          TEXT    NOT NULL,
    p0_threshold     REAL    NOT NULL DEFAULT 3.0,
    p1_threshold     REAL    NOT NULL DEFAULT 2.0,
    cold_start_days  INTEGER NOT NULL DEFAULT 28,
    notify_immediate INTEGER NOT NULL DEFAULT 1,       -- P0即时推
    notify_digest    INTEGER NOT NULL DEFAULT 1,       -- P1汇总推
    updated_at       INTEGER NOT NULL
);

-- =============================================================================
-- 索引
-- =============================================================================
CREATE INDEX IF NOT EXISTS idx_crawl_stock_code ON crawl_snapshots(stock_code);
CREATE INDEX IF NOT EXISTS idx_crawl_time       ON crawl_snapshots(crawl_time);
CREATE INDEX IF NOT EXISTS idx_crawl_stock_time ON crawl_snapshots(stock_code, crawl_time);

CREATE INDEX IF NOT EXISTS idx_senti_stock_code ON sentiment_stats(stock_code);
CREATE INDEX IF NOT EXISTS idx_senti_stat_date  ON sentiment_stats(stat_date);
CREATE INDEX IF NOT EXISTS idx_senti_stock_date ON sentiment_stats(stock_code, stat_date);

CREATE INDEX IF NOT EXISTS idx_alert_stock_code ON change_alert(stock_code);
CREATE INDEX IF NOT EXISTS idx_alert_time       ON change_alert(alert_time);
CREATE INDEX IF NOT EXISTS idx_alert_type       ON change_alert(alert_type);
CREATE INDEX IF NOT EXISTS idx_alert_priority   ON change_alert(priority);

CREATE INDEX IF NOT EXISTS idx_hwe_stock_code ON hot_word_event(stock_code);
CREATE INDEX IF NOT EXISTS idx_hwe_word       ON hot_word_event(word);
CREATE INDEX IF NOT EXISTS idx_hwe_time       ON hot_word_event(event_time);

CREATE INDEX IF NOT EXISTS idx_push_stock_code ON push_history(stock_code);
CREATE INDEX IF NOT EXISTS idx_push_alert_id   ON push_history(alert_id);
CREATE INDEX IF NOT EXISTS idx_push_time       ON push_history(push_time);

CREATE INDEX IF NOT EXISTS idx_comments_snap  ON comments(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_comments_post  ON comments(post_id);

CREATE INDEX IF NOT EXISTS idx_ann_snapshot ON announcements(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_ann_stock    ON announcements(stock_code);

CREATE INDEX IF NOT EXISTS idx_cw_source  ON content_weight(source);
CREATE INDEX IF NOT EXISTS idx_cw_keyword ON content_weight(keyword);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cw_unique ON content_weight(source, keyword);

CREATE UNIQUE INDEX IF NOT EXISTS idx_up_user_id ON user_preference(user_id);
