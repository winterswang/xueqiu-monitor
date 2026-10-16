"""Unit tests for scripts/backfill_fulltext.py 的通道迁移（2026-10-13）。

原实现走 `opencli browser <session> open` + 逐选择器 `extract --selector`。
那条路**不触发插件钩子**，既不受统一限频保护，也不进
`~/.opencli/adapter-calls.jsonl` 台账（上游 jackwener/opencli#2606）。
现改为复用 ``src.detail_fetcher.fetch_page_opencli``（即 `opencli web article`）。

这里钉的是**状态映射**：调用方靠 ``status.startswith("opencli")`` 决定
"桥断了就提前收工"，所以 ``opencli-error``（通道不可用）必须与 ``error``
（这一篇抓不到）严格区分 —— 混在一起要么空耗、要么早退。
"""

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent))

import backfill_fulltext as bf  # noqa: E402


@pytest.fixture
def stub_page(monkeypatch):
    """替掉 detail_fetcher 的抓取实现，由测试指定返回体，并记录被问过的 URL 与 kwargs。"""
    import src.detail_fetcher as df

    seen: list[str] = []
    kwargs_seen: list[dict] = []

    def _apply(result: dict) -> list[str]:
        def _fake(url: str, **kwargs) -> dict:
            seen.append(url)
            kwargs_seen.append(kwargs)
            return dict(result)

        monkeypatch.setattr(df, "fetch_page_opencli", _fake)
        return seen

    _apply.kwargs_seen = kwargs_seen  # type: ignore[attr-defined]
    return _apply


@pytest.mark.parametrize(
    ("page_status", "bridge_ok", "expected"),
    [
        ("ok", True, "ok"),
        ("empty", True, "empty"),
        ("junk", True, "empty"),
        ("error", True, "error"),          # 这一篇坏了但桥还在 → 继续下一篇
        ("error", False, "opencli-error"),  # 桥断了 → 调用方据此提前收工
    ],
)
def test_status_mapping(stub_page, monkeypatch, page_status, bridge_ok, expected):
    stub_page(
        {
            "title": "t",
            "content": "正文" if page_status == "ok" else "",
            "status": page_status,
        }
    )
    monkeypatch.setattr(bf, "_opencli_ok", lambda: bridge_ok)

    text, status = bf.fetch_fulltext("https://xueqiu.com/1/2")

    assert status == expected
    assert text == ("正文" if expected == "ok" else "")


def test_delegates_to_the_governed_channel(stub_page):
    """必须问 detail_fetcher 的 web article 实现，URL 原样传。"""
    seen = stub_page({"title": "t", "content": "x", "status": "ok"})

    bf.fetch_fulltext("https://xueqiu.com/S/SZ000933/411499999")

    assert seen == ["https://xueqiu.com/S/SZ000933/411499999"]


def test_asks_for_untruncated_content(stub_page, monkeypatch):
    """必须显式要 max_chars=None —— 否则正文被静默截到 8000 字。

    回归背景：本脚本改走 `fetch_page_opencli` 后，曾默默继承了详情页那套
    DETAIL_MAX_CHARS 上限（旧实现不截断），长文会以 status='ok' 存进
    post_fulltext 却只有前 8000 字。见 monitor issue #74 第 3 条。
    """
    stub_page({"title": "t", "content": "x", "status": "ok"})
    monkeypatch.setattr(bf, "_opencli_ok", lambda: True)

    bf.fetch_fulltext("https://xueqiu.com/1/2")

    assert stub_page.kwargs_seen == [{"max_chars": None}]


def test_long_content_is_not_truncated(stub_page, monkeypatch):
    stub_page({"title": "t", "content": "正" * 20000, "status": "ok"})
    monkeypatch.setattr(bf, "_opencli_ok", lambda: True)

    text, status = bf.fetch_fulltext("https://xueqiu.com/1/2")

    assert status == "ok"
    assert len(text) == 20000


def test_content_is_stripped(stub_page, monkeypatch):
    stub_page({"title": "t", "content": "\n  正文  \n", "status": "ok"})
    monkeypatch.setattr(bf, "_opencli_ok", lambda: True)

    text, status = bf.fetch_fulltext("https://xueqiu.com/1/2")

    assert (text, status) == ("正文", "ok")


def test_browser_subcommand_is_gone():
    """回归钉子：旧实现的 `browser open/extract` 不该再出现 —— 那条路不进治理。

    只查模块 docstring 之后的正文（docstring 里会解释性地提到旧通道）。
    """
    src = (SCRIPTS_DIR / "backfill_fulltext.py").read_text(encoding="utf-8")
    body = src.split('"""', 2)[2]

    assert '"browser"' not in body
    assert "settle" not in body


# ── issue #74 剩余两点：适配器硬闸 + 风控停轮 ─────────────────────────────


def _seed_candidates_db(tmp_path, n_posts: int = 2):
    """造一个带候选的临时 monitor.db（crawl_snapshots 一行、n_posts 个高赞帖）。"""
    import json
    import sqlite3
    import time as _time

    db = tmp_path / "monitor.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE crawl_snapshots "
        "(stock_code TEXT, crawl_time INTEGER, posts_data TEXT)"
    )
    posts = [
        {"link": f"https://xueqiu.com/1/{1000 + i}", "type": "discussion",
         "author": f"u{i}", "time": "2026-10-16 09:00",
         "content": "卡" * 250, "like_count": 50,
         "comment_count": 0, "forward_count": 0}
        for i in range(n_posts)
    ]
    conn.execute(
        "INSERT INTO crawl_snapshots VALUES ('SH600000', ?, ?)",
        (int(_time.time()), json.dumps(posts)),
    )
    conn.commit()
    conn.close()
    return db


def _run_main(monkeypatch, db, fetch_results, adapter_ok=True):
    """跑 main()：替掉闸门与抓取，返回 (按序抓过的 url, 退出码)。"""
    calls = []

    def _fake_fetch(url):
        calls.append(url)
        return fetch_results[0] if len(fetch_results) == 1 else fetch_results.pop(0)

    monkeypatch.setattr(bf, "_opencli_ok", lambda: True)
    monkeypatch.setattr(bf, "_article_adapter_ok", lambda: adapter_ok)
    monkeypatch.setattr(bf, "fetch_fulltext", _fake_fetch)
    monkeypatch.setattr(sys, "argv", ["backfill_fulltext.py", "--db", str(db)])
    return calls, bf.main()


def test_adapter_missing_fails_fast(tmp_path, monkeypatch):
    """适配器未注册：进门就明确失败(exit 3)，一次请求都不发(issue #74 第 1 条)。"""
    db = _seed_candidates_db(tmp_path)
    calls, code = _run_main(monkeypatch, db, [("正文", "ok")], adapter_ok=False)

    assert code == 3
    assert calls == []


def test_waf_block_stops_the_round(tmp_path, monkeypatch):
    """撞 WAF：立即停轮 + exit 4，剩余候选不再尝试(issue #74 第 2 条)。"""
    db = _seed_candidates_db(tmp_path, n_posts=2)
    calls, code = _run_main(monkeypatch, db, [("", "blocked-waf")])

    assert code == 4
    assert len(calls) == 1


def test_cooldown_stops_the_round(tmp_path, monkeypatch):
    """插件熔断冷却同样算风控：停轮 + exit 4。"""
    db = _seed_candidates_db(tmp_path, n_posts=2)
    calls, code = _run_main(monkeypatch, db, [("", "cooldown")])

    assert code == 4
    assert len(calls) == 1


def test_normal_round_still_exits_zero(tmp_path, monkeypatch):
    db = _seed_candidates_db(tmp_path, n_posts=2)
    calls, code = _run_main(
        monkeypatch, db, [("正" * 100, "ok"), ("文" * 100, "ok")]
    )

    assert code == 0
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        ({"title": "", "content": "", "status": "error", "blocked_waf": True},
         "blocked-waf"),
        ({"title": "", "content": "", "status": "cooldown", "channel": "cooldown"},
         "cooldown"),
    ],
)
def test_risk_signals_distinct_from_single_failure(
    stub_page, monkeypatch, page, expected
):
    """blocked-waf/cooldown 与「这一篇坏了」(error)必须可区分 —— 停轮逻辑靠它。"""
    stub_page(page)
    monkeypatch.setattr(bf, "_opencli_ok", lambda: True)

    assert bf.fetch_fulltext("https://xueqiu.com/1/2")[1] == expected
