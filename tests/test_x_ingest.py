"""Unit tests for scripts/x_ingest.py 与 scripts/validate_x_posts.py.

覆盖采集落盘契约的关键不变量：
- 跨切片同一条推文只留互动最高
- 空数据也写文件（健康检查要能区分「0 条」与「没采」）
- 原子写不产生半截文件
- 状态文件按票合并，不互相覆盖
- 校验器能抓出字段缺失/日期错配/重复
"""

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import x_ingest  # noqa: E402
import validate_x_posts  # noqa: E402


def _post(tid, like=10, reply=1, repost=2, slice_="top", text=None):
    return {
        "tweet_id": tid,
        "url": f"https://x.com/user/status/{tid}",
        "author": "Someone @someone",
        "text": text or f"$META some meaningful commentary number {tid}",
        "lang": "en",
        "slice": slice_,
        "created_at_utc": "2026-11-10T02:00:00.000Z",
        "metrics": {"like": like, "reply": reply, "repost": repost},
    }


@pytest.fixture
def root(tmp_path):
    return tmp_path / "x_posts"


def test_ingest_dedupes_across_slices_keeping_highest_engagement(root, tmp_path):
    raw = {
        "ticker": "META",
        "collected_at": "2026-11-10T06:40:00+08:00",
        "slices": [
            {"name": "top", "requested": 25, "posts": [_post("1", like=5)]},
            {"name": "min_faves", "requested": 25, "posts": [_post("1", like=900, slice_="min_faves")]},
        ],
    }
    raw_path = tmp_path / "META.raw.json"
    raw_path.write_text(json.dumps(raw), encoding="utf-8")

    res = x_ingest.ingest(root, "2026-11-10", "META", raw_path)

    assert res["posts"] == 1
    assert res["dropped"] == 1
    payload = json.loads((root / "2026-11-10" / "META.json").read_text())
    assert payload["posts"][0]["metrics"]["like"] == 900
    assert payload["posts"][0]["slice"] == "min_faves"


def test_ingest_writes_file_even_when_empty(root, tmp_path):
    raw_path = tmp_path / "META.raw.json"
    raw_path.write_text(json.dumps({"ticker": "META", "slices": []}), encoding="utf-8")

    res = x_ingest.ingest(root, "2026-11-10", "META", raw_path)

    assert res["posts"] == 0
    assert (root / "2026-11-10" / "META.json").exists()
    status = json.loads((root / "2026-11-10" / "_status.json").read_text())
    assert status["tickers"]["META"]["status"] == "empty"


def test_ingest_accepts_flat_list_and_backfills_slices(root, tmp_path):
    raw_path = tmp_path / "GOOGL.raw.json"
    raw_path.write_text(json.dumps([_post("7", slice_="live")]), encoding="utf-8")

    res = x_ingest.ingest(root, "2026-11-10", "GOOGL.US", raw_path)

    assert res["ticker"] == "GOOGL"
    payload = json.loads((root / "2026-11-10" / "GOOGL.json").read_text())
    assert payload["slices"] == [{"name": "live", "requested": None, "returned": 1}]


def test_login_required_records_status_without_post_file(root):
    x_ingest.ingest(
        root, "2026-11-10", "RDDT", None, status="login_required", note="跳登录页"
    )

    assert not (root / "2026-11-10" / "RDDT.json").exists()
    status = json.loads((root / "2026-11-10" / "_status.json").read_text())
    assert status["tickers"]["RDDT"]["status"] == "login_required"
    assert "跳登录" in status["tickers"]["RDDT"]["note"]


def test_status_merges_multiple_tickers(root, tmp_path):
    for ticker in ("META", "GOOGL"):
        raw_path = tmp_path / f"{ticker}.raw.json"
        raw_path.write_text(json.dumps([_post("1")]), encoding="utf-8")
        x_ingest.ingest(root, "2026-11-10", ticker, raw_path)

    status = json.loads((root / "2026-11-10" / "_status.json").read_text())
    assert set(status["tickers"]) == {"META", "GOOGL"}


def test_atomic_write_leaves_no_tmp(root, tmp_path):
    raw_path = tmp_path / "META.raw.json"
    raw_path.write_text(json.dumps([_post("1")]), encoding="utf-8")
    x_ingest.ingest(root, "2026-11-10", "META", raw_path)

    leftovers = list((root / "2026-11-10").glob("*.tmp"))
    assert leftovers == []


def test_prune_keeps_recent_days(root):
    for d in ("2026-10-01", "2026-11-08", "2026-11-10"):
        (root / d).mkdir(parents=True)
    removed = x_ingest.prune(root, keep_days=7, dry_run=True)

    # 以「今天」为准的相对判定：只断言过期目录在候选里、最近的在候选外
    assert "2026-10-01" in removed
    assert "2026-11-10" not in removed


def test_validator_passes_good_file_and_flags_bad_date(root, tmp_path):
    raw_path = tmp_path / "META.raw.json"
    raw_path.write_text(json.dumps([_post("1")]), encoding="utf-8")
    x_ingest.ingest(root, "2026-11-10", "META", raw_path)

    assert validate_x_posts.main(["--root", str(root), "--date", "2026-11-10"]) == 0

    # 手工把 date 改错，校验器必须抓出来
    p = root / "2026-11-10" / "META.json"
    payload = json.loads(p.read_text())
    payload["date"] = "2026-11-09"
    p.write_text(json.dumps(payload), encoding="utf-8")
    assert validate_x_posts.main(["--root", str(root), "--date", "2026-11-10"]) == 1


def test_validator_reports_missing_ticker_but_passes_if_no_errors(root, tmp_path):
    raw_path = tmp_path / "META.raw.json"
    raw_path.write_text(json.dumps([_post("1")]), encoding="utf-8")
    x_ingest.ingest(root, "2026-11-10", "META", raw_path)

    # 缺失票只算覆盖问题，不阻断（日报会降级为纯雪球）
    rc = validate_x_posts.main(
        ["--root", str(root), "--date", "2026-11-10", "--tickers", "META,GOOGL"]
    )
    assert rc == 0


def test_validator_catches_missing_post_fields(root):
    day = root / "2026-11-10"
    day.mkdir(parents=True)
    bad = {
        "ticker": "META",
        "date": "2026-11-10",
        "status": "ok",
        "posts": [{"tweet_id": "", "url": "", "author": "x", "text": "", "slice": "top"}],
    }
    (day / "META.json").write_text(json.dumps(bad), encoding="utf-8")

    assert validate_x_posts.main(["--root", str(root), "--date", "2026-11-10"]) == 1


def test_validator_catches_duplicate_tweet_id(root):
    day = root / "2026-11-10"
    day.mkdir(parents=True)
    p = _post("42")
    payload = {
        "ticker": "META",
        "date": "2026-11-10",
        "status": "ok",
        "slices": [{"name": "top", "returned": 2}],
        "posts": [p, dict(p)],
    }
    (day / "META.json").write_text(json.dumps(payload), encoding="utf-8")

    assert validate_x_posts.main(["--root", str(root), "--date", "2026-11-10"]) == 1
