"""xueqiu-monitor: notification module (Feishu webhook)

Phase 1: structured text cards (no LLM).
- P0 immediate push
- P1 digest push
- Daily report generation
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
import urllib.error
from typing import Any

from models import ChangeAlert, PushHistory

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════
# Feishu webhook
# ════════════════════════════════════════════════════════

def _send_webhook(webhook_url: str, payload: dict, timeout: int = 5) -> bool:
    """Send message to Feishu webhook. Returns True on success."""
    if not webhook_url:
        logger.warning("未配置 FEISHU_WEBHOOK_URL，跳过推送")
        return False
    try:
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception as e:
        logger.error(f"飞书推送失败: {e}")
        return False


def _format_alert_card(alert: ChangeAlert, stock_name: str = "") -> dict:
    """Build Feishu interactive card for a single alert."""
    priority_emoji = {"P0": "🔴", "P1": "🟡", "P2": "⚪"}
    type_labels = {
        "sentiment_shift": "情感偏移",
        "post_spike": "帖子激增",
        "hot_word_surge": "热词涌现",
        "new_announcement": "新公告",
    }
    emoji = priority_emoji.get(alert.priority, "⚪")
    type_label = type_labels.get(alert.alert_type, alert.alert_type)
    name_line = f" {stock_name}" if stock_name else ""

    title = f"{emoji} [{alert.priority}] {alert.stock_code}{name_line} — {type_label}"
    content = [
        f"Z-score: {alert.z_score:.2f}",
        f"幅度: {alert.magnitude:.2f}",
    ]
    if alert.filter_reason:
        content.append(f"过滤: {alert.filter_reason}")

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "red" if alert.priority == "P0" else "blue",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": "\n".join(content)},
                },
                {
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text", "content": f"触发时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(alert.alert_time))}"}
                    ],
                },
            ],
        },
    }


# ════════════════════════════════════════════════════════
# Push functions
# ════════════════════════════════════════════════════════

def push_immediate(
    alert: ChangeAlert,
    webhook_url: str,
    stock_name: str = "",
    timeout: int = 5,
) -> bool:
    """P0 immediate push — send single alert card."""
    payload = _format_alert_card(alert, stock_name)
    return _send_webhook(webhook_url, payload, timeout)


def push_digest(
    alerts: list[ChangeAlert],
    webhook_url: str,
    timeout: int = 5,
) -> bool:
    """P1 digest push — batch summary as text card."""
    if not alerts:
        return True
    lines = ["📊 **舆情异常汇总**\n"]
    for a in alerts:
        lines.append(f"• {a.stock_code} Z={a.z_score:.1f} | {a.alert_type}")
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "📊 雪球舆情日报"},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": "\n".join(lines)},
                },
            ],
        },
    }
    return _send_webhook(webhook_url, payload, timeout)


def generate_daily_report(
    alerts: list[ChangeAlert],
    posts_data_map: dict[str, list[dict]] | None = None,
) -> str:
    """Generate structured daily report text (Phase 1: no LLM).

    Format: stock_code | alert_type | Z-score | magnitude | top_post_title
    """
    if not alerts:
        return "📊 今日无舆情异常变化。"

    lines = ["# 雪球舆情日报\n"]
    lines.append(f"**生成时间**: {time.strftime('%Y-%m-%d %H:%M')}\n")
    lines.append("| 股票 | 类型 | Z-score | 幅度 |")
    lines.append("|------|------|---------|------|")

    type_labels = {
        "sentiment_shift": "情感偏移",
        "post_spike": "帖子激增",
        "hot_word_surge": "热词涌现",
        "new_announcement": "新公告",
    }

    for a in alerts:
        tl = type_labels.get(a.alert_type, a.alert_type)
        lines.append(f"| {a.stock_code} | {tl} | {a.z_score:.2f} | {a.magnitude:.2f} |")

    # Summary stats
    p0_count = sum(1 for a in alerts if a.priority == "P0")
    p1_count = sum(1 for a in alerts if a.priority == "P1")
    lines.append(f"\n**统计**: P0={p0_count} P1={p1_count} 总计={len(alerts)}")

    return "\n".join(lines)
