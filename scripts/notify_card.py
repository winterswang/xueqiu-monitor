#!/usr/bin/env python3
"""立刻发一张飞书卡片 — 给 shell wrapper 用的入口(cron_healthgate.sh 等)。

用法:
    python3 scripts/notify_card.py --title "⛔ 舆情日更组级 gate 失败" \
        --color red --chat oc_xxx < 正文(markdown,从 stdin 读)

退出码: 0 = 已送达; 非 0 = 没发出去(调用方据此回退纯文本告警)。

背景: cron wrapper 里的 `lark-cli --text` 一行告警会把详情截断(实测只留 400 字);
卡片走 `--msg-type interactive`,容量大得多。卡片信封与 src/notifier.py 一致。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.notifier import send_card_now  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="发一张飞书交互卡片")
    ap.add_argument("--title", required=True, help="卡片标题")
    ap.add_argument("--chat", required=True, help="飞书群 chat_id (oc_xxx)")
    ap.add_argument("--color", default="red", help="卡片主题色,默认 red")
    ap.add_argument("--timeout", type=int, default=30, help="发送超时秒数")
    args = ap.parse_args()

    message = sys.stdin.read().strip()
    if not message:
        print("正文为空,不发送", file=sys.stderr)
        return 2

    ok = send_card_now(args.title, message, color=args.color,
                       chat_id=args.chat, push_timeout=args.timeout)
    if not ok:
        print("卡片发送失败", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
