# 📋 项目开发跟踪文档

> 📅 创建于：2026-06-01 | 最后更新：2026-06-02
> 🌿 当前分支：main
> 🤖 最后分析：29d3b38 | <!-- @@LAST_ANALYZED: 29d3b38 @@-->

---

## 📌 项目概述

| 属性 | 内容 |
|------|------|
| 项目名称 | xueqiu-monitor |
| 项目简介 | 自选股雪球舆情持续监控系统。爬取雪球帖子、资讯、公告，通过 MiniMax LLM 情感分析 + Z-score 统计检测自动发现舆情变化，分级推送到飞书。 |
| 技术栈 | Python 3.11+, SQLite, MiniMax LLM, scikit-learn, Playwright, lark CLI |
| 仓库地址 | https://github.com/winterswang/xueqiu-monitor |
| 主要负责人 | winterswang |

---

## 🏗️ 系统架构与设计

### 架构概览

```
cron (07:00)
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

**最后验证日期**：2026-06-01

### 组件说明

| 组件 | 路径/模块 | 职责 | 依赖 | 备注 |
|------|----------|------|------|------|
| CLI 入口 | `src/cli.py` | 命令行入口，编排完整 pipeline | config/db/crawler/detector/filter/notifier | `run_pipeline()` 主函数 |
| 配置管理 | `src/config.py` | 从 JSON + .env 加载配置 | python-dotenv | 支持环境变量覆盖 |
| 爬虫模块 | `src/crawler.py` | 调用 xueqiu-analyzer，处理 watchlist，增量爬取 | xueqiu-analyzer, Playwright | 支持超时保护 + 回查兜底 |
| 数据存储 | `src/db.py` | SQLite CRUD，10 张核心表 | sqlite3 | WAL 模式，busy_timeout=3000 |
| 数据模型 | `src/models.py` | 10 个 dataclass，from_dict/to_dict/from_row | 无 | 免 ORM |
| 变化检测 | `src/detector.py` | Z-score + TF-IDF 热词 + 公告检测 | numpy, scikit-learn | 14天滚动窗口 |
| 规则过滤 | `src/filter.py` | 广告/重复/短帖过滤，优先级分级 | 无 | per-type 抑制策略 |
| 通知模块 | `src/notifier.py` | 消息格式化，双模式推送(lark CLI / 文件) | lark CLI | auto 模式自动检测 |
| 反馈闭环 | `src/feedback.py` | 用户反馈权重调整 + 权重衰减 | db | 增减权重、7天衰减 |
| 情感分析 | `src/sentiment.py` | MiniMax LLM 批量情感分析 | anthropic SDK | 讨论分批≤80条 |
| 数据库 Schema | `src/schema.sql` | DDL — 10 表 + 索引 | 无 | PRAGMA WAL + foreign_keys |

### 数据流

1. **爬取**: xueqiu-analyzer Playwright → 讨论/资讯/公告 → 结构化 dict
2. **存储**: dict → CrawlSnapshot/Comment/Announcement → SQLite INSERT
3. **检测**: 当前快照 vs 历史 ↔ Z-score → ChangeAlert
4. **过滤**: ChangeAlert → ad/dup/short filter → 优先级 P0/P1/P2
5. **推送**: lark CLI 直推飞书 / JSON 文件 → 用户群聊

### 执行流 (关键路径)

| 场景 | 入口 | 关键步骤 | 输出 |
|------|------|---------|------|
| 全量爬取 64 只 | `cron 7:00` | crawl_watchlist → 增量过滤 → sentiment → store | 50+ 快照, 20k+ 帖子 |
| 增量检测 | detect 循环 | post_spike → sentiment_shift → hot_word → announcement | ChangeAlert |
| 飞书推送 | dispatch_messages | lark CLI → +messages-send → OK/FAIL | 群聊消息 |
| 每日日报 | generate_daily_report | 汇总当日 alerts + 爬取摘要 | Markdown 文件 |

### 设计原则与约束

- 不自己实现爬虫，依赖 xueqiu-analyzer
- 单机 SQLite 部署，免运维
- 冷启动 28 天积累基线，不误报
- Phase 1 规则检测，Phase 2 升级 LLM 精检测（未开始）

---

## 📐 技术决策记录 (ADR)

| ID | 日期 | 决策 | 背景 | 结论 | 替代方案 | 影响范围 |
|----|------|------|------|------|---------|---------|
| ADR-001 | 2026-05-27 | MiniMax M2.7 作为情感分析 LLM | 需要中文财经帖情感分析，兼顾成本和准确性 | 采用 MiniMax Anthropic 兼容 API | 纯关键词 / OpenAI | sentiment.py |
| ADR-002 | 2026-05-28 | lark CLI 双模式推送 | 飞书官方 CLI 支持 bot 身份直接推送 | auto 检测 + file fallback | 直接 webhook | notifier.py |
| ADR-003 | 2026-05-30 | `.env` 文件管理敏感配置 | 环境变量不可追踪，shell 容易泄漏 | python-dotenv 在 `__init__.py` 加载 | shell env / config.json | 全局 |
| ADR-004 | 2026-05-30 | requests 直连 Longbridge API 替代 Rust SDK | SDK Rust 类无法从 Python 构造，OAuthBuilder 有本地回调端口 bug | HTTP requests + OAuth refresh_token | Rust SDK | sync_watchlist.py |
| ADR-005 | 2026-06-01 | `crawl()` 增加 `days=1` 增量参数 | 全量爬取 64 只耗时 24h，cron 窗口期不够 | xueqiu-analyzer 的 `crawl(days=1)` 参数 | max_pages=50 全量 | crawler.py |

---

## 🚀 功能特性 (Features)

| ID | 特性描述 | 优先级 | 状态 | 负责人 | 开始日期 | 目标版本 | 关联模块 | 备注 |
|----|---------|--------|------|--------|----------|----------|---------|------|
| F-001 | 雪球爬虫集成 | P0 | 已发布 | winterswang | 2026-05-25 | v0.1 | crawler.py | xueqiu-analyzer Playwright |
| F-002 | SQLite 10 表存储 | P0 | 已发布 | winterswang | 2026-05-25 | v0.1 | db.py, models.py, schema.sql | |
| F-003 | Z-score 变化检测 | P0 | 已发布 | winterswang | 2026-05-25 | v0.1 | detector.py | 14天窗口 |
| F-004 | 公告变化检测 | P1 | 已发布 | winterswang | 2026-05-27 | v0.1 | detector.py, cli.py | P1-5 修复 |
| F-005 | 情感偏移 Trigger 1 | P0 | 已发布 | winterswang | 2026-05-27 | v0.1 | detector.py | 两期对比 |
| F-006 | TF-IDF 热词检测 | P0 | 已发布 | winterswang | 2026-05-25 | v0.1 | detector.py | |
| F-007 | 规则过滤 (广告/重复/短帖) | P0 | 已发布 | winterswang | 2026-05-25 | v0.1 | filter.py | per-type |
| F-008 | 分级推送 (P0/P1/P2) | P0 | 已发布 | winterswang | 2026-05-26 | v0.1 | notifier.py | |
| F-009 | MiniMax LLM 情感分析 | P0 | 已发布 | winterswang | 2026-05-26 | v0.1 | sentiment.py | 分批≤80条 |
| F-010 | lark CLI 双模式推送 | P1 | 已发布 | winterswang | 2026-05-30 | v0.1 | notifier.py | |
| F-011 | 增量爬取 (去重 + 回查兜底) | P0 | 已发布 | winterswang | 2026-05-26 | v0.1 | crawler.py | DB 回查 |
| F-012 | 冷启动保护 | P0 | 已发布 | winterswang | 2026-05-26 | v0.1 | detector.py | 28天 |
| F-013 | .env 文件配置 | P1 | 已发布 | winterswang | 2026-05-30 | v0.1 | `__init__.py`, config.py | |
| F-014 | Longbridge 自选股同步 | P1 | 已发布 | winterswang | 2026-05-30 | v0.1 | sync_watchlist.py | 64 只 |
| F-015 | 爬取成功率告警 | P2 | 已发布 | winterswang | 2026-05-30 | v0.1 | cli.py | <98% |
| F-016 | 结构化日志 + 阶段计时 | P2 | 已发布 | winterswang | 2026-05-30 | v0.1 | cli.py | [PHASE] / [SUMMARY] |
| F-017 | 健康检查脚本 | P2 | 已发布 | winterswang | 2026-06-01 | v0.1 | health_check.py | |
| F-018 | 用户反馈闭环 | P0 | 已发布 | winterswang | 2026-05-25 | v0.1 | feedback.py | |
| F-019 | 自选股情绪日报 | P1 | 已发布 | winterswang | 2026-06-02 | v0.1 | daily_sentiment_report.py | 64只全景看板 |

---

## 🐛 Bug 跟踪

| ID | Bug描述 | 严重程度 | 状态 | 发现日期 | 修复日期 | 关联Commit | 关联Issue | 负责人 |
|----|---------|---------|------|----------|----------|------------|-----------|--------|
| B-001 | comments 表 JOIN 错误(无 stock_code 列) | Critical | 已修复 | 2026-05-27 | 2026-05-28 | a8f2940 | — | winterswang |
| B-002 | 情感偏移 Trigger 1 从未触发 | Major | 已修复 | 2026-05-27 | 2026-05-27 | 1e68aa6 | — | winterswang |
| B-003 | ContentWeight schema-model 不一致 (缺 preference_level) | Major | 已修复 | 2026-05-27 | 2026-05-27 | 1e68aa6 | — | winterswang |
| B-004 | 公告检测未集成到 main 流程 | Major | 已修复 | 2026-05-27 | 2026-05-27 | 1e68aa6 | — | winterswang |
| B-005 | 硬编码 `/root/` 路径不可移植 | Major | 已修复 | 2026-05-27 | 2026-05-27 | 1e68aa6 | — | winterswang |
| B-006 | filter_alerts 一刀切 (全部告警被同比例过滤) | Major | 已修复 | 2026-05-27 | 2026-05-27 | 1e68aa6 | — | winterswang |
| B-007 | upsert_weight TOCTOU 竞态 | Minor | 已修复 | 2026-05-27 | 2026-05-27 | 1e68aa6 | — | winterswang |
| B-008 | 爬虫超时线程泄漏 (Playwright 未关闭) | Minor | 已修复 | 2026-05-27 | 2026-05-27 | 1e68aa6 | — | winterswang |
| B-009 | MiniMax 返回裸数组导致 JSON parse 失败 | Minor | 已修复 | 2026-05-28 | 2026-05-28 | a6264ec | — | winterswang |
| B-010 | MiniMax 长讨论组超时 (400+ 条) | Major | 已修复 | 2026-05-28 | 2026-05-28 | 7021a04 | — | winterswang |
| B-011 | 增量爬取新帖被误过滤 (回查兜底) | Major | 已修复 | 2026-05-27 | 2026-05-27 | f9899fc | — | winterswang |
| B-012 | 首次运行公告告警爆炸 (17k 条) | Major | 已修复 | 2026-06-01 | 2026-06-01 | a332446 | — | winterswang |
| B-013 | detect 阶段卡住 (单连接逐条插入告警) | Critical | 已修复 | 2026-06-01 | 2026-06-01 | a332446 | — | winterswang |
| B-014 | sentiment_stats.z_score 被公告 3.0 污染 | Minor | 已修复 | 2026-06-01 | 2026-06-01 | a332446 | — | winterswang |

---

## 📝 Code Review 记录

| 日期 | 审查人 | 审查范围 | 类型 | 发现的问题 | 处理状态 | 关联PR/MR |
|------|--------|---------|------|-----------|---------|-----------|
| 2026-05-27 | deepseek | 全项目 P0/P1 代码审查 | 架构审查 | P0:2 / P1:5 / P2:9 | 全部已修复 | #1 |
| 2026-05-30 | deepseek | 合并后第二轮审查 | 架构审查 | N1: get_existing_post_ids 性能 | 已修复 | #1 |
| 2026-06-01 | deepseek | 基于真实运行数据的审查 | 性能审查 | 3 个生产问题 | 已修复 | — |

---

## ✅ TODO 事项

| ID | 事项 | 优先级 | 状态 | 创建日期 | 截止日期 | 负责人 | 关联 | 备注 |
|----|------|--------|------|----------|----------|--------|------|------|
| TODO-001 | 新增股票时自动添加到 watchlist | 低 | 待开始 | 2026-06-01 | — | — | — | 通过 Longbridge watchlist group |
| TODO-002 | 日报推送到飞书 | 低 | 待开始 | 2026-06-01 | — | — | notifier.py | 目前只写文件 |
| TODO-003 | Phase 2: LLM 内容质量评估 + 智能摘要 | 低 | 已搁置 | 2026-05-25 | — | — | — | requirement 中原定 |
| TODO-004 | Web UI | 低 | 已搁置 | 2026-05-25 | — | — | — | out of scope Phase 1 |

---

## 🔄 版本发布记录

### v0.1.1 (2026-06-02)

**变更摘要**：增量爬取性能优化 + 自选股情绪日报。

#### ✨ 新增功能
- 自选股情绪日报（`scripts/daily_sentiment_report.py`）：64只全景看板 + 情感聚合 + 板块排行 + 热词提取 + 飞书推送
- 行业板块映射（`etc/sectors.json`）

#### 🔧 优化改进
- `crawl()` 增加 `days=1` 参数：增量爬取从 32min 降至 ~9min（65只股）

#### 📚 文档更新
- 价值投资者工具链种子想法 + 需求文档

### v0.1.0 (2026-06-01)

**变更摘要**：初始版本，全功能 MVP。

#### ✨ 新增功能
- 雪球爬虫集成 (xueqiu-analyzer Playwright)
- MiniMax LLM 情感分析 + news 关键词匹配
- Z-score 统计检测 (post_spike / sentiment_shift)
- TF-IDF 热词涌现检测
- 公告变化检测 (对比上下期)
- 规则过滤 (广告/重复/短帖) + per-type 抑制
- 分级推送 (P0 即时 / P1 汇总 / P2 静默)
- lark CLI 双模式通知 (auto/file)
- 增量爬取 (时间过滤 + DB 回查兜底)
- 64 只自选股全量爬取
- 结构化日志 + 阶段计时 + 健康检查
- cron 定时调度 (6:00 同步 → 7:00 监控 → 健康检查)
- Longbridge 自选股同步

#### 🐛 Bug 修复
- Trigger 1 情感偏移修复
- 公告检测集成修复
- 硬编码路径修复
- 告警爆炸上限 + 批量插入性能修复
- sentiment_stats Z-score 污染修复
- MiniMax 超时 + JSON 解析修复
- 增量爬取回查兜底修复
- 多个 CR 发现的问题

#### 🔧 优化改进
- concurrency 2（并行爬取缩短时间）
- 批量 SQLite 写入（31,000 → 62 次连接）
- 通知模块重复代码消除

#### 📚 文档更新
- 完整 README
- PROJECT_LOG.md (由 AI 分析代理维护)

---

## 🏗️ 技术债务

| ID | 债务描述 | 影响范围 | 优先级 | 计划处理版本 | 创建日期 | 状态 |
|----|---------|---------|--------|-------------|----------|------|
| TD-001 | article sentiment 解析失败 (MiniMax 返回解释而非 JSON) | sentiment.py | 低 | v0.2 | 2026-05-30 | 待处理 |
| TD-002 | 日志文件轮转未配置 | 运维 | 低 | v0.2 | 2026-06-01 | 待处理 |

---

## 📊 项目指标

| 指标 | 数值 | 更新时间 |
|------|------|----------|
| Git 提交数 | 43 | 2026-06-02 |
| 已修复 Bug 数 | 14 | 2026-06-02 |
| 已完成 Feature 数 | 19 | 2026-06-02 |
| 活跃分支数 | 2 (main, feature/daily-sentiment-report) | 2026-06-02 |
| 测试覆盖率 | ~53% | 2026-06-02 |
| 爬取股票数 | 65（含新增） | 2026-06-02 |
| 增量爬取耗时 | 32min → ~9min (days=1) | 2026-06-02 |
| 生产运行次数 | 2+ | 2026-06-02 |

---

## 📅 重要里程碑

| 日期 | 里程碑 | 描述 | 状态 |
|------|--------|------|------|
| 2026-05-25 | Phase 1 MVP | 核心 pipeline：爬取→存储→检测→推送 | 已达成 |
| 2026-05-30 | 全流程 E2E | 64 只股票全量爬取 + 飞书推送 | 已达成 |
| 2026-06-01 | 生产稳定 | 3 个生产问题修复 + cron + 健康检查 | 已达成 |

---

## 🔧 提交分析记录

> Git post-commit hook 在每次提交后通过 `deepseek exec --auto` 触发即时分析。
> AI 分析代理自动读取 diff、理解语义并更新上方各板块。
> `LAST_ANALYZED` 标记追踪最近一次分析的 commit，确保不重复处理。
