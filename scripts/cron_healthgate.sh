#!/bin/bash
# cron_healthgate.sh — 舆情日更健康 gate: 复用 health_check(全批次版)。
#
# 挂在两个调度上（Scheduler，都是 `bash scripts/cron_healthgate.sh` 无参）:
#   19:05        组级完整性 gate — 当日六个批次是否都收尾
#   14:25–18:25  每小时批间巡检  — 同一条链路, 但此刻「上一组还在跑」是正常态
# 静默条件: health_check 退出 0 **且** JSON status=ok。全 ok → cindy-script/1
# complete 帧；否则飞书卡片告警（发不出去回退纯文本 —— 告警通道不能哑）。
#
# 为什么加 status 判定而不只看退出码（2026-10-15）:
#   health_check 对 WARN 档（no_summary / high_failure_rate / detect_errors /
#   source_failures）退出 0，只看退出码会把这些真实异常静默掉 —— 头部注释却
#   一直声称会告警。
#
# 为什么不会因此误报「批次还在跑」:
#   health_check 只把「进程活着 **且** 这股日志 15 分钟内还在写」的批次标成
#   「(运行中)」并**不计** no_summary 错误（pipeline_completed 分支），status
#   保持 ok; 崩溃/停滞的组（日志不再增长）照旧计错告警。
#
# 日报产物不在这里查: 当日日报由 19:00 的 crawler 任务生成（实测落盘 19:03–19:08），
# 19:05 这个 gate 跑在前面必然随机失败; 日报核验由 19:30 的收尾巡检负责, 那条
# 命令显式带 --require-daily-report（见 cron-ops.md 的 health_check 每日收尾）。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LARK="${LARK_CLI:-$HOME/.npm-global/bin/lark-cli}"
CHAT="oc_2fe3cfa05bfe4860c71a1bd1efbe3751"
STATE="$ROOT/logs/.healthgate_last_alert"
# notify_card.py 内部按名字调 lark-cli,调度环境 PATH 可能很窄,显式补上
export PATH="$HOME/.npm-global/bin:$PATH"

fail_alert() { # $1=告警正文(含 health_check 输出摘要)
  local stamp short
  stamp="$(date '+%m-%d %H:%M')"
  if printf '%s\n' "$1" | python3 "$ROOT/scripts/notify_card.py" \
       --title "⛔ 舆情日更组级 gate 失败" --color red --chat "$CHAT" >/dev/null 2>&1; then
    return 0
  fi
  # 回退: 纯文本只带 400 字摘要(旧的 --text 形态), 同样按字符截断
  short=$(python3 "$ROOT/scripts/health_gate_status.py" --truncate 400 "$1")
  "$LARK" im +messages-send --chat-id "$CHAT" --as bot \
    --text "⛔ 舆情日更组级 gate 失败($stamp): $short" >/dev/null 2>&1 || true
}

OUT=$(python3 "$ROOT/scripts/health_check.py" 2>&1)
rc=$?
# 输入走参数而不是管道: cron 环境把输出 pipe 进解释器会被安全扫描拦成
# pending_approval(无人审批即卡死)。解析不出来一律 fail(= 告警), 宁可多报。
LEVEL=$(python3 "$ROOT/scripts/health_gate_status.py" "$OUT")
if [ "$rc" -eq 0 ] && [ "$LEVEL" = ok ]; then
  printf '%s\n' '{"protocol":"cindy-script/1","type":"complete"}'
  exit 0
fi

# 当天同一份失败只发一次卡片。high_failure_rate / detect_errors 是**当日累计**量,
# 一次瞬时抖动会让 14:25 / 15:25 … 19:05 每次 gate 各发一张内容几乎相同的红卡
# (最多 6 张)。告警疲劳最终会把真正的组级失败一起淹掉 —— 20:30 的收尾巡检
# 仍会照常报告当天状态, 信息不丢。
mkdir -p "$(dirname "$STATE")" 2>/dev/null || true
SIG="$(date +%F):$(python3 "$ROOT/scripts/health_gate_status.py" --fingerprint "$OUT")"
if [ -f "$STATE" ] && [ "$(cat "$STATE" 2>/dev/null)" = "$SIG" ]; then
  printf '%s\n' '{"protocol":"cindy-script/1","type":"complete","ok":false,"error":"health_check failed (already alerted today)"}'
  exit 1
fi

# 卡片不怕长,给足 2000 **字符**(按字节切会切坏多字节字符, 见 health_gate_status.py)
BODY=$(python3 "$ROOT/scripts/health_gate_status.py" --truncate 2000 "$OUT")
fail_alert "health_check 未通过(rc=$rc, status=$LEVEL):

$BODY"
printf '%s' "$SIG" > "$STATE" 2>/dev/null || true
printf '%s\n' '{"protocol":"cindy-script/1","type":"complete","ok":false,"error":"health_check failed"}'
exit 1
