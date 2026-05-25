"""xueqiu-monitor: change detector (Z-score + TF-IDF hot words)

Phase 1: rule-based detection, no LLM.
- Z-score for post count spikes and sentiment shifts
- TF-IDF for hot word emergence
- 14-day rolling window for baseline
- 28-day cold start fallback to full history
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from typing import Any

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from .models import SentimentStat, HotWordEvent, ChangeAlert

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
    prev_snapshot_sentiment: float | None = None,
) -> ChangeAlert | None:
    """Detect significant sentiment shift.

    Two triggers (two-period direct threshold takes priority):
    1. Direct threshold: |curr - prev_snapshot| > 0.2 → immediate alert
    2. Z-score: |Z| > 2.0 against 14-day historical window
    """
    if not historical_stats:
        return None

    stock_code = historical_stats[0].stock_code

    # ── Trigger 1: two-period direct threshold ──
    if prev_snapshot_sentiment is not None:
        raw_shift = curr_sentiment_avg - prev_snapshot_sentiment
        if abs(raw_shift) > 0.2:
            return ChangeAlert(
                stock_code=stock_code,
                alert_type="sentiment_shift",
                z_score=0.0,
                magnitude=round(abs(raw_shift), 3),
                detail={
                    "curr_sentiment": round(curr_sentiment_avg, 3),
                    "prev_snapshot_sentiment": round(prev_snapshot_sentiment, 3),
                    "shift": round(raw_shift, 3),
                    "trigger": "two_period",
                },
            )

    # ── Trigger 2: Z-score against historical window ──
    hist_means = [s.sentiment_mean for s in historical_stats[-window_days:]]
    z = compute_z_score(curr_sentiment_avg, hist_means)
    if abs(z) <= 2.0:
        return None
    shift = curr_sentiment_avg - float(np.mean(hist_means)) if hist_means else 0.0
    return ChangeAlert(
        stock_code=stock_code,
        alert_type="sentiment_shift",
        z_score=round(z, 2),
        magnitude=round(abs(shift), 3),
        detail={
            "curr_sentiment": round(curr_sentiment_avg, 3),
            "prev_sentiment": round(float(np.mean(hist_means)), 3),
            "shift": round(shift, 3),
            "trigger": "z_score",
        },
    )


# ════════════════════════════════════════════════════════
# TF-IDF hot word detection
# ════════════════════════════════════════════════════════

def _tokenize(text: str) -> list[str]:
    """Simple Chinese/English tokenizer: split on non-word chars, filter short tokens."""
    tokens = re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z]+', text.lower())
    return [t for t in tokens if len(t) >= 2]


# Chinese stopwords — xueqiu UI noise and common low-signal words
_CN_STOPWORDS = {
    '讨论', '来源', '回复', '小时前', '来自', '转发', '关注', '发布', '查看',
    '评论', '单位', '扫描', '分享', '收藏', '展开', '全部', '公告',
    '亿元', '万美元', '亿港元', '亿美元',  # unit words, not signal
}


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
            stop_words=list(_CN_STOPWORDS),
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


# ════════════════════════════════════════════════════════
# New announcement detection
# ════════════════════════════════════════════════════════

ANN_TITLE_NOISE = re.compile(
    r'(贵州茅台|五粮液|腾讯控股|[A-Z]{2}\d{6})?'  # stock name/code prefix
    r'[：:：]?'
    r'\s*'
)


def _normalize_title(title: str) -> str:
    """Normalize announcement title for comparison: strip noise, lower, trim."""
    t = ANN_TITLE_NOISE.sub('', title, count=1)
    t = re.sub(r'\s+', '', t)
    return t.strip()


def detect_new_announcement(
    stock_code: str,
    curr_announcements: list[dict],
    prev_announcements: list[dict],
    z_threshold: float = 2.0,
) -> list[ChangeAlert]:
    """Detect new announcements by comparing titles with previous crawl.

    A new announcement is one whose normalized title does not appear in
    the previous day's announcement set.

    Args:
        stock_code: Stock code (e.g. SH600519)
        curr_announcements: Today's announcements [{title, time, notice_type}]
        prev_announcements: Previous crawl's announcements (same format)

    Returns ChangeAlert per new announcement.
    """
    if not curr_announcements:
        return []

    prev_titles = {_normalize_title(p.get("title", "")) for p in prev_announcements}

    now_ts = int(time.time())
    alerts = []
    for ann in curr_announcements:
        norm = _normalize_title(ann.get("title", ""))
        if not norm or len(norm) < 4:
            continue
        if norm in prev_titles:
            continue
        alerts.append(ChangeAlert(
            stock_code=stock_code,
            alert_time=now_ts,
            alert_type="new_announcement",
            z_score=3.0,  # announcements are binary new/old, fixed significant Z
            magnitude=1.0,
            detail={
                "title": ann.get("title", ""),
                "time": ann.get("time", ""),
                "notice_type": ann.get("notice_type", ""),
                "prev_count": len(prev_announcements),
                "new_count": len(curr_announcements),
            },
        ))
    return alerts


def detect_changes(
    stock_code: str,
    curr_posts_count: int,
    curr_sentiment_avg: float,
    curr_posts_texts: list[str],
    curr_announcements: list[dict],
    prev_announcements: list[dict],
    historical_stats: list[SentimentStat],
    historical_events: list[HotWordEvent],
    cold_start: bool,
) -> list[ChangeAlert]:
    """Unified detection entry point — orchestrates all 4 detection types.

    Returns combined list of ChangeAlert (unfiltered, no priority assigned).
    Cold start suppresses all alerts (returns empty).
    """
    if cold_start:
        return []

    alerts: list[ChangeAlert] = []

    spike = detect_post_spike(curr_posts_count, historical_stats)
    if spike:
        alerts.append(spike)

    shift = detect_sentiment_shift(curr_sentiment_avg, historical_stats)
    if shift:
        alerts.append(shift)

    alerts.extend(detect_hot_word_emergence(
        stock_code, curr_posts_texts, historical_events))

    alerts.extend(detect_new_announcement(
        stock_code, curr_announcements, prev_announcements))

    return alerts
