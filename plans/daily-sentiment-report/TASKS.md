# 方向 2 — 执行任务

> 关联 PLAN: plans/daily-sentiment-report/PLAN.md
> 状态: 未开始

## Phase 1：数据读取 + 情感聚合

- [ ] 创建 `scripts/daily_sentiment_report.py`
  - [ ] 连接 monitor.db
  - [ ] 读取今天所有股票的快照
  - [ ] 实现 `aggregate_sentiment()` — 遍历 posts_data JSON
- [ ] 创建 `etc/sectors.json`（64 只股票行业映射）
- [ ] 验证输出：每只股票 `{stock, posts, avg_score, bull, bear, neutral}`

## Phase 2：板块聚合 + 日报模板

- [ ] 实现 `aggregate_by_sector()` — 按行业汇总
- [ ] 实现 `build_top5()` — 热度排序
- [ ] 实现 Markdown 渲染模板
- [ ] 手动测试：`data/reports/2026-06-01.md`

## Phase 3：热词矩阵 + 异常联动

- [ ] 实现 `build_hotword_matrix()` — 读取 hot_word_dict 表
- [ ] 读取 change_alert 表 — Z > 2.0 标记
- [ ] 整合到日报中

## Phase 4：推送 + 定时

- [ ] 集成 lark CLI 推送
- [ ] 配置 cron: `30 8 * * *`
- [ ] 日报保留自动清理
