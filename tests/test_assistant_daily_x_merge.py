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


def _sec(code="PDD", name="拼多多", raw=0, x_n=58, x_today=17, info=2):
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
        "info": info, "none": 0, "x_n": x_n, "x_today": x_today,
    }


def test_render_md_shows_x_count(tmp_path):
    """X 条数要出现在标题行——否则读者无法判断这段是否含英文源。"""
    out = tmp_path / "a.md"
    totals = {"posts": 0, "info": 2, "none": 0, "stocks": 1, "changed": 0}
    ad.render_md([("电商", [_sec()])], totals, "2026-11-24", {}, out)
    md = out.read_text(encoding="utf-8")
    assert "X 17 条（今日）" in md, md[:400]
    assert "另 41 条历史" in md, "只报总数会让读者以为今天新增了 58 条"
    assert "PDD 宣布新一轮回购" in md, "X 来源的事实必须进正文"


def test_render_md_omits_x_count_when_zero(tmp_path):
    """纯雪球的票不该多出一个「X 0 条」噪音。"""
    out = tmp_path / "b.md"
    totals = {"posts": 10, "info": 1, "none": 0, "stocks": 1, "changed": 0}
    s = _sec(x_n=0, x_today=0, raw=10)
    ad.render_md([("电商", [s])], totals, "2026-11-24", {}, out)
    md = out.read_text(encoding="utf-8")
    assert "X 0 条" not in md
    assert "X " not in md


def test_x_caliber_uses_calendar_day_not_fresh_flag():
    """口径钉死：days_ago < 1.0 才是「今日」，不能用 is_fresh(1.5 天)。

    2026-11-25 实测 APP.US：<1.0 → 17 条，is_fresh(<=1.5) → 18 条。
    差的那一条是昨天下午发的，算进「今日」就是谎报。
    """
    assert ad.x_caliber(49, 17) == " · X 17 条（今日） / 另 32 条历史"
    assert ad.x_caliber(5, 5) == " · X 5 条（今日）"      # 全是今日，不写「另 0 条」
    assert ad.x_caliber(0, 0) == ""                        # 没有 X 就完全不出现
    # 防御：今日数不该超过总数
    assert ad.x_caliber(3, 9) == " · X 9 条（今日）"


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


def test_render_md_distinguishes_no_new_from_broken(tmp_path):
    """`0 帖，无新内容` 让读者分不清「真没新帖」与「采集挂了」。

    2026-11-25 评审改成三层口径：当日 / 近7日 / X 今日。
    2076.HK 实测：当日 0、近7日 1 —— 写「0 帖」是事实，但只写 0 会让人以为
    这只票一周都没动静。
    """
    out = tmp_path / "c.md"
    totals = {"posts": 0, "info": 0, "none": 0, "stocks": 0, "changed": 0}
    s = {
        "code": "2076.HK", "name": "BOSS直聘", "raw": 0,
        "groups": {}, "change": "", "trend": "", "thread": "",
        "anchors": [], "open_questions": [],
        "info": 0, "none": 0, "x_n": 0, "x_today": 0,
        "week7": 1, "visible_n": 11,
    }
    ad.render_md([("消费", [s])], totals, "2026-11-24", {}, out)
    md = out.read_text(encoding="utf-8")
    assert "0 帖（当日）" in md
    assert "近7日 1 帖" in md
    assert "无新内容" not in md, "旧文案会让读者误以为采集坏了"
    assert "今日无新增帖" not in md, "已改为口径行，不再用这句话"


def test_render_md_falls_back_to_snapshot_count_only_when_week7_is_zero(tmp_path):
    """近7日有帖 = 采集正常，不必再补快照可见数；近7日也是 0 才需要交代。"""
    out = tmp_path / "d.md"
    totals = {"posts": 0, "info": 0, "none": 0, "stocks": 0, "changed": 0}
    base = {
        "code": "X.HK", "name": "测试", "raw": 0, "groups": {},
        "change": "", "trend": "", "thread": "",
        "anchors": [], "open_questions": [],
        "info": 0, "none": 0, "x_n": 0, "x_today": 0, "visible_n": 11,
    }
    ad.render_md([("消费", [dict(base, week7=2)])], totals, "2026-11-24", {}, out)
    assert "快照可见" not in out.read_text(encoding="utf-8")

    ad.render_md([("消费", [dict(base, week7=0)])], totals, "2026-11-24", {}, out)
    txt = out.read_text(encoding="utf-8")
    assert "近7日 0 帖" in txt
    assert "快照可见 11 条历史帖" in txt


def test_snapshot_visible_count_does_not_change_post_count(tmp_path):
    """反向验证的代码级护栏：可见数只用于文案，不得影响 fetch 的帖数口径。

    FA 实测：APP.US 11-24 快照 11 条，发布时间 2026-07-10~11-22，
    11-24 发布的 0 条 —— 若拿可见数当帖数，就是把 4 个月前的帖当新闻。
    """
    src = _SCRIPT.read_text(encoding="utf-8")
    # snapshot_visible_count 不得出现在 fetch_day_posts 内部或其调用链上
    i_fn = src.index("def snapshot_visible_count(")
    i_next = src.index("def ", i_fn + 10)
    body = src[i_fn:i_next]
    assert "SELECT stock_code" not in body, "该函数不该返回帖数据，只返回条数"
    assert "fetch_day_posts(" not in body, "不得调用 fetch_day_posts（那会改变帖数口径）"
    # 它只能被渲染侧用（visible_n），不能进 analyze
    assert "snapshot_visible_count(" in src[i_fn + len(body):], "应被调用"
    assert "p['id']" not in body
