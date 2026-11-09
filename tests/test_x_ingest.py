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
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import x_collect  # noqa: E402
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
    """日期一律相对「今天」算：写死某天的测试过几天必红（CI 每天都在跑）。"""
    today = datetime.now(x_ingest.CST).date()
    recent = (today - timedelta(days=2)).isoformat()      # 保留期内
    boundary = (today - timedelta(days=6)).isoformat()    # keep_days=7 的最老一天
    expired = (today - timedelta(days=30)).isoformat()    # 早该删
    for d in (recent, boundary, expired):
        (root / d).mkdir(parents=True)

    removed = x_ingest.prune(root, keep_days=7, dry_run=True)

    assert expired in removed
    assert boundary not in removed
    assert recent not in removed


def test_prune_rejects_zero_keep_days(root):
    """--keep-days 0 的 cutoff 会落到明天，把当天数据删掉：必须报错而不是照办。"""
    (root / datetime.now(x_ingest.CST).date().isoformat()).mkdir(parents=True)
    with pytest.raises(ValueError):
        x_ingest.prune(root, keep_days=0, dry_run=True)


def test_validator_passes_good_file_and_flags_bad_date(root, tmp_path):
    raw_path = tmp_path / "META.raw.json"
    raw_path.write_text(json.dumps([_post("1")]), encoding="utf-8")
    x_ingest.ingest(root, "2026-11-10", "META", raw_path)

    assert validate_x_posts.main(["--root", str(root), "--date", "2026-11-10", "--tickers", "META"]) == 0

    # 手工把 date 改错，校验器必须抓出来
    p = root / "2026-11-10" / "META.json"
    payload = json.loads(p.read_text())
    payload["date"] = "2026-11-09"
    p.write_text(json.dumps(payload), encoding="utf-8")
    assert validate_x_posts.main(["--root", str(root), "--date", "2026-11-10", "--tickers", "META"]) == 1


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

    assert validate_x_posts.main(["--root", str(root), "--date", "2026-11-10", "--tickers", "META"]) == 1


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

    assert validate_x_posts.main(["--root", str(root), "--date", "2026-11-10", "--tickers", "META"]) == 1


# ─────────────────────────── 审查修复的回归用例 ───────────────────────────


def test_derive_status_marks_partial_when_a_slice_came_back_empty():
    """有一层切片 0 返回、整票仍有数据 → partial，不能报 ok 蒙混过关。"""
    posts = [_post("1")]
    slices = [
        {"name": "top", "requested": 20, "returned": 1},
        {"name": "live", "requested": 20, "returned": 0},
    ]
    assert x_ingest.derive_status(posts, "", slices) == "partial"


def test_derive_status_keeps_declared_failure_even_with_posts():
    """封号/登录墙不能被「恰好有几条帖」降级成 ok。"""
    posts = [_post("1")]
    assert x_ingest.derive_status(posts, "login_required") == "login_required"
    assert x_ingest.derive_status(posts, "blocked") == "failed"
    assert x_ingest.derive_status(posts, "error") == "failed"


def test_ticker_of_mirrors_x_sources_for_all_markets():
    """落盘与读取必须逐字同口径：A 股 / 港股也不能漏。"""
    assert x_ingest.ticker_of("META.US") == "META"
    assert x_ingest.ticker_of("BRK.B.US") == "BRK.B"
    assert x_ingest.ticker_of("600519.SH") == "600519"
    assert x_ingest.ticker_of("0700.HK") == "0700"
    assert x_ingest.ticker_of("300750.SZ") == "300750"


def test_validator_normalizes_us_suffix(root, tmp_path):
    """--tickers META.US 要能命中 META.json（配置里全是 .US 写法）。"""
    raw_path = tmp_path / "META.raw.json"
    raw_path.write_text(json.dumps([_post("1")]), encoding="utf-8")
    x_ingest.ingest(root, "2026-11-10", "META", raw_path)

    assert (
        validate_x_posts.main(
            ["--root", str(root), "--date", "2026-11-10", "--tickers", "META.US"]
        )
        == 0
    )


def test_validator_min_coverage_fails_when_too_many_missing(root, tmp_path):
    raw_path = tmp_path / "META.raw.json"
    raw_path.write_text(json.dumps([_post("1")]), encoding="utf-8")
    x_ingest.ingest(root, "2026-11-10", "META", raw_path)

    # 只有 1/3 覆盖，下限 0.5 → 必须判红（否则覆盖率闸形同虚设）
    rc = validate_x_posts.main(
        [
            "--root", str(root),
            "--date", "2026-11-10",
            "--tickers", "META,GOOGL,NVDA",
            "--min-coverage", "0.5",
        ]
    )
    assert rc == 1


def test_iso_utc_accepts_fractional_rfc3339():
    """适配器换成毫秒编码时不能把时间丢成空串（空串会被当成「今天」）。"""
    assert x_collect._iso_utc("2026-11-09T07:03:37.000Z") == "2026-11-09T07:03:37Z"
    assert x_collect._iso_utc("Mon Nov 09 07:03:37 +0000 2026") == "2026-11-09T07:03:37Z"
    assert x_collect._iso_utc("not a date") == ""
