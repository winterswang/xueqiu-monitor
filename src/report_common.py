"""Shared post-filtering helpers for report/export paths (v0.7 F3).

Single source of truth for recency-based post filtering, so the daily-report,
CSV-export and (formerly) sentiment-report paths apply identical time semantics.
Introduced in v0.7 to fix R1/R2: export_csv had no post-time filter at all and
daily_sentiment_report aggregated every post in the snapshot regardless of age.

All functions are pure — they take a list of post dicts and return a new list.
"""

from __future__ import annotations

import time
from typing import Any, Callable, List


def filter_posts_by_recency(
    posts: List[dict],
    now: float | None = None,
    max_age_days: int = 1,
    parse_time: Callable[[str, float], float] | None = None,
    fail_open: bool = True,
) -> List[dict]:
    """Filter posts to those published within ``max_age_days`` of ``now``.

    Args:
        posts: List of post dicts, each expected to have a ``time`` field
            (string as returned by the crawler, e.g. ISO 8601 or "X分钟前").
        now: Reference timestamp (Unix seconds). Defaults to time.time().
        max_age_days: Keep posts newer than this many days.
        parse_time: Time-string parser ``(str, now) -> ts``. Defaults to
            ``crawler._parse_post_time`` (lazy-imported, so this module stays
            import-light for scripts that don't need it).
        fail_open: If True (default), posts whose time cannot be parsed are
            KEPT (ts == 0). Matches the pipeline's existing fail-open policy so
            a parse regression never blanks out an entire feed. If False, they
            are dropped.

    Returns:
        New list of posts within the recency window (unparseable kept if
        fail_open). The input dicts are not mutated.
    """
    if now is None:
        now = time.time()
    if parse_time is None:
        from .crawler import _parse_post_time

        parse_time = _parse_post_time

    cutoff = now - max_age_days * 86400
    filtered: List[dict] = []
    for p in posts:
        ts = parse_time((p.get("time") or ""), now)
        if ts > 0:
            if ts >= cutoff:
                filtered.append(p)
        elif fail_open:
            filtered.append(p)  # unparseable → keep, per pipeline policy
    return filtered
