#!/bin/sh
# post-commit hook for xueqiu-monitor
# Triggered after each git commit to auto-update PROJECT_LOG.md
#
# To skip log update, include [skip-log] in the commit message.
#
# This hook calls deepseek exec --auto to analyze the latest commit.
# If deepseek is not available, it silently skips.

COMMIT_MSG=$(git log -1 --pretty=%B)
case "$COMMIT_MSG" in
  *"[skip-log]"*) exit 0 ;;
esac

# Check if deepseek CLI is available
if command -v deepseek >/dev/null 2>&1; then
  # Run analysis in background, don't block git
  (sleep 1 && deepseek exec --auto "分析最新提交") &
fi
