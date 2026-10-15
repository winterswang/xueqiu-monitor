#!/usr/bin/env python3
"""从 health_check.py 的 stdout 里取整体 status，供 cron gate 判定是否静默。

用法（cron_healthgate.sh 里）::

    LEVEL=$(printf '%s' "$OUT" | python3 "$ROOT/scripts/health_gate_status.py")

为什么需要它（2026-10-15）:
  gate 之前只看 health_check 的**退出码**。但 health_check 对 WARN 档
  (no_summary / high_failure_rate / detect_errors / source_failures) 退出 0，
  这些真实异常于是被静默 —— 头部注释却称会告警。

为什么不能只看 `"status"` 字段:
  health_check 的输出前面可能混有 stderr 噪声；更关键的是 `checks` 数组里
  每个元素**也带 status**，外层 JSON 被截断（进程中途被杀）时，简单匹配会
  取到某个 check 的 status（通常是 ok）而把真失败静默掉。所以只认同时含
  status/errors/checks 三个键的**整体报告**对象；找不到就输出 fail ——
  宁可多报一次，不能漏报。
"""
from __future__ import annotations

import json
import sys


def report_status(raw: str) -> str:
    """返回 'ok' / 'warn' / 'fail'（解析不出来一律 'fail'）。"""
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(raw):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(raw[idx:])
        except ValueError:
            continue
        if isinstance(obj, dict) and {"status", "errors", "checks"} <= set(obj):
            return str(obj["status"]).strip().lower() or "fail"
    return "fail"


def main() -> int:
    print(report_status(sys.stdin.read()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
