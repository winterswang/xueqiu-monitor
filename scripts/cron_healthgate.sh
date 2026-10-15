#!/bin/bash
# cron_healthgate.sh — 舆情日更健康 gate: 复用 health_check(全批次版)。
#
# 挂在两个调度上（Scheduler）:
#   19:05  组级完整性 gate          — 当日六个批次是否都收尾、日报是否产出
#   14:25–18:25 每小时  批间巡检     — 同一条链路，但此刻「上一组还没跑完」是正常态
# 静默条件: health_check 退出 0 **且** JSON status=ok。全 ok → cindy-script/1
# complete 帧；否则飞书卡片告警（发不出去回退纯文本 —— 告警通道不能哑）。
#
# 为什么加 status 判定而不只看退出码（2026-10-15）:
#   health_check 对 WARN 档（no_summary / high_failure_rate / detect_errors /
#   source_failures）退出 0，只看退出码会把这些真实异常静默掉 —— 头部注释却
#   一直声称会告警。
#
# 为什么不会因此误报「批次还在跑」:
#   health_check 把运行中的批次标成「(运行中)」并**不计** no_summary 错误
#   （scripts/health_check.py 的 pipeline_completed 分支），status 保持 ok；
#   进程活着但一小时没写日志才按「真没跑完」计错。所以 14:25–18:25 那 5 次
#   巡检不会把正常收尾中的批次当成失败。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LARK="${LARK_CLI:-$HOME/.npm-global/bin/lark-cli}"
CHAT="oc_2fe3cfa05bfe4860c71a1bd1efbe3751"
# notify_card.py 内部按名字调 lark-cli,调度环境 PATH 可能很窄,显式补上
export PATH="$HOME/.npm-global/bin:$PATH"

fail_alert() { # $1=告警正文(含 health_check 输出摘要)
  local stamp
  stamp="$(date '+%m-%d %H:%M')"
  if printf '%s\n' "$1" | python3 "$ROOT/scripts/notify_card.py" \
       --title "⛔ 舆情日更组级 gate 失败" --color red --chat "$CHAT" >/dev/null 2>&1; then
    return 0
  fi
  # 回退: 纯文本只带 400 字摘要(旧的 --text 形态)
  "$LARK" im +messages-send --chat-id "$CHAT" --as bot \
    --text "⛔ 舆情日更组级 gate 失败($stamp): $(printf '%s' "$1" | head -c 400)" >/dev/null 2>&1 || true
}

OUT=$(python3 "$ROOT/scripts/health_check.py" 2>&1)
rc=$?
# 输出前面可能混有 stderr 噪声, 交给 health_gate_status.py 扫整体报告对象;
# 解析不出来它给 fail(= 告警), 宁可多报一次也不漏报。
LEVEL=$(printf '%s' "$OUT" | python3 "$ROOT/scripts/health_gate_status.py")
if [ "$rc" -eq 0 ] && [ "$LEVEL" = ok ]; then
  printf '%s\n' '{"protocol":"cindy-script/1","type":"complete"}'
  exit 0
fi
# 卡片不怕长,给足 2000 字(旧 --text 只截 400 字,排障时经常不够)
fail_alert "health_check 未通过(rc=$rc, status=$LEVEL):

$(printf '%s' "$OUT" | head -c 2000)"
printf '%s\n' '{"protocol":"cindy-script/1","type":"complete","ok":false,"error":"health_check failed"}'
exit 1
