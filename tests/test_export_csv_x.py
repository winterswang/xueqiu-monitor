"""CSV 导出合并 X 舆情（2026-11-25 D）。

此前 CSV 只导雪球：X 舆情早就进了日报，却进不了知识库——同一个库，
两条信息源只有一条进去，事后按 CSV 复盘会漏掉英文侧线索。

三条约束在这里钉住：
1. 只有美股走 X（`is_us`），港股 A 股不该凭空多出 X 行
2. 导出集合与分析口径一致（layer_for_prompt 分层限量），不是全量
3. CSV 必须带 source 列，否则雪球和 X 的行混在一起分不清
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "export_csv_mod", PROJECT_ROOT / "scripts" / "export_csv.py")
ec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ec)

D = "2026-11-24"


def _x_root(tmp_path: Path, ticker: str, today: int, hist: int) -> Path:
    root = tmp_path / "x_posts"
    d = root / D
    d.mkdir(parents=True, exist_ok=True)
    posts = []
    for i in range(today):
        posts.append({
            "tweet_id": f"{ticker}-t{i}",
            "url": f"https://x.com/t/status/{ticker}t{i}",
            "author": "@trader",
            "text": f"{ticker} 当日第 {i:02d} 条，内容足够长以避免触发最短长度过滤。",
            "lang": "en", "slice": "top",
            "created_at_utc": "2026-11-24T02:00:00Z",
            "metrics": {"like": 10, "reply": 1, "repost": 1},
        })
    for i in range(hist):
        posts.append({
            "tweet_id": f"{ticker}-h{i}",
            "url": f"https://x.com/t/status/{ticker}h{i}",
            "author": "@trader",
            "text": f"{ticker} 历史第 {i:02d} 条，内容同样足够长以通过最短长度过滤。",
            "lang": "en", "slice": "top",
            "created_at_utc": "2026-07-10T02:00:00Z",
            "metrics": {"like": 5, "reply": 1, "repost": 1},
        })
    (d / f"{ticker}.json").write_text(
        json.dumps({"ticker": ticker, "date": D, "posts": posts}, ensure_ascii=False),
        encoding="utf-8")
    return root


def test_source_column_exists():
    assert "source" in ec.CSV_HEADERS, "没有 source 列，雪球和 X 的行分不清"


def test_only_us_stocks_get_x_rows(tmp_path):
    root = _x_root(tmp_path, "PDD", 5, 0)
    stocks = {"PDD.US": {"name": "拼多多"}, "700.HK": {"name": "腾讯"}}
    rows = ec.fetch_x_posts(stocks, D, {"enabled": True, "root": str(root)})
    assert rows, "美股应当有 X 行"
    assert {r["stock_code"] for r in rows} == {"PDD.US"}, "港股不该凭空多出 X 行"


def test_rows_carry_source_and_date(tmp_path):
    root = _x_root(tmp_path, "PDD", 3, 0)
    rows = ec.fetch_x_posts({"PDD.US": {"name": "拼多多"}}, D,
                            {"enabled": True, "root": str(root)})
    assert all(r["source"] == "X" for r in rows)
    # 时间列写日期而不是 UTC 时间戳，避免与北京日期错位
    assert all(len(r["time"]) == 10 and r["time"].count("-") == 2 for r in rows), \
        [r["time"] for r in rows]


def test_export_set_matches_analysis_layering(tmp_path):
    """导出集合必须与分析一致：近期全留 + 历史 ≤5。不是全量 40 条。"""
    root = _x_root(tmp_path, "PDD", 3, 40)
    rows = ec.fetch_x_posts({"PDD.US": {"name": "x"}}, D,
                            {"enabled": True, "root": str(root),
                             "max_posts_per_stock": 30})
    assert len(rows) == 3 + 3, f"应为近期 3 + 历史 3（min(5,3)），实际 {len(rows)}"


def test_disabled_x_source_yields_nothing(tmp_path):
    root = _x_root(tmp_path, "PDD", 5, 0)
    rows = ec.fetch_x_posts({"PDD.US": {"name": "x"}}, D,
                            {"enabled": False, "root": str(root)})
    assert rows == []


def test_missing_x_file_does_not_break_export(tmp_path):
    """单只票缺 X 文件不该中断整轮导出。"""
    rows = ec.fetch_x_posts({"NOPE.US": {"name": "x"}}, D,
                            {"enabled": True, "root": str(tmp_path / "nonexistent")})
    assert rows == []
