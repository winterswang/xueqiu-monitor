"""xueqiu-monitor: notification module (Feishu IM bot via pending JSON)

Phase 1: structured text messages (no LLM).
- P0 immediate alert formatting
- P1 digest formatting
- Pending message file writer (consumed by external scheduler)
- Daily report generation
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from models import ChangeAlert, PushHistory

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════
# Alert card (keep for future card-format needs)
# ════════════════════════════════════════════════════════

def _format_alert_card(alert: ChangeAlert, key_data: dict) -> dict:
    """Build Feishu interactive card for a single alert with key data fields."""
    priority_emoji = {"P0": "🔴", "P1": "🟡", "P2": "⚪"}
    type_labels = {
        "sentiment_shift": "情感偏移",
        "post_spike": "帖子激增",
        "hot_word_surge": "热词涌现",
        "new_announcement": "新公告",
    }
    emoji = priority_emoji.get(alert.priority, "⚪")
    type_label = type_labels.get(alert.alert_type, alert.alert_type)
    stock_name = key_data.get("stock_name", "")
    name_line = f" {stock_name}" if stock_name else ""

    title = f"{emoji} [{alert.priority}] {alert.stock_code}{name_line} — {type_label}"
    content = [
        f"Z-score: {alert.z_score:.2f}",
        f"情感均值: {key_data.get('sentiment_avg', 0):.2f}",
        f"情感偏移: {key_data.get('sentiment_shift', 0):.2f}",
        f"幅度: {alert.magnitude:.2f}",
        f"新帖数: {key_data.get('posts_count', 0)} (变化: {key_data.get('posts_count_delta', 0):+d})",
    ]
    hot_words = key_data.get("hot_words", [])
    if hot_words:
        content.append(f"热词: {', '.join(hot_words[:3])}")
    post_titles = key_data.get("post_titles", [])
    if post_titles:
        content.append(f"高互动帖: {post_titles[0][:50]}")
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
# Message formatters (plain markdown text for IM bot)
# ════════════════════════════════════════════════════════

def format_immediate_alert_message(alert: ChangeAlert, key_data: dict) -> str:
    """Format a single P0 alert as a markdown text message.

    Args:
        alert: ChangeAlert to format.
        key_data: dict with stock_name, sentiment_avg, sentiment_shift,
                  posts_count, posts_count_delta, hot_words, post_titles,
                  magnitude, priority, trigger_time.

    Returns:
        Formatted markdown string for Feishu IM bot.
    """
    priority_emoji = {"P0": "🔴", "P1": "🟡", "P2": "⚪"}
    type_labels = {
        "sentiment_shift": "情感偏移",
        "post_spike": "帖子激增",
        "hot_word_surge": "热词涌现",
        "new_announcement": "新公告",
    }
    emoji = priority_emoji.get(alert.priority, "⚪")
    type_label = type_labels.get(alert.alert_type, alert.alert_type)
    stock_name = key_data.get("stock_name", "")
    name_line = f" {stock_name}" if stock_name else ""

    lines = [
        f"{emoji} **[{alert.priority}] {alert.stock_code}{name_line} — {type_label}**",
        "",
        f"Z-score: {alert.z_score:.2f}",
        f"情感均值: {key_data.get('sentiment_avg', 0):.2f}",
        f"情感偏移: {key_data.get('sentiment_shift', 0):.2f}",
        f"幅度: {alert.magnitude:.2f}",
        f"新帖数: {key_data.get('posts_count', 0)} (变化: {key_data.get('posts_count_delta', 0):+d})",
    ]

    hot_words = key_data.get("hot_words", [])
    if hot_words:
        lines.append(f"热词: {', '.join(hot_words[:3])}")

    post_titles = key_data.get("post_titles", [])
    if post_titles:
        lines.append(f"高互动帖: {post_titles[0][:50]}")

    if alert.filter_reason:
        lines.append(f"过滤: {alert.filter_reason}")

    trigger_time = key_data.get("trigger_time", "")
    if trigger_time:
        lines.append(f"\n触发时间: {trigger_time}")

    return "\n".join(lines)


def format_digest_message(alerts: list[ChangeAlert]) -> str:
    """Format a batch of P1 alerts as a digest text message.

    Args:
        alerts: List of ChangeAlert objects (typically P1).

    Returns:
        Formatted markdown string for Feishu IM bot.
    """
    if not alerts:
        return ""

    lines = ["📊 **舆情异常汇总**", ""]
    for a in alerts:
        lines.append(f"• {a.stock_code} Z={a.z_score:.1f} | {a.alert_type}")
    lines.append(f"\n共 {len(alerts)} 条异常")

    return "\n".join(lines)


# ════════════════════════════════════════════════════════
# Pending message writer
# ════════════════════════════════════════════════════════

def write_pending_messages(messages: list[str], output_path: str) -> str:
    """Write formatted messages to a JSON file for external scheduler pickup.

    The external scheduler (e.g. OpenClaw cron agent) reads this file and
    sends messages via Feishu IM bot.

    Args:
        messages: List of formatted markdown message strings.
        output_path: Path to write the JSON file.

    Returns:
        The output_path that was written to.
    """
    payload = {
        "messages": messages,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logger.info(f"待发送消息已写入: {output_path} ({len(messages)} 条)")
    return output_path


# ════════════════════════════════════════════════════════
# Daily report
# ════════════════════════════════════════════════════════

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
