"""X 数据根目录解析（P5）与缓存有效性（P2）—— 2026-11-25 四方评审。

## P5：两条解析基准

同一份配置 ``x_source.root = "data/x_posts"`` 被两个调用方按不同基准解析：

    assistant_daily   PROJECT_DIR / root   → 仓库内，绝对
    report_generator  Path(root)           → 相对**进程 cwd**

从仓库根跑测试时，后者会读到生产 ``data/x_posts``；换个 cwd 就换一份数据。
修法是把解析收到 ``x_sources.resolve_root()`` 一处，调用方不再各自拼基准。

## P2：缓存只比条数

``assistant_daily`` 把 X 帖按 ``X_ID_BASE + i``（位置）编号注入 prompt，
LLM 回来也按编号引用。缓存命中时若只有条数相同、内容或顺序变了，
同一编号就指向另一条帖 —— **事实静默错配**，读者看不出来。

所以缓存有效性必须按内容指纹判定。这里把这条钉死。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import x_sources  # noqa: E402

_SCRIPT = PROJECT_ROOT / "scripts" / "assistant_daily.py"
_spec = importlib.util.spec_from_file_location("assistant_daily_mod2", _SCRIPT)
ad = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ad)


def _xp(*texts):
    """最小可用的 xp 形状（只要 link / content，指纹就用这两个字段）。"""
    return [
        {"link": f"https://x.com/i/status/{i}", "content": t, "source": "x"}
        for i, t in enumerate(texts)
    ]


# ── P5：root 解析与 cwd 无关 ────────────────────────────────────────


def test_relative_root_resolves_to_repo_root_not_cwd(tmp_path, monkeypatch):
    """相对 root 必须固定在仓库根，不能被 cwd 带跑。"""
    monkeypatch.chdir(tmp_path)  # 故意换到一个完全无关的目录
    got = x_sources.resolve_root("data/x_posts")
    assert got == PROJECT_ROOT / "data" / "x_posts"
    assert got.is_absolute()


def test_absolute_root_is_left_alone(tmp_path):
    """测试用 tmp_path 传绝对路径时必须原样生效，否则隔离就白做了。"""
    assert x_sources.resolve_root(tmp_path) == tmp_path
    assert x_sources.resolve_root(str(tmp_path)) == tmp_path


def test_posts_path_resolves_regardless_of_cwd(tmp_path, monkeypatch):
    """换 cwd 不改变 posts_path 指向的文件 —— P5 的直接回归点。"""
    before = x_sources.posts_path("data/x_posts", "2026-11-24", "PDD.US")
    monkeypatch.chdir(tmp_path)
    after = x_sources.posts_path("data/x_posts", "2026-11-24", "PDD.US")
    assert before == after
    assert before.name == "PDD.json"


def test_empty_root_stays_empty():
    """空 root 仍返回 None，load_x_posts 的降级路径不受影响。"""
    assert x_sources.resolve_root(None) is None
    assert x_sources.resolve_root("") is None


# ── P2：缓存有效性按内容判定 ───────────────────────────────────────


def test_cache_valid_when_input_identical():
    xp = _xp("a", "b", "c")
    cached = {"x_count": 3, "x_fingerprint": x_sources.fingerprint(xp)}
    ok, why = ad.cache_is_valid(cached, xp)
    assert ok, why


def test_cache_invalid_when_same_count_different_content():
    """**P2 的核心**：条数一样、内容换了一条 → 必须重算。

    否则缓存里「#900001 提到 X」会指向另一条帖。
    """
    cached_xp = _xp("旧的帖子一", "旧的帖子二", "旧的帖子三")
    fresh_xp = _xp("旧帖一（编辑过）", "旧的帖子二", "旧的帖子三")
    cached = {"x_count": 3, "x_fingerprint": x_sources.fingerprint(cached_xp)}
    ok, why = ad.cache_is_valid(cached, fresh_xp)
    assert not ok
    assert "内容" in why or "顺序" in why


def test_cache_invalid_when_order_swapped():
    """同一批帖换了顺序也必须失效 —— 位置编号同样会错位。"""
    a = _xp("一", "二", "三")
    b = list(reversed(a))
    cached = {"x_count": 3, "x_fingerprint": x_sources.fingerprint(a)}
    ok, _ = ad.cache_is_valid(cached, b)
    assert not ok


def test_legacy_cache_without_fingerprint_is_invalidated():
    """旧缓存没有指纹字段 → 一律重算。

    这是「X 输入口径变更」的安全网：改成只喂当日帖那天，
    旧的全量分析会自动作废，而不是拿旧编号去指新列表。
    """
    xp = _xp("a", "b")
    ok, why = ad.cache_is_valid({"x_count": 2}, xp)
    assert not ok
    assert "内容" in why or "顺序" in why


def test_cache_invalid_when_count_changes():
    xp = _xp("a", "b", "c")
    cached = {"x_count": 2, "x_fingerprint": x_sources.fingerprint(xp[:2])}
    ok, why = ad.cache_is_valid(cached, xp)
    assert not ok
    assert "条数" in why


def test_fingerprint_empty_is_stable():
    """空列表要有稳定指纹，不能每次都不一样（否则每轮都重算）。"""
    assert x_sources.fingerprint([]) == x_sources.fingerprint([])
