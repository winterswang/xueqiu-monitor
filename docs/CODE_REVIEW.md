# xueqiu-monitor Phase 1 代码审查报告

> 审查日期：2026-05-25 | 审查范围：src/ 全部 10 个 .py + schema.sql
> 对照基准：docs/requirements.md（v1，855行）
> 审查维度：需求对齐 / 错误处理 / 数据完整性 / 代码质量

---

## 总览

| 维度 | 状态 | 问题数 |
|------|------|--------|
| 1. 需求对齐（4.4节模块接口契约）| ⚠️ 多处偏差 | 14 |
| 2. 错误处理（3.4节）| ❌ 两项关键缺失 | 5 |
| 3. 数据完整性（5.1节数据模型）| ✅ 基本完整 | 2 |
| 4. 代码质量 | ⚠️ 可改进 | 12 |
| **总计** | | **33** |

---

## 1. 需求对齐 — 对照 §4.4「模块接口契约」

### 1.1 爬虫调度模块（crawler.py）

**问题 1-A** | 严重度: P2 | 函数名与需求不一致
- 文件: `src/crawler.py:80`
- 需求定义: `crawl_stock(stock_code, timeout=30)`（L300）
- 当前: `crawl_single_stock`
- 影响: 调用方按需求文档引用 `crawler.crawl_stock` 报 AttributeError
- 建议: 添加 `crawl_stock = crawl_single_stock` 别名

**问题 1-B** | 严重度: P1 | crawl_time 类型与需求不符
- 文件: `src/crawler.py:87、95`
- 需求: `crawl_time` 为 ISO 8601 字符串如 `"2024-01-15T09:30:00"`（L313）
- 当前: `int(time.time())` 返回 Unix 时间戳整数
- 影响: 下游按需求期望解析 ISO 8601 字符串会格式错误
- 建议: 统一为 int 并更新需求，或改用 `datetime.now().isoformat()`

**问题 1-C** | 严重度: P1 | posts_data 字段缺失 5 项
- 文件: `src/crawler.py:101-137`
- 需求（L316-328）每帖必含: post_id, title, content, author, author_id, created_at(ISO), comment_count, forward_count, like_count, sentiment_score
- 当前仅含: type, post_id, title, content, author, time(原始字符串), sentiment_score(0.0)
- 缺失: author_id, created_at(ISO), comment_count, forward_count, like_count
- 建议: 从 xueqiu-analyzer CrawlResult 解析更多字段

**问题 1-D** | 严重度: P1 | announcements 字段名不匹配
- 文件: `src/crawler.py:137-141`
- 需求（L331-338）: ann_id, title, date(YYYY-MM-DD), type, is_new
- 当前: title, time(原始字符串), notice_type — 缺失 ann_id/is_new
- 建议: 补全字段并统一命名

**问题 1-E** | 严重度: P0 | timeout 参数未实际生效
- 文件: `src/crawler.py:80、102-103`
- 需求: 爬虫超时(>30s)记录超时状态并跳过该股票（L194）
- 当前: `crawler.crawl()` 无 timeout 包裹，挂死时调度卡住
- 建议:
```python
import concurrent.futures
with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
    future = executor.submit(crawler.crawl, stock_code, max_pages=3, max_articles=10)
    try:
        crawl_result = future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        result["status"] = "timeout"
        return result
```

### 1.2 数据存储模块（db.py）

**问题 2-A** | 严重度: P2 | 缺少 `get_snapshots_by_date_range`
- 文件: `src/db.py`（全局）
- 需求（L385-397）: `get_snapshots_by_date_range(stock_code, start_date, end_date) -> list[dict]`
- 当前: 函数不存在
- 建议: 补充实现

**问题 2-B** | 严重度: P1 | 缺少 `update_sentiment_stats`（按日聚合更新）
- 文件: `src/db.py`（全局）
- 需求（L399-410）: `update_sentiment_stats(stock_code, stat_date) -> int`
- 当前: 仅 `insert_sentiment_stat`，同一天多次调度产生重复记录
- 建议: 实现 upsert（ON CONFLICT 或先查后更新）

**问题 2-C** | 严重度: P2 | 接口参数风格不一致
- 文件: `src/db.py`（全局）
- 需求: 函数接受业务对象，无 db_path 参数
- 当前: 所有函数首参为 `db_path: str`
- 建议: 封装为 Database 类

### 1.3 变化检测模块（detector.py）

**问题 3-A** | 严重度: P2 | 缺少统一检测入口 `detect_changes`
- 文件: `src/detector.py`（全局）
- 需求（L451-475）: `detect_changes(curr_snapshot, prev_snapshot, historical_stats) -> list[dict]`
- 当前: 拆为 `detect_post_spike` / `detect_sentiment_shift` / `detect_hot_word_emergence`，cli.py 手动编排
- 建议: 实现 `detect_changes` 作为编排层

**问题 3-B** | 严重度: P0 | `new_announcement` 告警类型完全缺失
- 文件: `src/detector.py`（全局）
- 需求: 4 种告警类型（sentiment_shift / hot_word_surge / post_spike / new_announcement）（L53-60）
- 当前: 仅实现前三种，公告差异对比缺失
- 建议: 补充 `detect_new_announcement(curr_anns, prev_anns)`

**问题 3-C** | 严重度: P2 | detect_post_spike 未使用 prev_snapshot
- 文件: `src/detector.py:51-73`
- 需求（L55-56）: 需两期情感值对比 |S2-S1| > 0.2 触发告警
- 当前: 仅基于历史 stats Z-score，缺少直接情感偏移阈值
- 建议: 在 detect_sentiment_shift 增加 `abs(curr - prev) > 0.2` 条件

### 1.4 规则筛选模块（filter.py）

**问题 4-A** | 严重度: P2 | cold_start 参数语义不符
- 文件: `src/filter.py:117`
- 需求: `cold_start_days: int = 28`，内部自行判断
- 当前: 接收 `cold_start: bool`，判断外移
- 建议: 改为接收 cold_start_days 内省判断，或文档说明设计选择

**问题 4-B** | 严重度: P0 | filter_alerts 过滤逻辑误杀所有告警
- 文件: `src/filter.py:131-137`
- 问题: 检测到任意广告帖即标记**所有** alert 为 filtered
```python
for alert in alerts:
    if ad_set:  # ← 只要存在任何广告帖，全部告警被丢弃
        alert.filtered = 1
```
- 影响: 1条广告帖就丢弃所有检测结果，误杀率极高
- 建议: 按比例判断（如噪音帖超过 50% 才标记该告警）

### 1.5 分级通知模块（notifier.py）

**问题 5-A** | 严重度: P1 | push_immediate 缺少 key_data 参数
- 文件: `src/notifier.py:91-97`
- 需求（L527-549）: `push_immediate(alert: dict, key_data: dict)`，key_data 含 stock_name, alert_type, z_score, sentiment_avg, sentiment_shift, posts_count, hot_words, post_titles
- 当前: `push_immediate(alert, webhook_url, stock_name, timeout)` — 缺 key_data
- 影响: 推送卡片仅含 Z-score 和幅度，无热词/帖子数/高互动标题
- 建议: 丰富推送卡片，至少包含需求 L97-113 的 13 个关键字段

**问题 5-B** | 严重度: P1 | push_digest 同理缺少 key_data
- 文件: `src/notifier.py:100-129`
- 需求: `push_digest(alerts, key_data_list)`（L551-562）
- 当前: 无 key_data_list 参数

**问题 5-C** | 严重度: P2 | generate_daily_report 格式不完整
- 文件: `src/notifier.py:154-156`
- 需求（L115-119）: `股票代码 | 告警类型 | Z-score | 变化值 | 最新帖子标题`
- 当前: 缺少「最新帖子标题」列、「变化值」无方向指示（↑/↓+百分比）

### 1.6 用户反馈闭环模块（feedback.py）

**问题 6-A** | 严重度: P2 | record_feedback 返回值类型不一致
- 文件: `src/feedback.py:16`
- 需求（L587）: `record_feedback(push_id, verdict) -> bool`
- 当前: `record_feedback(db_path, push_id, verdict, config) -> float | None`
- 建议: 统一返回值或文档说明

---

## 2. 错误处理 — 对照 §3.4「错误处理」

### 2.1 爬虫超时（>30s）

- 状态: ❌ **未实际处理**
- 文件: `src/crawler.py:80-103`
- timeout 参数接受但未应用（见问题 1-E）
- 严重度: P0

### 2.2 SQLite 并发锁定重试

- 状态: ❌ **未实现**
- 文件: `src/db.py:22-27`
- 需求: "等待 3 秒重试，最多 3 次"（L196）
- 当前: WAL 模式已开启但未设 `busy_timeout`，无重试逻辑
- 严重度: P0
- 建议:
```python
def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
```

### 2.3 飞书推送失败处理

- 状态: ✅ 基本正确
- 文件: `src/notifier.py:29-36` / `src/cli.py:185`
- `_send_webhook` 返回 bool，cli.py 正确标记 status="success"/"failed"
- 需求（L197）"下一次调度重试"自然满足

### 2.4 冷启动期（28天）静默

- 状态: ⚠️ 语义偏差
- 文件: `src/detector.py:188-194` / `src/cli.py:107`
- `is_cold_start` 统计 sentiment_stats 的 unique_dates，非日历天数
- 若系统断续运行，unique_dates 远小于日历天数，误判冷启动
- 建议: 基于 crawl_snapshots 最早时间戳计算日历天数

### 2.5 单只股票失败不阻塞

- 状态: ✅ 正确实现
- 文件: `src/crawler.py:148-158`
- `crawl_watchlist` 循环独立调用，异常在单只内部捕获

---

## 3. 数据完整性 — 对照 §5.1「数据模型」

### 3.1 表完整性

| 需求表名 | schema.sql | 状态 |
|----------|-----------|------|
| crawl_snapshots | L8-17 | ✅ |
| sentiment_stats | L20-31 | ✅ |
| change_alert | L34-46 | ✅（多 detail/filter_reason 合理扩展） |
| hot_word_dict | L49-54 | ✅ |
| hot_word_event | L57-64 | ✅ |
| push_history | L67-76 | ✅ |
| comments | L79-89 | ✅ |
| announcements | L92-100 | ✅ |
| content_weight | L103-108 | ⚠️ 缺 preference_level |
| user_preference | L111-120 | ✅ |

### 3.2 字段缺失

**问题 7-A** | 严重度: P2
- 文件: `src/schema.sql:103-108` / `src/models.py:316-344`
- 需求（L776）: content_weight 需要 `preference_level` REAL 字段（0.0-2.0，默认1.0）
- 当前: 仅有 `weight` 字段
- 建议: 确认后补上或更新需求文档说明 weight 已覆盖该维度

### 3.3 索引覆盖

全部高频查询路径有索引，**无缺失**。关键索引: idx_crawl_stock_time, idx_senti_stock_date, idx_alert_priority, idx_hwe_time, idx_cw_unique。

### 3.4 外键约束

- push_history.alert_id → change_alert.id ✅
- comments.snapshot_id → crawl_snapshots.id ✅
- announcements.snapshot_id → crawl_snapshots.id ✅

---

## 4. 代码质量

### 4.1 SQL 注入防护

✅ **全部通过** — 所有查询使用参数化 `?` / `:param`，无字符串拼接

### 4.2 类型注解

**问题 8-A** | 严重度: P2 | 多处 Any 泛化
- `crawler.py:33` `def load_watchlist(config: dict)` — 建议 `dict[str, Any]`
- `cli.py:48` `def run_pipeline(...) -> dict` — 建议 TypedDict
- `notifier.py:134` `posts_data_map` 参数声明但从未使用

### 4.3 异常链保留

**问题 8-B** | 严重度: P2
- `crawler.py:125-128` `str(e)` 传递异常信息丢失 traceback
- 场景下不重新抛出是合理的，但建议日志保留完整 exc_info

### 4.4 死代码 / 未使用导入

| 文件 | 行号 | 未使用导入 | 状态 |
|------|------|-----------|------|
| db.py | 9 | `import json` | ❌ 未使用 |
| db.py | 13 | `from typing import Any` | ❌ 未使用 |
| detector.py | 14 | `from collections import Counter` | ❌ 未使用 |
| detector.py | 16 | `from typing import Any` | ❌ 未使用 |
| filter.py | 11 | `import re` | ❌ 未使用 |
| filter.py | 14 | `from typing import Any` | ❌ 未使用 |
| notifier.py | 10 | `import urllib.error` | ❌ 未使用 |
| feedback.py | 10 | `from typing import Any` | ❌ 未使用 |

### 4.5 日志覆盖

| 模块 | 关键操作 | 日志 | 状态 |
|------|----------|------|------|
| crawler.py | 爬取成功/失败 | INFO/ERROR | ✅ |
| db.py | CRUD 操作 | 无 | ❌ 完全无日志 |
| detector.py | 检测到告警 | 无 | ❌ 完全无日志 |
| filter.py | 过滤操作 | 无 | ❌ 无过滤日志 |
| notifier.py | 推送成功/失败 | WARNING/ERROR | ✅ |
| feedback.py | 权重调整 | INFO | ✅ |
| cli.py | 管道进度 | INFO/DEBUG | ✅ |

### 4.6 其他代码问题

**问题 9-A** | 严重度: P1 | requirements.txt 依赖不全
- 文件: `requirements.txt`
- 需求（L222-244）要求: pandas>=2.0.0, requests>=2.28.0, python-json-logger>=2.0.0, pytest>=7.4.0, pytest-cov>=4.1.0, mypy>=1.5.0
- 当前仅含: numpy>=1.24.0, scikit-learn>=1.3.0
- 缺失 6 个依赖

**问题 9-B** | 严重度: P0 | comments/announcements 表从不写入
- 文件: `src/cli.py:82-169`
- `run_pipeline` 爬取后有 posts_data 和 announcements 但未调用 `db.insert_comments` / `db.insert_announcements`
- 两张表永远为空

**问题 9-C** | 严重度: P2 | cli.py 内层 logger 遮蔽
- 文件: `src/cli.py:59`
- `logger = logging.getLogger(__name__)` 在 run_pipeline 内部重新定义，与模块级冲突虽不报错但为不良实践

**问题 9-D** | 严重度: P2 | detector.py 窗口重复切片
- 文件: `src/detector.py:60`
- `historical_stats[-window_days:]` 已在调用方（cli.py L103）限定窗口，重复切片

**问题 9-E** | 严重度: P2 | 时区处理
- 文件: `src/cli.py:128`
- `today_start = now // 86400 * 86400` 基于 UTC，中国时区下日期界限偏差

**问题 9-F** | 严重度: P2 | sentiment_std 硬编码 0.0
- 文件: `src/cli.py:134`
- `sentiment_std=0.0` 未基于历史数据计算，影响后续 Z-score 准确性

---

## 严重程度总表

### P0（阻塞 — 6 项）

| # | 问题 | 文件:行号 |
|---|------|----------|
| 1-E | timeout 参数未实际生效 | crawler.py:80/102 |
| 2.2 | SQLite 锁定无重试 | db.py:22-27 |
| 3-B | new_announcement 检测缺失 | detector.py（全局） |
| 4-B | filter_alerts 误杀逻辑 | filter.py:131-137 |
| 9-B | comments/announcements 表从不写入 | cli.py:82-169 |
| 2.1 | 爬虫超时无超时机制 | crawler.py:102-103 |

### P1（接口偏差 — 6 项）

| # | 问题 | 文件:行号 |
|---|------|----------|
| 1-B | crawl_time 类型错误 | crawler.py:87/95 |
| 1-C | posts_data 字段缺失 | crawler.py:101-137 |
| 1-D | announcements 字段名不匹配 | crawler.py:137-141 |
| 2-B | 缺少 update_sentiment_stats | db.py（全局） |
| 5-A | push_immediate 缺 key_data | notifier.py:91-97 |
| 9-A | requirements.txt 缺失 6 个依赖 | requirements.txt |

### P2（改进建议 — 21 项）

| # | 问题 | 文件:行号 |
|---|------|----------|
| 1-A 到 9-F | 详见各节 | 见上文 |

---

## 修复路线图

```
第1轮（今天必须修）：
├── 1-E + 2.1: crawler timeout 包裹（concurrent.futures）
├── 4-B: filter_alerts 按比例判断
├── 9-B: cli.py 写入 comments/announcements
└── 2.2: SQLite busy_timeout + retry

第2轮（本周修）：
├── 1-B/1-C/1-D: posts_data + announcements 字段补全
├── 3-B: new_announcement 检测
├── 5-A/5-B: notifier 推送卡片丰富
└── 9-A: requirements.txt 补全

第3轮（发布前）：
├── 7-A: preference_level 字段确认
├── 死导入清理（8 处）
├── db/detector/filter 补充日志
├── 3-A: 统一 detect_changes 入口
└── 9-C~9-F: 代码卫生改进
```

---

## PRAISE

- 数据模型与需求 10 张表完整对齐，索引覆盖高频查询 ✅
- 模块层次清晰: schema.sql → models.py → db.py → 业务模块 → cli.py ✅
- SQL 注入防护完备，全参数化查询 ✅
- 单只失败不阻塞其他股票 ✅
- 冷启动 + 权重衰减策略正确 ✅
- 配置灵活（JSON + 环境变量覆盖） ✅

---

**总结**: 代码骨架扎实，核心流程（爬取→存储→检测→筛选→通知→反馈）已跑通。6 个 P0 问题集中在超时、过滤逻辑、新公告检测和表写入缺失，修复后可直接进入冷启动数据积累阶段。
