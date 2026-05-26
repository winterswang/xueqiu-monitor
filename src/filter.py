"""xueqiu-monitor: rule engine filter (Phase 1)

Filters noise:
- Ad keyword detection
- Duplicate short-text dedup (>85% similarity)
- Content-level similarity dedup (>85%)
- Short post filtering (<20 chars)
- P0/P1/P2 priority assignment
- Cold-start gate (no push during accumulation phase)
"""

from __future__ import annotations

import difflib
import hashlib
import logging
from typing import Any

from .models import ChangeAlert

logger = logging.getLogger(__name__)

# ── Configurable defaults (overridden by config file) ──

AD_KEYWORDS = ["开户", "佣金", "万一", "万0.5", "低手续费", "加群", "荐股", "内幕"]
DUPLICATE_SIMILARITY_THRESHOLD = 0.85
CONTENT_SIMILARITY_THRESHOLD = 0.85
SHORT_POST_THRESHOLD = 20
P0_Z_THRESHOLD = 3.0
P1_Z_THRESHOLD = 2.0


# ════════════════════════════════════════════════════════
# Filter logic
# ════════════════════════════════════════════════════════

def check_content_similarity(text1: str, text2: str) -> float:
    """Compute content similarity (0.0-1.0) using difflib.SequenceMatcher.

    Both texts are truncated to 500 chars for performance.
    """
    t1 = (text1 or "")[:500]
    t2 = (text2 or "")[:500]
    if not t1 and not t2:
        return 1.0
    return difflib.SequenceMatcher(None, t1, t2).ratio()


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


def filter_duplicates(
    posts_data: list[dict],
    threshold: float | None = None,
    content_threshold: float | None = None,
) -> tuple[list[int], list[int]]:
    """Detect duplicate posts via title-hash and content similarity.

    Stage 1 (title hash): hash first 200 chars of title+content for fast dedup.
    Stage 2 (content similarity): difflib.SequenceMatcher comparison,
    content truncated to 500 chars.

    Returns (title_dup_indices, content_dup_indices).
    """
    hash_thresh = threshold or DUPLICATE_SIMILARITY_THRESHOLD
    sim_thresh = content_threshold or CONTENT_SIMILARITY_THRESHOLD
    seen_hashes: dict[str, int] = {}   # hash → first index
    seen_contents: list[str] = []      # first-kept contents for similarity check
    title_dup_indices: list[int] = []
    content_dup_indices: list[int] = []

    for i, p in enumerate(posts_data):
        text = ((p.get("title") or "") + (p.get("content") or ""))[:200]
        if len(text) < 10:
            h = str(i)  # unique
        else:
            h = hashlib.md5(text.encode()).hexdigest()

        # Stage 1: title-hash dup check
        if h in seen_hashes:
            title_dup_indices.append(i)
            continue

        # Stage 2: content similarity check against all kept posts
        content = p.get("content", "") or ""
        if len(content) >= 10:
            for kept_content in seen_contents:
                if check_content_similarity(content, kept_content) > sim_thresh:
                    content_dup_indices.append(i)
                    break

        # Not a duplicate → record for future comparison
        if i not in content_dup_indices:
            seen_hashes[h] = i
            seen_contents.append(content)

    return title_dup_indices, content_dup_indices


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
    3. Annotate each alert with post-level noise stats (ad/dup/short ratios)
    4. Suppress only when a specific alert type is unreliable due to noise
    Returns alerts with priority and filtered status set.
    """
    # Merge config
    ad_kw = config.get("ad_keywords", AD_KEYWORDS) if config else AD_KEYWORDS
    dup_thresh = config.get("duplicate_similarity_threshold", DUPLICATE_SIMILARITY_THRESHOLD) if config else DUPLICATE_SIMILARITY_THRESHOLD
    content_sim_thresh = config.get("content_similarity_threshold", CONTENT_SIMILARITY_THRESHOLD) if config else CONTENT_SIMILARITY_THRESHOLD
    short_thresh = config.get("short_post_threshold", SHORT_POST_THRESHOLD) if config else SHORT_POST_THRESHOLD

    # Step 1: assign priority
    for alert in alerts:
        alert.priority = assign_priority(alert, config)
        if cold_start:
            alert.priority = "P2"

    # Step 2: detect noise posts
    ad_set = set(filter_ads(posts_data, ad_kw))
    title_dup, content_dup = filter_duplicates(posts_data, dup_thresh, content_sim_thresh)
    dup_set = set(title_dup + content_dup)
    short_set = set(filter_short_posts(posts_data, short_thresh))
    total = max(len(posts_data), 1)

    ad_ratio = len(ad_set) / total
    dup_ratio = len(dup_set) / total
    short_ratio = len(short_set) / total

    # Step 3: per-alert filtering — annotate with noise context,
    # but only suppress if the alert type is specifically unreliable
    for alert in alerts:
        if alert.filtered:
            continue

        # Attach noise stats to detail for downstream visibility
        alert.detail.setdefault("noise", {
            "ad_ratio": round(ad_ratio, 3),
            "dup_ratio": round(dup_ratio, 3),
            "short_ratio": round(short_ratio, 3),
            "total_posts": len(posts_data),
        })

        # sentiment_shift: if >50% posts are noise, the sentiment calculation
        # is unreliable → suppress
        if alert.alert_type == "sentiment_shift" and ad_ratio > 0.5:
            alert.filtered = 1
            alert.filter_reason = f"情感计算不可靠：广告帖占比 {ad_ratio:.0%}"
            continue

        # post_spike: if >70% posts are noise, the spike is from noise, not real content
        if alert.alert_type == "post_spike" and (ad_ratio + dup_ratio) > 0.7:
            alert.filtered = 1
            alert.filter_reason = f"帖子激增由噪音驱动（广告{ad_ratio:.0%} 重复{dup_ratio:.0%}）"
            continue

        # hot_word_surge / new_announcement: not suppressed by post noise

    return alerts
