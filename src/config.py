"""xueqiu-monitor: configuration management"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# Load .env if python-dotenv is available
try:
    from dotenv import load_dotenv
    _DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"
    if _DOTENV_PATH.exists():
        load_dotenv(_DOTENV_PATH)
except ImportError:
    pass


# Default paths: sibling directories relative to this project root
# Overridable via env vars or config.json
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
DEFAULT_CONFIG = {
    "db_path": "data/monitor.db",
    "watchlist_path": "../morning-brief/data/watchlist.json",
    "crawler": {
        "timeout_seconds": 30,
        "max_retries": 0,         # no immediate retry, retry on next schedule
        "concurrency": 1,          # sequential (Playwright single-process)
        "whitelist": [],            # 非空时仅爬取列表中的股票，空=全量
        "xueqiu_analyzer_path": os.environ.get(
            "XUEQIU_ANALYZER_PATH",
            str(Path(_PROJECT_ROOT).parent / "xueqiu-analyzer-skill" / "src"),
        ),
        "morning_brief_db": os.environ.get(
            "MORNING_BRIEF_DB",
            str(Path(_PROJECT_ROOT).parent / "morning-brief" / "data" / "morning-brief.db"),
        ),
    },
    "detector": {
        "z_score_window_days": 14,
        "z_score_threshold": 2.0,
        "tfidf_min_df": 2,
        "tfidf_max_df": 0.8,
        "tfidf_ngram_range": [1, 2],
    },
    "filter": {
        "ad_keywords": ["开户", "佣金", "万一", "万0.5", "低手续费", "加群", "荐股", "内幕"],
        "duplicate_similarity_threshold": 0.85,
        "short_post_threshold": 20,
        "p0_z_threshold": 5.0,
        "p1_z_threshold": 3.0,
    },
    "notification": {
        "webhook_url": "",         # set via env FEISHU_WEBHOOK_URL
        "push_timeout": 5,         # seconds
        "max_retries": 2,
        "pending_path": "/tmp/xueqiu_monitor_pending.json",
        "mode": "auto",            # auto | lark_cli | file
        "lark_chat_id": "",        # feishu group chat id (required for lark_cli mode)
    },
    "cold_start": {
        "enabled": True,
        "days": 7,
        "min_data_points": 7,      # minimum data points before Z-score is meaningful
    },
    "feedback": {
        "useful_delta": 0.1,
        "useless_delta": -0.1,
        "decay_days": 7,
        "decay_rate": 0.05,
        "weight_floor": 0.3,
    },
    "schedule": {
        "interval_hours": 4,
    },
}


@dataclass
class Config:
    """Runtime configuration loaded from JSON + env overrides."""
    db_path: str = "data/monitor.db"
    watchlist_path: str = ""
    crawler: dict = field(default_factory=lambda: DEFAULT_CONFIG["crawler"].copy())
    detector: dict = field(default_factory=lambda: DEFAULT_CONFIG["detector"].copy())
    filter: dict = field(default_factory=lambda: DEFAULT_CONFIG["filter"].copy())
    notification: dict = field(default_factory=lambda: DEFAULT_CONFIG["notification"].copy())
    cold_start: dict = field(default_factory=lambda: DEFAULT_CONFIG["cold_start"].copy())
    feedback: dict = field(default_factory=lambda: DEFAULT_CONFIG["feedback"].copy())
    schedule: dict = field(default_factory=lambda: DEFAULT_CONFIG["schedule"].copy())

    @classmethod
    def from_file(cls, path: str) -> Config:
        """Load config from JSON file, merge env overrides."""
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        p = Path(path)
        if p.exists():
            user_cfg = json.loads(p.read_text())
            _deep_merge(cfg, user_cfg)
        # Environment variable overrides
        cfg["notification"]["webhook_url"] = os.environ.get(
            "FEISHU_WEBHOOK_URL", cfg.get("notification", {}).get("webhook_url", "")
        )
        if "XUEQIU_ANALYZER_PATH" in os.environ:
            cfg.setdefault("crawler", {})["xueqiu_analyzer_path"] = os.environ["XUEQIU_ANALYZER_PATH"]
        if "MORNING_BRIEF_DB" in os.environ:
            cfg.setdefault("crawler", {})["morning_brief_db"] = os.environ["MORNING_BRIEF_DB"]
        if "LARK_CHAT_ID" in os.environ:
            cfg.setdefault("notification", {})["lark_chat_id"] = os.environ["LARK_CHAT_ID"]
        return cls(**cfg)

    @classmethod
    def default(cls) -> Config:
        """Default config (mostly for tests)."""
        return cls(**copy.deepcopy(DEFAULT_CONFIG))


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
