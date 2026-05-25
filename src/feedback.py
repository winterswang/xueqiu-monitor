"""xueqiu-monitor: user feedback loop

Records user feedback on pushes and adjusts content_weight.
Implements weight decay for stale sources.
"""

from __future__ import annotations

import logging
from typing import Any

import db

logger = logging.getLogger(__name__)


def record_feedback(
    db_path: str,
    push_id: int,
    verdict: str,  # "useful" | "useless"
    config: dict | None = None,
) -> float | None:
    """Record user feedback and adjust content weight.

    Returns new weight value, or None if push not found.
    """
    push = db.get_push_by_id(db_path, push_id)
    if not push:
        logger.warning(f"push_id={push_id} 未找到，跳过反馈")
        return None

    delta = 0.1 if verdict == "useful" else -0.1
    if config:
        delta = config.get("useful_delta", 0.1) if verdict == "useful" else config.get("useless_delta", -0.1)

    # Adjust weight for this stock_code as source + keyword from content
    # Phase 1: source = stock_code, keyword = "all" (simple)
    source = push.stock_code
    keyword = "all"

    new_weight = db.upsert_weight(db_path, source, keyword, delta)
    logger.info(f"反馈: push_id={push_id} verdict={verdict} → {source} 权重={new_weight:.2f}")
    return new_weight


def adjust_weight(
    db_path: str,
    source: str,
    keyword: str,
    verdict: str,
    config: dict | None = None,
) -> float:
    """Directly adjust content_weight."""
    delta = 0.1 if verdict == "useful" else -0.1
    if config:
        delta = config.get("useful_delta", 0.1) if verdict == "useful" else config.get("useless_delta", -0.1)
    return db.upsert_weight(db_path, source, keyword, delta)


def decay_stale_weights(
    db_path: str,
    config: dict | None = None,
) -> int:
    """Daily decay of stale weights (7 days no update). Returns count of decayed rows."""
    days = 7
    rate = 0.05
    floor = 0.3
    if config:
        days = config.get("decay_days", 7)
        rate = config.get("decay_rate", 0.05)
        floor = config.get("weight_floor", 0.3)
    count = db.decay_stale_weights(db_path, days, rate, floor)
    if count > 0:
        logger.info(f"权重衰减: {count} 条记录已衰减")
    return count
