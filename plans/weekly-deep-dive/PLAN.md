# 方向 4：周报 — 自动深度分析 — PLAN

---

## 一句话

> 每周日自动选出 64 只自选股中**变化最大的 5 只**，对它们跑完整 xueqiu analyzer 深度分析，合成一份周报。

---

## 解决了什么问题

### 现在的状态

```
xueqiu-monitor → 日间告警（有事件就推，零散）
方向 2          → 日间看板（64 只有概况，但只到情绪层）
缺少的：         → 每周一次"这周哪只股票变了，到底是怎么回事"
```

### 现实场景

周五晚上，你想知道这周自选股发生了什么、哪些值得周末花时间研究。

现在的做法：刷飞书告警历史 → 凭记忆拼凑 → 凭感觉挑一只来看。一周有 64 只 × 7 天 = 448 次的快照数据，靠人整理不过来。

### 方向 4 的回答

> 这周你的 64 只自选股里，变化最大的是这 5 只。每只的完整分析报告在下面。

---

## 怎么做

### 每周日晚上自动跑

```
Step 1: 读方向 2 的情绪数据
    │  对比这周 vs 上周每只股票的情感趋势
    └─ 选出情感变化最大的 Top 5（正向和负向）

Step 2: 对 Top 5 跑 xueqiu analyzer
    ├─ crawl → evaluate → reanalyze
    └─ 输出 5 份完整分析报告

Step 3: 合成周报
    ├─ 本周全景（方向 2 的 7 天汇总）
    ├─ Top 5 变化股摘要（每只 200 字）
    └─ 5 份报告全文作为附录
```

### 选 Top 5 的标准

不是简单的"情绪变差"，而是**变化幅度 × 当前评分**：

```python
def prioritization_score(stock):
    """
    综合评分，选出最值得深度分析的股票。
    
    - sentiment_delta: 本周 vs 上周的情感变化绝对值
    - current_score: 当前 xueqiu analyzer 的充分性评分（如果有）
    - volatility: 讨论量本周的波动率
    - days_since_last_deep: 距离上次深度分析的天数
    
    得分越高，越值得本周分析。
    """
    return (
        abs(sentiment_delta) * 3 +
        (150 - current_score) * 2 +   # 评分越低越值得关注
        volatility * 1.5 +
        days_since_last_deep * 1.0
    )
```

这个打分保证：每周选的 5 只不会是同一批——已经分析过的短期内不会再被选上。

---

## 输出长什么样

```
📬 自选股周报 — 2026-05-25~06-01
═══════════════════════════════════════

 📈 本周全景
 覆盖: 64 只 | 总帖子: 32,847 条 | 对比上周: +8%

 板块趋势变化
 ───────────────────────
 板块       上周     本周     变化
 白酒      42%      38%      ↓4%
 互联网    58%      65%      ↑7% ←
 新能源    52%      56%      ↑4%
 潮玩      60%      55%      ↓5% ←

───────────────────────────────────────
 🔍 本周深度分析 TOP 5

 1. 茅台 SH600519  — 评分 155 → 155 (持平)  — 应对推荐: 持有
    情绪从偏空回升，主要受分红公告推动。
    关键事件: 2025 年度利润分配方案（每 10 股派 238 元）
    📄 报告: data/reports/2026-06-01/SH600519_report.md

 2. 泡泡玛特 09992 — 评分 145 → 145 (持平)  — 应对推荐: 持有
    8 条回购连续公告，段永平持仓曝光。
    讨论从"增速放缓"转向"IP 出海"。
    📄 报告: data/reports/2026-06-01/09992_report.md

 3. 拼多多 PDD    — 评分 120 → 135 (↑15)   — 应对推荐: 留意
    Q1 营收 1062 亿超预期，讨论区情绪从 悲观 转为 谨慎乐观。
    管理层 5 分是主要短板，SEC 公告正文缺失。
    📄 报告: data/reports/2026-06-01/PDD_report.md

 4. 腾讯 00700    — 首次分析                — 应对推荐: 观察
    本周讨论量暴增，段永平加仓信号 + 回购持续。
    📄 报告: data/reports/2026-06-01/00700_report.md

 5. 宁德时代       — 每周一次（首次）推荐      — 应对推荐: 观察
    固态电池技术进展带动讨论，情绪偏多。
    📄 报告: data/reports/2026-06-01/NIO_report.md

───────────────────────────────────────
 ⚠️ 本周需立即关注的 3 只

 1. 小米 01810 — Z=3.2, 帖子 +892, 情绪 -0.4
    → su7 产能不足引发大量负面，股价跌破成本价
    → 方向 2 已检测到，monitor 已推送 P0

 2. 茅台 SH600519 — 情绪回升但历史冲击钝化
    → 本次分红公告的情绪提升仅为历史均值的 35%
    → 市场对股东回报政策已"审美疲劳"

 3. 泡泡玛特 — 本周公告密集（8 条回购+季报）
    → 需要关注下季度 IP 新品实际销售数据
```

---

## 技术方案

### 数据流

```
方向 2 的情绪数据 (data/reports/*.json)
    │  逐日对比，找出情感变化 Top 5
    ▼
xueqiu analyze (xueqiu-analyzer CLI)
    │  对 Top 5 跑完整分析
    ▼
data/reports/weekly/{date}/             ← 周报目录
    ├── _summary.md                      ← 周报概览
    ├── SH600519_report.md               ← 5 份完整报告
    ├── 09992_report.md
    ├── PDD_report.md
    ├── 00700_report.md
    └── 300750_report.md
```

### 脚本结构

```python
scripts/weekly_report.py

def main():
    last_week_dates = get_last_week_dates()
    sentiment_data = load_daily_reports(last_week_dates)
    top5 = select_top5(sentiment_data)
    for stock in top5:
        run(f"xueqiu crawl {stock.symbol}")
        run(f"xueqiu evaluate --data data/{stock.symbol}_data.json")
        run(f"xueqiu reanalyze --data data/{stock.symbol}_data.json")
    render_weekly_summary(top5, sentiment_data)
    push_report()
```

### 与方向 2 的关系

```
方向 2：每天跑 → 产出 daily sentiment 数据（入 SQLite/JSON）
方向 4：每周日跑 → 读取方向 2 的一周数据 → 挑 Top 5 → 跑深度分析

方向 4 本身不爬虫，不分析情感，只做：
  1. 读方向 2 的产出
  2. 算谁的 delta 最大
  3. 触发 xueqiu analyzer 的 CLI
```

---

## 执行计划

| 天 | 任务 | 输出 |
|----|------|------|
| 1 | 写选 Top 5 逻辑 | `prioritization_score()` |
| 2 | 写自动重分析逻辑（调用 CLI） | 5 份报告输出到目录 |
| 3 | 写周报母版渲染 | `_summary.md` |
| 4 | 整合推送 + cron | 每周日 20:00 推送 |
