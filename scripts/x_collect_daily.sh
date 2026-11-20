#!/bin/bash
# x_collect_daily.sh — X 舆情采集·每日0630 的纯脚本版（2026-11-20 改造）。
#
# 背景：本任务原为 AI 会话任务（deepseek-flash 执行任务书）。11-19/11-20 连续两天
# 同款死法：agent 把 8-12 分钟的采集命令「放后台」后结束回合，调度器判定完成
# （23.5s），后台进程随会话回收被杀，采集未落盘。改造为宿主直接执行的脚本，
# 无 LLM 参与，消灭该失败模式。18:30 只读健康检查保留为第二层网。
#
# 流程与退出码语义严格沿用原任务书（主人 2026-11-10/11-19 定稿）：
#   第 0 步 代理自检（curl 经 127.0.0.1:7890，非 200 告警结束）
#   第 0 步 登录态探针（x_collect --probe；失效记 login_required 并告警）
#   第 1 步 采集 22 只 --ingest（4-9s 内部频控，约 8-12 分钟；网络类失败最多重试 1 次）
#   第 2 步 契约校验 validate_x_posts（FAIL 原样告警，不改数据硬凑）
#   第 3 步 保留期清理 --prune --keep-days 7
#   收尾   全绿静默；任何降级 lark-cli 发飞书（chat_id 见下）
#
# 退出码：0 全绿 | 10 代理失败 | 11 登录失效 | 12 采集失败 | 13 校验 FAIL | 30 目录缺失
set -u

MONITOR_DIR="/Users/wangguangchao/code/claude_code/xueqiu-monitor"
PYBIN="/Users/wangguangchao/.pyenv/versions/3.11.12/bin/python3"
LARK="$HOME/.npm-global/bin/lark-cli"
CHAT_ID="oc_5d38729181081d4ee50d2d2ad2b662f7"
TICKERS="PDD,NVDA,TSM,BABA,GOOGL,TSLA,UBER,SKHY,META,CRWV,CBRS,ASML,TCOM,APP,NVO,DIS,TEM,AMD,BRK.B,AAPL,AMZN,BEKE"

cd "$MONITOR_DIR" || { echo "monitor dir missing"; exit 30; }
D=$(date +%F)
echo "[x_collect_daily] $D 开始"

notify_red() {
  printf '%s' "$1" > /tmp/x_collect_alert_0630.txt
  "$LARK" im +messages-send --chat-id "$CHAT_ID" --as bot \
    --text "$(cat /tmp/x_collect_alert_0630.txt)" >/dev/null 2>&1 || true
}

# ── 第 0 步：代理自检 ─────────────────────────────────────────────
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
  -x http://127.0.0.1:7890 https://x.com/ || echo "000")
if [ "$code" != "200" ]; then
  echo "[x_collect_daily] 代理自检失败（x.com 经 7890 返回 $code）"
  notify_red "X采集0630：ClashX 代理不可用（x.com 返回 $code），今日未采集，X 段将降级纯雪球。请检查 ClashX。"
  exit 10
fi

# ── 第 0 步：登录态探针 ───────────────────────────────────────────
if ! "$PYBIN" scripts/x_collect.py --probe >/dev/null 2>&1; then
  echo "[x_collect_daily] 登录态探针失败"
  "$PYBIN" scripts/x_ingest.py --date "$D" --ticker META \
    --status login_required --note "opencli 登录态失效" >/dev/null 2>&1 || true
  notify_red "X采集0630：opencli 登录态失效，今日未采集。需要主人执行：opencli twitter login"
  exit 11
fi

# ── 第 1 步：采集并落盘（网络类失败最多重试 1 次）─────────────────
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
  notify_red "X采集0630：采集中发现登录态失效。需要主人执行：opencli twitter login"
  exit 11
fi
if [ "$rc" -ne 0 ]; then
  if [ "$rc" -eq 4 ]; then
    notify_red "X采集0630：22 只全部 0 条（empty），未落盘。X 段降级纯雪球。疑似风控/频控，按任务书不连续硬试。"
  else
    notify_red "X采集0630：采集失败 rc=$rc，未落盘。X 段降级纯雪球。"
  fi
  exit 12
fi

# ── 第 2 步：契约校验 ─────────────────────────────────────────────
if ! "$PYBIN" scripts/validate_x_posts.py --date "$D" --tickers "$TICKERS"; then
  notify_red "X采集0630：契约校验 FAIL（errors 原文见本轮运行日志）。X 段降级纯雪球。"
  exit 13
fi

# ── 第 3 步：保留期清理 ───────────────────────────────────────────
"$PYBIN" scripts/x_ingest.py --prune --keep-days 7 >/dev/null 2>&1 || true

echo "[x_collect_daily] $D 全绿（采集+校验+清理完成）"
# 全绿：静默（不发飞书、不发桌面通知）
exit 0
