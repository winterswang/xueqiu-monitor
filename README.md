# xueqiu-monitor — 自选股雪球舆情持续监控

面向 A 股/港股投资者的舆情监控系统。爬取雪球帖子、资讯、公告，通过 **MiniMax LLM 情感分析 + Z-score 统计检测** 自动发现舆情实质性变化，**分级推送到飞书群**。

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
- **MiniMax M2.7 LLM** 批量分析讨论帖情感（-1 到 +1）
- 新闻标题 **关键词匹配**（大涨/暴跌/增持/减持 等），零 API 成本
- 无 LLM key 时自动降级为纯关键词模式

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

**10 张核心表**：`crawl_snapshots` / `sentiment_stats` / `change_alert` / `hot_word_dict` / `hot_word_event` / `push_history` / `comments` / `announcements` / `content_weight` / `user_preference`

## 快速开始

```bash
git clone https://github.com/winterswang/xueqiu-monitor.git
cd xueqiu-monitor
pip install -r requirements.txt

# 配置（二选一）
cp .env.example .env   # 编辑填入 MiniMax key + LARK_CHAT_ID
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
| `MINIMAX_API_KEY` | `.env` | MiniMax LLM（情感分析，可选） |
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

# 仅生成日报（从已有数据）
python -m src.cli -c etc/config.json --report

# 单独爬取一支股票
python -c "from src.crawler import crawl_single_stock; r = crawl_single_stock('600519.SH', timeout=1200); print(r['posts_count'])"
```

## 测试

```bash
python -m pytest tests/ -v --cov=src
```

## 依赖

- Python 3.11+
- [xueqiu-analyzer](https://github.com/winterswang/xueqiu-analyzer-skill)（爬虫）
- MiniMax API（情感分析，可选——缺失时用关键词匹配）
- lark CLI（飞书推送，可选——缺失时写文件）

## 与 morning-brief 的关系

| | morning-brief | xueqiu-monitor |
|------|--------------|----------------|
| 视角 | 量价（K 线 / PE / 技术指标） | 舆情（情绪面 / 热词 / 公告） |
| 数据源 | 行情 API | 雪球帖子 + 资讯 + 公告 |
| 推送内容 | 早报 + 技术指标 | 舆情异常 + 日报 |
| 调度 | 独立 cron | 独立 cron |

两套系统互补运行，不做合并。
