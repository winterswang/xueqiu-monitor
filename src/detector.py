"""xueqiu-monitor: change detector (Z-score + TF-IDF hot words)

Phase 1: rule-based detection, no LLM.
- Z-score for post count spikes and sentiment shifts
- TF-IDF for hot word emergence
- 14-day rolling window for baseline
- 28-day cold start fallback to full history
"""

from __future__ import annotations

import hashlib
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

# Multi-word entities that jieba would otherwise fragment (媒体账号名、
# 公告模板、通用金融词). By adding them to jieba's custom dict BEFORE
# segmentation, they become single tokens and can be cleanly filtered by
# _CN_STOPWORDS. This avoids the sklearn "inconsistent stop_words" warning
# caused by pre-fragmenting multi-word stopwords.
_JIEBA_CUSTOM_WORDS = {
    # media accounts — keep as single tokens
    "环球市场播报", "新浪证券", "红岸工作室", "市场资讯", "财联社",
    "每日经济新闻", "证券时报", "国际金融报", "国家知识产权局",
    # generic finance phrases — keep as single tokens
    "同比增长", "同比下降", "环比增长", "申请公布号",
    # phrases that should stay whole to avoid fragment noise
    "以下简称", "小时前", "网页链接",
}

# Register custom words once at import time
for _w in _JIEBA_CUSTOM_WORDS:
    import jieba
    jieba.add_word(_w)


def _tokenize(text: str) -> list[str]:
    """Hybrid Chinese/English tokenizer for TF-IDF.

    English tokens: regex split (preserves PDD.US → pdd, us — jieba would
    split on the dot, creating a '.' noise token).

    Chinese tokens: jieba.cut (segments continuous CJK runs into words).
    This is the critical fix — the old regex [\u4e00-\u9fff]+ treated
    "心动公司" or "深圳迈瑞生物医疗电子股份有限公司申请一项名为" as a
    single token, polluting hot word rankings with company names and
    announcement boilerplate.
    """
    import jieba

    text = text.lower()
    # English: regex (unchanged behavior)
    en_tokens = re.findall(r"[a-zA-Z]+", text)
    # Chinese: jieba segmentation
    cn_tokens = []
    for w in jieba.cut(text, cut_all=False):
        w = w.strip()
        if w and "\u4e00" <= w[0] <= "\u9fff":
            cn_tokens.append(w)
    return [t for t in en_tokens + cn_tokens if len(t) >= 2]


# Chinese stopwords — xueqiu UI noise, unit words, rendered placeholders,
# and short ASCII tokens that hit 80-100% of posts but carry no signal.
# These are filtered during TF-IDF vectorization (never appear in results).
_CN_STOPWORDS = {
    # xueqiu UI noise
    '讨论', '来源', '回复', '小时前', '来自', '转发', '关注', '发布', '查看',
    '评论', '单位', '扫描', '分享', '收藏', '展开', '全部', '公告',
    # unit words (6/24 300750.SZ「万元」P0 z=9.84 was missing from original set)
    '万元', '亿元', '万亿', '千万', '百万', '十万',
    '万美元', '亿港元', '亿美元', '美元', '港元', '元', '块',
    # rendered placeholders — xueqiu UI text embedded in post body
    # (6/27 300750.SZ「网页链接」P1 z=3.78)
    '网页链接', '图片', '视频', '收起',
    # exchange code suffixes — always noise
    'hk', 'sz', 'sh',
    # media accounts (whole tokens via jieba custom dict)
    '环球市场播报', '新浪证券', '格隆汇', '红岸工作室', '市场资讯',
    '每日经济新闻', '证券时报', '国际金融报', '国家知识产权局',
    # media account fragments (jieba splits these, so list the fragments)
    '联社',  # 财联社 → 联社
    '新闻',  # 每日经济新闻 also appears standalone
    # announcement/patent boilerplate fragments
    '信息显示', '申请号', '申请公布号', '消息', '发布公告',
    '知识产权', '申请', '公布', '信息', '显示',
    # generic finance terms (whole tokens via jieba custom dict)
    '同比增长', '同比下降', '环比增长',
    '回购', '增持', '减持', '市值', '估值', '涨停', '跌停',
    # jieba fragmentation debris — company names like 心动公司/紫金矿业 get
    # segmented into [心动, 公司] / [紫金, 矿业]; these generic fragments
    # carry no topic signal
    '公司', '有限公司', '股份', '集团', '有限',
    # geographic/qualifier fragments with no topic specificity
    '深圳',  # 迈瑞/腾讯 company location
    # high-frequency generic Chinese words (jieba segments these out)
    '一个', '金额', '授权', '决议', '设备', '观点',
    # 拼多多 → [拼多, 多多] fragment
    '多多',
}


# ════════════════════════════════════════════════════════
# Hot word pre-filters (dynamic, run after TF-IDF, before Z-score)
# ════════════════════════════════════════════════════════

def _is_short_token(word: str) -> bool:
    """Filter short tokens that statistically dominate but carry no specific signal.

    Catches: pe (PE ratio), ai, etf, ipo — tokens that appear in 80-100% of
    posts about any stock but have no topic value as standalone words.
    Real signal words (yoyo=4, molly=5, labubu=6) are longer and pass.

    Rule: pure ASCII ≤3 chars, or Chinese ≤2 chars with total len ≤3.
    """
    chinese_chars = sum(1 for c in word if '\u4e00' <= c <= '\u9fff')
    if chinese_chars == 0 and len(word) <= 3:
        return True
    if chinese_chars <= 2 and len(word) <= 3:
        return True
    return False


def _is_username_like(word: str, posts_texts: list[str]) -> bool:
    """Detect if a hot word is actually a username in @mention patterns.

    Xueqiu reply chains embed usernames as '回复 @<username> :' or '// @<username> :'.
    When a KOL posts, their name gets high TF-IDF but it's not a topic word.

    Heuristic: if >70% of the word's occurrences are preceded by @, it's a username.
    Cases: '多伦多的大道信徒' (PDD 6/23 z=5.44), '大道无形我有型' (9992 6/24 z=3.02).
    """
    total = 0
    mentions = 0
    word_lower = word.lower()
    at_pattern = re.compile(r'@\s*' + re.escape(word_lower), re.IGNORECASE)
    for text in posts_texts:
        lower_text = text.lower()
        total += lower_text.count(word_lower)
        mentions += len(at_pattern.findall(lower_text))
    if total == 0:
        return False
    return (mentions / total) > 0.7


def filter_noise_words(words: list[str], posts_texts: list[str]) -> list[str]:
    """Filter hot words that carry no signal (short tokens / username-like).

    Shared by the alert path (detect_hot_word_emergence) and the storage path
    (cli.py insert_hot_word_event / hot_word_dict), so hot_word data quality
    matches alert quality (v0.7 F4). Stopwords are already removed during
    TF-IDF vectorization via ``_CN_STOPWORDS``.

    Args:
        words: Raw TF-IDF top words for a stock.
        posts_texts: Post texts used to detect username-like tokens.

    Returns:
        Words that survive both filters, in input order.
    """
    return [
        w for w in words
        if not _is_short_token(w) and not _is_username_like(w, posts_texts)
    ]


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
        # Layer 2: dynamic pre-filters (run before Z-score to prevent false alerts)
        if _is_short_token(word):
            logger.debug(f"[FILTER] skip short token: {word!r}")
            continue
        if _is_username_like(word, curr_posts_texts):
            logger.debug(f"[FILTER] skip username-like word: {word!r}")
            continue

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

def is_cold_start(historical_stats: list[SentimentStat], min_days: int = 7) -> bool:
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


def _normalize_announcement_time(raw_time: str, now: float) -> str:
    """Normalize an announcement time string to a stable YYYY-MM-DD identity.

    Announcement `time` arrives from three crawler sources in three formats:
      - opencli `created_at`:  "2026-08-04T10:00:00" (ISO 8601)
      - requests API fallback: "2026-08-04" (%Y-%m-%d)
      - DOM supplement:        "3小时前" / "昨天" / "HH:MM" (relative)

    A raw-string dedup identity would change whenever the active crawler path
    changes, so the same announcement could be re-alerted within the 7-day
    window. Normalizing to a day-granularity date makes the identity stable
    across sources (and matches the existing 7-day window granularity).

    Returns the raw string unchanged (trimmed) when it cannot be mapped to a
    date, so behavior degrades to the previous title+raw-time identity.
    """
    import datetime as _dt

    if not raw_time or not isinstance(raw_time, str):
        return raw_time or ""
    t = raw_time.strip()
    if not t:
        return ""

    # ISO 8601 (opencli): "2026-08-04T10:00:00[.fff][Z|±HH:MM]" → date only.
    m = re.match(
        r"(\d{4})-(\d{2})-(\d{2})T",
        t,
    )
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    # Absolute date (API fallback): "2026-08-04".
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", t)
    if m:
        return t

    now_dt = _dt.datetime.fromtimestamp(now)

    # Relative "X天前" (DOM supplement).
    m = re.match(r"^(\d+)\s*天前$", t)
    if m:
        d = now_dt - _dt.timedelta(days=int(m.group(1)))
        return d.strftime("%Y-%m-%d")

    # "昨天[ HH:MM]" (DOM supplement).
    if t.startswith("昨天"):
        d = now_dt - _dt.timedelta(days=1)
        return d.strftime("%Y-%m-%d")

    # "X小时前" / "X分钟前" / "X秒前": map to today (day granularity).
    if re.match(r"^\d+\s*(小时前|分钟前|秒前)$", t):
        return now_dt.strftime("%Y-%m-%d")

    # "HH:MM" (DOM supplement, no date): map to today.
    if re.match(r"^\d{1,2}:\d{2}$", t):
        return now_dt.strftime("%Y-%m-%d")

    # "MM-DD" or "MM-DD HH:MM" (DOM supplement, current year assumed).
    m = re.match(r"^(\d{2})-(\d{2})(?:\s+\d{1,2}:\d{2})?$", t)
    if m:
        year = now_dt.year
        try:
            d = now_dt.replace(year=year, month=int(m.group(1)), day=int(m.group(2)))
        except ValueError:
            return t
        if d.timestamp() > now:
            d = d.replace(year=year - 1)
        return d.strftime("%Y-%m-%d")

    return t


def detect_new_announcement(
    stock_code: str,
    curr_announcements: list[dict],
    prev_announcements: list[dict],
    db_path: str | None = None,
    z_threshold: float = 2.0,
    max_alerts: int = 50,
) -> list[ChangeAlert]:
    """Detect new announcements with DB-level dedup and real Z-score.

    Two-stage dedup:
    1. Cross-check against previous snapshot's announcement titles
    2. DB-level check: skip if same stock+title was alerted within 7 days

    Z-score is computed from historical daily new-announcement counts
    (replaces hardcoded Z=3.00 sentinel).

    Args:
        stock_code: Stock code (e.g. SH600519)
        curr_announcements: Today's announcements [{title, time, notice_type}]
        prev_announcements: Previous crawl's announcements (same format)
        db_path: Path to SQLite DB for cross-run dedup + Z-score baseline.
                 When None, falls back to Z=3.00 sentinel (no DB context).
        z_threshold: Z-score threshold for alert (default 2.0)
        max_alerts: Max alerts per stock per run (default 50)

    Returns ChangeAlert per genuinely new announcement.
    """
    if not curr_announcements:
        return []

    prev_titles = {_normalize_title(p.get("title", "")) for p in prev_announcements}
    # Dedup within the current batch — crawler can return duplicate titles
    # (e.g. daily share-buyback reports appear N times in the API response).
    seen_keys: set[str] = set()

    now_ts = int(time.time())
    alerts = []

    # Compute Z-score from historical daily announcement counts
    if db_path:
        from . import db  # lazy import to avoid circular dependency at module level
        hist_counts = db.get_historical_new_announcement_counts(db_path, stock_code)
        z_score = compute_z_score(
            float(len(curr_announcements)), hist_counts
        ) if len(hist_counts) >= 2 else 0.0
    else:
        z_score = 3.0  # legacy sentinel, no DB context

    for ann in curr_announcements:
        if len(alerts) >= max_alerts:
            break

        title = ann.get("title", "")
        norm = _normalize_title(title)
        if not norm or len(norm) < 4:
            continue

        # Stage 0: within-batch dedup (same title+normalized-date seen earlier).
        # Key includes a normalized announcement date so that legitimately
        # distinct announcements sharing a generic title (e.g. "财报披露" on
        # different dates) survive, while the same announcement coming from
        # different crawler paths (ISO vs %Y-%m-%d vs relative) still collapses.
        ann_time_raw = ann.get("time", "")
        ann_date = _normalize_announcement_time(ann_time_raw, now_ts)
        dedup_key = f"{title}|{ann_date}"
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        # dedup_hash (title+time) is the announcement identity used by both the
        # permanent UNIQUE index and the 7-day window. A title-only hash would
        # permanently swallow generic titles ("财报披露") that legitimately
        # recur on different dates (v0.8.1 fix).
        dedup_hash = hashlib.md5(dedup_key.encode()).hexdigest()

        # Stage 1: cross-check against previous snapshot
        if norm in prev_titles:
            continue

        # Stage 2: DB-level dedup (7-day window)
        if db_path:
            from . import db
            recent = db.get_recent_announcement_alerts(db_path, stock_code, dedup_hash, days=7)
            if recent:
                logger.debug(
                    f"  {stock_code}: skip dup announcement \"{title[:40]}...\" "
                    f"(alerted {len(recent)}x in last 7d)"
                )
                continue

        alerts.append(ChangeAlert(
            stock_code=stock_code,
            alert_time=now_ts,
            alert_type="new_announcement",
            z_score=round(abs(z_score), 2),  # real Z-score from historical volume
            magnitude=float(len(curr_announcements)),  # total new announcements today
            detail={
                "title": title,
                "dedup_hash": dedup_hash,
                "time": ann_time_raw,
                "ann_date": ann_date,
                "notice_type": ann.get("notice_type", ""),
                "link": ann.get("link", ""),
                "prev_count": len(prev_announcements),
                "new_count": len(curr_announcements),
                "ann_z_score": round(abs(z_score), 2),
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
    db_path: str | None = None,
) -> list[ChangeAlert]:
    """Unified detection entry point — orchestrates all 4 detection types.

    During cold start, still generates alerts but marks them for priority handling.
    P0 alerts pass through even in cold start; P1/P2 may be suppressed.
    """
    if cold_start:
        # During cold start, still generate alerts — filter.py handles priority
        # (P0 passes through, P1/P2 may be suppressed)
        pass

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
        stock_code, curr_announcements, prev_announcements, db_path))

    return alerts
