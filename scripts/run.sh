#!/bin/bash
# xueqiu-monitor scheduler entry point
# Called by cron every 4 hours

set -euo pipefail

PROJECT_DIR="/root/code/xueqiu-monitor"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
LOG_FILE="$LOG_DIR/run_${TIMESTAMP}.log"

echo "=== xueqiu-monitor $(date '+%Y-%m-%d %H:%M:%S') ===" | tee -a "$LOG_FILE"

cd "$PROJECT_DIR"
/usr/bin/python3 -m src.cli -c etc/config.json >> "$LOG_FILE" 2>&1

EXIT_CODE=$?
echo "Exit code: $EXIT_CODE" | tee -a "$LOG_FILE"

# Clean logs older than 30 days
find "$LOG_DIR" -name "run_*.log" -mtime +30 -delete 2>/dev/null || true

exit $EXIT_CODE
