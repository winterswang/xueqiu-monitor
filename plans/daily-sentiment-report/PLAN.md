# 自选股情绪看板 — PLAN

> 基于 xueqiu-monitor 现有数据，每日生成自选股全景情绪日报。
> 不替代 monitor，只概括 monitor。

---

## 1. 背景

### 1.1 已有能力

xueqiu-monitor 已经具备：
- 64 只自选股定时爬取（SQLite 存储）
- 帖子情感分析（MiniMax LLM + 关键词降级）
- Z-score 变化检测 + 飞书告警
- TF-IDF 热词追踪

### 1.2 缺什么

xueqiu-monitor 是**告警系统**——单只股票异常时推送到飞书。但它缺少一个**全景看板**：

| 用户想知道 | 现在怎么回答 |
|-----------|-------------|
| 今天 64 只里哪 5 只最火？ | 翻飞书历史消息，逐条看告警 |
| 白酒板块整体情绪偏多还是偏空？ | 自己汇总所有白酒股的零散告警 |
| 今天有什么新热词在跨股票出现？ | 没有现成工具 |
| 我关注的板块趋势在向上还是向下？ | 凭感觉 |

### 1.3 定位

> 每天早上 8:30 推送一份 Markdown 日报到飞书，告诉你 64 只自选股昨天发生了什么。

---

## 2. 需求

### 2.1 功能列表

| ID | 功能 | 优先级 | 说明 |
|----|------|--------|------|
| F-001 | 读取今日所有股票的快照数据 | P0 | 从 monitor.db 的 crawl_snapshots 表读最近一次快照 |
| F-002 | 按讨论量排序 TOP5 热度 | P0 | 基于 posts_count（已有字段） |
| F-003 | 情感聚合：每只股票的多空得分 | P0 | 遍历 posts_data 中的 sentiment_score（已有字段） |
| F-004 | 行业板块聚合：按行业归类计算 | P0 | 需要一个行业映射表 |
| F-005 | 跨股票热词矩阵 | P1 | 读取 hot_word_dict 表（已有），交叉到股票维度 |
| F-006 | 异常信号联动 | P1 | 读取 change_alert 表（已有），标记 Z>2.0 的股票 |
| F-007 | Markdown 日报渲染 | P0 | 模板化输出 |
| F-008 | 飞书推送 | P0 | 复用 monitor 现有推送或 lark CLI |
| F-009 | 历史对比（昨日 vs 今日） | P2 | 需要跨日数据比较 |
| F-010 | cron 定时执行 | P0 | 每天早上 8:30 |

### 2.2 行业映射

需要维护一个 `etc/sectors.json`，格式：

```json
{
  "SH600519": {"name": "贵州茅台", "sector": "白酒"},
  "09992":    {"name": "泡泡玛特", "sector": "潮玩"},
  "PDD":      {"name": "拼多多",   "sector": "电商"},
  "00700":    {"name": "腾讯控股", "sector": "互联网"}
}
```

注：此表与 xueqiu-monitor 的 `etc/config.json` 无关，独立维护。

### 2.3 输出格式

见 output 附录。

---

## 3. 技术方案

### 3.1 架构

```
xueqiu-monitor/sqlite  ──► scripts/daily_report.py
      │                           │
      ├─ crawl_snapshots          ├─ 读今日快照
      ├─ sentiment_stats          ├─ 聚合情感
      ├─ change_alert             ├─ 读异常信号
      └─ hot_word_dict            └─ 读热词
                                         │
                                         ▼
                                   data/reports/{date}.md
                                         │
                                         ▼
                                   lark CLI → 飞书群
```

### 3.2 模块结构（单文件 ~250 行）

```
scripts/daily_sentiment_report.py

函数                                  职责
────────────────────────────────────────────────
main()                               入口：读取→聚合→渲染→推送
load_config()                        读取 etc/sectors.json
read_snapshots(db, date_range)       从 SQLite 读取快照
aggregate_sentiment(posts_data)      对帖子跑情感聚合（均值/多空比）
aggregate_by_sector(stocks)          按行业汇总
build_hotword_matrix(hotwords)       构建热词 × 股票矩阵
render_report(stats, alerts, matrix) Markdown 渲染
push_report(markdown)                推送到飞书
```

### 3.3 数据依赖

| 数据 | 表名 | 字段 | 已有 |
|------|------|------|------|
| 快照基础数据 | `crawl_snapshots` | `stock_code`, `posts_count`, `posts_data`, `crawl_time` | ✅ |
| 帖子详情 | `posts_data`（JSON） | `content`, `sentiment_score` | ✅ |
| 情感统计 | `sentiment_stats` | 可选，当前 snapshot 已有 | ✅ |
| 变化告警 | `change_alert` | `stock_code`, `z_score`, `reason`, `created_at` | ✅ |
| 热词 | `hot_word_dict` | `word`, `stock_code`, `frequency` | ✅ |
| 行业映射 | `etc/sectors.json` | 新增 | ⏳ |

### 3.4 情感聚合逻辑

```python
def aggregate_sentiment(posts_json):
    posts = json.loads(posts_json)
    scores = [p.get("sentiment_score", 0) for p in posts]
    positive = sum(1 for s in scores if s > 0.1)
    negative = sum(1 for s in scores if s < -0.1)
    neutral = len(scores) - positive - negative
    return {
        "total": len(scores),
        "avg_score": sum(scores) / len(scores) if scores else 0,
        "positive_ratio": positive / len(scores) if scores else 0,
        "negative_ratio": negative / len(scores) if scores else 0,
        "neutral_ratio": neutral / len(scores) if scores else 0,
    }
```

### 3.5 日报文件结构

```
data/reports/
├── 2026-06-01.md      # 今日日报
├── 2026-05-31.md      # 昨日日报（历史对比用）
└── ...
```

保留最近 30 天的日报，超出自动清理。

---

## 4. 执行计划

### Phase 1：数据读取 + 情感聚合（Day 1）

| 任务 | 输出 |
|------|------|
| 创建 `scripts/daily_report.py` | 能读取 SQLite，输出每只股票的统计 dict |
| 创建 `etc/sectors.json`（64 只映射） | 完成行业分类 |
| 实现 `aggregate_sentiment()` | 验证与 monitor 现有数据一致 |

### Phase 2：板块聚合 + 日报模板（Day 2）

| 任务 | 输出 |
|------|------|
| 实现 `aggregate_by_sector()` | 板块统计结果 |
| 创建 Markdown 模板 | 渲染出美观的日报 |
| 手动测试一次完整输出 | `data/reports/2026-06-01.md` |

### Phase 3：热词矩阵 + 异常联动（Day 3）

| 任务 | 输出 |
|------|------|
| 实现 `build_hotword_matrix()` | 热词矩阵表格 |
| 读取 `change_alert` 表 | 加上 Z > 2.0 标注 |
| 生成完整日报 | 包含热度/情感/板块/热词/异常 |

### Phase 4：推送 + 定时（Day 4）

| 任务 | 输出 |
|------|------|
| 集成 lark CLI 推送 | 飞书群收到日报 |
| 配置 cron `30 8 * * *` | 每天早上 8:30 自动执行 |
| 30 天日志保留清理 | `find data/reports/ -mtime +30 -delete` |

---

## 5. 风险

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| sentiment_score 全部为 0 | 中 | 高 | 降级为纯关键词打分 |
| 部分股票当日无数据（爬取失败） | 低 | 低 | 输出标注"数据不足" |
| 飞书推送限频 | 低 | 中 | 日报切分成 2000 字以内 |
| 行业映射缺失 | 中 | 中 | 按股票代码自动归类：A=沪深/港股/美股 |

---

## 附录：日报输出模板

```markdown
☀️ 自选股情绪日报 — {date}
═══════════════════════════════════════
覆盖: {stock_count} 只 | 新增帖子: {post_count} 条

📈 整体概况
  看多: {bull_pct}% | 看空: {bear_pct}% | 中性: {neu_pct}%
  {summary_sentence}

───────────────────────────────────────
🔥 今日热度 TOP5

{for each:
  {rank}. {name} {symbol}   {delta}{marker} 帖  情绪: {sentiment_label}  {reason}
}

───────────────────────────────────────
📊 行业板块情绪

{for each:
  {sector}  {bull}%  {bar}  {trend_label} {trend_marker}
}

───────────────────────────────────────
⚡ 异常信号

{for z > 2.0:
  {name} — Z={z_score} ({reason})
  → {detail}
}

───────────────────────────────────────
💬 热词矩阵（仅展示频率 > 5 的）

{hotword_table}
```
