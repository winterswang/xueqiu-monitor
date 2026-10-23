# 方向 8：期权感知系统（options-sense）— PLAN

> 舆情是散户在说什么，期权数据记录合约成交、持仓与定价，但不直接揭示参与者或交易意图。
> 把衍生品市场的「定价」和舆情论坛的「情绪」并排放在一起，找它们互相矛盾的时刻。

## P0 可信口径护栏（2026-10-23）

- PCR、OI、成交量只描述合约数量与持仓/成交结构，不能识别主动买卖方向、开平仓、参与者身份或资金流；不得据此声称“机构买保险”或“新增资金偏多”。
- 逐合约指标和 PCR 必须使用相同的来源质量闸门，并记录有效/总合约数与过滤规则。CBOE 使用 bid>0、ask≥0.03、spread/mid≤60%；HKEX DTOP 使用 settle>0 且 OI 或成交量为正。
- 墙与 Max Pain 使用明确标记的单一到期链。Max Pain 仅为该链静态 OI 数学结果，不表述为磁吸、支撑、阻力或方向预测。
- 跨标的比较行权价与正股价格前，必须校验标的身份、币种、ADR 比例与汇率。缺任一转换元数据时不做比较；BABA.US 与 9988.HK 不得直接比较。
- 不支持的字段、冷启动、过期或有效覆盖不足时应明确降级，不得补造结论。
- 全期限汇总 PCR 仅保留作数量汇总；不能作为近端方向信号。期限桶按单一实际 expiry 存储，报告展示 1–20 DTE 近端与 21–45 DTE 月度链；没有合格到期链就标不可用。
- CBOE P1 指标限定报价过闸链：指定到期 ATM IV 要求现价 ±5% 范围内 Call 与 Put 都有 IV；25Δ skew/RR 要求同到期 Call delta +0.15…+0.35、Put delta −0.35…−0.15 同时有 IV。1σ 标尺按 `spot × IV × sqrt(DTE/365)` 计算，代表期限波动尺度，不是预测或风险金额。DTOP 无 IV/Greeks，明确不可用。
- 不计算 signed order flow、dealer GEX 或风险金额；期权合约乘数/调整合约没有可信元数据时不做金额标准化。

### 历史校准验收（后续，不含本轮回测平台）

在累计至少 60 个有效交易日后，按标的、来源和 expiry 桶分别检查：采集/报价过滤覆盖是否稳定；近端与月度 ATM IV 是否各自有足够样本；以同一 underlying 的到期前实现波动对照所报 1σ 尺度，汇总覆盖率与误差分布；同时检视 25Δ skew/RR 是否因稀疏 delta 合约频繁缺失。覆盖率偏离标称区间或数据缺失集中时，应先修订数据质量/口径并记录版本，再讨论阈值；不得用单次样本宣称方向预测能力。

---

## 1. 背景与独特价值

### 1.1 为什么期权数据和舆情数据天生互补

| | 雪球舆情（现有系统） | 期权市场（本方向新增） |
|--|---------------------|----------------------|
| 参与者 | 散户为主 | 多类参与者；仅凭合约统计无法识别身份 |
| 表达成本 | 发帖零成本，噪音 95% | 每张合约真金白银，噪音低 |
| 时间指向 | 描述**已发生**的事 | 定价**未来**的波动与价位 |
| 情绪特征 | 情绪化、滞后、易反转 | 冷静、抢先、用仓位说话 |

单看任何一个都有盲区。**两者背离的时刻信息量最大**：

```
案例形态 A（顶部预警）：
  雪球情绪连续走高 + put 未平仓量一周激增 40%
  → 只能确认 put/call OI 结构变化；不能判断参与者身份、开平仓或方向

案例形态 B（左侧机会）：
  雪球情绪跌到冰点 + call 成交量 PCR 骤降 + ATM 权利金率走高
  → 只能确认成交数量比值与报价变化；不能推断新增资金或方向
```

### 1.2 解决什么问题

| 场景 | 现在的做法 | options-sense 的做法 |
|------|-----------|---------------------|
| 财报前总觉得该做点什么，但不知道市场博弈到什么程度了 | 刷雪球看热闹，凭感觉 | 系统告警：vol-PCR 一天从 0.6 跳到 2.9，put 成交激增 = 对冲盘已经下场 |
| 画的支撑位压力位总是被打破 | 主观画趋势线 | 期权 OI 集中位只作仓位分布描述，不作为支撑/压力判断 |
| 想卖权利金收租，不知道现在权利金算贵算便宜 | 打开软件看一眼，没有历史刻度 | ATM 权利金率年化 + IV 一年分位，便宜/贵一目了然 |
| 情绪告警收到后不知道该多信几分 | 只能看单一信号 | 日报并列期权数量/波动信息，但不据此推断机构行为或方向 |

### 1.3 定位

> 不是期权交易系统，不下买卖指令。
> 是给现有舆情监控加一双「看聪明钱」的眼睛，输出的是环境变量：市场为未来定价了什么。

---

## 2. 数据源（已全部实测验证，2026-10-14/15）

### 2.1 数据可得性矩阵

| 数据 | 美股（19 只，池内全部） | 港股（10 只，池内 20 只中有上市期权的部分） |
|------|------------------------------|--------------------------|
| 聚合 PCR（量/仓）日频历史 | ✅ Longbridge volume daily（20 天滚动，首日回填）+ CBOE 自算交叉校验 | ✅ DTOP 逐合约聚合（首日即建，无历史回填源 → 冷启动 30 天） |
| 逐合约 OI 分布（期权墙/Max Pain） | ✅ CBOE 快照（PDD 886 / META 7624 / NVDA 4342 / TSM 2498 / CRWV 1712 合约） | ✅ DTOP 逐合约（9992 552 / 981 566 合约） |
| ATM IV / IV 分位 | ✅ CBOE 合约级 iv + 标的级 iv30 | ❌ DTOP 无 IV 字段（2026-10-15 实测） |
| IV 期限结构（近月 vs 次近月） | ✅ CBOE 多到期周期（15~26 个） | ❌ 同上 |
| Skew 偏度近似 | ✅ CBOE delta 字段直接筛 25Δ | ❌ 同上 |
| 权利金收益率（ATM premium %） | ✅ CBOE bid/ask ÷ current_price | ✅ DTOP 结算价 ÷ Longbridge 正股价 |
| Max Pain | ✅ 由合约 OI 计算 | ✅ 由合约 OI 计算 |
| Greeks（delta/gamma/theta/vega） | ✅ CBOE 现成 | ❌ DTOP 无 |

**关键修正（2026-10-15）：CBOE 公开延迟快照已覆盖美股逐合约全套字段，此前「美股 L2 需 Longbridge LV1 权限（错误码 301604）」的判断作废——LV1 只是可选升级（实时性），不是依赖项。美股 L2 信号全部可做。**

**关键修正之二（2026-10-15，港股通道换轨）：** 本文档原定的港股通道 `stock_options_detail.aspx`（逐合约 CSV）在 HKEX 改版后**已 404**。改走 **DTOP 日报**（见下表），一个 zip 覆盖全市场所有期权类别，港股采集成本从「每只 100~200 请求」降为**全市场 1 个请求**。代价是 DTOP 只有 OI/成交量/结算价，**没有 IV 与盘口** —— 港股不做 IV 与 Greeks（用户 2026-10-15 确认口径）。

### 2.2 渠道明细

| 渠道 | 覆盖 | 成本 | 说明 |
|------|------|------|------|
| CBOE 延迟快照 | 美股逐合约（主通道） | 免费、无账号、~15 分钟延迟 | cdn.cboe.com/api/global/delayed_quotes/options/{SYM}.json，单请求整条链（1~3.4MB），每合约 bid/ask/last/volume/OI/iv/delta/gamma/theta/vega，标的级 current_price+iv30。注意 307 重定向需跟随；历史快照仍需每日落库自建 |
| Longbridge CLI option volume daily | 美股聚合（交叉校验+回填） | 免费（已认证） | PCR 双口径约 20 天滚动——**首日即可回填 20 天 PCR 历史，大幅缩短 L1 冷启动**；此后作为 CBOE 自算 PCR 的第二数据源 |
| **HKEX DTOP 日报**（2026-10-15 改用） | 港股全市场逐合约（主通道） | 免费、无账号，约 19:24 发布 | `https://www.hkex.com.hk/eng/stat/dmstat/oi/DTOP_O_<yyyymmdd>.zip`，内含 `*_opt_dtl_all.raw`（干净 CSV，158 个类别）：每行 strike + Call/Put 各自的 Gross/Net OI、Net Change、T/O、Deal、Settle Price。**一次请求覆盖全市场**。无 IV、无盘口。OI 是当日收盘清算值（非 T-1） |
| HKEX 类代码表 xlsx | 港股标的映射（必需） | 免费、无账号 | 产品页挂的 `The-List-of-Stock-Option-Classes-Available-for-Trading_<yyyymmdd>.xlsx`：SEHK code ↔ HKATS code（981→SMC / 9992→POP）。DTOP 文件名用字母类代码，必须靠它映射 |
| ~~stock_options_detail.aspx~~ | ~~港股逐合约~~ | ❌ **2026-10-15 实测 404，HKEX 改版后路径作废** | 原「逐合约 CSV」方案作废，改走 DTOP |
| etnet AJAX | 港股整链 | 免费、实时 | component_futures_stockoptions.php?period=monthly&atscode=POP，有 bid/ask/量，**无 OI**；备选交叉校验源（当前未启用） |
| Longbridge quote | 港股价（收租率分母） | 免费（已认证） | `longbridge quote <c1> <c2> --format json` 批量取 `last`；DTOP 不含标的价格 |

**与已有 PDD 期权矩阵的关系**：~/pdd-options/daily_matrix.py（每日 0 点调度已运行）是单股消费者产品，走的就是 CBOE 通道；options-sense 把它的 CBOE fetcher 和占位报价三道过滤闸（bid>0 / ask≥0.03 / 价差≤60% mid）泛化为多股采集器，两层互不干扰。

### 2.3 实测样例（2026-10-14/15）

```
META.US  OI-PCR 0.488（近 20 天 0.46~0.49，稳定偏多）
PDD.US   OI-PCR 0.728（中性）；10-01 vol-PCR 2.90 = 事件前对冲激增
9992.HK  POP 147.5C 11月到期：last 5.56 / IV 21.82% / premium 3.73% / OI 590

CBOE 覆盖（2026-10-15 00:00 快照）：
  PDD  S0=81.71  iv30=32.2   886 合约  OI>0 642
  META S0=740.22 iv30=42.8  7624 合约  OI>0 5340
  CRWV S0=66.03  iv30=89.5  1712 合约  OI>0 1596
  NVDA S0=222.58 iv30=33.0  4342 合约  OI>0 3400
  TSM  S0=434.04 iv30=32.6  2498 合约  OI>0 2191
```

---

## 3. 信号库（三层）

### 3.1 L1 聚合层（美股+港股都可做，日频）

| 信号 | 定义 | 解读 | 检测方法 |
|------|------|------|---------|
| PCR-OI 结构 | put OI / call OI（按到期分别计算） | 数量结构比值，不识别方向、开平仓或参与者；慢变量描述 | 60 日同到期/来源历史基线 |
| PCR-Vol | put vol / call vol（按到期分别计算） | 数量结构比值，不识别主动方向或开平仓 | 同到期/来源历史基线 |
| 总量异动 | 期权总成交量/OI 突增 | 合约活动变化，不能推导资金流或事件方向 | 成交量 z-score（复用 compute_z_score） |

### 3.2 L2 结构层（美股 CBOE + 港股 HKEX 逐合约都做）

| 信号 | 定义 | 解读 | 注意事项 |
|------|------|------|---------|
| OI 集中位 | 指定到期链中 call/put OI 最大的行权价 | 仓位分布描述，不作支撑/压力或方向解释 | 按到期和质量过滤分别计算 |
| Max Pain | 指定到期链静态 OI 下到期内在价值总和最小的价格 | 数学统计值，不代表价格磁吸或目标 | 单到期计算并标注 expiry |
| ATM IV 分位 | 当前 IV 在自建历史中的百分位 | 低 = 买期权便宜（事件博弈窗口）；高 = 卖权利金收租窗口 | 需 ≥60 天自建历史 |
| IV 期限结构 | 近月 IV − 次近月 IV | 正向陡峭 = 财报/事件恐慌；事件后回落 = 利好出尽 | 财报日历联动 |
| Skew | OTM put IV − ATM IV | 走陡 = 尾部保护需求升温，常领先舆情恶化 | 9992 流动性有限，用 25Δ 附近最近合约近似 |
| 权利金收益率 | ATM call premium % 年化 | >15% 年化 = 持股收租窗口提示（对持仓者直接可执行） | 仅对报告中有持仓标记的股票推送 |

### 3.3 L3 组合层（全系统独有信号）

| 信号 | 触发条件 | 输出 |
|------|---------|------|
| 舆情-期权并列观察 | 需先有经历史校准的同到期指标 | 当前不启用方向性背离告警；数量统计不能确认机构行为 |
| 舆情-期权并列观察 | 情绪与期限桶数据仅并列展示 | 不使用“资金布局”等方向性结论 |
| 事件博弈升级 | 财报前 N 天 + (vol spike 或 IV surge) | P1：财报博弈加剧，附 IV 期限结构 |
| 三重共振 | 情绪 + 期权 + 量价同向极端 | P0，历史罕见，值得人工看盘 |

---

## 4. 系统设计

### 4.1 架构（完全复用现有管道）

```
scripts/collect_options.py            cron：--market hk 20:00 / --market us 07:05（北京时间）
    ├─ 美股: CBOE 快照（单请求整链）──→ options_contracts + options_daily
    │        Longbridge volume daily（20 天回填+交叉校验）──→ options_daily
    └─ 港股: 类代码表 xlsx + DTOP zip（共 3 请求）──→ options_daily + options_contracts
             正股价: Longbridge quote（收租率分母）
src/options_detector.py               复用 compute_z_score / is_cold_start
    ├─ L1 聚合信号 → change_alert (alert_type='options_*')
    ├─ L2 结构信号（HK 合约数据 → 聚合指标写回 options_daily）
    └─ L3 背离信号（join sentiment_stats × options_daily）
src/report_generator.py               新增「期权温度计」章节
notifier                              完全复用现有 P0/P1/P2 分级推送
```

### 4.2 表设计（对齐现有 schema 风格）

```sql
CREATE TABLE IF NOT EXISTS options_daily (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code   TEXT    NOT NULL,
    stat_date    INTEGER NOT NULL,                 -- unix 00:00:00
    call_volume  INTEGER,
    put_volume   INTEGER,
    call_oi      INTEGER,
    put_oi       INTEGER,
    pcr_volume   REAL,
    pcr_oi       REAL,
    atm_iv       REAL,                             -- 美股=CBOE iv30(%); 港股=NULL (DTOP 无 IV)
    iv_percentile REAL,                            -- detector 写回
    premium_yield REAL,                            -- ATM call premium % 年化
    max_pain     REAL,
    z_pcr_oi     REAL,
    z_volume     REAL,
    source       TEXT NOT NULL DEFAULT 'cboe'      -- cboe/longbridge/hkex
);
CREATE TABLE IF NOT EXISTS options_contracts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code  TEXT    NOT NULL,
    snap_date   INTEGER NOT NULL,
    contract_id TEXT    NOT NULL,                  -- HKEX oID
    expiry      TEXT    NOT NULL,                  -- YYYY-MM-DD
    strike      REAL    NOT NULL,
    cp          TEXT    NOT NULL,                  -- C/P
    last REAL, bid REAL, ask REAL,
    volume INTEGER, oi INTEGER, net_oi INTEGER,
    iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL,
    premium_pct REAL,
    moneyness TEXT,
    UNIQUE(stock_code, snap_date, contract_id)
);
-- 信号复用 change_alert：alert_type='options_pcr_spike'/'options_iv_extreme'/
-- 'options_divergence_top'/'options_divergence_bottom'/'options_event_heat'
```

### 4.3 采集器设计要点

- **美股**：CBOE 单请求拿整条链（跟随 307 重定向），合约落 options_contracts、聚合 PCR 写 options_daily；另跑 Longbridge volume daily 回填 20 天 PCR 历史并做第二源交叉校验（两源 PCR 偏差>20% 记数据质量告警）。复用 daily_matrix.py 的三道报价过滤闸（bid>0 / ask≥0.03 / 价差≤60% mid）防占位价污染。
- **港股（2026-10-15 换轨）**：抓 HKEX 产品页 → 解析出类代码表 xlsx 链接 → 下载得 `SEHK code ↔ HKATS code` 映射（缓存 `data/hk_option_classes.json`，7 天有效）→ 下载当日 DTOP zip → 只解出 `*_opt_dtl_all.raw` → 按映射筛出自选股 → 聚合。**全流程 3 个请求**。`stock_code` 用 `9992.HK` 形式（与 config.report.json 同键，日报才能 join）。
- **港股流动性过滤**：DTOP 无盘口，闸门改为 `settle>0 且 (oi>0 或 volume>0)`；期权墙/max pain 另加 ±20% 价内外带。
- **交易日历**：美股复用 src/trading_days.py；港股以 **DTOP 是否存在**为准（不存在即非交易日/未发布，往前回看最多 7 天）。

### 4.4 日报「期权温度计」章节（示意）

实际渲染（2026-10-14 数据，已上线）：

```
## 五、期权温度计
- **拼多多** · PCR-仓 0.73 · PCR-量 0.69 · IV 32.1% · 收租率(年化) 10% · Call 墙 80 / Put 墙 80 · MaxPain 80 · (10-14)
- **中芯国际** · PCR-仓 1.20 · PCR-量 0.89 · 收租率(年化) 19% · Call 墙 65 / Put 墙 55 · MaxPain 57.5 · (10-14)

- 自选港股中 10 只无上市期权：心动公司、华住、中国中免、古茗、布鲁可、海吉亚医疗、新城发展、中远海能、腾讯音乐、BOSS直聘

*期权读数为环境参考，不构成操作建议。美股 CBOE ~15 分钟延迟快照，OI 为 T-1；
 港股为 HKEX 收盘清算值、无 IV；口径见 plans/options-sense/PLAN.md。*
```

要点：美股行有 IV，港股行没有（DTOP 无 IV）；「无上市期权」清单把「标的本就没有期权」与「采集漏了」区分开，避免误判。

### 4.5 冷启动与告警纪律

- 冷启动 30 天：只落库不告警（复用 is_cold_start 思路），IV 分位类信号需 ≥60 天。
- 所有期权类推送文案统一后缀「期权信号为环境参考，不构成操作建议」，与现有「提醒而非指令」哲学一致。
- P0 背离信号设每日上限（每股 1 条），防止震荡期刷屏。

---

## 5. 分阶段交付

| 阶段 | 内容 | 产出 | 工作量 |
|------|------|------|--------|
| Phase 1 | CBOE 美股合约采集 + Longbridge 回填 + L1 聚合信号 + 期权墙/ATM IV/权利金率（字段现成） | 美股全指标温度计，首日即有 20 天 PCR 历史 | ~1-2 天 |
| Phase 2 | **港股改走 HKEX DTOP 日报**（原逐合约 CSV 路径已失效）+ 类代码表映射 + 覆盖补齐至池内全部标的 | 港股 10 只温度计段上线；美股从 3 只补齐到 19 只 | 2026-10-15 完成 |
| Phase 3 | L3 舆情×期权背离 + P0/P1 推送接入 | 全系统独有信号上线 | ~1-2 天 |
| Phase 4（可选） | 用积累数据回测：PCR spike/背离后 5/20 日收益分布 | 信号有效性报告，反哺阈值 | 数据够 60 天后 |

**依赖与前置**：Phase 1-3 全部零新增账号/权限（CBOE 与 HKEX 均为公开接口）。Longbridge 美股期权行情（LV1）仅在未来想要实时数据时作为可选升级。

## 6. 边界（明确不做什么）

❌ 不做期权交易执行、不下单
❌ 不做实时盘中监控（日频快照足够，盘中告警是另一个量级的工程）
❌ 不做 A 股个股期权（市场不存在该产品）
❌ 不把 Max Pain/期权墙表述为「必然回归/必然支撑」——只作为仓位分布参考
❌ 不把 CBOE 15 分钟延迟快照当实时数据用——本系统只做日频，天然规避
