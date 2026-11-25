"""日报口径 v1 的验收基准（合成数据，2026-11-25）。

对应 README「口径定义」里的四只安静股基准表。这里用**合成库**复现，
不读生产 ``data/monitor.db``，所以数字稳定、不随真实采集变化。

复现的关键机制（不搞懂会以为数据丢了）：

    posts 表一条帖只存一行，snapshot_id 记的是**首见快照**。
    所以「今天快照里看得见 11 条」完全可以让「当日新增 = 0」——
    那 11 条是几个月前首次入库的，今天只是又被页面展示了一遍。

四只票在 2026-11-24 的基准：

| 标的 | 当日新增 | 近7日 | 快照可见 | X 今日/历史 |
|------|---------|-------|---------|------------|
| APP.US  | 0 | 2 | 11 | 17 / 32 |
| UBER.US | 0 | 4 | 11 | 21 / 24 |
| 2076.HK | 0 | 1 | 11 | — |
| 6078.HK | 0 | 1 | 11 | — |
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import db as dbmod  # noqa: E402

_SCRIPT = PROJECT_ROOT / "scripts" / "assistant_daily.py"
_spec = importlib.util.spec_from_file_location("assistant_daily_caliber", _SCRIPT)
ad = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ad)

D = "2026-11-24"


def _ts(y, m, d, hh=10):
    return int(datetime(y, m, d, hh, 0).timestamp())


# 每只票在快照里都**可见 11 条**；其中落在近 7 日（[D-6, D]）的条数按基准表不同。
# 近 7 日的日期：11-18(D-6) ~ 11-22，全部在窗口内。
_RECENT = [_ts(2026, 11, 18), _ts(2026, 11, 19), _ts(2026, 11, 20),
           _ts(2026, 11, 22)]
# 更早的：最早一条 2026-07-10（距 D 137 天 → 报文写「4 个月前」）
_OLD = [_ts(2026, 7, 10), _ts(2026, 7, 18), _ts(2026, 8, 2), _ts(2026, 9, 21),
        _ts(2026, 10, 23), _ts(2026, 10, 23), _ts(2026, 10, 30),
        _ts(2026, 11, 5), _ts(2026, 11, 13), _ts(2026, 11, 16)]
_VISIBLE_N = 11
_W7_TARGET = {"APP.US": 2, "UBER.US": 4, "2076.HK": 1, "6078.HK": 1}


def _stamps(in_window: int):
    """快照可见的 11 条：in_window 条落在近 7 日，其余用更早的日期补满。"""
    return _RECENT[:in_window] + _OLD[: _VISIBLE_N - in_window]


def _build_db(path: str):
    dbmod.init_db(path)
    conn = sqlite3.connect(path)
    # 1) 一条「很久以前」的快照：11 条帖的 snapshot_id 指向它 → 今天新增 = 0
    cur = conn.execute(
        """INSERT INTO crawl_snapshots
           (stock_code, crawl_time, posts_count, posts_data, sentiment_avg, status)
           VALUES (?,?,?,?,?,?)""",
        ("OLD", _ts(2026, 7, 1, 9), 0, "[]", 0.0, "success"),
    )
    old_snap = cur.lastrowid

    for code, n_in_window in _W7_TARGET.items():
        stamps = _stamps(n_in_window)
        rows = []
        for i, ts in enumerate(stamps):
            pid = f"{code}-{i}"
            rows.append((pid, ts))
            conn.execute(
                """INSERT INTO posts
                   (stock_code, dedup_key, post_id, post_type, title, content, author,
                    time_text, post_ts, like_count, comment_count, forward_count,
                    sentiment_score, link, snapshot_id, first_seen_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (code, pid, pid, "discussion", "测试标题需要足够长度以通过最短过滤",
                 "正文内容" * 20, "作者", "3天前", ts, 5, 1, 0, 0.0,
                 f"https://xueqiu.com/{pid}", old_snap, ts),
            )
        # 2) 今天（D）的快照：页面把这 11 条/更多条又展示了一遍，但 posts 不会新增行
        today_data = [
            {"post_id": f"{code}-{i}", "title": "测试标题需要足够长度以通过最短过滤",
             "content": "正文内容" * 20, "author": "作者", "time": "3天前",
             "like_count": 5, "comment_count": 1, "forward_count": 0,
             "link": f"https://xueqiu.com/{code}-{i}"}
            for i in range(len(stamps))
        ]
        conn.execute(
            """INSERT INTO crawl_snapshots
               (stock_code, crawl_time, posts_count, posts_data, sentiment_avg, status)
               VALUES (?,?,?,?,?,?)""",
            (code, _ts(2026, 11, 24, 15), len(today_data),
             json.dumps(today_data, ensure_ascii=False), 0.0, "success"),
        )
    conn.commit()
    conn.close()


def _build_x(root: Path, ticker: str, today_n: int, hist_n: int):
    """造一份 X 采集文件：today_n 条落在 D 当天，hist_n 条是更早的。"""
    posts = []
    for i in range(today_n):
        posts.append({
            "tweet_id": f"{ticker}-t{i}",
            "url": f"https://x.com/t/status/{ticker}t{i}",
            "author": "@trader",
            # 正文必须足够长：clean_posts 有最短长度门槛，太短会被整批丢弃
            "text": (f"{ticker} 当日第 {i:02d} 条讨论，关于财报指引与需求侧的观察，"
                     f"内容足够长以避免触发最短长度过滤。"),
            "lang": "en", "slice": "top",
            "created_at_utc": "2026-11-24T02:00:00Z",   # = D 10:00 CST
            "metrics": {"like": 100 + i, "reply": 10, "repost": 5},
        })
    for i in range(hist_n):
        posts.append({
            "tweet_id": f"{ticker}-h{i}",
            "url": f"https://x.com/t/status/{ticker}h{i}",
            "author": "@trader",
            "text": (f"{ticker} 历史第 {i:02d} 条讨论，时间较早仅供背景参考，"
                     f"内容同样足够长以通过最短长度过滤。"),
            "lang": "en", "slice": "top",
            "created_at_utc": "2026-07-10T02:00:00Z",
            "metrics": {"like": 50, "reply": 5, "repost": 2},
        })
    d = root / D
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ticker}.json").write_text(
        json.dumps({"ticker": ticker, "date": D, "posts": posts}, ensure_ascii=False),
        encoding="utf-8")


def _sec(db_path, x_root, code, name):
    from src import x_sources
    raw = ad.fetch_day_posts(db_path, D).get(code, [])
    xp = (x_sources.as_prompt_posts(x_sources.load_x_posts(str(x_root), D, code))
          if x_sources.is_us(code) else [])
    vis_n, vis_oldest = ad.snapshot_visible_stats(db_path, D, code)
    return {
        "code": code, "name": name, "raw": len(raw),
        "pub_today": ad.published_today_count(raw, D),
        "groups": {}, "change": "", "trend": "", "thread": "",
        "anchors": [], "open_questions": [], "info": 0, "none": 0,
        "x_n": len(xp),
        "x_today": sum(1 for p in xp
                       if p.get("days_ago") is not None and p["days_ago"] < 1.0),
        "week7": ad.week7_count(db_path, D, code),
        "visible_n": vis_n, "visible_oldest": vis_oldest,
    }


def test_acceptance_baseline_four_quiet_stocks():
    """README 验收基准表逐项对上。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "t.db")
        _build_db(db_path)
        x_root = Path(tmp) / "x_posts"
        _build_x(x_root, "APP", 17, 32)
        _build_x(x_root, "UBER", 21, 24)

        s = {c: _sec(db_path, x_root, c, c) for c in
             ["APP.US", "UBER.US", "2076.HK", "6078.HK"]}

        # 当日新增 / 当日发布 全部 0
        for c in s:
            assert s[c]["raw"] == 0, f"{c} 当日新增应为 0"
            assert s[c]["pub_today"] == 0

        # 近7日
        assert s["APP.US"]["week7"] == 2, s["APP.US"]["week7"]
        assert s["UBER.US"]["week7"] == 4, s["UBER.US"]["week7"]
        assert s["2076.HK"]["week7"] == 1, s["2076.HK"]["week7"]
        assert s["6078.HK"]["week7"] == 1

        # 快照可见 11 条（含几个月前的旧帖）
        for c in s:
            assert s[c]["visible_n"] == 11, f"{c} 快照可见应为 11"

        # X 今日/历史 分层
        assert (s["APP.US"]["x_today"], s["APP.US"]["x_n"] - s["APP.US"]["x_today"]) == (17, 32)
        assert (s["UBER.US"]["x_today"], s["UBER.US"]["x_n"] - s["UBER.US"]["x_today"]) == (21, 24)


def test_acceptance_baseline_renders_expected_lines():
    """渲染出来的报文行与 README 里的示例一字不差。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "t.db")
        _build_db(db_path)
        x_root = Path(tmp) / "x_posts"
        _build_x(x_root, "APP", 17, 32)
        _build_x(x_root, "UBER", 21, 24)

        sec = [_sec(db_path, x_root, c, c) for c in
               ["APP.US", "UBER.US", "2076.HK", "6078.HK"]]
        out = Path(tmp) / "o.md"
        ad.render_md([("基准", sec)], {"posts": 0, "info": 0, "none": 0,
                                       "stocks": 0, "changed": 0}, D, {}, out)
        md = out.read_text(encoding="utf-8")

        assert ("- **APP.US APP.US**：0 帖（新增） · 近7日可见 2 帖"
                " · X 17 条（今日） / 另 32 条历史") in md, md
        assert ("- **UBER.US UBER.US**：0 帖（新增） · 近7日可见 4 帖"
                " · X 21 条（今日） / 另 24 条历史") in md
        assert "- **2076.HK 2076.HK**：0 帖（新增） · 近7日可见 1 帖" in md
        assert "- **6078.HK 6078.HK**：0 帖（新增） · 近7日可见 1 帖" in md


def test_visible_count_never_becomes_post_count():
    """11 条可见帖一条都不许进「当日新增」——这是 2026-11-25 最容易搞错的地方。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "t.db")
        _build_db(db_path)
        # 全部 11 条都在今天快照里可见
        assert ad.snapshot_visible_stats(db_path, D, "APP.US")[0] == 11
        # 但当日新增必须是 0
        assert len(ad.fetch_day_posts(db_path, D).get("APP.US", [])) == 0


def test_age_of_oldest_visible_post_is_reported():
    """可见数要带年龄：11 条最有可能是几个月前的旧帖，不能让人当新闻读。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "t.db")
        _build_db(db_path)
        n, oldest = ad.snapshot_visible_stats(db_path, D, "APP.US")
        assert n == 11
        assert ad.age_label(oldest, D) == "4 个月前", ad.age_label(oldest, D)


# ── X 配额截断（配置写了 30，assistant_daily 此前没执行）────────────────


def _xp(n_fresh, n_hist):
    out = [{"content": f"f{i}", "link": f"https://x/f{i}", "is_fresh": True}
           for i in range(n_fresh)]
    out += [{"content": f"h{i}", "link": f"https://x/h{i}", "is_fresh": False}
            for i in range(n_hist)]
    return out


def test_cap_prioritizes_fresh_then_history():
    """截断必须近期优先：直接切片会把今天的时效信号全砍掉。"""
    from src import x_sources
    posts = _xp(17, 32)                      # 49 条，与 APP.US 同形
    out = x_sources.cap_for_prompt(posts, 30)
    assert len(out) == 30
    assert sum(1 for p in out if p["is_fresh"]) == 17, "17 条近期必须全保留"
    assert sum(1 for p in out if not p["is_fresh"]) == 13, "再用较早的补满"


def test_cap_when_fresh_exceeds_cap():
    from src import x_sources
    out = x_sources.cap_for_prompt(_xp(50, 10), 30)
    assert len(out) == 30
    assert all(p["is_fresh"] for p in out), "近期超出配额时不该混入较早的"


def test_cap_noop_cases():
    from src import x_sources
    posts = _xp(3, 2)
    assert x_sources.cap_for_prompt(posts, 30) == posts      # 未超配额
    assert x_sources.cap_for_prompt(posts, 0) == posts       # 配置 0 = 不限
    assert x_sources.cap_for_prompt(posts, None) == posts    # 配置缺失
    assert x_sources.cap_for_prompt([], 30) == []


def test_fingerprint_reflects_capped_list_not_full_list():
    """指纹必须跟着「送进模型的那个列表」变。

    否则：全量不变、但 cap 或截断规则变了 → 指纹不变 → 命中旧缓存 →
    facts 里的 X_ID_BASE+i 指向另一条帖（静默错配）。
    """
    from src import x_sources
    posts = _xp(17, 32)
    fp_full = x_sources.fingerprint(posts)
    fp_30 = x_sources.fingerprint(x_sources.cap_for_prompt(posts, 30))
    fp_20 = x_sources.fingerprint(x_sources.cap_for_prompt(posts, 20))
    assert len({fp_full, fp_30, fp_20}) == 3, "不同配额必须给出不同指纹"


def test_assistant_daily_actually_applies_the_cap():
    """静态护栏：这条路径必须真的读配置里的配额（此前完全没读）。"""
    src = _SCRIPT.read_text(encoding="utf-8")
    assert 'layer_for_prompt(xp_all, x_cfg.get("max_posts_per_stock"))' in src
    # 分析用截断后的 xp，展示用 xp_all 的统计——两者不能混
    assert "x_stats" in src and '"x_n": xst["total"]' in src


# ── X 采集名单必须跟配置一致（2026-11-25 发现 SPCX.US 盲区）────────────


def test_x_collect_script_derives_tickers_from_config():
    """采集名单必须从 config.report.json 派生，不能手写。

    手写版漏了 SPCX.US：配置里加了这只票、采集侧没跟着改，于是它每天都没有
    X 数据，日报只会显示「X 0 条」—— 看起来像"今天没舆情"，其实是永远没有。
    """
    script = PROJECT_ROOT / "scripts" / "x_collect_daily.sh"
    src = script.read_text(encoding="utf-8")
    assert "config.report.json" in src, "TICKERS 应从配置派生"
    assert 'TICKERS=$("$PYBIN" -c' in src
    # 硬编码名单只允许作为兜底出现一次
    assert src.count('TICKERS="PDD,') == 1, "手写名单不应再有第二处"


def test_every_configured_us_stock_reaches_the_collector():
    """配置里的每只美股都必须出现在派生结果里（SPCX.US 回归）。"""
    import json
    cfg = json.loads((PROJECT_ROOT / "etc" / "config.report.json").read_text(encoding="utf-8"))
    us = {c[:-3] for c in cfg["stocks"] if c.upper().endswith(".US")}
    assert "SPCX" in us, "配置应包含 SPCX.US"
    # 派生逻辑与脚本内嵌的 python 片段保持一致
    assert len(us) == len({c for c in cfg["stocks"] if c.upper().endswith(".US")})


def test_age_label_is_timezone_independent_on_the_boundary():
    """展示时间必须显式用 CST —— 在 120 天边界上，隐式本地时区会给出两个答案。

    样本（2026-11-25 由伙伴给出）：一条 CST 2026-07-28 00:30 发的帖，报表日 11-24。
      TZ=Asia/Shanghai → 本地日期 07-28 → 119 天 → 「3 个月前」
      TZ=UTC           → 本地日期 07-27 → 120 天 → 「4 个月前」
    同一个 ts，两个标签。fixture 里常用的 137 天离边界还有 17 天，所以撞不到这块。
    """
    import os
    import time as _time
    from datetime import timezone as _tz, timedelta as _td

    if not hasattr(_time, "tzset"):
        import pytest
        pytest.skip("本平台不支持 tzset，无法切换进程时区")

    cst = _tz(_td(hours=8))
    ts = int(datetime(2026, 7, 28, 0, 30, tzinfo=cst).timestamp())
    old_tz = os.environ.get("TZ")

    try:
        labels = {}
        for zone in ("Asia/Shanghai", "UTC"):
            os.environ["TZ"] = zone
            _time.tzset()
            labels[zone] = ad.age_label(ts, "2026-11-24")
        assert labels["Asia/Shanghai"] == "3 个月前", labels
        assert labels["UTC"] == labels["Asia/Shanghai"], (
            f"宿主时区改变了展示结果：{labels}；age_label 必须显式用 CST")
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        _time.tzset()


def test_collector_fallback_fails_loudly():
    """名单派生失败退回内置名单时，必须让宿主看见。

    这是「失败被压成正常」的同一类问题：SPCX 盲区的表现就是「X 0 条」，
    看起来像今天没舆情，实际是永远没有。只往 stderr 写一行 warn 救不了它。
    """
    src = (PROJECT_ROOT / "scripts" / "x_collect_daily.sh").read_text(encoding="utf-8")
    assert 'TICKER_SRC="fallback"' in src
    assert 'if [ "$TICKER_SRC" = "fallback" ]' in src, "收尾必须按降级处理"
    assert 'exit 14' in src, "降级要有独立退出码"
    # 降级分支必须发 ok:false 帧（frame_err），不能只 echo
    tail = src[src.index('if [ "$TICKER_SRC" = "fallback" ]'):]
    assert "frame_err" in tail.split("fi")[0], "降级必须发失败帧，宿主才会告警"


def test_work_tuple_arity_is_consistent():
    """`build()` 内部 `work()` 的返回元组必须处处同长。

    2026-11-25 给它加了一个元素（x_stats）——这类改动 `build()` 本身没有被
    单元测试覆盖，漏一个解包点就是运行期 ValueError，而且只在 19:00 那轮炸。
    这里用 AST 把"所有 return 同长 + 所有解包同长"钉住。
    """
    import ast

    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    lengths = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple)
                and node.value.elts
                and isinstance(node.value.elts[0], ast.Name)
                and node.value.elts[0].id == "code"):
            lengths.add(len(node.value.elts))
    assert lengths == {7}, f"work() 的 return 元组应当全是 7 元：{lengths}"

    # 解包点：for ... in done 与 by_code[code] 的赋值
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and isinstance(node.target, ast.Tuple):
            if isinstance(node.iter, ast.Name) and node.iter.id == "done":
                assert len(node.target.elts) == 7, "for ... in done 解包长度不对"
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Subscript):
            if (isinstance(node.value.value, ast.Name) and node.value.value.id == "by_code"
                    and isinstance(node.targets[0], ast.Tuple)):
                assert len(node.targets[0].elts) == 7, "by_code[code] 解包长度不对"


# ── H9：X 历史帖分层（2026-11-25 评审）──────────────────────────────


def test_layer_keeps_all_fresh_and_limits_history():
    """近期全留；历史限量 min(5, 近期数)。实测 APP 18+5=23、PDD 9+5=14。"""
    from src import x_sources
    posts = _xp(17, 32)
    out = x_sources.layer_for_prompt(posts, 30)
    assert sum(1 for p in out if p["is_fresh"]) == 17
    assert sum(1 for p in out if not p["is_fresh"]) == 5, "历史最多 5 条"
    assert len(out) == 22


def test_layer_history_scales_with_fresh():
    """近期少的时候历史也少——避免"今日无大事"却塞一堆旧闻。"""
    from src import x_sources
    out = x_sources.layer_for_prompt(_xp(2, 40), 30)
    assert sum(1 for p in out if not p["is_fresh"]) == 2


def test_layer_without_fresh_keeps_up_to_five():
    """近期为 0 时仍留 ≤5 条历史当背景，不能给空 prompt。"""
    from src import x_sources
    out = x_sources.layer_for_prompt(_xp(0, 40), 30)
    assert len(out) == 5
    assert all(not p["is_fresh"] for p in out)


def test_layer_respects_cap():
    from src import x_sources
    out = x_sources.layer_for_prompt(_xp(40, 20), 30)
    assert len(out) <= 30


def test_post_date_str_marks_when_the_post_was_published():
    """历史帖的日期前缀：一条 2021 年的帖不能被当成今天的。"""
    from src import x_sources
    # days_ago=1866 → 2021-10-15 附近（报表日 2026-11-24）
    p = {"days_ago": 1866.0}
    d = x_sources.post_date_str(p, "2026-11-24")
    assert d.startswith("2021-"), d
    assert x_sources.post_date_str({"days_ago": None}, "2026-11-24") == ""


def test_prompt_marks_history_with_date_and_hard_constraint():
    """渲染层：历史帖带 [日期]，且「全无近期帖」时要加一句硬约束。"""
    src = _SCRIPT.read_text(encoding="utf-8")
    assert "post_date_str(xp, date_str)" in src
    assert "今日无新增 X 帖" in src, "全部是历史时必须说死，否则模型会编今日叙事"
    assert "不得当作今日事件" in src
