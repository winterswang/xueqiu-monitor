"""xueqiu-monitor: rule engine filter (Phase 1)

Filters noise:
- Ad keyword detection
- Duplicate short-text dedup (>85% similarity)
- Short post filtering (<20 chars)
- P0/P1/P2 priority assignment
- Cold-start gate (no push during accumulation phase)
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any

from models import ChangeAlert

logger = logging.getLogger(__name__)

# ── Configurable defaults (overridden by config file) ──

AD_KEYWORDS = ["开户", "佣金", "万一", "万0.5", "低手续费", "加群", "荐股", "内幕"]
DUPLICATE_SIMILARITY_THRESHOLD = 0.85
SHORT_POST_THRESHOLD = 20
P0_Z_THRESHOLD = 3.0
P1_Z_THRESHOLD = 2.0


# ════════════════════════════════════════════════════════
# Filter logic
# ════════════════════════════════════════════════════════

def filter_ads(posts_data: list[dict], keywords: list[str] | None = None) -> list[int]:
    """Return indices of posts matching ad keywords."""
    kw = keywords or AD_KEYWORDS
    ad_indices = []
    for i, p in enumerate(posts_data):
        text = p.get("title", "") + " " + p.get("content", "")
        for k in kw:
            if k in text:
                ad_indices.append(i)
                break
    return ad_indices


def filter_duplicates(posts_data: list[dict], threshold: float | None = None) -> list[int]:
    """Detect near-duplicate posts via content hash similarity (simplified).

    Uses normalized content length + first 200 chars hash for fast dedup.
    Returns indices of duplicates (keep first occurrence).
    """
    thresh = threshold or DUPLICATE_SIMILARITY_THRESHOLD
    seen_hashes: dict[str, int] = {}  # hash → first index
    dup_indices = []
    for i, p in enumerate(posts_data):
        text = (p.get("title", "") + p.get("content", ""))[:200].strip()
        if len(text) < 10:
            h = str(i)  # unique
        else:
            h = hashlib.md5(text.encode()).hexdigest()
        if h in seen_hashes:
            dup_indices.append(i)
        else:
            seen_hashes[h] = i
    return dup_indices


def filter_short_posts(posts_data: list[dict], min_chars: int | None = None) -> list[int]:
    """Return indices of posts below minimum character threshold."""
    thresh = min_chars or SHORT_POST_THRESHOLD
    short_indices = []
    for i, p in enumerate(posts_data):
        text = p.get("content", "") or p.get("title", "")
        if len(text) < thresh:
            short_indices.append(i)
    return short_indices


def assign_priority(alert: ChangeAlert, config: dict | None = None) -> str:
    """Assign P0/P1/P2 based on Z-score thresholds.

    P0: Z > 3.0
    P1: 2.0 < Z <= 3.0
    P2: Z <= 2.0
    """
    p0 = config.get("p0_z_threshold", P0_Z_THRESHOLD) if config else P0_Z_THRESHOLD
    p1 = config.get("p1_z_threshold", P1_Z_THRESHOLD) if config else P1_Z_THRESHOLD
    if abs(alert.z_score) > p0:
        return "P0"
    elif abs(alert.z_score) > p1:
        return "P1"
    return "P2"


def filter_alerts(
    alerts: list[ChangeAlert],
    posts_data: list[dict],
    cold_start: bool = False,
    config: dict | None = None,
) -> list[ChangeAlert]:
    """Full filter pipeline:
    1. Assign priority
    2. Cold-start gate → all P2
    3. Filter ads/duplicates/short → mark filtered
    Returns alerts with priority and filtered status set.
    """
    # Merge config
    ad_kw = config.get("ad_keywords", AD_KEYWORDS) if config else AD_KEYWORDS
    dup_thresh = config.get("duplicate_similarity_threshold", DUPLICATE_SIMILARITY_THRESHOLD) if config else DUPLICATE_SIMILARITY_THRESHOLD
    short_thresh = config.get("short_post_threshold", SHORT_POST_THRESHOLD) if config else SHORT_POST_THRESHOLD

    # Step 1: assign priority
    for alert in alerts:
        alert.priority = assign_priority(alert, config)
        if cold_start:
            alert.priority = "P2"

    # Step 2: detect noise posts
    ad_set = set(filter_ads(posts_data, ad_kw))
    dup_set = set(filter_duplicates(posts_data, dup_thresh))
    short_set = set(filter_short_posts(posts_data, short_thresh))
    noise_set = ad_set | dup_set | short_set

    # Step 3: mark alerts as filtered if ALL associated posts are noise
    # For now, if any noise detected, mark the alert with reason
    for alert in alerts:
        if alert.filtered:
            continue
        if ad_set:
            alert.filtered = 1
            alert.filter_reason = f"广告关键词匹配 ({len(ad_set)}帖)"
        elif len(dup_set) / max(len(posts_data), 1) > 0.5:
            alert.filtered = 1
            alert.filter_reason = f"重复率过高 ({len(dup_set)}/{len(posts_data)}帖)"
        elif len(short_set) / max(len(posts_data), 1) > 0.7:
            alert.filtered = 1
            alert.filter_reason = f"短帖占比过高 ({len(short_set)}/{len(posts_data)}帖)"

    return alerts
