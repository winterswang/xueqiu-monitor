# xueqiu-monitor Code Review

> 基准需求: idea-code/requirements.md (854行, 2026-05-25)
> 最后一次 Review: 2026-05-25 18:30 CST

## 状态总览

| 级别 | 总计 | 已修复 | 剩余 |
|------|------|--------|------|
| P0 | 6 | 6 | **0** |
| P1 | 6 | 5 | **1** (1-C, xueqiu-analyzer 数据源限制) |
| P2 | 16 | 7 | **9** |
| 新发现 | 2 | 2 | **0** |

---

## P0 — 核心功能阻塞 (0 剩余 ✅)

全部已修复，详情见下方修复记录。

---

## P1 — 重要但非阻塞 (1 剩余)

### 1-A: 爬虫超时保护 ✅
- **修复**: `crawler.py` `_crawl_with_timeout` daemon thread + `threading.Thread.join(timeout=600)`
- **验证**: `crawl_single_stock('SH600519', timeout=2)` → status=timeout

### 1-B: 爬取失败不阻塞 ✅
- **验证**: 单股失败继续处理下一只

### 1-C: posts_data 字段不完整 🔶
- **现状**: xueqiu-analyzer Discussion 模型仅含 `author, content, time, link, comments`
- **缺失**: author_id, comment_count, forward_count, like_count
- **影响**: push key_data 的 `posts_count_delta` / `hot_words` / `post_titles` 有数据，但互动指标不可用
- **解决方案**: 需 xueqiu-analyzer 升级 Discussion 模型，增加 count 类字段
- **优先级**: Phase 2，当前不阻塞 MVP

### 1-D: 自选股直读 morning-brief ✅
- **验证**: `_get_watchlist` 优先从 morning-brief DB 读取

### 2-A: 配置文件路径解析 🔶 → P2降级
- **现状**: config.json 中 `db_path: "data/monitor.db"` 相对 cwd
- **影响**: CLI 从不同目录运行可能访问不同 DB
- **缓解**: cron `scripts/run.sh` 先 `cd` 到项目目录
- **优先级**: P2，当前不阻塞

### 2-B: insert_sentiment_stats upsert ✅
- **修复**: `ON CONFLICT(stock_code, stat_date) DO UPDATE`
- **验证**: 两次插入同一 (stock_code, stat_date) → 同 row id

### 3-A: 缺失 requirements.txt 依赖 ✅
- **修复**: numpy, scikit-learn, pytest, pytest-cov, mypy

### 4-A: detect_new_announcement 集成 ✅
- **修复**: `detect_changes` 统一入口，调用 announcement 检测

### 4-B: filter 阈值调整 ✅
- **修复**: 从 "any ad post → filter entire batch" 改为 ">20% ad ratio → filter"

---

## P2 — 代码卫生 (9 剩余)

### 已修复 (7)
- [x] ✅ 2-C: 缺失 init_historical.py (冷启动脚本)
- [x] ✅ 2-D: 死导入 json (db.py)
- [x] ✅ 2-E: 死导入 math (detector.py)
- [x] ✅ 2-F: 死导入 time (feedback.py)
- [x] ✅ 2-G: 死导入 re, time (filter.py)
- [x] ✅ 2-H: alert.id 未更新 → PushHistory alert_id=0 bug
- [x] ✅ 2-I: timezone 用时区无关的 Unix timestamp

### 剩余 (9)
- [ ] 2-J: 数据库日志 → `logging.info` 替换 `print`
- [ ] 2-K: detector.py `time` 导入有时未用 (仅 hot_word 分支使用)
- [ ] 2-L: filter.py / detector.py 独立日志记录器配置
- [ ] 2-M: 异常处理统一 (部分文件用 `exc_info=True`, 部分不)
- [ ] 2-N: models.py 增加 `from __future__ import annotations` (已有，全模块)
- [ ] 2-O: requirements.txt 添加版本号下限注释
- [ ] 2-P: scripts/run.sh 错误处理完善 (trap EXIT)
- [ ] 2-Q: Dockerfile CMD → 考虑改为 ENTRYPOINT + CMD 分离
- [ ] 2-R: 配置验证: 启动时检查 db_path 可写性
- [ ] 2-S: 爬取成功率 <98% 告警未实现 (§2.1 验收标准 3)

---

## 本轮 Review 新发现

### B-1: alert.id 未更新 ✅ 已修复
- **文件**: `cli.py` L219
- **问题**: `db.insert_alert` 返回 alert_id 但未赋值给 `alert.id`
- **影响**: PushHistory 所有记录 `alert_id=0`，反馈闭环无法准确关联推送
- **修复**: 添加 `alert.id = alert_id`

### B-2: 5 个未使用导入 ✅ 已修复
- **文件**: db.py, detector.py, feedback.py, filter.py
- **影响**: 无功能影响，代码清洁度
- **修复**: 移除 json, math, time, re 等死导入

### B-3: §3.3 成功率告警缺失 🔶 
- **需求**: 单次调度成功率 <98% 时发送飞书告警
- **现状**: crawler 记录了成功率但未触发告警
- **优先级**: P2，冷启动期手动关注即可

---

## 需求对齐验证

| § 需求 | 检查项 | 状态 |
|--------|--------|------|
| 2.1 爬虫调度 | 60只股票批量 + 超时30s + 成功率 | ✅ (成功率告警 P2) |
| 2.2 数据存储 | 10张表 + CRUD + 情感聚合 | ✅ |
| 2.3 变化检测 | Z-score + 两期情感 + TF-IDF + post_spike + new_announcement | ✅ |
| 2.4 规则筛选 | 广告 + 标题去重 + 内容去重 + 短帖 + P0-P2 + 冷启动 | ✅ |
| 2.5 分级通知 | P0即时 + P1汇总 + 每日早报 | ✅ |
| 2.6 反馈闭环 | 权重调整 + 衰减 | ✅ (触发入口P2) |
| 3.1 性能 | 30min/60股 | ⚠️ 未测 |
| 3.2 安全 | 凭证/日志脱敏 | ✅ |
| 3.3 可用性 | 99% 正常运行 | ⚠️ 成功率告警未实现 |
| 3.4 错误处理 | timeout/网络/DB锁/推送失败 | ✅ |
| 4.1 技术栈 | Python + SQLite + numpy/sklearn | ✅ |
| 4.2 部署 | Cron + systemd | ✅ Cron已配, systemd未写(P2) |
| 5.1 数据模型 | 10张表 | ✅ |

---

## 正式运行前 Checklist

- [x] P0 清零
- [x] P1 关键项修复 (upsert, timeout, filter, cli, detector)
- [x] 飞书通知验证 (IM Bot 发送成功)
- [x] Cron 注册 (每4小时)
- [x] 冷启动脚本就绪 (init_historical.py)
- [x] Docker 部署方案就绪
- [x] GitHub 代码同步

**🔴 运行前需要手动确认:**
1. morning-brief DB 路径可达 (`/root/code/morning-brief/data/monitor.db`)
2. 飞书 IM Bot 已授权 (已确认 ✅)
3. xueqiu-analyzer 模块可导入 (cron 环境)

---

## 评分

| 维度 | 分数 | 说明 |
|------|------|------|
| 需求覆盖 | 90% | 核心功能全部覆盖，P2项非阻塞 |
| 代码质量 | 85% | 死导入清理后，9个P2项待修复 |
| 测试覆盖 | 0% | ⚠️ 无 pytest，需求要求80% |
| 部署就绪 | 75% | Cron+IM已配, systemd/监控未配 |
| 综合 | **B+** | 可以投入冷启动运行 |
---

# Round 2 Review — 2026-08-18（基准 main @ 2578288，PR #28-#38 后）

> 方法：实测复现优先（DB 只读查询 / 边界输入实测 / pytest / 最小复现），3 个并行 review 覆盖 report_generator / crawler+sentiment / P2核销+scripts。5/25 以来 +4400 行全部纳入。

## 修复验证（Round 1 → 现状）

- ISO 8601 时间修复（PR #37）✅ 生效：last_crawl_time 全部更新至 8/17，零帖快照消失
- 2-J/2-K/2-L/2-N/2-O/2-P/2-S 均已修复（行号证据略，见 git log）
- 2-M（exc_info 统一）、2-Q（Dockerfile）、2-R（db_path 可写检查）仍开口，均为低价值

## 新发现问题

### P1（7 项）

| # | 问题 | 证据 |
|---|------|------|
| N1 | **db.py:108 `log.info` NameError** — legacy DB 迁移分支用 `log` 但模块只有 `logger`；旧库迁移必崩且 ALTER 回滚 → 永久迁移失败循环 | 最小复现：旧 schema init_db → `NameError: name 'log' is not defined` |
| N2 | **health_check.py 缺 `from __future__ import annotations`** — python3.9 手动/Docker 路径 `Path \| None` TypeError 崩溃（cron 用 3.11 显式路径不受影响） | `python3 scripts/health_check.py` (3.9.6) → TypeError |
| N3 | **requirements.txt 缺 jieba** — detector.py:148,164 硬依赖，新环境起不来 | 3.9 环境 import 链断实测 |
| N4 | **filter.py:159 公告告警硬编码永远 P2** — 4635 条公告 100% 静默；CBRS.US 8 条 SCHEDULE 13G 大额持仓披露从未推送 | `SELECT priority FROM change_alert WHERE alert_type='new_announcement'` → 全 P2 |
| N5 | **health_check 无数据新鲜度语义检查** — last_crawl_time 停更/时间解析失败率均不查，8/8 事故拖 5 天靠肉眼 | grep health_check.py 无 last_crawl/parse |
| N6 | **export_csv.py 无时间过滤**，docstring 谎称与 report_generator 一致；CSV 知识库混入 6/25 旧帖 | 8/17 CSV 华住帖 time 追溯 6/25 |
| N7 | **daily_sentiment_report.py（423行）孤儿脚本**：输出目录 data/reports/ 最新 2026-05-31，cron 实际走 report_generator；其 push 用 `lark` binary（系统只有 lark-cli）必失败被吞；main 文件写两遍（L312+L409）；且市场温度计无帖子发布时间过滤（全量 100 帖聚合） | ls data/reports/ + `command -v lark` 为空 |

### P2（按代码洁癖标准）

- 热词存储路径（cli.py:285）无停用词过滤：hot_word_dict top30 全泛词（ai/市场/这个/就是…）；daily_sentiment_report._NOISE_WORDS 仅 15 词打地鼠未复用 detector 67+ 停用表（若 N7 删脚本则此项消解一半）
- US 公告链接指向通用股页非详情页（可从标题 Accession Number 反解 EDGAR 深链）
- 反馈闭环死代码：content_weight/user_preference 0 行、feedback.py+decay 全链路无真实消费（待产品决策去留）
- cli.py `run_pipeline` ~400 行巨型函数，per-stock 处理 170 行应提取 `_process_stock()`
- cli.py `_build_summary`（L482-503）死代码，无调用
- get_previous_snapshot SELECT * 且 cli.py:200/227 同参调用两次（MB 级 JSON 拉两次 parse 两次）
- insert_sentiment_stat 每次 upsert 重复 CREATE UNIQUE INDEX IF NOT EXISTS（L156-159）
- push_history 先写 status="sent" 再发送，失败不回写（反馈数据失真）
- init_historical.py:164 硬编码 `/root/code/morning-brief`（历史教训重犯，一次性脚本）
- export_csv.py:331-336 同日重跑加时间戳改名继续上传（知识库重复条目）；COS 脚本绑定 ~/.hermes 外部路径
- sentiment.py 429 无重试（只有 max_tokens 溢出重试），批失败→整批静默降级 0.0 分
- _parse_post_time 仍不识别 `08月15日`、`今天 08:30` 格式（fail-open 保留，污染当天过滤）
- 测试 fixture 时区 bug：tmp_db 归一 UTC midnight vs 生产查询本地 midnight window → test_returns_has_data_when_yesterday_exists 恒失败（生产本身 OK：生产 stat_date 也是 UTC midnight 且落窗口内——但两侧约定脆弱，靠 8h 恰好兼容）

### 通过项（一句话）

db.py 连接管理（_ClosingConnection/WAL/重试）质量好；notifier 分级推送与 §2.5 一致、无硬编码 secret；crawler retry 语义正确（线性退避）；report_generator 时间过滤+两轮排序+prompt 构造合理，24/25 测试过；crawler 双路径时间映射已一致。

## 结论

Round 1 的 B+ → 当前 **B+（债增）**：三个月 11 个 PR 快速迭代把功能推到位（分组调度/公告链路/日报形态），但衍生脚本与主路径口径漂移、公告分级缺位、健康检查无语义层。建议 v0.7 修复 N1-N7 + 公告分级 + 哨兵，详见 docs/v0.7_design.md。

---

# Round 3 Review — 2026-09-20（分支 `feature/v2-daily-report`，v2 升级 9 个提交）

> 方法：全量 diff 走查（18 文件 / +3013 行）+ 真实数据端到端实测（DB 只读副本、本地 Chrome、
> 真实公告与新闻链接）+ ruff + 291 测试。

## 本轮修掉

| # | 问题 | 证据 |
|---|------|------|
| R3-1 | `tests/test_detail_fetcher.py` 有两个同名 `TestJunkPageDetection` 类，后者遮蔽前者 → 3 个测试从未执行 | ruff F811；已删重复类 |
| R3-2 | `requirements.txt` 缺 `pymupdf`：新环境/Docker 里公告 PDF 通道静默降级到付费 reader | 代码 `import fitz` 无声明；已补 |
| R3-3 | 日报尾注写"详情来源: 智谱 reader"，与 v2 实际通道不符（用户可见输出） | 当日日报正文；改为本地浏览器/PDF/EDGAR + reader 兜底 |
| R3-4 | README 三处失真：表数量 8（实际 13）、依赖缺 pymupdf/opencli、配置表写 `MINIMAX_API_KEY`（代码已明确不读该变量） | 代码实测；已更正 |
| R3-5 | Round 2 的 N4（公告告警硬编码 P2）由 `classify_announcement` 分级闭环；同表 P2 项"US 公告链接非详情页"由 SEC EDGAR 通道闭环 | `src/filter.py` / `src/detail_fetcher.py` |
| R3-6 | 会话内修掉的详情层缺陷：公告任务 SQL 漏选 `stock_code` → 整轮详情失败；美股 form 未纳入分级 → EDGAR 通道空转；时效闸不认东财 URL 日期 → 最旧 159 天旧闻漏网 | `tests/test_enrich_details.py` / `tests/test_detail_fetcher.py` 回归 |

## 实测验证（2026-09-20，真实数据）

- 291 测试全绿（v2 前 266）
- `scripts/verify_posts_migration.py` 全绿：集合等值 / 双写无缺口 / 内容抽查（10.1 万行 posts）
- 详情冷启动（清空 `detail_fetch_log` 的副本）：时效闸修复后任务 111 → 56 条，83s，**付费 reader 调用 0 次**
- 通道逐一实测：巨潮/港股/科创板 PDF 本地解析全文清晰；新浪/东财 news 正文干净；TSM 6-K、TCOM 20-F EDGAR 原文可读

## 遗留（未修，非阻塞）

- 8 条 `xueqiu.com/n/<中文标题>` 畸形 news 链接来自爬虫新闻流解析，详情层已判废，源头待查
- `detail_fetch_log` 缓存实为 24 小时滚动窗口（docstring 写"当日"），跨日语义略有出入
- `posts_data` 停写仍在观察期，`fetch_day_posts_union` 等读路径仍读 JSON 列
- 既有 lint 债（src 12 处 F401/F841、scripts E402 等）与 `run_pipeline` 巨型函数未动；新增行 0 lint 问题
