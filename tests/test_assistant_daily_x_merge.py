"""X 舆情并入助理日报 —— 两层失效的回归测试（2026-11-25）。

## 事故

11-24 的 `2026-11-24-assistant.md` 里 `[X]` 标记 **0 次**，而 `data/x_posts/2026-11-24/`
有 23 个文件 1184 条帖（22/23 status=ok）。原因有两层，缺一不可：

1. `work()` 里 `if not kept: return ... "empty"` —— **雪球无帖就提前返回，X 根本没被读**。
   美股在雪球中文社区当天 0 帖很常见（11-24 有 22 只如此，X 侧各有 54~58 条）。
2. 渲染时 `by_id` 只索引雪球帖，X 帖用的是虚拟 id（`X_ID_BASE + i`）不在其中，
   `rep not in by_id → continue` 会把 X 来源的事实**全部丢掉**。

只修第 1 层不够：X 数据读进来了，渲染仍然会静默丢弃。

## 这两个测试守什么

- `test_render_md_shows_x_count`：渲染层不再吞掉 x_n（读者能看出这段含英文源）
- `test_x_read_precedes_empty_return`：静态护栏——X 读取必须在 `return ... "empty"` 之前
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_SCRIPT = PROJECT_ROOT / "scripts" / "assistant_daily.py"
_spec = importlib.util.spec_from_file_location("assistant_daily_mod", _SCRIPT)
ad = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ad)


def _sec(code="PDD", name="拼多多", raw=0, x_n=58, info=2):
    """一只「雪球 0 帖 + X 有帖」的票，事实全部来自 X（虚拟 id）。"""
    xid = ad.X_ID_BASE + 0
    return {
        "code": code, "name": name, "raw": raw,
        "groups": {"一手": [{
            "fact": "PDD 宣布新一轮回购", "kind": "一手",
            "rep": xid, "sources": [xid], "_n": 1, "_tier": ad.TIER_PASSERBY,
            "_author": "someone", "_link": "https://x.com/i/status/1",
            "_ids": [xid],
        }]},
        "change": "", "trend": "", "thread": "",
        "anchors": [], "open_questions": [],
        "info": info, "none": 0, "x_n": x_n,
    }


def test_render_md_shows_x_count(tmp_path):
    """X 条数要出现在标题行——否则读者无法判断这段是否含英文源。"""
    out = tmp_path / "a.md"
    totals = {"posts": 0, "info": 2, "none": 0, "stocks": 1, "changed": 0}
    ad.render_md([("电商", [_sec()])], totals, "2026-11-24", {}, out)
    md = out.read_text(encoding="utf-8")
    assert "X 58 条" in md, md[:400]
    assert "PDD 宣布新一轮回购" in md, "X 来源的事实必须进正文"


def test_render_md_omits_x_count_when_zero(tmp_path):
    """纯雪球的票不该多出一个「X 0 条」噪音。"""
    out = tmp_path / "b.md"
    totals = {"posts": 10, "info": 1, "none": 0, "stocks": 1, "changed": 0}
    s = _sec(x_n=0, raw=10)
    ad.render_md([("电商", [s])], totals, "2026-11-24", {}, out)
    md = out.read_text(encoding="utf-8")
    assert "X 0 条" not in md


def test_x_read_precedes_empty_return():
    """静态护栏：`load_x_posts` 必须出现在 `"empty"` 返回之前。

    这是本次事故的第一层——顺序反了，X 就永远读不到。
    """
    src = _SCRIPT.read_text(encoding="utf-8")
    i_load = src.index("load_x_posts")
    i_empty = src.index('dict(EMPTY), "empty"')
    assert i_load < i_empty, (
        "X 读取必须在「雪球空 → empty」之前；否则雪球无帖的美股会整只丢掉 X 数据"
    )


def test_render_index_includes_x_virtual_ids():
    """静态护栏：渲染前要把 X 帖补进 by_id，否则 X 事实被 continue 掉。"""
    src = _SCRIPT.read_text(encoding="utf-8")
    assert "by_id.setdefault(X_ID_BASE" in src, (
        "渲染层必须把 X 帖按 X_ID_BASE+i 补进 by_id —— 只修读取、不修渲染等于没修"
    )
