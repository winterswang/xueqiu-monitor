"""xueqiu-monitor: data models (dataclass-based, no ORM)

10 dataclass models matching schema.sql tables.
Each model supports: from_dict() / to_dict() / from_row() (sqlite3 Row).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from typing import Any


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _parse_json(raw: str | None, default: Any = None) -> Any:
    """Safe JSON parse, returns default on failure."""
    if not raw:
        return default if default is not None else {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default if default is not None else {}


def _to_json(obj: Any) -> str:
    """Serialize to JSON string."""
    return json.dumps(obj, ensure_ascii=False)


def _now() -> int:
    return int(time.time())


# ═══════════════════════════════════════════════════════════════
# Models
# ═══════════════════════════════════════════════════════════════

@dataclass
class CrawlSnapshot:
    """Single crawl snapshot for a stock."""
    stock_code: str
    crawl_time: int = field(default_factory=_now)
    posts_count: int = 0
    posts_data: list[dict] = field(default_factory=list)
    sentiment_avg: float = 0.0
    status: str = "pending"
    id: int | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["posts_data"] = _to_json(d["posts_data"])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> CrawlSnapshot:
        return cls(
            id=d.get("id"),
            stock_code=d["stock_code"],
            crawl_time=d.get("crawl_time", _now()),
            posts_count=d.get("posts_count", 0),
            posts_data=_parse_json(d.get("posts_data", "[]"), []),
            sentiment_avg=d.get("sentiment_avg", 0.0),
            status=d.get("status", "pending"),
        )

    @classmethod
    def from_row(cls, row: tuple | dict) -> CrawlSnapshot:
        if isinstance(row, dict):
            return cls.from_dict(row)
        cols = ["id","stock_code","crawl_time","posts_count","posts_data","sentiment_avg","status"]
        return cls.from_dict(dict(zip(cols, row)))


@dataclass
class SentimentStat:
    """Daily-aggregated sentiment stats."""
    stock_code: str
    stat_date: int
    posts_count: int = 0
    sentiment_mean: float = 0.0
    sentiment_std: float = 0.0
    z_score: float = 0.0
    z_alert: int = 0
    id: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> SentimentStat:
        return cls(
            id=d.get("id"),
            stock_code=d["stock_code"],
            stat_date=d.get("stat_date", 0),
            posts_count=d.get("posts_count", 0),
            sentiment_mean=d.get("sentiment_mean", 0.0),
            sentiment_std=d.get("sentiment_std", 0.0),
            z_score=d.get("z_score", 0.0),
            z_alert=d.get("z_alert", 0),
        )

    @classmethod
    def from_row(cls, row: tuple | dict) -> SentimentStat:
        if isinstance(row, dict):
            return cls.from_dict(row)
        cols = ["id","stock_code","stat_date","posts_count","sentiment_mean","sentiment_std","z_score","z_alert"]
        return cls.from_dict(dict(zip(cols, row)))


@dataclass
class ChangeAlert:
    """Detected change alert."""
    stock_code: str
    alert_time: int = field(default_factory=_now)
    alert_type: str = "sentiment_shift"   # sentiment_shift|hot_word_surge|post_spike|new_announcement
    z_score: float = 0.0
    magnitude: float = 0.0
    detail: dict = field(default_factory=dict)
    priority: str = "P2"
    filtered: int = 0
    filter_reason: str | None = None
    id: int | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["detail"] = _to_json(d["detail"])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> ChangeAlert:
        return cls(
            id=d.get("id"),
            stock_code=d["stock_code"],
            alert_time=d.get("alert_time", _now()),
            alert_type=d.get("alert_type", "sentiment_shift"),
            z_score=d.get("z_score", 0.0),
            magnitude=d.get("magnitude", 0.0),
            detail=_parse_json(d.get("detail", "{}"), {}),
            priority=d.get("priority", "P2"),
            filtered=d.get("filtered", 0),
            filter_reason=d.get("filter_reason"),
        )

    @classmethod
    def from_row(cls, row: tuple | dict) -> ChangeAlert:
        if isinstance(row, dict):
            return cls.from_dict(row)
        cols = ["id","stock_code","alert_time","alert_type","z_score","magnitude","detail","priority","filtered","filter_reason"]
        return cls.from_dict(dict(zip(cols, row)))


@dataclass
class HotWordDict:
    """Hot word dictionary entry."""
    word: str
    last_seen: int = field(default_factory=_now)
    frequency: int = 1
    id: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> HotWordDict:
        return cls(
            id=d.get("id"),
            word=d["word"],
            frequency=d.get("frequency", 1),
            last_seen=d.get("last_seen", _now()),
        )

    @classmethod
    def from_row(cls, row: tuple | dict) -> HotWordDict:
        if isinstance(row, dict):
            return cls.from_dict(row)
        return cls.from_dict(dict(zip(["id","word","frequency","last_seen"], row)))


@dataclass
class HotWordEvent:
    """Hot word emergence event."""
    stock_code: str
    word: str
    event_time: int = field(default_factory=_now)
    tfidf_score: float = 0.0
    z_score: float = 0.0
    id: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> HotWordEvent:
        return cls(
            id=d.get("id"),
            stock_code=d["stock_code"],
            word=d["word"],
            tfidf_score=d.get("tfidf_score", 0.0),
            event_time=d.get("event_time", _now()),
            z_score=d.get("z_score", 0.0),
        )

    @classmethod
    def from_row(cls, row: tuple | dict) -> HotWordEvent:
        if isinstance(row, dict):
            return cls.from_dict(row)
        return cls.from_dict(dict(zip(["id","stock_code","word","tfidf_score","event_time","z_score"], row)))


@dataclass
class PushHistory:
    """Push notification history."""
    stock_code: str
    alert_id: int
    push_time: int = field(default_factory=_now)
    priority: str = "P2"
    content: str = ""
    status: str = "pending"
    id: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> PushHistory:
        return cls(
            id=d.get("id"),
            stock_code=d["stock_code"],
            alert_id=d["alert_id"],
            push_time=d.get("push_time", _now()),
            priority=d.get("priority", "P2"),
            content=d.get("content", ""),
            status=d.get("status", "pending"),
        )

    @classmethod
    def from_row(cls, row: tuple | dict) -> PushHistory:
        if isinstance(row, dict):
            return cls.from_dict(row)
        cols = ["id","stock_code","alert_id","push_time","priority","content","status"]
        return cls.from_dict(dict(zip(cols, row)))


@dataclass
class Comment:
    """Post comment & engagement snapshot."""
    snapshot_id: int
    post_id: str
    comment_count: int = 0
    forward_count: int = 0
    like_count: int = 0
    sentiment_avg: float = 0.0
    id: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Comment:
        return cls(
            id=d.get("id"),
            snapshot_id=d["snapshot_id"],
            post_id=d["post_id"],
            comment_count=d.get("comment_count", 0),
            forward_count=d.get("forward_count", 0),
            like_count=d.get("like_count", 0),
            sentiment_avg=d.get("sentiment_avg", 0.0),
        )

    @classmethod
    def from_row(cls, row: tuple | dict) -> Comment:
        if isinstance(row, dict):
            return cls.from_dict(row)
        cols = ["id","snapshot_id","post_id","comment_count","forward_count","like_count","sentiment_avg"]
        return cls.from_dict(dict(zip(cols, row)))


@dataclass
class Announcement:
    """Announcement snapshot."""
    snapshot_id: int
    stock_code: str
    ann_date: int
    ann_title: str = ""
    ann_type: str = ""
    is_new: int = 1
    id: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Announcement:
        return cls(
            id=d.get("id"),
            snapshot_id=d["snapshot_id"],
            stock_code=d["stock_code"],
            ann_title=d.get("ann_title", ""),
            ann_date=d.get("ann_date", 0),
            ann_type=d.get("ann_type", ""),
            is_new=d.get("is_new", 1),
        )

    @classmethod
    def from_row(cls, row: tuple | dict) -> Announcement:
        if isinstance(row, dict):
            return cls.from_dict(row)
        cols = ["id","snapshot_id","stock_code","ann_title","ann_date","ann_type","is_new"]
        return cls.from_dict(dict(zip(cols, row)))


@dataclass
class ContentWeight:
    """Content & source weights for feedback loop."""
    source: str
    keyword: str
    weight: float = 1.0
    preference_level: float = 1.0
    updated_at: int = field(default_factory=_now)
    id: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> ContentWeight:
        return cls(
            id=d.get("id"),
            source=d["source"],
            keyword=d["keyword"],
            weight=d.get("weight", 1.0),
            preference_level=d.get("preference_level", 1.0),
            updated_at=d.get("updated_at", _now()),
        )

    @classmethod
    def from_row(cls, row: tuple | dict) -> ContentWeight:
        if isinstance(row, dict):
            return cls.from_dict(row)
        cols = ["id","source","keyword","weight","preference_level","updated_at"]
        return cls.from_dict(dict(zip(cols, row)))


@dataclass
class UserPreference:
    """User notification preferences."""
    user_id: str
    p0_threshold: float = 3.0
    p1_threshold: float = 2.0
    cold_start_days: int = 28
    notify_immediate: int = 1
    notify_digest: int = 1
    updated_at: int = field(default_factory=_now)
    id: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> UserPreference:
        return cls(
            id=d.get("id"),
            user_id=d["user_id"],
            p0_threshold=d.get("p0_threshold", 3.0),
            p1_threshold=d.get("p1_threshold", 2.0),
            cold_start_days=d.get("cold_start_days", 28),
            notify_immediate=d.get("notify_immediate", 1),
            notify_digest=d.get("notify_digest", 1),
            updated_at=d.get("updated_at", _now()),
        )

    @classmethod
    def from_row(cls, row: tuple | dict) -> UserPreference:
        if isinstance(row, dict):
            return cls.from_dict(row)
        cols = ["id","user_id","p0_threshold","p1_threshold","cold_start_days","notify_immediate","notify_digest","updated_at"]
        return cls.from_dict(dict(zip(cols, row)))
