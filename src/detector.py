"""xueqiu-monitor: change detector (Z-score + TF-IDF hot words)

Phase 1: rule-based detection, no LLM.
- Z-score for post count spikes and sentiment shifts
- TF-IDF for hot word emergence
- 14-day rolling window for baseline
- 28-day cold start fallback to full history
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections import Counter
from typing import Any

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from models import SentimentStat, HotWordEvent, ChangeAlert

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════
# Z-score detection
# ════════════════════════════════════════════════════════

def compute_z_score(
    current_value: float,
    historical_values: list[float],
) -> float:
    """Compute Z-score: Z = (x - μ) / σ.

    Returns 0.0 if insufficient data (σ=0 or <2 data points).
    """
    if len(historical_values) < 2:
        return 0.0
    mu = np.mean(historical_values)
    sigma = np.std(historical_values, ddof=1)  # sample std
    if sigma == 0:
        return 0.0
    return float((current_value - mu) / sigma)


def detect_post_spike(
    curr_posts_count: int,
    historical_stats: list[SentimentStat],
    window_days: int = 14,
) -> ChangeAlert | None:
    """Detect abnormal post count spike."""
    if not historical_stats:
        return None
    hist_counts = [s.posts_count for s in historical_stats[-window_days:]]
    z = compute_z_score(curr_posts_count, hist_counts)
    if z <= 2.0:
        return None
    return ChangeAlert(
        stock_code=historical_stats[0].stock_code,
        alert_type="post_spike",
        z_score=round(z, 2),
        magnitude=float(curr_posts_count - np.mean(hist_counts)),
        detail={
            "curr_count": curr_posts_count,
            "historical_mean": round(float(np.mean(hist_counts)), 1),
            "historical_std": round(float(np.std(hist_counts, ddof=1)), 1),
        },
    )


def detect_sentiment_shift(
    curr_sentiment_avg: float,
    historical_stats: list[SentimentStat],
    window_days: int = 14,
) -> ChangeAlert | None:
    """Detect significant sentiment shift."""
    if not historical_stats:
        return None
    hist_means = [s.sentiment_mean for s in historical_stats[-window_days:]]
    z = compute_z_score(curr_sentiment_avg, hist_means)
    if abs(z) <= 2.0:
        return None
    shift = curr_sentiment_avg - float(np.mean(hist_means)) if hist_means else 0.0
    return ChangeAlert(
        stock_code=historical_stats[0].stock_code,
        alert_type="sentiment_shift",
        z_score=round(z, 2),
        magnitude=round(abs(shift), 3),
        detail={
            "curr_sentiment": round(curr_sentiment_avg, 3),
            "prev_sentiment": round(float(np.mean(hist_means)), 3),
            "shift": round(shift, 3),
        },
    )


# ════════════════════════════════════════════════════════
# TF-IDF hot word detection
# ════════════════════════════════════════════════════════

def _tokenize(text: str) -> list[str]:
    """Simple Chinese/English tokenizer: split on non-word chars, filter short tokens."""
    tokens = re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z]+', text.lower())
    return [t for t in tokens if len(t) >= 2]


def compute_tfidf(
    documents: list[str],
    min_df: int = 2,
    max_df: float = 0.8,
    ngram_range: tuple = (1, 2),
    top_n: int = 20,
) -> list[tuple[str, float]]:
    """Compute TF-IDF scores across documents. Returns top N (word, score) pairs.

    Returns empty list if fewer than min_df documents.
    """
    if len(documents) < min_df:
        return []
    try:
        vectorizer = TfidfVectorizer(
            tokenizer=_tokenize,
            min_df=min_df,
            max_df=max_df,
            ngram_range=ngram_range,
            stop_words=None,
        )
        tfidf_matrix = vectorizer.fit_transform(documents)
        feature_names = vectorizer.get_feature_names_out()
        scores = np.asarray(tfidf_matrix.sum(axis=0)).flatten()
        indices = np.argsort(scores)[::-1][:top_n]
        return [(feature_names[i], float(scores[i])) for i in indices if scores[i] > 0]
    except ValueError:
        return []


def detect_hot_word_emergence(
    stock_code: str,
    curr_posts_texts: list[str],
    historical_events: list[HotWordEvent],
    min_df: int = 2,
    max_df: float = 0.8,
) -> list[ChangeAlert]:
    """Detect hot words with TF-IDF surge relative to history.

    For each top TF-IDF word in current posts:
    1. Get historical TF-IDF values for that word (14-day window)
    2. Compute Z-score
    3. Alert if Z > 2.0
    """
    if not curr_posts_texts:
        return []

    # Current TF-IDF
    curr_tfidf = dict(compute_tfidf(curr_posts_texts, min_df, max_df))

    # Build historical TF-IDF per word
    hist_tfidfs: dict[str, list[float]] = {}
    for he in historical_events:
        if he.word not in hist_tfidfs:
            hist_tfidfs[he.word] = []
        hist_tfidfs[he.word].append(he.tfidf_score)

    alerts = []
    for word, score in curr_tfidf.items():
        hist = hist_tfidfs.get(word, [])
        if len(hist) < 3:  # need some history for meaningful Z-score
            continue
        z = compute_z_score(score, hist)
        if z > 2.0:
            alerts.append(ChangeAlert(
                stock_code=stock_code,
                alert_time=int(time.time()),
                alert_type="hot_word_surge",
                z_score=round(z, 2),
                magnitude=round(score, 4),
                detail={
                    "word": word,
                    "curr_tfidf": round(score, 4),
                    "hist_mean": round(float(np.mean(hist)), 4),
                },
            ))
    return alerts


# ════════════════════════════════════════════════════════
# Cold start helper
# ════════════════════════════════════════════════════════

def is_cold_start(historical_stats: list[SentimentStat], min_days: int = 28) -> bool:
    """Check if we're still in cold start (insufficient baseline data)."""
    if not historical_stats:
        return True
    unique_dates = len(set(s.stat_date for s in historical_stats))
    return unique_dates < min_days
