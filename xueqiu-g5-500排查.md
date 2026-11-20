# xueqiu-pipeline-g5 HTTP 500 排查

- 排查时间：2026-11-05 21:00 前后
- 对象：Hermes cron job `e5873e8f8a9c` / `xueqiu-pipeline-g5`（cron `0 18 * * *`）
- 结论口径：**不是 xueqiu-monitor 的故障，是 cron agent 的模型服务端（火山引擎方舟）瞬时 500**

---

## 1. 一句话结论

**单次服务端抖动。Pipeline 本体 18:18:06 已正常跑完、数据一条没丢；失败的是 cron agent 自己的 LLM 调用——它在 18:06 连吃 3 次 HTTP 500 后放弃，job 被标记 error。丢的只有 G5 的飞书汇报卡片。不需要改代码。**

叠着一个背景隐患（不是本次失败原因，但放大了影响面）：主模型 `zai/glm-5.3` 自 **2026-11-04 12:00** 起触发 7 天用量上限，所有 cron 早已被迫走 fallback `volcengine-plan/deepseek-v4-flash`，配额到 `2026-11-08 09:11:37` 才恢复。

---

## 2. 五件事逐项证据

### 2.1 500 是谁返回的

**是模型 API（LLM），不是雪球、不是数据 API、不是搜索 API。**

`021…` 前缀是**火山引擎方舟（Ark）**的 request id 格式。失败请求 dump 里 URL 与模型写得很清楚：

```
url   : https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions
model : deepseek-v4-flash
reason: max_retries_exhausted
```

来源：`~/.hermes/sessions/request_dump_cron_e5873e8f8a9c_20261105_180040_20261105_180622_130772.json`

**发起该请求的位置**：不在 xueqiu-monitor 仓库里，是 Hermes cron agent 的对话循环。可对账的配置与代码位置：

| 位置 | 内容 |
|---|---|
| `~/.hermes/config.yaml:11` | `base_url: https://ark.cn-beijing.volces.com/api/coding/v3` |
| `~/.hermes/config.yaml:21` | `api_max_retries: 3` |
| `~/.hermes/config.yaml:498-500` | `fallback_model: provider volcengine-plan / model deepseek-v4-flash` |
| `~/.hermes/cron/jobs.json` → id `e5873e8f8a9c` | job 定义（`model: glm-5.3`, `provider: zai`） |
| `~/.pyenv/versions/3.11.12/lib/python3.11/site-packages/cron/scheduler.py:2948` | `run_job` 里 `raise RuntimeError(_err_text)` —— job 失败的抛出点 |

**payload 规模**（不存在上下文爆炸）：

- 21 条 message，`6` 个 tools
- 总字符 **129,880**
- 日志终局行自报：`msgs=21 tokens=~32,134`

两个大头是 prompt 本身，不是数据：
- 52,513 字符 = `lark-card` skill 全文被注入 user message
- 33,531 字符 = agent 读的 `cron-reporting-checklist.md`

判断依据：**同一份 payload 在 18:01–18:06 之间成功返回了 8 次**（`API call #1` 到 `#8`）。上下文规模没变、模型没变，只有服务端状态变了。所以不是「请求太大打挂了」。

**二次根因（为什么用 fallback 模型）**：

```
2026-11-05 18:00:54,738 INFO [cron_e5873e8f8a9c_20261105_180040] agent.turn_context:
  conversation turn: session=cron_e5873e8f8a9c_20261105_180040 model=glm-5.3 provider=zai platform=cron
2026-11-05 18:00:55,161 INFO agent.chat_completion_helpers: Streaming failed before delivery:
  Error code: 429 - {'error': {'code': '1310', 'message': 'You have reached the 7-day usage limit.
  You can continue using it after 2026-11-08 09:11:37. If you need overage pay-as-you-go access,
  please contact your admin to enable it.'}}
2026-11-05 18:00:55,189 INFO agent.chat_completion_helpers:
  Fallback to volcengine-plan/deepseek-v4-flash: clearing primary credential pool (pool_provider=zai) ...
2026-11-05 18:00:56,278 INFO agent.chat_completion_helpers: Fallback activated: glm-5.3 → deepseek-v4-flash (volcengine-plan)
```

429 的首次出现是 **2026-11-04 12:00:36**（`cron_3284f3d4bed1`，自选股同步），此后每个整点 job 都先吃一次 429 再切 fallback。今天全天 **10 次 429**，08:00 / 12:01 / 13:01 / 14:00 / 15:00 / 16:00 / 17:00 / 18:00 / 19:01 / 19:31 各一次。

---

### 2.2 是不是单次抖动

**是。而且这种 500 在本机历史上几乎没出现过。**

**（a）失败前的进度**（`logs/pipeline-g5.log` 原文）

agent 在 18:02:31 把 pipeline 丢到后台跑，然后每 60 秒轮询一次。到 18:06:09 第三次轮询返回时，python 侧已经爬到最后一支：

```
2026-11-05 18:02:33 [INFO] src.crawler: 从 morning-brief 加载 72 只自选股
2026-11-05 18:02:33 [INFO] __main__: 白名单模式: 9/72 只 (配置 9 只)
2026-11-05 18:02:33 [INFO] __main__: 开始爬取 9 只自选股 (concurrency=1, max_retries=2) ...
...
2026-11-05 18:06:14 [INFO] src.crawler: [9/9] 爬取 600519.SH ...
2026-11-05 18:07:35 [INFO] src.crawler:   opencli: 39 条公告
2026-11-05 18:07:37 [INFO] httpx: HTTP Request: POST https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions "HTTP/1.1 500 Internal Server Error"
2026-11-05 18:07:37 [INFO] openai._base_client: Retrying request to /chat/completions in 0.431212 seconds
2026-11-05 18:08:04 [INFO] httpx: HTTP Request: POST https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions "HTTP/1.1 200 OK"
```

注意 18:07:37 那一条：**pipeline 自己的情感分析调用也在同一个窗口吃到了同一个 500**，但它被 openai SDK 的自动重试接住了，0.43 秒后重试成功。同一个上游、同一个时段，两条链路都中招 —— 这就是服务端侧的特征。

**（b）历史对比**

```
$ grep "error_type=InternalServerError" agent.log{,.1,.2,.3} | 按日统计
   2 2026-09-16
   1 2026-10-29
  16 2026-11-05
```

- 10-11 ~ 11-04 期间 **0 次**（10 月只有 10-29 一次单发）
- 今天 **16 次，全部落在 17:18–18:06**

**（c）同组兄弟**

| 组 | 时刻 | 今日 500 次数 | 结果 |
|---|---|---:|---|
| g1 | 14:00 | 0 | ok |
| g4 | 15:00 | 0 | ok |
| g2 | 16:00 | 0 | ok |
| **g3** | 17:00 | **6**（17:18:32–17:22:19） | **ok**（重试活下来，17:22:24 完成） |
| **g5** | 18:00 | **10**（18:01:34–18:06:22） | **error**（3 连 500 打穿重试预算） |

g5 近 7 天历史：10-30 / 11-02 / 11-04 各 1 次 500，都被第一次重试救回来了；今天是唯一一次打穿。

**结论：17:18–18:06 是一个约 48 分钟的上游劣化窗口，g3 撑过去了，g5 没撑过去。**

---

### 2.3 有没有自动重试

**有，但只在 LLM 单次调用层，且预算太小。**

**（a）LLM 层重试**：`api_max_retries: 3`，退避 2.0–5.5 秒。原文：

```
2026-11-05 18:06:10,932 WARNING agent.conversation_loop: Retrying API call in 2.0320422155862388s (attempt 1/3)
2026-11-05 18:06:14,879 WARNING agent.conversation_loop: Retrying API call in 5.543659881991736s (attempt 2/3)
2026-11-05 18:06:22,095 WARNING agent.conversation_loop: API call failed (attempt 3/3) error_type=InternalServerError
2026-11-05 18:06:22,126 ERROR   agent.conversation_loop: API call failed after 3 retries.
  HTTP 500: The service encountered an unexpected internal error. Please retry later.
  Request id: 021793873180946bbc1b38287ece59059a49659ad58bad9614e76 |
  provider=volcengine-plan model=deepseek-v4-flash msgs=21 tokens=~32,134
```

三次尝试的三个 request id 各不相同（`021793873169462…` / `021793873173427…` / `021793873180946…`），说明确实是三次独立请求，不是同一个错误缓存。三次总共只花了 **12 秒**。

**（b）job 层重试：没有。** `~/.hermes/config.yaml` 的 `cron:` 段只有 `wrap_response: true`，没有 job 级 retry。Hermes 调度器在 agent 抛错后直接记 `last_status: error`，不再执行。

**（c）`批次失败自动补跑`（b42f8edba62c）为什么没补**

脚本 `~/.hermes/scripts/xueqiu_auto_retry.sh` 的判定是「**无 [SUMMARY] 且无进程**」才补跑：

```bash
SUMMARY=$(grep -E "^\S+ \S+ .*\[SUMMARY\]" "$LOG" | grep "^$TODAY" | tail -1)
[ -n "$SUMMARY" ] && exit 0
if pgrep -f "config\.$G\.json" >/dev/null 2>&1; then exit 0; fi
```

本例 pipeline 18:18:06 写出了 `[SUMMARY]`，所以 18:40 正确静默（output 记录 `Status: silent (empty output)`）。**数据层有兜底，卡片层没有兜底。**

**（d）`| tee` 会不会吃退出码：会，但本次不是它的问题**

命令 `python3 ... 2>&1 | tee logs/pipeline-g5.log` 没有 `set -o pipefail`，`$?` 拿到的是 tee 的 0。不过本例 agent 用的是后台进程 + `process` 轮询，压根没读 python 的退出码 —— job 的成败完全等于 agent 的成败。风险是将来 pipeline 真失败时这个退出码也不可见；`auto_retry.sh` 用 `[SUMMARY]` 行判定，等于绕开了这个坑。

---

### 2.4 数据是不是断了

**没断，9/9 完整。**

pipeline 是独立后台进程（`proc_8e517327ad31`, pid 81570），agent 18:06:22 死掉之后它继续跑，18:18:06 正常收尾：

```
2026-11-05 18:18:06 [INFO] __main__: Pipeline完成: {"crawled": 9, "failed": 0, "total_stocks": 9,
  "alerts": 12, "p0": 1, "p1": 4, "p2": 7, "filtered": 0, "elapsed_seconds": 350,
  "report": "data/daily_reports/2026-11-05-alerts.md", ...}
2026-11-05 18:18:06 [INFO] __main__: [SUMMARY] stocks=9/9 success=9 failed=0
  alerts=12 p0=1 p1=4 p2=7 filtered=0 elapsed=933s
2026-11-05 18:18:06 [INFO] __main__: 有帖股票数: 9/9 (100%) | health=healthy
```

落盘核对：

| 产物 | 状态 |
|---|---|
| `data/daily_reports/2026-11-05-alerts.md` | ✅ 18:15 生成 |
| `data/daily_reports/2026-11-05-sentiment.md` | ✅ 19:12（19:00 日报任务消费 g5 数据后产出） |
| `data/monitor.db` change_alert（G5 九只） | ✅ 12 条，18:09–18:13 写入 |
| 19:00 全池日报 | ✅ 51 只全部成功 |
| 19:30 健康检查 | ✅ ok（`[SILENT]`） |

DB 原文（`sqlite3 data/monitor.db`，G5 白名单）：

```
TEM.US|new_announcement|P2|59.60|2026-11-05 18:11:36
2423.HK|new_announcement|P2|48.00|2026-11-05 18:13:24
UBER.US|post_spike|P1|3.45|2026-11-05 18:12:30
SKHY.US|hot_word_surge|P1|3.21|2026-11-05 18:11:36
SKHY.US|sentiment_shift|P0|0.00|2026-11-05 18:09:54
UBER.US|new_announcement|P1|0.00|2026-11-05 18:12:30
...
```

**没有断点续跑问题**：pipeline 本身不需要续跑（进程一直活着），也就没有「半途失败已抓数据丢失」这回事。

**真正丢的东西只有一个**：G5 的飞书舆情卡片。job 死在构建卡片之前，这张卡没发。19:00 的全池日报覆盖了 G5 的股票，但那不是分组卡。

---

### 2.5 现在还能不能跑通

**能。**

1. **同一条命令今晚已经跑通过**：18:02:33 → 18:18:06，9/9 成功，12 条告警，日报落盘。失败点不在 pipeline。
2. **出故障的那一环现在恢复**：20:53 用同一个 provider/base_url/model 打了一次最小探针 ——

```
http=200 time=2.317754s
request id: 021793883224233816811b617e2b5631e8927bae226874c428dc9
```

   前缀 `0217938…` 与故障 request id 完全同源，可以确认就是火山方舟，且现在返回正常。

3. **没有重跑整条 pipeline**：它会重复抓取、重复产生告警，而数据已经在库里；重跑也补不回那张没发的卡片。真要端到端验证，代价约 16 分钟 + 一次重复的告警卡片。

---

## 3. 修复建议

> 前提：本次是单次抖动，**不改代码也能正常跑**。下面按「值得做」排序，不是必做清单。

### 立即（今晚/明天）

**① 补发 G5 卡片（或明确接受它被 19:00 全池日报覆盖）**

- 改哪里：无需改代码。手工触发一次 G5 的卡片构建（按 job prompt 第 2–5 步），或直接认账。
- 怎么验：飞书群 `oc_2fe3cfa05bfe4860c71a1bd1efbe3751` 收到标题含 G5 九只的卡片。
- 成本：一次 LLM 会话 + 一条卡片。

**② 处理 zai 配额（这才是长期隐患）**

主模型 `glm-5.3/zai` 要到 **2026-11-08 09:11:37** 才恢复。在这之前：

- 每个整点 job 都会先吃一次 429、再切 fallback，单次切换损耗约 1–37 秒；
- 所有 cron 实际都跑在 `deepseek-v4-flash` 上，而今天的 500 全出在这个 fallback 上。

二选一：
- 找 zai admin 开 overage（按量付费），或
- 把 `~/.hermes/cron/jobs.json` 里高频 job 的 `model` / `provider` 显式改成 `deepseek-v4-flash` / `volcengine-plan`，省掉每次 429 与切换。

怎么验：`~/.hermes/logs/agent.log` 里当天不再出现 `error_type=RateLimitError` 且 `Fallback activated` 消失。

### 短期

**③ 给 job 层加重试（目前完全缺失）**

现在的重试预算是 **3 次 / 12 秒**，而这次的劣化窗口是 **48 分钟** —— 3 次根本不在同一个量级上。建议任选其一：

- 在 Hermes cron 调度层加「失败后 5–10 分钟重试一次，最多 1 次」；
- 或更轻量：让 `xueqiu_auto_retry.sh` 顺带补一次卡片 —— 它已经有 `[SUMMARY]` 判定，加一段「本次 job 是否发过卡片」的检查即可。

怎么验：人为把 fallback 模型打到 500（或临时改错 API key），看 10 分钟内是否自动出现第二次尝试。

**④ 把 3 次固定预算改成按墙钟时间**

`api_max_retries: 3` + 2–5 秒退避，对 48 分钟的抖动窗口没有意义。建议改成「最多 5 次、指数退避到 60 秒」，总预算约 2 分钟。改哪里：`~/.hermes/config.yaml:21` 附近的重试策略。

### 长期

**⑤ 把「发卡片」从 agent 循环里拆出来**

现在为了发一张卡片，agent 要跑 15 分钟、8+ 次 LLM 调用、每 60 秒轮询一次，任何一次 LLM 抖动都会让整个 job 白跑。pipeline 已经把结论写进 `summary.json` 了，卡片完全可以由一个 `no_agent` 脚本直接生成发送（模式同 `批次失败自动补跑`）。收益：省 token、消除 LLM 单点、失败面从「agent 全流程」缩到「一次 HTTP」。

**⑥ `| tee` 前加 `set -o pipefail`**

让 python 的非零退出码可见（`set -o pipefail` 或取 `${PIPESTATUS[0]}`）。本次不是它导致的，但它是数据链路上一个长期被遮住的信号。

---

## 4. 明确回答：「不需要改代码」

- **500 是火山引擎方舟返回的**，本机历史上几乎没出现过（10-11~11-04 仅 10-29 出现过 1 次）；
- **pipeline 本体 18:18:06 已完整跑完**，9/9、12 告警、日报落盘、19:00 和 19:30 下游任务全部 ok；
- **重试机制存在**（3 次），只是预算对 48 分钟的抖动窗口太小；
- **唯一实际损失**是 G5 的分组飞书卡片没发出去。

所以：**不要为这次故障改 pipeline 代码**。要动的只有两件运维动作 —— 补发卡片、处理 zai 配额/主模型；再把「job 层重试」和「卡片脚本化」作为能力改进另立事项。

---

## 附：原始证据清单

| 证据 | 路径 |
|---|---|
| 失败请求完整 dump（URL/model/21 条 message/129,880 字符/错误体） | `~/.hermes/sessions/request_dump_cron_e5873e8f8a9c_20261105_180040_20261105_180622_130772.json` |
| agent 逐次调用与重试日志 | `~/.hermes/logs/agent.log`（18:00:54–18:06:23 段） |
| job 定义与 `last_error` | `~/.hermes/cron/jobs.json` → `e5873e8f8a9c` |
| 失败 run 记录 | `~/.hermes/cron/output/e5873e8f8a9c/2026-11-05_18-06-22.md` |
| pipeline 本体日志 | `logs/pipeline-g5.log`（152 行，18:02:33–18:18:06） |
| 补跑判定脚本 | `~/.hermes/scripts/xueqiu_auto_retry.sh` |
| 模型/重试配置 | `~/.hermes/config.yaml:1-21`、`:498-500` |
