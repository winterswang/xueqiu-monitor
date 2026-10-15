#!/usr/bin/env python3
"""cron gate 的判定辅助: 取整体 status / 去重指纹 / 按字符截断。

用法（cron_healthgate.sh 里）::

    LEVEL=$(python3 "$ROOT/scripts/health_gate_status.py" "$OUT")
    SIG=$(python3 "$ROOT/scripts/health_gate_status.py" --fingerprint "$OUT")
    BODY=$(python3 "$ROOT/scripts/health_gate_status.py" --truncate 2000 "$OUT")

输入走**参数**而不是 stdin: 本机 cron 环境有 pipe_to_interpreter 安全扫描
(把命令输出 pipe 进解释器会 pending_approval, 无人审批即卡死, 见
skills/data-science/xueqiu-monitor/references/cron-ops.md 的 cron 陷阱一节)。

为什么需要 status 判定（2026-10-15）:
  gate 原先只看 health_check 的退出码, 而 WARN 档 (no_summary /
  high_failure_rate / detect_errors / source_failures) 退出 0 —— 真实异常被
  静默, 头部注释却一直声称会告警。

为什么不能简单匹配 "status":
  health_check 的输出前面可能混有 stderr 噪声; 更关键的是 `checks` 数组里每个
  元素**也带 status**, 外层 JSON 被截断(进程中途被杀)时会取到某个 check 的
  status(通常是 ok)而把真失败静默掉。所以只认同时含 status/errors/checks 三个
  键的整体报告对象; 解析不出来一律 fail —— 宁可多报一次, 不能漏报。
"""
from __future__ import annotations

import json
import sys


def _find_report(raw: str) -> dict | None:
    """扫出第一个「整体报告」JSON 对象(必须含 status/errors/checks)."""
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(raw):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(raw[idx:])
        except ValueError:
            continue
        if isinstance(obj, dict) and {"status", "errors", "checks"} <= set(obj):
            return obj
    return None


def report_status(raw: str) -> str:
    """返回 'ok' / 'warn' / 'fail'（解析不出来一律 'fail'）。"""
    report = _find_report(raw)
    if report is None:
        return "fail"
    return str(report["status"]).strip().lower() or "fail"


def fingerprint(raw: str) -> str:
    """同一份失败在当天各次 gate 上的稳定指纹(用于去重, 只在告警路径用)。

    取错误码 + 非 ok 检查项(名/状态)的排序集合, **不含** detail —— detail 里
    带计数与时长(如「3 小时前」), 逐次都变; 若把它算进指纹, 同一个持续故障
    每小时都会发一张新卡片, 正是要避免的告警疲劳。解析失败给固定串: 那种
    情况本身就每次都该报。
    """
    report = _find_report(raw)
    if report is None:
        return "unparseable"
    errors = sorted(str(e) for e in (report.get("errors") or []))
    checks = sorted(
        f"{c.get('check')}={c.get('status')}"
        for c in (report.get("checks") or [])
        if isinstance(c, dict) and c.get("status") != "ok"
    )
    return "|".join(errors + checks) or "no-error-but-not-ok"


def truncate(text: str, limit: int) -> str:
    """按**字符**截断(不是字节) —— head -c 会切在多字节字符中间, 造成
    notify_card.py 侧 UnicodeDecodeError, 卡片路径失败后静默退回 400 字纯文本。"""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…(已截断)"


def main(argv: list[str]) -> int:
    args = argv[1:]
    mode = "status"
    limit = 2000
    if args and args[0] in ("--fingerprint", "--truncate"):
        mode = args[0]
        args = args[1:]
        if mode == "--truncate":
            limit = int(args[0])
            args = args[1:]
    if not args:
        print("fail")
        return 0
    # 调用方约定把整段输出作为**一个**参数传进来; 多参时按行拼回(便于手工试)。
    raw = args[0] if len(args) == 1 else "\n".join(args)
    if mode == "--fingerprint":
        print(fingerprint(raw))
    elif mode == "--truncate":
        print(truncate(raw, limit))
    else:
        print(report_status(raw))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
