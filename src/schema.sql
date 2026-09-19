-- =============================================================================
-- xueqiu-monitor: SQLite 数据库 DDL
-- 8 张核心表 + 索引
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

-- Dedup: announcements are keyed by stock+dedup_hash (title+time identity in
-- detail JSON); other alert types by stock+type+alert_time, plus the hot-word
-- identity (detail.word) because detect_hot_word_emergence emits one alert per
-- word all sharing the same time.time() stamp — a word-blind index both fails
-- to build on legacy DBs (26 groups of distinct-word same-second rows) and
-- silently swallows every word after the first via INSERT OR IGNORE (v0.8.2).
-- COALESCE keeps NULL (non-hot-word types) collapsing to '' so those types
-- still dedup on (stock, type, time) alone.
CREATE UNIQUE INDEX IF NOT EXISTS uq_change_alert_announcement
    ON change_alert(stock_code, json_extract(detail, '$.dedup_hash'));
CREATE UNIQUE INDEX IF NOT EXISTS uq_change_alert_signal
    ON change_alert(stock_code, alert_type, COALESCE(json_extract(detail, '$.word'), ''), alert_time)
    WHERE alert_type != 'new_announcement';

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
    ann_link    TEXT    NOT NULL DEFAULT '',
    is_new      INTEGER NOT NULL DEFAULT 1,
    ann_detail  TEXT    NOT NULL DEFAULT '',
    FOREIGN KEY (snapshot_id) REFERENCES crawl_snapshots(id)
);

-- 11. detail_fetch_log — 详情抓取缓存 (v2 Phase 2, 2026-09-20)
-- news/公告正文按 link 幂等缓存, status 含失败标记 (error/garbled) 防当日反复重试;
-- 当日缓存次日过期 (get 侧按 fetched_at 过滤), 跨日公告解读允许更新。
CREATE TABLE IF NOT EXISTS detail_fetch_log (
    link       TEXT PRIMARY KEY,
    status     TEXT    NOT NULL,
    title      TEXT    NOT NULL DEFAULT '',
    content    TEXT    NOT NULL DEFAULT '',
    fetched_at INTEGER NOT NULL
);

-- 12. posts — 帖子独立表 (v2 Phase 4, 2026-09-20)
-- 从 crawl_snapshots.posts_data (JSON 数组塞一个 TEXT 列) 拆出:
-- 去重查询不再 json_each 全展开; (stock_code, dedup_key) 全局唯一约束
-- 根治 90 天窗口外老帖周期性重复入库。dedup_key = post_id→link 兜底,
-- 两者皆空为 NULL (SQLite 唯一索引 NULL 互不相等, 退化行不去重不冲突)。
-- posts_data 双写保留一个观察期, 之后写 '[]' (列不 DROP, 旧 JSON 归档)。
CREATE TABLE IF NOT EXISTS posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code      TEXT    NOT NULL,
    dedup_key       TEXT,
    post_id         TEXT    NOT NULL DEFAULT '',
    post_type       TEXT    NOT NULL DEFAULT '',
    title           TEXT    NOT NULL DEFAULT '',
    content         TEXT    NOT NULL DEFAULT '',
    author          TEXT    NOT NULL DEFAULT '',
    time_text       TEXT    NOT NULL DEFAULT '',
    post_ts         INTEGER NOT NULL DEFAULT 0,
    like_count      INTEGER NOT NULL DEFAULT 0,
    comment_count   INTEGER NOT NULL DEFAULT 0,
    forward_count   INTEGER NOT NULL DEFAULT 0,
    sentiment_score REAL    NOT NULL DEFAULT 0.0,
    link            TEXT    NOT NULL DEFAULT '',
    snapshot_id     INTEGER NOT NULL,
    first_seen_ts   INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (snapshot_id) REFERENCES crawl_snapshots(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_posts_stock_dedup ON posts(stock_code, dedup_key);
CREATE INDEX IF NOT EXISTS idx_posts_stock_seen ON posts(stock_code, first_seen_ts);
CREATE INDEX IF NOT EXISTS idx_posts_snapshot ON posts(snapshot_id);

-- 13. db_meta — 迁移进度标记等 KV (v2 Phase 4)
CREATE TABLE IF NOT EXISTS db_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 9. xueqiu_monitor_meta — 增量爬取元数据
CREATE TABLE IF NOT EXISTS xueqiu_monitor_meta (
    stock_code      TEXT UNIQUE NOT NULL,
    last_crawl_time REAL    NOT NULL DEFAULT 0.0,
    last_post_time  REAL    NOT NULL DEFAULT 0.0
);

-- 10. pool_history — 股票池轮换台账（v0.9 T1）
-- 每行一次 add/remove；按 effective_date 顺序重放，最后状态 = 现 report 池。
-- 回填由 scripts/backfill_pool_history.py 从 git 历史自动推导，不手工维护。
CREATE TABLE IF NOT EXISTS pool_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code      TEXT    NOT NULL,
    action          TEXT    NOT NULL CHECK(action IN ('add','remove')),
    effective_date  TEXT    NOT NULL,               -- YYYY-MM-DD
    reason          TEXT,
    avg_daily_posts REAL,                           -- 轮换时声量快照（14d 日均帖，出池复盘用）
    config_version  TEXT,                           -- 引入/移除该股的 config 版本
    UNIQUE(stock_code, effective_date, action)      -- 幂等：同股同日同动作只记一次
);
CREATE INDEX IF NOT EXISTS idx_pool_history_code ON pool_history(stock_code);
CREATE INDEX IF NOT EXISTS idx_pool_history_date ON pool_history(effective_date);

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
CREATE UNIQUE INDEX IF NOT EXISTS idx_comments_post  ON comments(post_id);

CREATE INDEX IF NOT EXISTS idx_ann_snapshot ON announcements(snapshot_id);
-- 2026-09-14 修: 唯一键加上 ann_date。原来只有 (stock_code, ann_title),
-- 导致每只股票的每个公告标题**一辈子只能存一行**: 同一家公司反复发的同类公告
-- (如"月度经营数据""股份回购进展")全部被 INSERT OR IGNORE 丢弃 ——
-- 实测 9/13 每只股票抓到 50 条公告、当天新增 0 行, 公告信号实际是断的。
CREATE UNIQUE INDEX IF NOT EXISTS idx_ann_title_date ON announcements(stock_code, ann_title, ann_date);
