#!/bin/bash
# xueqiu-monitor scheduler entry point
# Called by cron every day at 2am
#
# v2: lockfile 防并发 + 内存检查

set -euo pipefail

PROJECT_DIR="/root/code/xueqiu-monitor"
LOCK_FILE="$PROJECT_DIR/.monitor_running.lock"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
LOG_FILE="$LOG_DIR/run_${TIMESTAMP}.log"

# ── Lockfile 防并发 ──
if [ -f "$LOCK_FILE" ]; then
    LOCK_AGE=$(($(date +%s) - $(stat -c %Y "$LOCK_FILE" 2>/dev/null || echo 0)))
    if [ "$LOCK_AGE" -lt 3600 ]; then
        echo "[$(date)] 前一次执行尚未结束 (lock age=${LOCK_AGE}s)，跳过" | tee -a "$LOG_FILE"
        exit 0
    fi
    echo "[$(date)] 清理过期锁 (age=${LOCK_AGE}s)" | tee -a "$LOG_FILE"
    rm -f "$LOCK_FILE"
fi
touch "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

# ── 内存检查：可用 < 500MB 时清缓存 ──
AVAIL_MB=$(free -m | awk '/^Mem:/{print $7}')
if [ "$AVAIL_MB" -lt 500 ]; then
    echo "[$(date)] 可用内存不足 (${AVAIL_MB}MB)，清理缓存" | tee -a "$LOG_FILE"
    sync && echo 3 > /proc/sys/vm/drop_caches 2>/dev/null || true
fi

echo "[$(date '+%Y年 %m月 %d日 %A %H:%M:%S CST')] 开始执行 xueqiu-monitor v2" | tee -a "$LOG_FILE"
echo "可用内存: ${AVAIL_MB}MB" | tee -a "$LOG_FILE"

cd "$PROJECT_DIR"

# Python 解释器：默认用 PATH 中的 python3，可通过环境变量覆盖
PYTHON_BIN="${PYTHON_BIN:-python3}"

$PYTHON_BIN -m src.cli -c etc/config.json >> "$LOG_FILE" 2>&1

EXIT_CODE=$?
echo "Exit code: $EXIT_CODE" | tee -a "$LOG_FILE"

# Clean logs older than 30 days
find "$LOG_DIR" -name "run_*.log" -mtime +30 -delete 2>/dev/null || true

exit $EXIT_CODE
