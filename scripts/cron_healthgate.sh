#!/bin/bash
# cron_healthgate.sh — 19:05 组级完整性 gate(20261009): 复用 health_check(全批次版)。
# 任一批次当日无 [SUMMARY]/高失败率/数据陈旧 → health_check 退出 1 → 飞书告警。
# 全 ok → 静默(cindy-script/1 complete 帧)。
set -u
LARK="${LARK_CLI:-$HOME/.npm-global/bin/lark-cli}"
CHAT="oc_2fe3cfa05bfe4860c71a1bd1efbe3751"

fail_alert() {
  "$LARK" im +messages-send --chat-id "$CHAT" --as bot \
    --text "⛔ 舆情日更组级 gate 失败($(date '+%m-%d %H:%M')): $1" >/dev/null 2>&1 || true
}

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT=$(python3 "$ROOT/scripts/health_check.py" 2>&1)
rc=$?
if [ $rc -eq 0 ]; then
  printf '%s\n' '{"protocol":"cindy-script/1","type":"complete"}'
  exit 0
fi
fail_alert "health_check 异常(rc=$rc): $(echo "$OUT" | head -c 400)"
printf '%s\n' '{"protocol":"cindy-script/1","type":"complete","ok":false,"error":"health_check failed"}'
exit 1
