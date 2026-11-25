"""`build()` 端到端冒烟：不调 LLM，但把整条编排链路走一遍。

## 为什么需要它

2026-11-25 的 `published_today_count(raw, ...)` 引用了一个只存在于内层函数
里的变量。所有单元测试都绿——因为它们测的是 `render_md`、`render_html`、
`layer_for_prompt` 这些**纯函数**，而 `build()` 这段编排代码从没被执行过。

结果：19:00 日报一跑就 NameError，整个产物出不来。

这条测试把 LLM 换成桩，其余原样跑：
取数 → 分层 → 缓存判定 → 分析（桩）→ 分组 → 渲染 → 写盘。
它抓不住"结果不对"，但能抓住"根本跑不通"，而那正是今天缺的一层。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import db as dbmod  # noqa: E402

_spec = importlib.util.spec_from_file_location("ad_build", PROJECT_ROOT / "scripts" / "assistant_daily.py")
ad = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ad)

D = "2026-11-24"


def _ts(y, m, d, hh=10):
    return int(datetime(y, m, d, hh, 0).timestamp())


def _seed(db_path: str):
    dbmod.init_db(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        """INSERT INTO crawl_snapshots
           (stock_code, crawl_time, posts_count, posts_data, sentiment_avg, status)
           VALUES (?,?,?,?,?,?)""",
        ("OLD", _ts(2026, 7, 1, 9), 0, "[]", 0.0, "success"))
    old_snap = cur.lastrowid
    for code in ("AAA.US", "BBB.HK"):
        for i in range(3):
            pid = f"{code}-{i}"
            conn.execute(
                """INSERT INTO posts
                   (stock_code, dedup_key, post_id, post_type, title, content, author,
                    time_text, post_ts, like_count, comment_count, forward_count,
                    sentiment_score, link, snapshot_id, first_seen_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (code, pid, pid, "discussion", "测试标题需要足够长度以通过最短过滤",
                 "正文内容" * 20, "作者", "3天前", _ts(2026, 11, 24, 9 + i),
                 5, 1, 0, 0.0, f"https://xueqiu.com/{pid}", old_snap, _ts(2026, 11, 24)))
        today = [{"post_id": f"{code}-{i}", "title": "测试标题需要足够长度以通过最短过滤",
                  "content": "正文内容" * 20, "author": "作者", "time": "3天前",
                  "like_count": 5, "comment_count": 1, "forward_count": 0,
                  "link": f"https://xueqiu.com/{code}-{i}"} for i in range(3)]
        conn.execute(
            """INSERT INTO crawl_snapshots
               (stock_code, crawl_time, posts_count, posts_data, sentiment_avg, status)
               VALUES (?,?,?,?,?,?)""",
            (code, _ts(2026, 11, 24, 15), len(today),
             json.dumps(today, ensure_ascii=False), 0.0, "success"))
    conn.commit()
    conn.close()


def test_build_runs_end_to_end_without_name_errors(monkeypatch, tmp_path):
    db_path = str(tmp_path / "t.db")
    _seed(db_path)

    # 只保留两只票，其余配置照旧；LLM 换成桩
    monkeypatch.setattr(ad, "resolve_model", lambda cfg: "stub-model")
    # _get_llm_client 是在 build() 内部 import 的，得从源模块打桩
    import src.report_generator as _rg
    monkeypatch.setattr(_rg, "_get_llm_client", lambda: object())
    monkeypatch.setattr(ad, "analyze_stock",
                        lambda *a, **k: {"facts": [], "noise_ids": [],
                                         "change": "", "trend": "", "thread": "",
                                         "anchors": [], "open_questions": [],
                                         "x_fingerprint": "stub"})

    # 先读真配置（此时 PROJECT_DIR 还没被打桩）
    real_cfg = json.loads(
        (PROJECT_ROOT / "etc" / "config.report.json").read_text(encoding="utf-8"))

    def _cfg():
        cfg = dict(real_cfg)
        cfg["stocks"] = {
            "AAA.US": {"name": "测试A", "sector": "测试"},
            "BBB.HK": {"name": "测试B", "sector": "测试"},
        }
        cfg["x_source"] = {"enabled": False, "root": str(tmp_path / "nonexistent")}
        return cfg

    monkeypatch.setattr(ad, "load_config", _cfg)
    # 缓存与叙事写到临时目录，别污染 data/
    monkeypatch.setattr(ad, "PROJECT_DIR", tmp_path)

    out_md = tmp_path / "out.md"
    out_html = tmp_path / "out.html"
    ad.build(db_path, D, out_md, out_html, workers=1, use_cache=False)

    assert out_md.exists() and out_md.stat().st_size > 0
    assert out_html.exists() and out_html.stat().st_size > 0
    md = out_md.read_text(encoding="utf-8")
    assert "AAA.US" in md and "BBB.HK" in md


def test_build_survives_empty_snapshot_day(monkeypatch, tmp_path):
    """当天完全没数据的票也要能走完（空快照 → 0 帖分支）。"""
    db_path = str(tmp_path / "t.db")
    dbmod.init_db(db_path)

    monkeypatch.setattr(ad, "resolve_model", lambda cfg: "stub-model")
    # _get_llm_client 是在 build() 内部 import 的，得从源模块打桩
    import src.report_generator as _rg
    monkeypatch.setattr(_rg, "_get_llm_client", lambda: object())
    monkeypatch.setattr(ad, "analyze_stock",
                        lambda *a, **k: {"facts": [], "noise_ids": [],
                                         "change": "", "trend": "", "thread": "",
                                         "anchors": [], "open_questions": [],
                                         "x_fingerprint": "stub"})

    real_cfg = json.loads(
        (PROJECT_ROOT / "etc" / "config.report.json").read_text(encoding="utf-8"))

    def _cfg():
        cfg = dict(real_cfg)
        cfg["stocks"] = {"ZZZ.HK": {"name": "空票", "sector": "测试"}}
        cfg["x_source"] = {"enabled": False, "root": str(tmp_path / "nonexistent")}
        return cfg

    monkeypatch.setattr(ad, "load_config", _cfg)
    monkeypatch.setattr(ad, "PROJECT_DIR", tmp_path)

    out_md = tmp_path / "out.md"
    ad.build(db_path, D, out_md, tmp_path / "out.html", workers=1, use_cache=False)
    assert out_md.exists() and "ZZZ.HK" in out_md.read_text(encoding="utf-8")
