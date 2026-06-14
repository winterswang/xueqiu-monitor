"""Regression tests for xueqiu-analyzer path used by opencli pre-fetch."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from src import crawler


def test_opencli_analyzer_path_uses_sibling_default(monkeypatch):
    monkeypatch.delenv("XUEQIU_ANALYZER_PATH", raising=False)
    hardcoded = str(Path.home() / "code" / "claude_code" / "xueqiu-analyzer-skill" / "src")
    sys.path[:] = [p for p in sys.path if p != hardcoded and "xueqiu-analyzer-skill/src" not in p]

    path = crawler._ensure_xueqiu_analyzer_path()

    assert path == crawler._DEFAULT_XA
    assert path in sys.path
    assert hardcoded not in sys.path


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
