"""熔断冷却期**不回退 legacy 浏览器序列**。

熔断器拦下调用时，站点是「正因为被打才开的冷却」；而 legacy 序列走
`opencli browser …` —— 不经插件钩子、**不限速**。回退回去等于继续打它，
正是熔断器要停止的事。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import detail_fetcher as df  # noqa: E402


def test_in_cooldown_skips_the_legacy_browser_fallback(monkeypatch):
    monkeypatch.setattr(df, "_article_adapter_available", lambda: False)
    monkeypatch.setattr(df, "in_cooldown", lambda *_a, **_k: True)
    called = []
    monkeypatch.setattr(
        df, "_fetch_page_opencli_legacy", lambda *a, **k: called.append(1)
    )

    out = df.fetch_page_opencli("https://xueqiu.com/1/2")

    assert out["status"] == "cooldown"
    assert out["content"] == ""
    assert called == [], "冷却期内绝不能走 legacy 浏览器序列（那条通道不限速）"


def test_not_in_cooldown_still_uses_the_legacy_fallback(monkeypatch):
    """没冷却时行为不变 —— 这条判断只该在熔断期生效。"""
    monkeypatch.setattr(df, "_article_adapter_available", lambda: False)
    monkeypatch.setattr(df, "in_cooldown", lambda *_a, **_k: False)
    monkeypatch.setattr(
        df, "_fetch_page_opencli_legacy", lambda *a, **k: {"status": "legacy-called"}
    )

    assert df.fetch_page_opencli("https://xueqiu.com/1/2")["status"] == "legacy-called"
