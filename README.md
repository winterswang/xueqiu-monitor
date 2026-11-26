# xueqiu-monitor — 自选股雪球舆情持续监控

面向 A 股/港股投资者的舆情监控系统。爬取雪球帖子、资讯、公告，通过 **LLM 情感分析（字节 coding plan，经火山方舟）+ Z-score 统计检测** 自动发现舆情实质性变化，**分级推送到飞书群**。

> 区别于全文推送：95% 的雪球内容是灌水/重复/噪音。本系统只推送有增量信息的变化。
> 区别于 morning-brief：morning-brief 看量价（K 线/PE/技术面），xueqiu-monitor 看舆情（情绪面），两套独立互补。

---

## 核心能力

### 🔍 智能爬取
- 调用 [xueqiu-analyzer](https://github.com/winterswang/xueqiu-analyzer-skill) 的 Playwright 爬虫
- 自动翻页、登录弹窗处理、cookies 持久化
- 增量爬取：只拉取上次以来的新帖，支持「回查兜底」防止漏帖
- 超时保护 + 单股失败不阻塞

### 🧠 情感分析
- **字节 coding plan（火山方舟，OpenAI 兼容）** 批量分析讨论帖情感（-1 到 +1）；模型 id 的唯一来源是 `etc/config.report.json` 的 `llm.model`（当前 `deepseek-v4-1-flash-260910`，经 `src/llm_config.py` 解析，不读环境变量）
- 新闻标题 **关键词匹配**（大涨/暴跌/增持/减持 等），零 API 成本
- 无 LLM key 时自动降级为纯关键词模式

### 📄 详情富化（v2）
- **news 全文**：三道过滤闸（时效 / 噪音 / 限量）后，用 opencli 驱动本地 Chrome 抓正文，零 API 成本
- **公告详情**：A 股/港股 PDF 本地解析（pymupdf）；美股公告按标题里的 accession number 直连 SEC EDGAR 原文
- 两者都拿不到时才降级搜标题拿「新闻解读」（产物标注来源）；智谱 reader 仅作兜底
- 单条失败不阻塞日报，结果按 link 当日缓存

### 📊 变化检测
- **Z-score 统计检测**：帖子数异常暴增、情感倾向显著偏移（14 天滚动窗口）
- **两期直接对比**：相邻快照情感偏移 > 0.2 即时触发
- **TF-IDF 热词涌现**：新热词突增告警
- **新公告检测**：对比上下两期公告列表，发现新增公告
- **冷启动保护**：运行不足 28 天时静默积累，不误报

### 🎯 分级过滤
- **P0 即时推**：Z > 3.0 的重大变化，逐条推送
- **P1 汇总推**：2.0 < Z ≤ 3.0，批量汇总
- **P2 静默**：Z ≤ 2.0，仅记录不推送
- 广告帖 / 重复帖 / 短帖 自动识别，高噪音时抑制 sentiment 告警

### 📬 飞书推送
- **lark CLI 直推**（默认 `auto` 模式，检测到已授权自动启用）
- **文件模式**（写入 JSON，由外部 scheduler 读取）
- 日报自动生成，含爬取摘要 + 异常汇总

---

## 你可能能达成什么效果

| 场景 | 效果 |
|------|------|
| 盯盘盯不过来 | 每天打开飞书看舆情摘要，不用逐个刷雪球帖子 |
| 茅台突然多了一堆"回购""分红"讨论 | 飞书收到 P0 告警 → 立即查看 → 提前布局 |
| 某只股票讨论量异常爆发但股价还没动 | 发现「讨论已在发酵」→ 抢在量价反应前进场 |
| 追了太多资讯频道，信息过载 | 每日日报汇总，只看有变化的 |
| 想量化追踪情绪变化 | SQLite 历史数据可导出做回测因子 |
| 想让系统越来越懂你 | 标记推送「有用/无用」→ 权重自动调整 |

---

## 架构

```
cron/timer
    │
    ▼
┌──────────┐    ┌──────────┐    ┌──────────┐
│ crawler  │───→│    db    │───→│ detector  │
│(雪球爬取) │    │(SQLite)  │    │(Z-score)  │
└──────────┘    └──────────┘    └──────────┘
                                      │
    ┌──────────┐                      ▼
    │ notifier │◄──── filter ◄─────────┘
    │(飞书推送) │    (规则筛选)
    └──────────┘
         │
    ┌────▼─────┐
    │lark CLI  │  → 飞书群消息
    │ JSON文件  │  → 外部 scheduler
    └──────────┘
```

**13 张表**（`src/schema.sql`）：`crawl_snapshots` / `posts` / `sentiment_stats` / `change_alert` / `hot_word_dict` / `hot_word_event` / `push_history` / `comments` / `announcements` / `detail_fetch_log` / `db_meta` / `xueqiu_monitor_meta` / `pool_history`
（另有 `post_fulltext` / `post_replies` 由 `scripts/backfill_*.py` 按需创建）
舆情日报由 `src/report_generator.py` 生成：详情富化 → 逐股 LLM 增量分档（深读/标准/平稳）→ 温度计/要点/主线组装 → 输出 Markdown 并推 IMA。

## 快速开始

```bash
git clone https://github.com/winterswang/xueqiu-monitor.git
cd xueqiu-monitor
pip install -r requirements.txt

# 配置（二选一）
cp .env.example .env   # 编辑填入 ARK_API_KEY + LARK_CHAT_ID
# 或编辑 etc/config.json 设置白名单等

# 初始化数据库
python -m src.cli --init-db

# 单次运行（白名单模式）
python -m src.cli -c etc/config.json --dry-run -v

# 定时运行（cron）
*/4 * * * * cd /path/to/xueqiu-monitor && python -m src.cli -c etc/config.json >> logs/cron.log 2>&1
```

### 配置说明

| 配置项 | 来源 | 说明 |
|--------|------|------|
| `ARK_API_KEY` | `.env` | LLM（情感分析 + 日报，OpenAI 兼容；也兼容 `ARKCODE_API_KEY`） |
| `ZHIPU_API_KEY` | `.env` | 详情兜底通道（智谱 reader / web-search-pro，可选） |
| `LARK_CHAT_ID` | `.env` | 飞书群 ID（lark CLI 模式） |
| `XUEQIU_ANALYZER_PATH` | `.env` 或 `config.json` | xueqiu-analyzer 路径 |
| `MORNING_BRIEF_DB` | `.env` 或 `config.json` | morning-brief 自选股 DB 路径 |
| `notification.mode` | `config.json` | `auto` / `lark_cli` / `file` |
| `crawler.whitelist` | `config.json` | 白名单股票列表（空=全量） |

## 使用方法

```bash
# 全量爬取 + 检测 + 推送
python -m src.cli -c etc/config.json -v

# 仅爬取不推送
python -m src.cli -c etc/config.json --dry-run

# 仅生成日报（v2，从已有数据；写入 data/daily_reports/YYYY-MM-DD-sentiment.md）
python -m src.report_generator etc/config.report.json

# 推送日报到 IMA 笔记（日报生成为独立步骤，需显式调用）
python scripts/publish_ima.py --report data/daily_reports/$(date +%F)-sentiment.md

# 单独爬取一支股票
python -c "from src.crawler import crawl_single_stock; r = crawl_single_stock('600519.SH', timeout=1200); print(r['posts_count'])"
```

## 口径定义（v1，2026-11-25）

本项目里「帖数」有五个互不相同的意思。**写报文、写测试、引用数字时必须写明是哪一个**，
否则会出现「同一只票昨天 62 帖今天 20 帖」这类看着像 bug、其实是口径混用的争论。

### 时间

- **报表日 D**：日报文件名里的日期，CST 日历日。
- **快照日**：`date(crawl_time,'unixepoch','localtime') == D`。
- **帖子发布时间**：`posts.post_ts`。在**采集时刻**由 `time_text` 解析后落库，此后不重算。

### 五个「帖数」

| 名称 | 定义 | 数据源 |
|---|---|---|
| 当日新增帖 | 首次被采到落在 D（`snapshot_id IN 当日快照`） | `posts` |
| 当日发布帖 | `date(post_ts) == D` | `posts.post_ts` |
| 近7日可见帖 | 当日快照并集 ∩ `post_ts ∈ [D-6, D]`（含 D，共 7 个日历日） | `posts.post_ts` |
| 快照可见帖 | 当日快照 `posts_data` 的条数（含历史热帖） | `crawl_snapshots` |
| 抓取量 | `posts_count` | `crawl_snapshots` |

关系：**当日新增 ≥ 当日发布**——一天只爬一轮，那一轮会带上「前一晚发布、上次又没拓到」的帖。
快照可见与其余四者没有固定大小关系。

「当日新增」的真实窗口是**滚动 24 小时，锚在爬取时刻**，不是日历日：
`[爬取时刻 - 24h, 爬取时刻]`。而爬取时刻按分组分散在 **13:11~18:06**，
所以：

- 13 点组：窗口里约一半落在前一天（11-23 13:00~24:00）
- 18 点组：只有 6 小时落在前一天

实测 2026-11-24 全量前一日占比 **21%（447/2104）**；单只最高的 7 只
（PDD 68%、9992.HK 60%…）全部在 13~14 点组，且都是夜间活跃的票。

这**不是 bug**，不要改成"按午夜切窗"——那会把「自上次采集以来」换成另一种口径。
要做的是把两个名字写清楚（新增 / 当日发布），报文已经这么做了。

**「近7日可见」≠「全库近7日」**。前者只数今天页面上还看得到的，后者会把已沉底的也算进来，
实测差别很大（2026-11-24）：

| | APP.US | UBER.US | 2076.HK | 6078.HK |
|---|---|---|---|---|
| 近7日可见（报文用） | 2 | 4 | 1 | 1 |
| 全库近7日（活跃度口径） | 16 | 27 | 11 | 8 |

报文用**可见口径**（读者视角：页面上能看到几条），词形固定为「近7日可见 N 帖」。
要展示活跃度另开一个指标，**不要共用同一个词**。

实测 2026-11-24：当日新增 2104 条，其中当日发布 1657、前一日发布 447（21%）。
PDD.US 当日新增 62 条，其中当日发布 20 条。

### X 源

- **X 今日**：`days_ago < 1.0`（等价 CST 日历日 D）——**展示口径**
- **X 历史**：`days_ago >= 1.0`
- **入 prompt**：`clean_posts` → `layer_for_prompt`（近期全留 + 历史限量）→ 截断
- `days_ago` 不落盘，每次现算；`keep_days=7` 只清**目录**，不清文件内的旧帖

**时间窗（2026-11-26 起，硬规则）**：只喂**最近 `keep_days`（默认 7）天**的帖，
超出的一律丢弃，无论互动多高。

为什么加这条：X 的 `top` / `min_faves` 切片会带回历史高互动帖。实测 2026-11-26
当天 1112 条里 **337 条（30%）在 7 天之外**，最老一条 **3103 天**。其中一条
2022-07-16 的帖（「截至 2022 年 4 月的 12 个月内，携程 DAU 从 2600 万降至 595 万」）
被当成近期舆情读进了日报——内容是真的，但它是四年前的旧闻，对今天的判断毫无意义。

分层的日期前缀只解决了「别把它当今日事件」，没解决「旧闻本来就无关」。

**历史帖分层规则**（2026-11-25 起，在时间窗之后生效）：

| 分层 | 保留 | 在 prompt 里 |
|---|---|---|
| 近期（`is_fresh`） | **全留**（最多到配额） | 原样 |
| 历史（窗口内但非近期） | 最多 `min(5, 近期条数)` | **带 `[YYYY-MM-DD]` 前缀** |

近期一条都没有时，prompt 顶部会加一句「今日无新增 X 帖…不得当作今日事件」。

不删历史的原因：一条老文今天被**重新传播**，传播行为本身是今日信号，删掉会丢真信号；
但它内容老，必须落日期。这是降权，不是删除。

实测（2026-11-24）：APP 30→23 条（−24%）、PDD 30→14 条（−38%）、UBER 30→26 条（−18%）；
其中 APP 最老的一条是 **2021-10-15**。

**采集条数 ≠ 入 prompt 条数**，中间隔着 `clean_posts` 三层过滤：

| 层 | 规则 | 实测影响（2026-11-24） |
|---|---|---|
| 诈骗/引流 | WhatsApp、wa.me、`$X→$Y` 等特征词 | PDD 丢 9 条 / APP 丢 7 条 |
| 过短 | 正文短于 `MIN_TEXT_LEN` | UBER 丢 2 条 |
| 重复 | 同 URL；或正文构成前缀包含的抄袭号 | PDD 丢 2 条 / UBER 丢 1 条 |

所以「采集读了 58 条、报文写 45 条」是正常的，不是数据丢失。
拿原始采集条数去对账时，记得先把这三层算掉。

**两个阈值不要合并**（改任何一个之前先确认改的是哪个）：

| 阈值 | 取值 | 用途 | 出现在 |
|---|---|---|---|
| `is_fresh` | `days_ago ≤ 1.5` 天 | **截断配额**的分层缓冲，避免把「昨晚发的」排到「三天前的热搜」之后 | 日志 `fresh≤1.5d` |
| X 今日 | `days_ago < 1.0` | **展示语义**，严格按 CST 日历日 | 报文 `X 17 条（今日）` |

所以同一只票日志可能写「fresh≤1.5d: 18」，报文写「X 17 条（今日）」——这是对的，
差的 1 条是昨天下午发的：截断时给它留了位置，展示时不能算进今天。

### 已知陷阱

1. **相对时间重解析漂移**：`report_generator` 的 union 路径会重解析 `time_text`，
   且拿**解析时刻**当 now —— 旧日报重跑时「昨天」会漂到后一天。一律以 `posts.post_ts` 为准。
   （`src/cli.py` 采集入口用的就是爬取时刻，那里不漂。）
2. **dedup 不是丢数据**：`INSERT OR IGNORE` + 唯一索引，一条帖只在**首见快照**存一行。
   「快照里可见 11 条、posts 当日 0 行」是正常语义，不是故障。
3. **两条路径不同源**：`assistant_daily` 读 `posts` 表；`report_generator` 读
   `crawl_snapshots.posts_data` 的并集。同一个「当日」可能给出不同数字，
   引用时必须说明是哪条路径。

### 验收基准（2026-11-24，四只安静股）

| 标的 | 当日新增 | 当日发布 | 近7日 | 快照可见 | X 今日/历史 |
|---|---|---|---|---|---|
| APP.US | 0 | 0 | 2 | 11 | 17 / 32 |
| UBER.US | 0 | 0 | 4 | 11 | 21 / 24 |
| 2076.HK | 0 | 0 | 1 | 11 | — |
| 6078.HK | 0 | 0 | 1 | 11 | — |

对应报文行：

```
0 帖（新增）· 近7日 2 帖 · X 17 条（今日）/ 另 32 条历史
```

### 硬规则

- 声称「数据丢了」之前，先打印 `post_ts` 分布（当日 / 更早 / 距今多少天）。
- 报文里出现的每个帖数，都必须能对上上表里**唯一**的名字与数据源。

## 测试

```bash
python -m pytest tests/ -v --cov=src
```

## 依赖

- Python 3.11+
- [xueqiu-analyzer](https://github.com/winterswang/xueqiu-analyzer-skill)（爬虫）
- LLM API（默认火山方舟 coding plan，OpenAI 兼容，可选——缺失时情感分析降级为关键词匹配）
- pymupdf（公告 PDF 本地解析，随 `requirements.txt` 安装）
- opencli + Chrome 扩展（news 正文抓取，可选——缺失时回退智谱 reader）
- lark CLI（飞书推送，可选——缺失时写文件）

## 与 morning-brief 的关系

| | morning-brief | xueqiu-monitor |
|------|--------------|----------------|
| 视角 | 量价（K 线 / PE / 技术指标） | 舆情（情绪面 / 热词 / 公告） |
| 数据源 | 行情 API | 雪球帖子 + 资讯 + 公告 |
| 推送内容 | 早报 + 技术指标 | 舆情异常 + 日报 |
| 调度 | 独立 cron | 独立 cron |

两套系统互补运行，不做合并。

### 爬取真源（source of truth）与池一致性

xueqiu-monitor 的爬取不是以自己的 `etc/config*.json` whitelist 为起点，而是
**继承 morning-brief 的自选股池**（`morning-brief/data/morning-brief.db` 的
`watchlist.is_active=1` 集合），config whitelist 只决定「哪几只进本组」。
三层加载链（`crawler.py` 实测）：

1. `morning-brief` DB active 集（真源，最高优先）
2. `data/watchlist.json`（fallback，每日 12:00 由 `scripts/sync_watchlist.py` 从 Longbridge 拉取）
3. `etc/config.json` whitelist（兜底）

因此「监控池 = 爬取并集」这条等式隐含依赖 morning-brief 池的同步。三道防线：

| 防线 | 机制 | 触发时机 |
|------|------|----------|
| 事前 | `scripts/rotate_pool.py --check` 第 5 项校验池 ⊆ morning-brief active 集 | 轮换时手动跑 |
| 事中 | `scripts/sync_watchlist.py` 末尾 `check_pool_coverage()` 覆盖告警（`[POOL_COVERAGE_ALERT]`） | 每日 12:00 同步后 |
| 事后 | `rotate_pool.py --check` 全套校验可随时复查 | 任意时刻 |

池变更（轮换）必须走 `scripts/rotate_pool.py --plan spec.json --apply` + 台账
`pool_history`（见 `docs/v0.9-requirements.md` T1/T2），不允许手工改 config 后不校验。
