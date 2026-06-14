"""Regression tests for xueqiu-analyzer path used by opencli pre-fetch."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from src import crawler


def test_opencli_analyzer_path_uses_sibling_default(monkeypatch):
    monkeypatch.delenv("XUEQIU_ANALYZER_PATH", raising=False)
    sys.path[:] = [p for p in sys.path if "xueqiu-analyzer-skill/src" not in p]

    path = crawler._ensure_xueqiu_analyzer_path()

    # _DEFAULT_XA must derive from the crawler module's location (sibling repo),
    # not from a hardcoded user-home prefix — verify it's anchored to crawler.__file__.
    expected = str(Path(crawler.__file__).resolve().parent.parent.parent / "xueqiu-analyzer-skill" / "src")
    assert path == crawler._DEFAULT_XA == expected
    assert path in sys.path
    # ensure there's only one analyzer entry on sys.path (no leak from older hardcoded insertions)
    assert sum(1 for p in sys.path if p.endswith("xueqiu-analyzer-skill/src")) == 1


def test_opencli_analyzer_path_honors_env(monkeypatch, tmp_path):
    custom = tmp_path / "xa" / "src"
    monkeypatch.setenv("XUEQIU_ANALYZER_PATH", str(custom))
    sys.path[:] = [p for p in sys.path if p != str(custom)]

    path = crawler._ensure_xueqiu_analyzer_path()

    assert path == str(custom)
    assert sys.path[0] == str(custom)


def test_load_watchlist_uses_sibling_morning_brief_default(monkeypatch):
    captured = {}

    def fake_exists(path):
        captured["path"] = path
        return False

    monkeypatch.delenv("MORNING_BRIEF_DB", raising=False)
    monkeypatch.setattr(crawler.os.path, "exists", fake_exists)

    assert crawler.load_watchlist({}) == []
    expected = str(Path(crawler.__file__).resolve().parent.parent.parent / "morning-brief" / "data" / "morning-brief.db")
    assert captured["path"] == expected
