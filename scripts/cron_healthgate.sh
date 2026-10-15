#!/bin/bash
# cron_healthgate.sh — 19:05 组级完整性 gate(20261009): 复用 health_check(全批次版)。
# 任一批次当日无 [SUMMARY]/高失败率/数据陈旧 → health_check 退出 1 → 飞书卡片告警。
# 全 ok → 静默(cindy-script/1 complete 帧)。
# 卡片发不出去时回退纯文本告警——告警通道不能哑。
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
if [ $rc -eq 0 ]; then
  printf '%s\n' '{"protocol":"cindy-script/1","type":"complete"}'
  exit 0
fi
# 卡片不怕长,给足 2000 字(旧 --text 只截 400 字,排障时经常不够)
fail_alert "health_check 异常(rc=$rc):

$(printf '%s' "$OUT" | head -c 2000)"
printf '%s\n' '{"protocol":"cindy-script/1","type":"complete","ok":false,"error":"health_check failed"}'
exit 1
