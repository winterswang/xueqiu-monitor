# 种子想法

## xueqiu-monitor: 自选股雪球舆情持续监控系统

### 核心痛点
用户有 60+ 只自选股，每天在雪球上产生大量帖子、资讯、公告，但 95% 是灌水/重复/噪音。手动逐只浏览费时且容易遗漏重大变化。目前的 morning-brief 看量价（K线/PE/技术指标），但缺失舆情维度——股价没动但讨论已经在发酵的事，morning-brief 抓不到。

### 核心价值
只推送有信息量的变化。不是全文推送，不是关键词匹配，而是增量价值——舆情发生实质性变化时才通知。

### 核心循环（每 N 小时每只自选股）
1. 爬取最新帖子/资讯/公告（调用 xueqiu-analyzer 的 Playwright 爬虫）
2. 存入 SQLite 历史快照（12 张表：帖子、评论、转发、情感统计、变化告警、热词词典等）
3. 与上次快照 diff —— 新帖子数、新公告、情感偏移、热词涌现
4. 语义筛选 —— Phase 1 规则引擎（关键词+短文本+去重+去广告），Phase 2 LLM 升级
5. 分级通知 —— P0 即时推（重大利空/利好）、P1 汇总推（明显变化）、P2 不推（日常波动）
6. 用户反馈闭环（标记有用/无用 → 调整权重）

### 依赖性
- xueqiu-analyzer: 爬取+质检（不重写爬虫）
- financial-sdk: 财务数据关联（舆情+基本面交叉验证）
- morning-brief: 自选股列表复用 + 飞书推送渠道

### 与 morning-brief 的关系
morning-brief 看量价（技术面），xueqiu-monitor 看舆情（情绪面）。两套 cron 独立运行，xueqiu-monitor 自己生成早报并推送到飞书，不与 morning-brief 的 K 线早报合并。

### 数据模型核心
- crawl_snapshots: 每次爬取完整快照
- sentiment_stats: 按日聚合的情感统计（Z-score 异常标记）
- change_alert: 异常变化检测告警
- hot_word_dict + hot_word_event: 热词词典 + 涌现事件
- push_history + user_preference + content_weight: 推送闭环

### 变化检测方法
Z-score: Z=(x-μ)/σ, 阈值 2.0 = 95% 置信；情感偏移 > 0.2；热词 TF-IDF 时间序列

### 分阶段策略
Phase 1（数据积累+规则）: 爬取→存储→Z-score检测→规则过滤→早报。冷启动期 3-4 周积累基线不推送。
Phase 2（LLM 精通知）: LLM 内容质量评估、情感深度分析、重要性评估、智能摘要。

### 成功标准
爬取成功率 ≥98%、60只全量 ≤30分钟、变化检测召回率 ≥85%、精确率 ≥70%、有效推送率 ≥50%

### 技术约束
Python 3.11+、SQLite 单机部署、Cron 调度、结构化 JSON 日志、飞书推送、PEP 8 + 测试覆盖率 ≥80%

### Out of Scope
Phase 1 不做 LLM 语义分析、不做 Web UI、不做雪球登录、不做其他平台爬取