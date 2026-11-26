#!/bin/sh
# post-commit hook for xueqiu-monitor
#
# ── 2026-11-25 安全改造（独立高危项）──────────────────────────────
# 原实现：
#     (sleep 1 && deepseek exec --auto "分析最新提交") &
#
# 问题不是"它会不会捣乱"，而是**没有任何东西约束它**：`--auto` 是 agentic 模式、
# 带工具访问，进程 cwd 就是这个仓库，对它来说「git reset / git add / 写文件」
# 与「读日志」没有任何区别。之后如果 git 索引坏了，也分不清是谁弄的。
#
# 实证：两次 `invalid object ... for 'data/narrative/*.json'`（索引里 51 条指向
# 从未写出的 blob），都发生在**有 staged narrative 的提交之后**。HEAD 里的 blob
# 完好，`git reset -- data/narrative` 即可恢复——所以不是历史损坏，而是索引被
# 某个带写权限的进程动过。这个钩子是当时唯一已知的、在 commit 后自动执行的
# 写权限进程。
#
# 新实现：把 HEAD 的**只读快照**（git archive，只含已跟踪文件）解到 mktemp
# 目录，LLM 在副本里跑。它无论做什么都落在临时目录，脚本退出即销毁；真实仓库
# 一个字节都不会被碰。分析结果进 logs/post-commit-analysis.log。
#
# 代价：不再自动改 PROJECT_LOG.md。功能上无损失——实测这个钩子从未成功写过它
# （PROJECT_LOG.md 的历次变更都是人工提交），所以去掉的只是"一个没人管的写权限"。
#
# 跳过：commit message 含 [skip-log]，或设 SKIP_POST_COMMIT=1。

case "$(git log -1 --pretty=%B)" in
  *"[skip-log]"*) exit 0 ;;
esac
[ "${SKIP_POST_COMMIT:-0}" = "1" ] && exit 0
command -v deepseek >/dev/null 2>&1 || exit 0

REPO=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
LOG_DIR="$REPO/logs"
mkdir -p "$LOG_DIR" 2>/dev/null || exit 0

# 后台跑，不阻塞 git commit
(
  sleep 1
  SNAP=$(mktemp -d "${TMPDIR:-/tmp}/xq-postcommit.XXXXXX") || exit 0
  trap 'rm -rf "$SNAP"' EXIT INT TERM

  # 只导出 HEAD 的已跟踪文件快照（不含 .git、不含未提交内容）
  git -C "$REPO" archive HEAD 2>/dev/null | tar -x -C "$SNAP" 2>/dev/null || exit 0

  # 提交信息在副本里没有 .git，单独落一份供分析
  {
    git -C "$REPO" log -1 --pretty=fuller HEAD 2>/dev/null
    echo
    git -C "$REPO" show --stat HEAD 2>/dev/null
  } > "$SNAP/_COMMIT.txt" 2>/dev/null

  cd "$SNAP" || exit 0
  {
    echo "===== $(date '+%Y-%m-%d %H:%M:%S') post-commit 分析（只读快照）====="
    deepseek exec --auto "你在一个只读快照副本里工作。先读 _COMMIT.txt 了解这次提交，再分析快照中的代码变更，输出简短摘要。不要修改任何文件。" 2>&1
    echo
  } >> "$LOG_DIR/post-commit-analysis.log" 2>&1
) &
