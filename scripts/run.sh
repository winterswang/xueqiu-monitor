#!/bin/bash
# xueqiu-monitor scheduler entry point
# Called by cron every day at 2am
#
# v3: cross-platform (macOS + Linux), single source of truth for PROJECT_DIR

set -euo pipefail

# ── PROJECT_DIR: 动态计算，禁止硬编码绝对路径 ──
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOCK_FILE="$PROJECT_DIR/.monitor_running.lock"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
LOG_FILE="$LOG_DIR/run_${TIMESTAMP}.log"

# ── Lockfile 防并发 ──
if [ -f "$LOCK_FILE" ]; then
    # 跨平台 mtime：Linux stat -c %Y / macOS stat -f %m
    if stat -c %Y "$LOCK_FILE" >/dev/null 2>&1; then
        LOCK_MTIME=$(stat -c %Y "$LOCK_FILE")
    else
        LOCK_MTIME=$(stat -f %m "$LOCK_FILE")
    fi
    LOCK_AGE=$(($(date +%s) - LOCK_MTIME))
    if [ "$LOCK_AGE" -lt 3600 ]; then
        echo "[$(date)] 前一次执行尚未结束 (lock age=${LOCK_AGE}s)，跳过" | tee -a "$LOG_FILE"
        exit 0
    fi
    echo "[$(date)] 清理过期锁 (age=${LOCK_AGE}s)" | tee -a "$LOG_FILE"
    rm -f "$LOCK_FILE"
fi
touch "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

# ── 内存检查：平台检测 ──
AVAIL_MB=""
if command -v free >/dev/null 2>&1; then
    # Linux
    AVAIL_MB=$(free -m | awk '/^Mem:/{print $7}')
elif command -v vm_stat >/dev/null 2>&1; then
    # macOS: 计算 free + inactive pages（MB）
    PAGES_FREE=$(vm_stat | awk '/Pages free/{print $3}' | tr -d '.')
    PAGES_INACTIVE=$(vm_stat | awk '/Pages inactive/{print $3}' | tr -d '.')
    PAGE_SIZE=$(pagesize)
    AVAIL_MB=$(( (PAGES_FREE + PAGES_INACTIVE) * PAGE_SIZE / 1024 / 1024 ))
fi

if [ -n "$AVAIL_MB" ] && [ "$AVAIL_MB" -lt 500 ]; then
    echo "[$(date)] 可用内存不足 (${AVAIL_MB}MB)，清理缓存" | tee -a "$LOG_FILE"
    sync 2>/dev/null || true
    if [ -w /proc/sys/vm/drop_caches ]; then
        echo 3 > /proc/sys/vm/drop_caches 2>/dev/null || true
    fi
fi

echo "[$(date '+%Y-%m-%d %A %H:%M:%S %Z')] 开始执行 xueqiu-monitor v3" | tee -a "$LOG_FILE"
echo "PROJECT_DIR: $PROJECT_DIR" | tee -a "$LOG_FILE"
[ -n "$AVAIL_MB" ] && echo "可用内存: ${AVAIL_MB}MB" | tee -a "$LOG_FILE"

cd "$PROJECT_DIR"

# Python 解释器：默认用 PATH 中的 python3，可通过环境变量覆盖
PYTHON_BIN="${PYTHON_BIN:-python3}"

# 同步自选股（依赖 longbridge CLI 共享 OAuth token）
if "$PYTHON_BIN" scripts/sync_watchlist.py 2>&1 | tee -a "$LOG_FILE"; then
    :
else
    echo "[$(date)] sync_watchlist.py 失败 (exit=$?)，继续执行主流程" | tee -a "$LOG_FILE"
fi

# 主流程
"$PYTHON_BIN" -m src.cli -c etc/config.json >> "$LOG_FILE" 2>&1

EXIT_CODE=$?
echo "Exit code: $EXIT_CODE" | tee -a "$LOG_FILE"

# Clean logs older than 30 days
find "$LOG_DIR" -name "run_*.log" -mtime +30 -delete 2>/dev/null || true

exit $EXIT_CODE
