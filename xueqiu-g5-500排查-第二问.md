# xueqiu-g5-500 排查 · 第二问：500 到底是什么

- 排查时间：2026-11-05 21:00–21:20
- 上一份：`xueqiu-g5-500排查.md`
- 本轮只做诊断，未改任何配置/代码

---

## 1. 一句话结论

**这个 500 是火山方舟的 `InternalServiceError`（官方定义：内部系统异常，稍后重试），不是额度错误。** 方舟的错误码表里，coding plan 额度耗尽是一个**独立错误码**：`429 TooManyRequests / QuotaExceeded`，并带 `{reset_time}`。**「额度耗尽被包成 500」这个假设，被官方文档和本地证据同时否掉。**

---

## 2. 证据链

### 2.1 Response body 原文：只说是内部错误，没有任何额度字段

`~/.hermes/sessions/request_dump_cron_e5873e8f8a9c_20261105_180040_20261105_180622_130772.json`：

```json
{"error":{
  "code":"InternalServiceError",
  "message":"The service encountered an unexpected internal error. Please retry later. Request id: 021793873180946bbc1b38287ece59059a49659ad58bad9614e76",
  "param":"",
  "type":"Internal Server Error"}}
```

`error` 对象里的结构字段：`status_code=500`、`code=InternalServiceError`、`param=""`（空）、`response_status=500`、`response_text` 与上面 body 一致。

**没有** `QuotaExceeded`、`quota`、`remaining`、`reset_at`、`reset_time` 之类的任何字段。如果真是额度耗尽，按官方表这里应该是 `QuotaExceeded` 且带重置时间（见 2.3）。

### 2.2 Response headers：查不到（如实说明）

Hermes 的 request dump 只保存**请求**头：`{Authorization, Content-Type}`，`error` 对象里也没有保存响应头。所以 `x-ratelimit-*`、`x-request-id` 这类响应头**无法核对**。

不是「没查到」就是「没有」——**这一项证据缺失**，只能靠 body + 官方表 + 反证来定案。

### 2.3 官方错误码表：额度是 429，不是 500

来源：<https://ark.volcengine.com/region:cn-beijing/docs/error-codes.md>（火山方舟官方文档 · 错误码，页面顶部注明「在任一文档页面地址后追加 `.md` 可获取该页的 Markdown 版本」）

原文逐行引用：

```
| 500 | InternalServerError | InternalServiceError | The service encountered an unexpected internal error. Please retry later. Request ID: {id}. | 内部系统异常，请您稍后重试。 |
```

```
| 429 | TooManyRequests | QuotaExceeded | You have exceeded the 5-hour/weekly/monthly usage quota. It will reset at {reset_time}. | 使用的额度超出5小时/周/月限额。 |
```

```
| 500 | InternalServerError | ModelLoading | The request cannot be processed at this time because the model is currently being loaded. Request id: {id}. | 下游返回模型加载中。请等待模型就绪后重试。 |
```

```
| 503 | ServiceUnavailable | ServiceConnectionRefused | Connection to service is refused: ... | 服务连接被拒绝... |
| 503 | ServiceUnavailable | NoAvailableService | No available service Request id: {id}. | 下游没有可用服务实例。 |
```

上下文超限也有独立错误码，同样不是 500：

```
| 400 | BadRequest | OutofContextError | Total tokens of image and text exceed max message tokens. | 当请求中包含图片时，文本和图片编码后的总 token 数超过了模型上下文长度限制 |
```

**结论：500 这一行只覆盖「内部系统异常」和「模型加载中」两种情形；额度、限流、上下文超长各自有专门的码，全部是 4xx。**

### 2.4 16 次 500 的 request id 能解码出请求时间（全部对得上）

我在实测中发现了 request id 的构造：`rid[2:15]` 是**请求发起时间的毫秒时间戳**。用 20:53 那次探针验证——`0217938832242338...` → `1793883224233 ms` → **2026-11-05 20:53:44**，与实际探测时刻完全一致。

用同一方法解码今天全部 16 次 agent 侧 500：

```
17:18:30.778  4afb291f  0217938703107784f644a6b4...   ← g3
17:20:12.263  4afb291f  02179387041226320c19fad7...
17:20:16.963  4afb291f  021793870416963bc68ba765...
17:21:52.144  4afb291f  0217938705121447353f3427...
17:22:08.035  4afb291f  021793870528035af71e85a4...
17:22:17.817  4afb291f  0217938705378174a18dd386...
18:01:33.188  e5873e8f  02179387289318858818742e...   ← g5
18:01:48.004  e5873e8f  021793872908004ea9999fb3...
18:02:02.526  e5873e8f  0217938729225264a18dd386...
18:02:16.835  e5873e8f  021793872936835a4e0bfe9b...
18:02:31.756  e5873e8f  021793872951756f0644a974...
18:03:44.802  e5873e8f  0217938730248024a18dd386...
18:03:49.309  e5873e8f  021793873029309841fc2298...
18:06:09.462  e5873e8f  021793873169462bc68ba765...
18:06:13.427  e5873e8f  021793873173427bc68ba765...
18:06:20.946  e5873e8f  021793873180946bbc1b3828...
总计: 16
```

加上 pipeline 侧 2 次（`logs/pipeline-g5.log` 18:03:35、18:07:37，都被 SDK 自动重试救回），今天全站 500 共 **18 次，全部落在 17:18–18:07 之间**。没有一次落在别的时间。

### 2.5 反证：抖动假设 vs 额度耗尽假设

| 检验 | 抖动假设预测 | 额度耗尽假设预测 | **实测** |
|---|---|---|---|
| 500 之后有没有过成功 | 有 | 没有 | **有，而且是交替的**。g5 在同一窗口内成功 8 次（18:01:32 / 01:47 / 02:02 / 02:16 / 02:31 / 02:44 / 04:04 / 05:09），失败 10 次 |
| 500 的分布 | 随机 | 集中在某个体量阈值之后 | **两簇**：17:18–17:22（g3，6 次）、18:01–18:06（g5，10 次）。同簇内成功失败交替，不像「跨过阈值后全挂」 |
| 同账号其它模型/endpoint | 一起抖 | 只有 coding plan 那条挂 | **同 endpoint 两条链路一起中招**：agent（model `deepseek-v4-flash`）与 pipeline sentiment（model `deepseek-v4-1-flash-260910`，同一个 `ARK_API_KEY`、同一个 `/api/coding/v3`）在 18:03 / 18:07 都吃到 500 |
| 恢复时间 | 不确定 | 对齐 5h/周/月重置边界 | **没有对齐任何边界**。最后一次 500 是 18:07:37，下一次有流量是 19:01，直接就成功了。5h 边界若按 08:00 首次请求算，应落在 13:00 / 18:00 / 23:00 —— 18:00 刚过就连续 500，与「重置后恢复」正好相反 |

**最硬的一条：18:06 之后到底有没有成功过。**

有，而且很多。18:06:22 之后再无一次 500，19:01–19:32 两个 job 共 **25 次调用全部成功**：

```
2026-11-05 19:01:08 | cron_41659d1d3eba | #1  | deepseek-v4-flash | volcengine-plan
2026-11-05 19:01:18 | cron_41659d1d3eba | #2  | ...
...
2026-11-05 19:13:59 | cron_41659d1d3eba | #19 | deepseek-v4-flash | volcengine-plan
2026-11-05 19:31:16 | cron_d230b68e0cd6 | #1  | deepseek-v4-flash | volcengine-plan
...
2026-11-05 19:32:13 | cron_d230b68e0cd6 | #6  | deepseek-v4-flash | volcengine-plan
```

（19:01 和 19:31 各出现一次失败，但看 provider 是 **zai / glm-5.3 的 429 RateLimitError**，不是 Ark 的 500——那是主模型配额，切 fallback 后就成功了。）

所以「额度耗尽 → 18:06 后再没成功过 → 18:18 pipeline 跑完只是因为不依赖 LLM」这个推断**不成立**：pipeline 18:18 跑完确实与 LLM 无关，但 18:06 之后 LLM 明明成功了 25 次。

### 2.6 我上次那次 200 探针，是不是同一路径

是。逐项核对：

| 项 | 失败请求 | 20:53 探针 |
|---|---|---|
| URL | `https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions` | 完全相同 |
| model | `deepseek-v4-flash` | 完全相同 |
| key | dump 里 `Authorization: Bearer cb254…`（被截断，共 15 字符） | `.env` 的 `VOLCENGINE_API_KEY`，前缀 `cb254`，36 字符 |

key 只能按前缀比对（dump 存的是截断值），前缀一致。**所以「端点已恢复」这个结论成立**——但上一份没写清「探针打的就是同一路径」，这是我的表述遗漏，本次补上。

---

## 3. 对上一份结论的修正

### 结论 1：要改的是「48 分钟劣化窗口」这个说法

上一份写「17:18–18:06 是一个约 48 分钟的上游劣化窗口」。**这个表述过度了。**

实际证据只支持「两个簇」：

- 17:18:30 – 17:22:17，g3 的 6 次失败，中间夹着 4 次成功
- 18:01:33 – 18:06:20，g5 的 10 次失败，中间夹着 8 次成功
- 17:22:24 – 18:00:55 之间**没有任何请求**（g3 已结束、g5 未开始）

所以「中间 38 分钟是不是也一直在挂」**无法证明**，当时没人在调。正确说法是：**两簇各约 4–5 分钟的间歇性 500，之间无流量、无法判定**。这不影响「单次抖动」的定性，但把「48 分钟持续劣化」改成「两簇间歇失败」更诚实。

### 结论 2：「单次服务端抖动」站得住，但要精确一点

站得住。理由不是「48 分钟后自己好了」，而是：

1. 官方把这个错误码定义成「内部系统异常，可重试」，本就不承诺稳定；
2. 同窗口内成功与失败交替，说明不是硬性拒绝（额度/封禁会是硬拒绝）；
3. 18:06 之后同一 endpoint 全部成功，恢复不需要等任何配额周期。

**但「抖动」不等于「与使用量无关」**：16 次 500 全部落在密集调用的分钟里。是不是「高并发下服务端过载返回 500」，日志层面分不出来。要定论得拿 request id 提工单问火山——这是唯一能拿到服务端内部原因的路。

### 结论 3：不需要改代码——不变

数据完整、pipeline 正常、恢复无需干预。这条不变。

### 需要更正的一句

上一份写「本机历史上几乎没出现过（10-11~11-04 仅 10-29 出现过 1 次）」——正确。但我在 2.5 里用它做过「首次成规模」的推断，这次补一个边界：**样本只有 agent.log 的保留期（约 1 个月），更早的轮转日志已覆盖不到。**

---

## 4. 额度现状与查询方法

### 4.1 官方额度模型（原文）

来源：<https://ark.volcengine.com/region:cn-beijing/docs/coding-plan-personal-plan-overview>

```
### 套餐额度刷新规则

套餐额度在时间周期内耗尽后，需要等待下一个周期自动恢复额度，不会消耗其他资源包或账户余额。
* 每5小时限额：根据首次请求发生时间，以 5 小时为周期定时刷新限额。
* 周限额：每周一 00:00:00 重置周限额。
* 月限额：每订阅月第 1 日 00:00:00 重置月限额。
```

同一页的使用限制（这一段是本轮最值得注意的发现）：

```
* 套餐额度仅在 AI 编程工具中生效，不可用于 API 调用。在非 AI 编程工具中使用方舟 Coding Plan
  权益对应的 Base URL 和 API Key 有可能被识别为滥用/违规，会导致订阅停用或账号封禁。
```

```
务必使用 Coding Plan 支持的模型及 Base URL：https://ark.cn-beijing.volces.com/api/coding/v3（兼容 OpenAI 协议）
* 如未使用指定的 Base URL，将无法使用 Coding Plan 额度，并可能产生额外 API 请求的费用。
```

套餐档位（Lite / Pro）与具体次数（1,200/5h、9,000/周、18,000/月 等）**没有出现在我抓到的这一页正文里**，它们要么在「开通管理」页动态渲染，要么在活动页。所以这几个数字我**不当作官方口径引用**；网上流传的「按 token 折算扣减（双层计费）」也属于社区说法，官方页面没有写，同样不作为结论。

### 4.2 本机实际用量（从日志估算，不是官方计量）

| 来源 | 今日请求数 | token |
|---|---:|---:|
| Hermes agent（成功） | 244 | in 21,094,627 + out 190,856 = **21.29M** |
| Hermes agent（重试级失败，含 429/500） | 27 | — |
| xueqiu pipeline（6 份日志） | 190（133 + 57） | 未统计 |
| **合计** | **≈ 460 请求/天** | **≥ 21M tokens/天**（不含 pipeline） |

口径：`grep "API call #" agent.log | grep 2026-11-05`，token 取自每行的 `in= out=`。这是**客户端自报**，与方舟套餐的扣减口径不一定一致。

值得注意：agent 单次请求平均 **86k input tokens**（244 次吃掉 21M），因为每次都要带 52k 字符的 lark-card skill。按社区流传的 token 折算口径，这类 Agent 负载确实是「额度杀手」——但这仍然解释不了 18:06 的 500。

### 4.3 实时额度：本机查不到（如实说明）

**查不到，原因是本机没有 AK/SK。** 方舟的额度查询接口走的是控制面 OpenAPI，需要 AccessKey/SecretKey + V4 签名；本机只有推理用的 `ARK_API_KEY`（`cb254…`），它只能调推理接口，不能查额度：

```
$ grep -E "ACCESS|SECRET" ~/.hermes/.env      # 无 VOLC_ACCESS_KEY / VOLC_SECRET_KEY
$ which ark arkcli                             # 未安装
```

要查，两条路：

**① 控制台（最快，手工）**

<https://ark.volcengine.com/region:cn-beijing/openManagement> → 「开通管理」页，Coding Plan 的 5 小时/周/月用量与重置倒计时都在那里。

**② OpenAPI（可脚本化）**

- 接口：`GetCodingPlanUsage`
- 服务：`ark`，版本 `2024-01-01`，域名 `open.volcengineapi.com`
- 认证：AK/SK + HMAC-SHA256 V4 签名（不是 `ARK_API_KEY`）
- 权限：给子用户挂 `ArkReadOnlyAccess` 只读策略即可
- 返回：`session` / `weekly` / `monthly` 三个窗口，各含 `used_percent`、`remaining_percent`、`total`、`reset_at`

官方 API Explorer 可在线试：<https://api.volcengine.com/api-explorer/?groupName=Coding%20Plan%20API&serviceCode=ark&version=2024-01-01>

拿到 AK/SK 后可以用现成实现（`volcengine/ark-cli` 的 `arkcli usage plan`，或 `ZnonEn/dsh-volcark-quota`）验证，不用自己写签名。

---

## 5. 运维动作建议

### 5.1 对本次故障：什么都不用做

已恢复（同 endpoint 20:53 返回 200，19:01–19:32 有 25 次成功）。**不要“等额度重置”，因为额度根本没耗尽**——等也等不到东西。

### 5.2 真正需要处理的两件事（都不是本次故障的原因）

**① zai/glm-5.3 的 7 天配额（这才是真正的额度错误）**

```
Error code: 429 - {'error': {'code': '1310', 'message': 'You have reached the 7-day usage limit.
You can continue using it after 2026-11-08 09:11:37.'}}
```

对照 2.3：**配额错误的形状是 429 + 专属 code + 明确 reset 时间**，和 500 完全不同。这条要到 **2026-11-08 09:11** 才恢复，期间每个整点 job 都会先吃一次 429 再切 fallback（今天共 10 次）。

处理选项：
- 找 zai 开 overage；或
- 把高频 job 的 `model`/`provider` 在 `~/.hermes/cron/jobs.json` 里显式改成 `deepseek-v4-flash`/`volcengine-plan`，省掉每次 429 与切换（本轮不改，只建议）。

**② coding plan 的合规风险（这条比 500 严重）**

官方文档白纸黑字：**「套餐额度仅在 AI 编程工具中生效，不可用于 API 调用」**，非编程工具使用可能「订阅停用或账号封禁」。

现状是：
- Hermes agent → 算 AI 编程工具，**站得住**；
- `xueqiu-monitor` 的批量 sentiment / 日报生成 → 纯 API 调用（服务端跑批），**不是编程工具**，这条踩在文档红线边缘。

建议把 pipeline 侧挪到按量付费的 `/api/v3`（官方文档：「如未使用指定 Base URL，将无法使用 Coding Plan 额度，并可能产生额外 API 请求的费用」，即按量计费）。代价是要付费，收益是不被封号。**是否迁移，需要主人拍板**，这轮只做提示，不动配置。

### 5.3 监控口径建议

现在的告警只看「job 失败」，两种错误混在一起。建议分开：

- 出现 `QuotaExceeded` / `RateLimitError` → **额度类告警**，动作是「等重置或扩容」；
- 出现 `InternalServiceError` → **服务端告警**，动作是「重试」，不该触发人工；
- 两者都不该走「补跑 pipeline」——那是数据层的事。

---

## 附：本轮证据与引用

| 证据 | 值/路径 |
|---|---|
| 失败响应体 | `code=InternalServiceError`, `type=Internal Server Error`, `param=""` |
| 失败请求 dump | `~/.hermes/sessions/request_dump_cron_e5873e8f8a9c_20261105_180040_20261105_180622_130772.json` |
| 响应头 | **未保存**（dump 只存请求头）→ 证据缺失 |
| 官方错误码表 | <https://ark.volcengine.com/region:cn-beijing/docs/error-codes.md>（推理错误码段，第 127/130/131 行附近） |
| 官方套餐文档 | <https://ark.volcengine.com/region:cn-beijing/docs/coding-plan-personal-plan-overview> |
| request id 解码规则 | `int(rid[2:15]) / 1000` → 北京时间；用 20:53 探针反向验证通过 |
| 18:06 之后的成功调用 | `~/.hermes/logs/agent.log`，19:01:08–19:32:13，25 次 |
| 共享同一 key 的两条链路 | xueqiu `.env` 的 `ARK_API_KEY` 与 hermes `.env` 的 `VOLCENGINE_API_KEY` 同为 `cb254…`/36 字符 |

### 查不到的项（如实列出）

1. **响应头**（`x-ratelimit-*`、`x-request-id`）——Hermes dump 不保存响应头。
2. **实时剩余额度**——本机无 AK/SK，无法调 `GetCodingPlanUsage`。
3. **套餐档位与具体次数**——官方页面正文未给出，不引用网传数字。
4. **服务端内部原因**——只有拿着 request id 提工单才能知道，日志层面到不了。
