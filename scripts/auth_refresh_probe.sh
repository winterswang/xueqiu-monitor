#!/bin/bash
# 雪球登录态续期 + 功能探针 (2026-11-15 事故防再发)
#
# 背景: 雪球会话会被服务端静默作废, stock-notices(唯一需登录态的命令)随即全 400,
# auth status 只查 cookie 文件存在所以显示"已登录", 无法自察。
# 本脚本每天 12:30 由 Cindy scheduler (script 模式) 调用:
#   1. opencli auth refresh --all --site xueqiu   续会话(--all 绕开 24h 节流)
#   2. 用 stock-notices 真实探针验证服务端认不认
# 探针通过 = 会话有效; 失败 = complete 帧 ok:false, 调度器记 failed 并通知。
# 日志: ~/code/claude_code/xueqiu-monitor/logs/auth_refresh.log
#
# ⚠ cindy-script/1 协议: stdout 只能输出协议帧, 结尾必须发 complete 帧
#   (缺了它调度器判 "exited without a complete frame" → run 记 failed,
#   2026-11-16 实测; 与 morning-brief/scripts/sync_watchlist.py 同款修法)。
set -o pipefail
LOG="$HOME/code/claude_code/xueqiu-monitor/logs/auth_refresh.log"

complete_ok()   { printf '{"protocol":"cindy-script/1","type":"complete"}\n'; }
complete_fail() { printf '{"protocol":"cindy-script/1","type":"complete","ok":false,"error":"%s"}\n' "$1"; }

echo "===== $(date '+%F %T') auth refresh 开始 =====" >> "$LOG"

opencli auth refresh --all --site xueqiu >> "$LOG" 2>&1
RC=$?
if [ $RC -ne 0 ]; then
    echo "[FAIL] auth refresh 退出码 $RC (可能浏览器桥卡死, 参考 2026-11-15: 重启 Chrome 可修复)" >> "$LOG"
    complete_fail "auth_refresh_failed_rc_$RC"
    exit 1
fi

# 功能探针: 会话被服务端认可时输出 JSON 数组(可能为空); 作废时输出 ok:false
OUT=$(opencli xueqiu stock-notices SZ000002 --limit 1 -f json 2>&1)
if printf '%s' "$OUT" | head -c 40 | grep -q '^\['; then
    N=$(printf '%s' "$OUT" | grep -c '"title"' || true)
    echo "[OK] 探针通过 (stock-notices 返回 ${N} 条)" >> "$LOG"
    complete_ok
    exit 0
else
    echo "[FAIL] 探针失败, 服务端不认会话: $(printf '%s' "$OUT" | head -c 200)" >> "$LOG"
    complete_fail "probe_failed_session_rejected"
    exit 1
fi
