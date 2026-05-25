#!/bin/bash
# xueqiu-monitor daily run script
set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONPATH="src:$PYTHONPATH"

# Log directory
mkdir -p logs

echo "=== xueqiu-monitor $(date '+%Y-%m-%d %H:%M:%S') ===" | tee -a logs/monitor.log

# Run pipeline
python3 src/cli.py -c config/config.json "$@" 2>&1 | tee -a logs/monitor.log

echo "=== done $(date '+%Y-%m-%d %H:%M:%S') ===" | tee -a logs/monitor.log
