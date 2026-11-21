#!/bin/bash
# x_collect_daily.sh — X 舆情采集·每日0630 的纯脚本版（2026-11-20 改造，11-21 协议修正）。
#
# ⚠️ stdout 协议（Cindy script 模式硬约束，2026-10-14/11-21 两次实测）：
#   1. stdout 只允许 cindy-script/1 的 JSONL 帧，一切人读日志必须走 stderr
#      （违者整轮直接判失败：script stdout must contain JSONL protocol frames only）；
#   2. 退出前必须发一个 complete 帧（不发报 script exited without a complete frame）。
# 实现：脚本开头把原 stdout 存到 fd3 并将 stdout 重定向到 stderr——此后所有
# echo/子进程输出（含 x_collect.py 的人读输出）都走 stderr；帧用 >&3 写回真 stdout。
#
# 流程与退出码语义严格沿用任务书（主人 2026-11-10/11-19 定稿）：
#   代理自检（ClashX 7890）→ 登录态探针 → 采集 22 只 --ingest（重试≤1）→
#   契约校验 → 7 天清理；全绿静默（complete 帧无附加字段），任何降级
#   complete 帧 ok:false 带原因（宿主判失败会告警）+ lark-cli 飞书补一条。
#
# 退出码：0 全绿 | 10 代理失败 | 11 登录失效 | 12 采集失败 | 13 校验 FAIL | 30 目录缺失
set -u

MONITOR_DIR="/Users/wangguangchao/code/claude_code/xueqiu-monitor"
PYBIN="/Users/wangguangchao/.pyenv/versions/3.11.12/bin/python3"
LARK="$HOME/.npm-global/bin/lark-cli"
CHAT_ID="oc_5d38729181081d4ee50d2d2ad2b662f7"
TICKERS="PDD,NVDA,TSM,BABA,GOOGL,TSLA,UBER,SKHY,META,CRWV,CBRS,ASML,TCOM,APP,NVO,DIS,TEM,AMD,BRK.B,AAPL,AMZN,BEKE"

# ── stdout 协议层：原 stdout 存 fd3，人读输出全部转 stderr ──
exec 3>&1 1>&2

frame_ok()  { printf '{"protocol":"cindy-script/1","type":"complete"}\n' >&3; }
frame_err() {  # $1 = 错误描述（会用 python json 转义，安全承载中文/引号）
  "$PYBIN" -c 'import json,sys; print(json.dumps({"protocol":"cindy-script/1","type":"complete","ok":False,"error":sys.argv[1]},ensure_ascii=False))' "$1" >&3
}

notify_red() {  # 飞书补一条（失败不再静默——写 stderr 让运行记录可见）
  printf '%s' "$1" > /tmp/x_collect_alert_0630.txt
  if "$LARK" im +messages-send --chat-id "$CHAT_ID" --as bot \
      --text "$(cat /tmp/x_collect_alert_0630.txt)" >/dev/null 2>&1; then
    echo "[notify] 飞书告警已发"
  else
    echo "[notify] ⚠️ 飞书告警发送失败（lark-cli 不可用），告警文本：$(cat /tmp/x_collect_alert_0630.txt)"
  fi
}

cd "$MONITOR_DIR" || { frame_err "monitor 目录缺失：$MONITOR_DIR"; exit 30; }
D=$(date +%F)
echo "[x_collect_daily] $D 开始"

# ── 第 0 步：代理自检 ──
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
  -x http://127.0.0.1:7890 https://x.com/ || echo "000")
if [ "$code" != "200" ]; then
  echo "[x_collect_daily] 代理自检失败（x.com 经 7890 返回 $code）"
  frame_err "代理自检失败：x.com 经 ClashX(7890) 返回 $code，今日未采集，X 段降级纯雪球。请检查 ClashX。"
  notify_red "X采集0630：ClashX 代理不可用（x.com 返回 $code），今日未采集，X 段将降级纯雪球。请检查 ClashX。"
  exit 10
fi

# ── 第 0 步：登录态探针 ──
if ! "$PYBIN" scripts/x_collect.py --probe >/dev/null 2>&1; then
  echo "[x_collect_daily] 登录态探针失败"
  "$PYBIN" scripts/x_ingest.py --date "$D" --ticker META \
    --status login_required --note "opencli 登录态失效" >/dev/null 2>&1 || true
  frame_err "登录态失效：opencli 登录态探针未通过，今日未采集。需要主人执行 opencli twitter login"
  notify_red "X采集0630：opencli 登录态失效，今日未采集。需要主人执行：opencli twitter login"
  exit 11
fi

# ── 第 1 步：采集并落盘（网络类失败最多重试 1 次）──
rc=0
"$PYBIN" scripts/x_collect.py --tickers "$TICKERS" --date "$D" --max 20 --ingest || rc=$?
if [ "$rc" -ne 0 ] && [ "$rc" -ne 4 ]; then
  echo "[x_collect_daily] 首次采集 rc=$rc，按任务书允许重试 1 次"
  sleep 30
  "$PYBIN" scripts/x_collect.py --tickers "$TICKERS" --date "$D" --max 20 --ingest || rc=$?
fi
if [ "$rc" -eq 3 ]; then
  "$PYBIN" scripts/x_ingest.py --date "$D" --ticker META \
    --status login_required --note "采集轮发现登录态失效" >/dev/null 2>&1 || true
  frame_err "采集中发现登录态失效（x_collect 退出码 3）。需要主人执行 opencli twitter login"
  notify_red "X采集0630：采集中发现登录态失效。需要主人执行：opencli twitter login"
  exit 11
fi
if [ "$rc" -ne 0 ]; then
  if [ "$rc" -eq 4 ]; then
    frame_err "22 只全部 0 条（empty），未落盘。疑似风控/频控，按任务书不连续硬试。"
    notify_red "X采集0630：22 只全部 0 条（empty），未落盘。X 段降级纯雪球。疑似风控/频控，按任务书不连续硬试。"
  else
    frame_err "采集失败 rc=$rc，未落盘。X 段降级纯雪球。"
    notify_red "X采集0630：采集失败 rc=$rc，未落盘。X 段降级纯雪球。"
  fi
  exit 12
fi

# ── 第 2 步：契约校验 ──
if ! "$PYBIN" scripts/validate_x_posts.py --date "$D" --tickers "$TICKERS" >/dev/null 2>&1; then
  frame_err "契约校验 FAIL（errors 原文见本轮运行日志 stderr）。X 段降级纯雪球。"
  notify_red "X采集0630：契约校验 FAIL，详见 xueqiu-monitor 今日运行日志。X 段降级纯雪球。"
  exit 13
fi

# ── 第 3 步：保留期清理 ──
"$PYBIN" scripts/x_ingest.py --prune --keep-days 7 >/dev/null 2>&1 || true

echo "[x_collect_daily] $D 全绿（采集+校验+清理完成）"
frame_ok
exit 0
