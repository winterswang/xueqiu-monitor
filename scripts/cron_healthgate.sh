#!/bin/bash
# cron_healthgate.sh — 舆情日更健康 gate: 复用 health_check(全批次版)。
#
# 挂在两个调度上: 14:25–18:25 批间巡检 与 19:05 组级完整性 gate。
# 静默条件: health_check 进程退出 0 **且** JSON status=ok;否则飞书告警
# (卡片优先,发送失败回退纯文本)。
#
# 为什么按 status 判定而不只看退出码: health_check 对 WARN 档
# (no_summary / high_failure_rate / detect_errors / source_failures)退出 0,
# 但都写进了 errors、整体 status=warn。只看退出码会把这些静默掉。
#
# 为什么不传 --require-daily-report: 白天批间巡检时日报尚未生成;日报由
# 19:30 的 xueqiu-health-check(显式传该参数)与 20:30 的 eod 巡检负责。
set -u
LARK="${LARK_CLI:-$HOME/.npm-global/bin/lark-cli}"
CHAT="oc_2fe3cfa05bfe4860c71a1bd1efbe3751"
# 卡片发送器依赖本机 ~/.hermes/skills/lark/lark-card;缺失时自动回退纯文本。
CARD_SENDER="$HOME/.hermes/skills/lark/lark-card/scripts/lark-card-sender.js"
NODE="$(command -v node 2>/dev/null || true)"

# 取 health_check 输出的整体 status。输出前面可能混有 stderr 噪声,
# 故扫描第一个可解析的 JSON 对象;解析不出来按 fail 兜底(宁可告警)。
# 必须同时含 errors/checks 才算整体报告 —— checks 数组里每个元素也带
# status,若外层被截断(进程中途被杀),仅凭 "status" 会误取某个 check 的
# status(通常是 ok)而把真失败静默掉。
report_status() {
  python3 -c '
import json, sys
raw = sys.stdin.read()
dec = json.JSONDecoder()
for i, ch in enumerate(raw):
    if ch != "{":
        continue
    try:
        obj, _ = dec.raw_decode(raw[i:])
    except ValueError:
        continue
    if isinstance(obj, dict) and "status" in obj and "errors" in obj and "checks" in obj:
        print(str(obj["status"]))
        break
else:
    print("fail")
'
}

fail_alert() {
  local json="$1" card_file plain sent=1
  card_file=$(mktemp "${TMPDIR:-/tmp}/xueqiu-health-card.XXXXXX") || card_file=""
  if [ -n "$card_file" ]; then
    if printf '%s' "$json" | python3 "$ROOT/scripts/health_check_card.py" >"$card_file" \
      && [ -n "$NODE" ] && [ -f "$CARD_SENDER" ] \
      && "$NODE" "$CARD_SENDER" send --chat-id "$CHAT" --as bot --content "$(cat "$card_file")"; then
      sent=0
    fi
    # 显式清理: 不能用 EXIT trap —— card_file 是局部变量,函数返回后
    # trap 才执行,set -u 下会报 unbound variable 且临时文件泄漏。
    rm -f "$card_file"
  fi
  [ "$sent" -eq 0 ] && return 0

  plain=$(printf '%s' "$json" | python3 "$ROOT/scripts/health_check_card.py" --text 2>/dev/null || true)
  [ -n "$plain" ] || plain="健康检查异常，但卡片内容生成失败。请查看 xueqiu-monitor/scripts/health_check.py 输出。"
  "$LARK" im +messages-send --chat-id "$CHAT" --as bot \
    --text "$(printf '⛔ 舆情日更检查未通过（%s）\n%s' "$(date '+%Y-%m-%d %H:%M')" "$plain")" >/dev/null 2>&1 || true
}

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT=$(python3 "$ROOT/scripts/health_check.py" 2>&1)
rc=$?
LEVEL=$(printf '%s' "$OUT" | report_status)
if [ "$rc" -eq 0 ] && [ "$LEVEL" = ok ]; then
  printf '%s\n' '{"protocol":"cindy-script/1","type":"complete"}'
  exit 0
fi
fail_alert "$OUT"
printf '%s\n' '{"protocol":"cindy-script/1","type":"complete","ok":false,"error":"health_check failed"}'
exit 1
