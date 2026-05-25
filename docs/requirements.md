# xueqiu-monitor: 自选股雪球舆情持续监控系统

## 1. 项目概述

### 1.1 一句话描述
面向 A 股/港股投资者的自选股舆情监控系统，通过爬取雪球帖子、资讯、公告，自动检测舆情实质性变化并分级推送到飞书。

### 1.2 目标用户

| 角色 | 定义 | 核心需求 |
|------|------|----------|
| 个人投资者 | 管理 20-100 只自选股，关注舆情异动 | 快速识别"股价没动但讨论已在发酵"的机会与风险 |
| 量化团队 | 需要舆情数据作为情绪因子 | 结构化的历史快照 + 变化告警 API |

### 1.3 核心价值主张

**区别于全文推送**：用户有 60+ 只自选股，每天雪球产生大量灌水/重复/噪音，95% 内容无信息量。本系统只推送有增量价值的变化。

**区别于 morning-brief**：morning-brief 看量价（K 线/PE/技术指标），xueqiu-monitor 看舆情（情绪面），两套系统互补独立运行。

---

## 2. 功能需求

### 2.1 爬虫调度模块

- **用户故事**：作为系统，我需要在用户设定的周期（每 N 小时）内，对全部自选股调用 xueqiu-analyzer 的 Playwright 爬虫，获取最新的帖子、资讯、公告。
- **验收标准**：
  - [ ] **给定** 用户配置了 60 只自选股列表 **当** cron 触发调度 **则** 系统按配置调用 xueqiu-analyzer 爬虫，60 只股票全部执行
  - [ ] **给定** 单次爬取超时 30 秒 **当** 爬虫超时 **则** 记录错误日志并标记该股票本次爬取失败，不阻塞其他股票
  - [ ] **给定** 爬取成功率目标 ≥98% **当** 单次运行成功率 <98% **则** 发送告警到飞书
- **输入/输出**：
  - 输入：自选股列表（stock_codes: List[str]）、调度周期（interval_hours: int）
  - 输出：爬取结果写入 SQLite crawl_snapshots 表，附带爬取时间戳
- **优先级**：P0（核心MVP）

### 2.2 数据存储模块

- **用户故事**：作为系统，我需要将每次爬取的完整快照存入 SQLite，支持历史对比。
- **验收标准**：
  - [ ] **给定** 一次爬取包含帖子列表、评论数、转发数、情感统计 **当** 爬取完成 **则** 将数据写入 crawl_snapshots 表，时间戳精确到秒
  - [ ] **给定** 需要按股票+日期聚合情感统计 **当** 日终计算 **则** 将结果写入 sentiment_stats 表，包含 Z-score 异常标记
  - [ ] **给定** 需要记录热词及其出现频次 **当** 检测到新热词 **则** 更新 hot_word_dict 并记录热词事件到 hot_word_event
- **输入/输出**：
  - 输入：原始爬取数据（JSON）
  - 输出：crawl_snapshots / sentiment_stats / hot_word_dict / hot_word_event 表记录
- **优先级**：P0（核心MVP）

### 2.3 变化检测模块

- **用户故事**：作为系统，我需要在每次爬取后与上次快照 diff，检测舆情实质性变化。
- **验收标准**：
  - [ ] **给定** sentiment_stats 表存储历史情感均值 μ 和标准差 σ **当** 新数据点 x 到达 **则** 计算 Z = (x-μ)/σ，Z > 2.0 时标记 change_alert
  - [ ] **给定** 两期快照的情感值 S1 和 S2 **当** |S2 - S1| > 0.2 **则** 触发 change_alert
  - [ ] **给定** 热词在时间序列中的 TF-IDF 值 **当** TF-IDF 突增超过历史均值 2 倍标准差 **则** 触发 change_alert
  - [ ] **给定** 今日新帖子数 > 历史均值 + 2σ **当** 新帖子数异常爆发 **则** 触发 change_alert
- **输入/输出**：
  - 输入：当前快照（curr）、上次快照（prev）、历史统计（sentiment_stats）
  - 输出：change_alert 表记录，包含变化类型（sentiment_shift/hot_word_surge/post_spike）和 Z-score
- **优先级**：P0（核心MVP）
- **热词 TF-IDF 时间序列计算细节**：
  - **计算范围**：增量计算（仅对比上次快照到当前快照之间新增的帖子文本）
  - **时间窗口**：对比最近 3 次快照的 TF-IDF 值，用于检测突增趋势
  - **TF-IDF 参数**：
    - `min_df=2`：热词必须在至少 2 个时间窗口出现才计入
    - `max_df=0.8`：出现频率超过 80% 的词（如"股票"）不计入
    - `ngram_range=(1,2)`：支持单词和双词组合（如"业绩超预期"）
  - **Z-score 计算**：取该热词最近 14 天的 TF-IDF 值序列计算均值 μ 和标准差 σ，TF-IDF 突增定义为 Z > 2.0

### 2.4 规则筛选模块（Phase 1）

- **用户故事**：作为系统，我需要通过规则引擎过滤噪音，保留有价值的变化信号。
- **验收标准**：
  - [ ] **给定** 新帖子列表 **当** 帖子包含广告关键词（"开户"+"佣金"等） **则** 标记为广告并过滤
  - [ ] **给定** 新帖子列表 **当** 帖子内容与历史某帖子相似度 >85%（短文本去重） **则** 标记为重复并过滤
  - [ ] **给定** 新帖子列表 **当** 帖子字数 <20 字符 **则** 标记为无效短帖并过滤
  - [ ] **给定** change_alert 列表 **当** 变化幅度低于阈值（P0: Z>3.0, P1: 2.0<Z≤3.0） **则** 标记为 P2 不推送
  - [ ] **给定** 冷启动期（前 3-4 周） **当** 基线数据不足 **则** 仅记录不推送（积累期策略）
- **输入/输出**：
  - 输入：crawl_snapshots + change_alert
  - 输出：过滤后的有效告警列表（带优先级标签）
- **优先级**：P0（核心MVP）

### 2.5 分级通知模块

- **用户故事**：作为投资者，我需要在舆情发生实质性变化时收到分级通知，P0 即时推，P1 汇总推。
- **验收标准**：
  - [ ] **给定** change_alert 标记为 P0（重大利空/利好，如 Z>3.0 + 情感偏移 >0.5） **当** 检测到 **则** 即时飞书推送，包含股票名称、变化类型、关键数据
  - [ ] **给定** change_alert 标记为 P1（明显变化，如 2.0<Z≤3.0） **当** 每 N 小时汇总 **则** 批量飞书推送
  - [ ] **给定** change_alert 标记为 P2（日常波动，如 Z≤2.0） **当** 检测到 **则** 静默不推送，仅记录
  - [ ] **给定** 每日早报时间（如 8:00） **当** 定时触发 **则** 生成并推送日报，格式为「股票 | 变化类型 | 摘要」
- **输入/输出**：
  - 输入：过滤后的告警列表（带优先级）
  - 输出：飞书 Webhook 调用日志 + push_history 表记录
- **优先级**：P0（核心MVP）
- **推送关键数据字段清单**：

| 字段名 | 类型 | 说明 | 示例 |
|--------|------|------|------|
| stock_code | str | 股票代码 | "SH600519" |
| stock_name | str | 股票名称（可选） | "贵州茅台" |
| alert_type | str | 告警类型 | "sentiment_shift" |
| z_score | float | Z-score 值 | 3.52 |
| sentiment_avg | float | 当前情感均值（-1 到 1） | 0.65 |
| sentiment_shift | float | 情感偏移量 | 0.35 |
| posts_count | int | 本次新帖子数 | 127 |
| posts_count_delta | int | 相比上次变化量 | +45 |
| hot_words | list[str] | Top 3 热词 | ["业绩", "分红", "增持"] |
| post_titles | list[str] | Top 3 高互动帖子标题 | ["茅台年报超预期", "..."] |
| magnitude | float | 变化幅度 | 0.35 |
| priority | str | 优先级 | "P0" |
| trigger_time | str | 触发时间（ISO 8601） | "2024-01-15T09:30:00" |

- **Phase 1 早报摘要生成逻辑**：
  - 格式：「股票代码 | 告警类型 | Z-score | 变化值 | 最新帖子标题」
  - 取每只股票最新告警的高互动帖子（点赞+评论+转发最多）作为代表性标题
  - 数值变化以「↑/↓ + 百分比」形式展示（如「情感偏移 +35%」）
  - 不生成自然语言摘要，所有内容为结构化数据拼接
  - Phase 2 升级为 LLM 智能摘要

### 2.6 用户反馈闭环模块

- **用户故事**：作为投资者，我需要能够标记收到的推送有用/无用，系统据此调整权重。
- **验收标准**：
  - [ ] **给定** 推送历史记录 **当** 用户标记某推送为"有用" **则** 在 content_weight 表中增加该来源/关键词权重 +0.1
  - [ ] **给定** 推送历史记录 **当** 用户标记某推送为"无用" **则** 在 content_weight 表中减少该来源/关键词权重 -0.1
  - [ ] **给定** 权重调整 **当** 某来源权重 < 0.3（持续无用） **则** 自动降低该来源优先级
  - [ ] **给定** 某来源 7 天内无正向反馈 **当** 每日权重衰减检查 **则** 该来源权重 -0.05（最低到 0.3）
- **输入/输出**：
  - 输入：用户反馈（push_id + verdict: useful/useless）
  - 输出：content_weight 表更新
- **优先级**：P0（核心MVP，Phase 1 必须交付）
- **权重调整规则**：

| 操作 | 权重变化 | 说明 |
|------|----------|------|
| 标记"有用" | +0.1 | 单次即时调整 |
| 标记"无用" | -0.1 | 单次即时调整 |
| 7 天无正向反馈 | -0.05/天 | 每日衰减 |
| 权重 < 0.3 | 自动降级 | 该来源下次检测自动降为 P2 |

- **每日权重衰减检查触发时机**：
  - 每次调度开始前（cron trigger 时）执行权重衰减清理任务
  - 具体逻辑：查询 content_weight 表中所有来源，筛选出 updated_at 距今超过 7 天且期间无正向反馈（verdict='useful'）的记录，将权重 -0.05
  - 权重最低降至 0.3，低于此阈值该来源在下一检测周期自动降为 P2
  - 衰减任务执行后更新 updated_at 字段，下次调度重新检查

### 2.7 模块依赖关系

```
2.1 爬虫调度 ──→ 2.2 数据存储 ──→ 2.3 变化检测 ──→ 2.4 规则筛选 ──→ 2.5 分级通知
                                                                              ↑
                                                                        2.6 反馈闭环
```

- 2.1 是入口，依赖 xueqiu-analyzer 爬虫
- 2.6 反馈闭环依赖 2.5 的推送历史

---

## 3. 非功能需求

### 3.1 性能

| 指标 | 目标 | 说明 |
|------|------|------|
| 爬取成功率 | ≥98% | 单次调度内成功完成爬取的股票数/总股票数 |
| 全量爬取时间 | ≤30 分钟 | 60 只自选股一次完整爬取耗时 |
| 变化检测延迟 | ≤5 分钟 | 爬取完成到变化检测完成的时间 |
| 单次 API 调用 | ≤500ms | 飞书 Webhook 推送响应时间 |

### 3.2 安全

| 要求 | 实现方式 |
|------|----------|
| 雪球凭证 | 不存储雪球登录态，依赖 xueqiu-analyzer 现有方案 |
| 飞书凭证 | Webhook URL 写入配置文件，不提交到代码仓库 |
| 日志脱敏 | 用户反馈内容若包含昵称需脱敏处理 |
| 数据加密 | SQLite 文件存储在用户本地，无额外加密需求 |

### 3.3 可用性

| 指标 | 目标 |
|------|------|
| 正常运行时间 | ≥99%（每日 cron 执行成功率） |
| 单次失败容忍 | 单只股票失败不阻塞其他股票 |
| 爬取失败重试 | 失败后下一次调度自动重试，不立即重试 |
| 告警机制 | 单次调度成功率 <98% 时发送飞书告警 |

### 3.4 错误处理

| 异常路径 | 触发条件 | 系统行为 | 用户可见效果 |
|----------|----------|----------|--------------|
| 爬虫超时 | 单次爬取 >30 秒 | 记录错误日志，标记失败，跳过该股票 | 告警中显示"XX 股票爬取超时" |
| 网络中断 | 调度期间网络不可达 | 记录日志，调度失败发送告警 | 收到"今日爬取因网络问题未完成" |
| 数据库锁定 | SQLite 并发写入冲突 | 等待 3 秒后重试，最多重试 3 次 | 无用户感知，仅日志记录 |
| 飞书推送失败 | Webhook 返回非 200 | 记录日志，标记推送失败，重试下一条 | 该推送记录显示"失败"，下一次调度重试 |
| 基线不足（冷启动） | 运行 <28 天 | 仅记录数据，不触发推送 | 无推送，用户感知"还在积累" |

---

## 4. 技术架构概要

### 4.1 技术栈建议

| 组件 | 选择 | 理由 |
|------|------|------|
| 语言 | Python 3.11+ | 依赖 xueqiu-analyzer（Python），快速原型 |
| 数据库 | SQLite | 单机部署，数据量可控（万级帖子/天），免运维 |
| 爬虫 | xueqiu-analyzer Playwright | 不重写爬虫，复用已有能力 |
| 调度 | Cron + Python cronwrap | 简单可靠，与 morning-brief 模式一致 |
| 通知 | 飞书 Webhook | 与 morning-brief 复用渠道 |
| 日志 | JSON 结构化日志 | 便于日志分析 + ELK/ Loki 对接 |
| 代码质量 | PEP 8 + pytest | 测试覆盖率 ≥80% |

### 4.1.1 Python 包依赖清单

```txt
# requirements.txt
# 核心依赖（必须）

# 数据处理
numpy>=1.24.0
pandas>=2.0.0
scikit-learn>=1.3.0  # TF-IDF (TfidfVectorizer)

# 数据库
sqlite3  # Python 标准库，无需安装

# HTTP 请求
requests>=2.28.0

# 飞书通知
# 自定义封装，无外部依赖

# 日志
python-json-logger>=2.0.0

# 测试
pytest>=7.4.0
pytest-cov>=4.1.0

# 类型检查
mypy>=1.5.0

# 依赖 xueqiu-analyzer
# 版本协调说明：
# - 依赖 xueqiu-analyzer@^1.0.0，需与 xueqiu-analyzer 项目协调正式发布
# - API 兼容性策略：SemVer 语义版本控制，主版本号不变时接口向后兼容
# - 兼容性测试：每次 xueqiu-analyzer 升级后，运行 requirements.txt 中的集成测试
# - 若 xueqiu-analyzer 尚未正式发布 v1.0.0，本项目使用其最新的稳定版本，并在 CHANGELOG.md 中记录版本
```

### 4.2 部署模式

**单机部署 + Cron 调度**

```
┌─────────────────────────────────────────────────────────┐
│                    xueqiu-monitor                       │
│                                                         │
│   ┌──────────┐    ┌──────────┐    ┌──────────┐         │
│   │  cron    │───→│ crawler  │───→│ storage  │         │
│   │ schedule │    │ (依赖)   │    │ SQLite   │         │
│   └──────────┘    └──────────┘    └──────────┘         │
│         │              │              │                 │
│         ▼              ▼              ▼                 │
│   ┌──────────┐    ┌──────────┐    ┌──────────┐         │
│   │ detector │←───│ analyzer │←───│ differ   │         │
│   │ (规则)   │    │ (Z-score)│    │ (diff)   │         │
│   └──────────┘    └──────────┘    └──────────┘         │
│         │              │                               │
│         ▼              ▼                               │
│   ┌──────────┐    ┌──────────┐                         │
│   │ notifier │───→│ pusher   │───→ 飞书 Webhook        │
│   │ (分级)   │    │ (反馈)   │◄── content_weight       │
│   └──────────┘    └──────────┘                         │
└─────────────────────────────────────────────────────────┘
```

### 4.3 关键设计决策

1. **不自己实现爬虫**：调用 xueqiu-analyzer 的 Playwright 爬虫，保持一致性
2. **SQLite 选型**：数据量可控（60 只股票 × 每日 N 次 × 100 条帖子 ≈ 万级/天），SQLite 足够
3. **规则优先 Phase 2 LLM**：冷启动期先积累数据 + 规则验证有效性，再引入 LLM 提升精确率
4. **反馈闭环轻量实现**：content_weight 表记录权重调整，不做实时模型训练

### 4.4 模块接口契约

以下定义各模块间的函数签名和数据格式。开发者可直接按此实现模块间的调用逻辑。

#### 4.4.1 爬虫调度模块 → 数据存储模块

**接口**：调用 xueqiu-analyzer 爬虫，返回结构化数据

```python
# 模块：crawler_scheduler
# 依赖：xueqiu-analyzer.playwright_crawler

def crawl_stock(stock_code: str, timeout: int = 30) -> dict:
    """
    调用 xueqiu-analyzer 爬取单只股票数据
    
    参数：
        stock_code (str): 股票代码，如 "SH600519"
        timeout (int): 超时秒数，默认 30
    
    返回：
        dict: 爬取结果，结构如下：
        {
            "status": "success" | "failed" | "timeout",
            "stock_code": str,
            "crawl_time": "2024-01-15T09:30:00",
            "posts_count": int,
            "posts_data": [  # 帖子详情列表
                {
                    "post_id": str,
                    "title": str,
                    "content": str,  # 纯文本，限制前 500 字符
                    "author": str,
                    "author_id": str,
                    "created_at": str,  # ISO 8601
                    "comment_count": int,
                    "forward_count": int,
                    "like_count": int,
                    "sentiment_score": float  # -1.0 到 1.0
                },
                ...
            ],
            "announcements": [  # 公告列表
                {
                    "ann_id": str,
                    "title": str,
                    "date": str,  # YYYY-MM-DD
                    "type": str,  # 年报/季报/重大事项
                    "is_new": bool
                },
                ...
            ],
            "error": str | None  # 失败时的原因
        }
    
    异常：
        TimeoutError: 爬取超时
        NetworkError: 网络不可达
    """
```

**调用示例**：
```python
result = crawl_stock("SH600519")
if result["status"] == "success":
    save_snapshot(result)  # 传递给数据存储模块
```

#### 4.4.2 数据存储模块（CRUD API）

```python
# 模块：data_storage

def save_snapshot(crawl_result: dict) -> int:
    """
    将爬取结果写入 crawl_snapshots 表
    
    参数：
        crawl_result (dict): crawl_stock() 返回的结果
    
    返回：
        int: snapshot_id（主键）
    """
    ...

def get_latest_snapshot(stock_code: str) -> dict | None:
    """
    获取指定股票的最新爬取快照
    
    参数：
        stock_code (str): 股票代码
    
    返回：
        dict | None: 快照数据，无数据时返回 None
    """
    ...

def get_snapshots_by_date_range(stock_code: str, start_date: str, end_date: str) -> list[dict]:
    """
    获取指定时间范围内的快照列表（用于 Z-score 计算）
    
    参数：
        stock_code (str): 股票代码
        start_date (str): 开始日期 YYYY-MM-DD
        end_date (str): 结束日期 YYYY-MM-DD
    
    返回：
        list[dict]: 快照列表，按时间升序
    """
    ...

def update_sentiment_stats(stock_code: str, stat_date: str) -> int:
    """
    按日聚合情感统计，更新 sentiment_stats 表
    
    参数：
        stock_code (str): 股票代码
        stat_date (str): 统计日期 YYYY-MM-DD
    
    返回：
        int: stat_id（主键）
    """
    ...

def save_change_alert(alert_data: dict) -> int:
    """
    保存变化告警到 change_alert 表
    
    参数：
        alert_data (dict): 告警数据，结构如下：
        {
            "stock_code": str,
            "alert_type": "sentiment_shift" | "hot_word_surge" | "post_spike" | "new_announcement",
            "z_score": float,
            "magnitude": float,  # 变化幅度
            "priority": "P0" | "P1" | "P2",
            "details": dict  # 告警详情（触发条件、涉及数据等）
        }
    
    返回：
        int: alert_id（主键）
    """
    ...

def get_pending_alerts(priority: str | None = None) -> list[dict]:
    """
    获取待推送的告警列表
    
    参数：
        priority (str | None): 筛选优先级，如 "P0"，None 表示全部
    
    返回：
        list[dict]: 告警列表
    """
    ...
```

#### 4.4.3 变化检测模块输入/输出格式

```python
# 模块：change_detector

# 输入：两次快照 diff
def detect_changes(curr_snapshot: dict, prev_snapshot: dict | None, 
                  historical_stats: list[dict]) -> list[dict]:
    """
    检测两次快照间的变化
    
    参数：
        curr_snapshot (dict): 当前快照（crawl_snapshots 表记录）
        prev_snapshot (dict | None): 上次快照，无数据时为 None
        historical_stats (list[dict]): 历史 sentiment_stats 列表（用于计算 Z-score）
    
    返回：
        list[dict]: 变化告警列表，每项结构如下：
        [{
            "alert_type": "sentiment_shift" | "hot_word_surge" | "post_spike" | "new_announcement",
            "z_score": float,
            "magnitude": float,
            "trigger_condition": str,  # 触发条件描述
            "details": {
                "prev_value": float,
                "curr_value": float,
                "threshold": float
            }
        }]
    """
    ...

# Z-score 计算依赖的历史数据窗口
# 默认取最近 14 天数据计算 μ（均值）和 σ（标准差）
# 冷启动期（<28 天）：取全部历史数据
```

#### 4.4.4 规则筛选模块输入/输出格式

```python
# 模块：rule_filter

def filter_alerts(alerts: list[dict], posts_data: list[dict], 
                  cold_start_days: int = 28) -> list[dict]:
    """
    规则引擎过滤噪音
    
    参数：
        alerts (list[dict]): 变化检测模块输出的告警列表
        posts_data (list[dict]): 帖子详情（用于去重、广告过滤）
        cold_start_days (int): 冷启动天数，默认 28
    
    返回：
        list[dict]: 过滤后的告警列表，每项新增字段：
        {
            ...original_alert,
            "filtered": bool,
            "filter_reason": str | None,  # 被过滤原因
            "priority": "P0" | "P1" | "P2"
        }
    """
    ...

# 广告关键词列表（初始集，可随用户反馈扩展）
AD_KEYWORDS = ["开户", "佣金", "万一", "万0.5", "低手续费", "加群", "荐股", "内幕"]

# 去重相似度阈值
DUPLICATE_SIMILARITY_THRESHOLD = 0.85

# 短帖字数阈值
SHORT_POST_THRESHOLD = 20

# P0/P1/P2 分级阈值
P0_Z_THRESHOLD = 3.0  # Z > 3.0
P1_Z_THRESHOLD = 2.0  # 2.0 < Z <= 3.0
```

#### 4.4.5 分级通知模块输入格式

```python
# 模块：notification

def push_immediate(alert: dict, key_data: dict) -> bool:
    """
    P0 即时推送
    
    参数：
        alert (dict): 告警数据
        key_data (dict): 推送关键数据，结构如下：
        {
            "stock_code": str,        # 股票代码
            "stock_name": str,        # 股票名称（可选，用于展示）
            "alert_type": str,        # 告警类型
            "z_score": float,         # Z-score 值
            "sentiment_avg": float,   # 情感均值（-1 到 1）
            "sentiment_shift": float, # 情感偏移量
            "posts_count": int,       # 新帖子数
            "hot_words": list[str],   # Top 3 热词
            "post_titles": list[str]  # Top 3 高互动帖子标题
        }
    
    返回：
        bool: 推送是否成功
    """
    ...

def push_digest(alerts: list[dict], key_data_list: list[dict]) -> bool:
    """
    P1 汇总推送
    
    参数：
        alerts (list[dict]): 待汇总的告警列表
        key_data_list (list[dict]): 每条告警的 key_data
    
    返回：
        bool: 推送是否成功
    """
    ...

def generate_daily_report(alerts: list[dict]) -> str:
    """
    生成每日早报
    
    Phase 1 实现（无 LLM）：
    - 格式：「股票 | 告警类型 | Z-score | 变化值 | 最新帖子标题」
    - 取每只股票最新告警的 top 1 帖子标题
    - 拼接关键数值变化（Z-score、情感偏移、帖子数变化）
    
    参数：
        alerts (list[dict]): 今日所有告警
    
    返回：
        str: 格式化早报文本
    """
    ...
```

#### 4.4.6 用户反馈闭环模块接口

```python
# 模块：feedback_loop

def record_feedback(push_id: int, verdict: str) -> bool:
    """
    记录用户反馈并调整权重
    
    参数：
        push_id (int): 推送历史记录 ID
        verdict (str): "useful" | "useless"
    
    返回：
        bool: 是否成功
    """
    ...

def adjust_weight(source: str, keyword: str, verdict: str) -> float:
    """
    调整 content_weight 表中的权重
    
    权重调整规则：
    - 有用 (useful): +0.1
    - 无用 (useless): -0.1
    - 每日衰减：若某来源 7 天内无正向反馈，每日 -0.05（最低到 0.3）
    - 权重 <0.3 时自动降低该来源优先级
    
    参数：
        source (str): 来源（如 stock_code 或 author_id）
        keyword (str): 关键词
        verdict (str): "useful" | "useless"
    
    返回：
        float: 调整后的权重值
    """
    ...
```

#### 4.4.7 模块间数据传递 JSON Schema（摘要）

```json
{
  "CrawlResult": {
    "type": "object",
    "required": ["status", "stock_code", "crawl_time", "posts_data"],
    "properties": {
      "status": {"type": "string", "enum": ["success", "failed", "timeout"]},
      "stock_code": {"type": "string"},
      "crawl_time": {"type": "string", "format": "date-time"},
      "posts_count": {"type": "integer"},
      "posts_data": {"type": "array", "items": {"$ref": "#/PostItem"}},
      "announcements": {"type": "array", "items": {"$ref": "#/AnnouncementItem"}}
    }
  },
  "PostItem": {
    "type": "object",
    "required": ["post_id", "content", "sentiment_score"],
    "properties": {
      "post_id": {"type": "string"},
      "title": {"type": "string"},
      "content": {"type": "string", "maxLength": 500},
      "author": {"type": "string"},
      "sentiment_score": {"type": "number", "minimum": -1, "maximum": 1}
    }
  },
  "AlertData": {
    "type": "object",
    "required": ["stock_code", "alert_type", "z_score", "priority"],
    "properties": {
      "stock_code": {"type": "string"},
      "alert_type": {"type": "string", "enum": ["sentiment_shift", "hot_word_surge", "post_spike", "new_announcement"]},
      "z_score": {"type": "number"},
      "magnitude": {"type": "number"},
      "priority": {"type": "string", "enum": ["P0", "P1", "P2"]}
    }
  },
  "PushKeyData": {
    "type": "object",
    "required": ["stock_code", "alert_type", "z_score"],
    "properties": {
      "stock_code": {"type": "string"},
      "stock_name": {"type": "string"},
      "alert_type": {"type": "string"},
      "z_score": {"type": "number"},
      "sentiment_avg": {"type": "number"},
      "sentiment_shift": {"type": "number"},
      "posts_count": {"type": "integer"},
      "hot_words": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
      "post_titles": {"type": "array", "items": {"type": "string"}, "maxItems": 3}
    }
  }
}
```

---

## 5. 数据模型概要

### 5.1 核心实体

**crawl_snapshots**（爬取快照表）
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| id | INTEGER | 是 | 主键自增 |
| stock_code | TEXT | 是 | 股票代码（如 "SH600519"） |
| crawl_time | DATETIME | 是 | 爬取时间戳 |
| posts_count | INTEGER | 是 | 本次爬取帖子数 |
| posts_data | JSON | 是 | 帖子详情列表（JSON 存储） |
| sentiment_avg | REAL | 是 | 平均情感值（-1 到 1） |
| status | TEXT | 是 | success/failed/timeout |

**sentiment_stats**（情感统计表，按日聚合）
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| id | INTEGER | 是 | 主键自增 |
| stock_code | TEXT | 是 | 股票代码 |
| stat_date | DATE | 是 | 统计日期 |
| sentiment_mean | REAL | 是 | 历史情感均值 μ |
| sentiment_std | REAL | 是 | 历史标准差 σ |
| z_score | REAL | 是 | 当日 Z-score |
| z_alert | INTEGER | 是 | 是否告警（0/1） |

**change_alert**（变化告警表）
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| id | INTEGER | 是 | 主键自增 |
| stock_code | TEXT | 是 | 股票代码 |
| alert_time | DATETIME | 是 | 告警时间 |
| alert_type | TEXT | 是 | sentiment_shift/hot_word_surge/post_spike |
| z_score | REAL | 是 | Z 值 |
| magnitude | REAL | 是 | 变化幅度 |
| priority | TEXT | 是 | P0/P1/P2 |
| filtered | INTEGER | 是 | 是否被规则过滤（0/1） |

**hot_word_dict**（热词词典）
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| id | INTEGER | 是 | 主键自增 |
| word | TEXT | 是 | 热词 |
| frequency | INTEGER | 是 | 累计出现次数 |
| last_seen | DATETIME | 是 | 最近出现时间 |

**hot_word_event**（热词事件）
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| id | INTEGER | 是 | 主键自增 |
| stock_code | TEXT | 是 | 关联股票 |
| word | TEXT | 是 | 热词 |
| tfidf_score | REAL | 是 | TF-IDF 得分 |
| event_time | DATETIME | 是 | 事件时间 |
| z_score | REAL | 是 | TF-IDF Z-score |

**push_history**（推送历史）
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| id | INTEGER | 是 | 主键自增 |
| stock_code | TEXT | 是 | 推送股票 |
| alert_id | INTEGER | 是 | 关联告警 ID |
| push_time | DATETIME | 是 | 推送时间 |
| priority | TEXT | 是 | P0/P1/P2 |
| content | TEXT | 是 | 推送内容摘要 |
| status | TEXT | 是 | success/failed/pending |

**comments**（评论快照表）
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| id | INTEGER | 是 | 主键自增 |
| snapshot_id | INTEGER | 是 | 外键关联 crawl_snapshots.id |
| post_id | TEXT | 是 | 雪球帖子 ID |
| comment_count | INTEGER | 是 | 评论数 |
| forward_count | INTEGER | 是 | 转发数 |
| like_count | INTEGER | 是 | 点赞数 |
| sentiment_avg | REAL | 是 | 评论区平均情感值 |

**announcements**（公告快照表）
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| id | INTEGER | 是 | 主键自增 |
| snapshot_id | INTEGER | 是 | 外键关联 crawl_snapshots.id |
| stock_code | TEXT | 是 | 股票代码 |
| ann_title | TEXT | 是 | 公告标题 |
| ann_date | DATE | 是 | 公告日期 |
| ann_type | TEXT | 是 | 公告类型（年报/季报/重大事项等） |
| is_new | INTEGER | 是 | 是否新发布（0/1） |

**content_weight**（内容权重，用户反馈闭环）
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| id | INTEGER | 是 | 主键自增 |
| source | TEXT | 是 | 来源（stock_code/author_id） |
| keyword | TEXT | 是 | 关键词 |
| weight | REAL | 是 | 权重值（默认 1.0） |
| updated_at | DATETIME | 是 | 更新时间 |
| preference_level | REAL | 否 | 用户偏好程度（0.0-2.0，默认 1.0，标记有用+0.1，无用-0.1） |

**user_preference**（用户偏好配置）
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| id | INTEGER | 是 | 主键自增 |
| user_id | TEXT | 是 | 用户标识 |
| p0_threshold | REAL | 否 | P0 触发阈值（默认 Z>3.0） |
| p1_threshold | REAL | 否 | P1 触发阈值（默认 2.0<Z≤3.0） |
| cold_start_days | INTEGER | 否 | 冷启动天数（默认 28 天） |
| notify_immediate | INTEGER | 否 | P0 是否即时推（默认 1） |
| notify_digest | INTEGER | 否 | P1 是否汇总推（默认 1） |
| updated_at | DATETIME | 是 | 更新时间 |

### 5.2 实体关系

```
crawl_snapshots (N) ──→ stock_code ←── (N) sentiment_stats
crawl_snapshots (1) ──→ alert_type ←── (N) change_alert
change_alert (1) ──→ push_id ←── (N) push_history
push_history (N) ──→ source ←── (N) content_weight
hot_word_dict (1) ──→ word ←── (N) hot_word_event
```

---

## 6. MVP 范围与未来扩展

### 6.1 MVP（Phase 1，必须交付）

| 模块 | 交付内容 |
|------|----------|
| 2.1 爬虫调度 | 调用 xueqiu-analyzer 爬虫，支持 60 只股票批量调度 |
| 2.2 数据存储 | SQLite 10 张核心表（crawl_snapshots/sentiment_stats/change_alert/hot_word_dict/hot_word_event/push_history/comments/announcements/content_weight/user_preference） |
| 2.3 变化检测 | Z-score 检测（阈值 2.0）+ 情感偏移 + 热词 TF-IDF + 帖子数异常 + 新公告检测 |
| 2.4 规则筛选 | 广告过滤 + 去重 + 短帖过滤 + P0/P1/P2 分级 + 冷启动静默 |
| 2.5 分级通知 | 飞书 Webhook 推送 + P0 即时推 + P1 汇总推 + 每日早报 |
| 2.6 用户反馈闭环 | 推送有用/无用标记 + content_weight 权重调整 + user_preference 配置 |
| 3.x 非功能 | 98% 成功率 + 30 分钟全量 + JSON 日志 + PEP 8 + 80% 测试覆盖 |

> 注：数据模型共 10 张表，详见 5.1 章节定义。种子想法中"12 张表"计数方式包含帖子原始数据的 JSON 嵌套结构，不单独建表。

### 6.2 明确不做（范围边界）

| 功能 | 原因 |
|------|------|
| LLM 语义分析 | Phase 1 不做，留 Phase 2。先用规则引擎积累有效数据，验证后再引入 LLM |
| Web UI | 纯命令行运行，通过配置文件管理自选股列表和推送偏好 |
| 雪球登录 | 依赖 xueqiu-analyzer 的爬虫能力，不自己实现认证 |
| 其他平台 | 仅雪球，避免爬虫复杂度扩散 |
| 与 morning-brief 合并 | 两套系统独立运行，各自从 cron 触发，数据不合并 |
| 实时推送（秒级） | Phase 1 Cron 调度（分钟级），未来可升级到流式处理 |

### 6.3 Phase 2 路线图（未来扩展）

| 特性 | 优先级 | 前置条件 |
|------|--------|----------|
| LLM 内容质量评估 | P1 | Phase 1 数据积累 3-4 周后 |
| LLM 情感深度分析 | P1 | OpenAI/Claude API 接入 |
| LLM 智能摘要 | P1 | LLM 评估稳定后 |
| 用户偏好学习 | P2 | content_weight 数据充分后 |
| Web UI（配置 + 查看） | P2 | Phase 1 稳定运行后 |

---

## 7. 术语表

| 术语 | 定义 |
|------|------|
| **Z-score** | 统计学中用于标记异常的标准化分数，Z = (x-μ)/σ。本系统中 Z>2.0 表示 95% 置信度下的显著变化 |
| **增量价值** | 用户收到一条推送相比不收到这条推送的认知增益。本系统仅推送有增量价值的变化，而非全文推送 |
| **TF-IDF** | Term Frequency-Inverse Document Frequency，热词重要性的评估方法，在时间序列中用于检测突增的热词 |
| **冷启动期** | Phase 1 前 3-4 周，基线数据不足，系统仅记录不推送，用于积累 μ 和 σ |
| **变化检测召回率** | 系统检测到的真实变化数 / 实际上发生的真实变化数，≥85% |
| **变化检测精确率** | 系统检测到的变化中，真正有价值的比例，≥70% |
| **有效推送率** | 用户标记"有用"的推送数 / 总推送数，≥50% |
| **P0/P1/P2 分级** | 推送优先级：P0 重大利空/利好即时推，P1 明显变化汇总推，P2 日常波动静默 |
| **xueqiu-analyzer** | 依赖的爬虫项目，提供 Playwright 实现的雪球帖子爬取能力 |
| **飞书 Webhook** | 飞书机器人的消息推送接口，通过 HTTP POST 发送卡片消息 |
| **情感偏移** | 情感均值的变化量，\|S2 - S1\| > 0.2 时触发告警 |