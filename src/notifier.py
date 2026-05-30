"""xueqiu-monitor: notification module

Two delivery modes (auto-detected):
  1. lark CLI — if `lark-cli` is installed and configured, sends directly
  2. File — writes JSON to a pending file (consumed by external scheduler)

Mode is selected by config and availability:
  - notification.mode = "lark_cli": force lark CLI (falls back to file if unavailable)
  - notification.mode = "file":     force file mode
  - notification.mode = "auto" (default): auto-detect, lark CLI preferred

Formatting:
  - P0: immediate alert (one message per alert)
  - P1: digest (batch summary)
  - Daily report: generated independently
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from typing import Any

from .models import ChangeAlert, PushHistory

logger = logging.getLogger(__name__)

# ── Shared constants ──────────────────────────────────────────
_PRIORITY_EMOJI = {"P0": "🔴", "P1": "🟡", "P2": "⚪"}
_TYPE_LABELS = {
    "sentiment_shift": "情感偏移",
    "post_spike": "帖子激增",
    "hot_word_surge": "热词涌现",
    "new_announcement": "新公告",
}

# ── Entry points exposed to cli.py ────────────────────────────
__all__ = [
    "format_immediate_alert_message", "format_digest_message",
    "dispatch_messages", "generate_daily_report",
]

# ════════════════════════════════════════════════════════
# Alert card (keep for future card-format needs)
# ════════════════════════════════════════════════════════

def _format_alert_card(alert: ChangeAlert, key_data: dict) -> dict:
    """Build Feishu interactive card for a single alert with key data fields."""
    priority_emoji = _PRIORITY_EMOJI
    type_labels = _TYPE_LABELS
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
    priority_emoji = _PRIORITY_EMOJI
    type_labels = _TYPE_LABELS
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
# Dispatch (dual-mode)
# ════════════════════════════════════════════════════════

def _lark_cli_available() -> tuple[bool, str]:
    """Check if lark-cli is installed and authenticated.

    Returns (available, reason).
    """
    if not shutil.which("lark-cli"):
        return False, "lark-cli not found in PATH"
    try:
        result = subprocess.run(
            ["lark-cli", "auth", "status"],
            capture_output=True, text=True, timeout=10,
        )
        output = result.stdout or result.stderr or ""
        status = json.loads(output)
        # Check if any identity (bot or user) is available
        identities = status.get("identities", {})
        for ident_name, ident_info in identities.items():
            if ident_info.get("available"):
                return True, f"{ident_name} identity: ready"
        # No identity available
        if status.get("error"):
            err = status["error"].get("message", "not configured")
            return False, f"lark-cli not configured: {err}"
        return False, "no available identity"
    except json.JSONDecodeError:
        return False, "lark-cli auth status returned non-JSON"
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)


def _send_via_lark_cli(text: str, chat_id: str) -> bool:
    """Send a single markdown message via lark-cli.

    Returns True on success.
    """
    try:
        result = subprocess.run(
            ["lark-cli", "im", "+messages-send",
             "--chat-id", chat_id,
             "--markdown", text,
             "--as", "bot"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            logger.info(f"lark-cli 消息发送成功: chat_id={chat_id[:12]}...")
            return True
        else:
            logger.warning(f"lark-cli 发送失败: {result.stderr[:200]}")
            return False
    except subprocess.TimeoutExpired:
        logger.warning("lark-cli 发送超时")
        return False
    except OSError as e:
        logger.warning(f"lark-cli 调用失败: {e}")
        return False


def dispatch_messages(
    messages: list[str],
    pending_path: str,
    mode: str = "auto",
    lark_chat_id: str | None = None,
) -> int:
    """Dispatch messages via the best available channel.

    Args:
        messages: List of formatted markdown strings.
        pending_path: Fallback file path when lark-cli is unavailable.
        mode: "auto" | "lark_cli" | "file"
        lark_chat_id: Required for lark_cli mode.

    Returns:
        Number of messages dispatched.
    """
    if not messages:
        return 0

    # Decide channel
    use_lark = False
    if mode == "lark_cli":
        use_lark = True
    elif mode == "auto":
        avail, reason = _lark_cli_available()
        use_lark = avail
        if not avail:
            logger.info(f"lark-cli 不可用 ({reason}) → 回退文件模式")

    if use_lark:
        if not lark_chat_id:
            logger.warning("lark_cli 模式需要 lark_chat_id，回退文件模式")
        else:
            sent = 0
            for msg in messages:
                if _send_via_lark_cli(msg, lark_chat_id):
                    sent += 1
            return sent

    # Fallback: write to file
    write_pending_messages(messages, pending_path)
    return len(messages)

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
        # 无告警时也输出爬取摘要
        lines = ["# 雪球舆情日报\n"]
        lines.append(f"**生成时间**: {time.strftime('%Y-%m-%d %H:%M')}\n")
        lines.append("📊 今日无舆情异常变化。\n")
        if posts_data_map:
            lines.append("## 爬取摘要\n")
            lines.append("| 股票 | 帖子数 | 情绪均值 | 情绪倾向 |")
            lines.append("|------|--------|----------|----------|")
            for code, posts in sorted(posts_data_map.items()):
                count = len(posts)
                scores = [p.get("sentiment_score", 0.0) for p in posts if p.get("sentiment_score") is not None]
                if scores:
                    avg = sum(scores) / len(scores)
                    if avg > 0.1:
                        mood = "😊 偏正面"
                    elif avg < -0.1:
                        mood = "😟 偏负面"
                    else:
                        mood = "😐 中性"
                else:
                    avg = 0.0
                    mood = "—"
                lines.append(f"| {code} | {count} | {avg:+.2f} | {mood} |")
        return "\n".join(lines)

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