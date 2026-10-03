from __future__ import annotations

import json
import time

from src import cli
from src import detail_fetcher
from src import sentiment
from src.db import get_meta, init_db, set_meta
from src.report_generator import _sentiment_degradation_note


def test_sentiment_missing_key_is_reported_not_silent(monkeypatch):
    monkeypatch.setattr(sentiment, "_client", None)
    monkeypatch.setattr(sentiment, "_client_unavailable_reason", "")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.delenv("ARKCODE_API_KEY", raising=False)
    monkeypatch.setattr(sentiment, "_load_openclaw_api_key", lambda: None)

    status = sentiment.get_client_status()

    assert status["available"] is False
    assert "ARK_API_KEY/ARKCODE_API_KEY" in status["reason"]


def test_sentiment_degradation_downgrades_health_and_is_visible():
    result = {
        "stock_code": "TEST.HK",
        "status": "success",
        "posts_count": 10,
        "diagnostic": {
            "sentiment_status": "degraded",
            "sentiment_reason": "ARK_API_KEY/ARKCODE_API_KEY 未配置",
        },
    }

    health = cli._evaluate_crawl_health([result])

    assert health["status"] == "warn"
    assert health["sentiment_degraded"] == 1
    assert health["sentiment_degraded_codes"] == ["TEST.HK"]


def test_sentiment_degradation_persists_across_runs(tmp_path):
    db_path = tmp_path / "monitor.db"
    init_db(str(db_path))
    first = {
        "sentiment_degraded_codes": ["A.HK"],
        "sentiment_degraded_reasons": ["key missing"],
    }
    second = {
        "sentiment_degraded_codes": ["B.HK"],
        "sentiment_degraded_reasons": ["key missing"],
    }

    cli._record_sentiment_degradation(str(db_path), first)
    cli._record_sentiment_degradation(str(db_path), second)
    raw = get_meta(
        str(db_path), f"sentiment_degraded:{time.strftime('%Y-%m-%d')}"
    )
    record = json.loads(raw or "{}")

    assert record["runs"] == 2
    assert record["codes"] == ["A.HK", "B.HK"]
    assert record["reasons"] == ["key missing"]


def test_report_footer_renders_sentiment_degradation(tmp_path):
    db_path = tmp_path / "monitor.db"
    init_db(str(db_path))
    date_str = "2026-10-03"
    set_meta(
        str(db_path),
        f"sentiment_degraded:{date_str}",
        json.dumps(
            {"runs": 2, "codes": ["A.HK", "B.HK"], "reasons": ["key missing"]},
            ensure_ascii=False,
        ),
    )

    note = _sentiment_degradation_note(str(db_path), date_str)

    assert "情绪模型曾降级" in note
    assert "key missing" in note
    assert "2 只标的" in note


def test_search_failure_is_distinguished_from_no_result(monkeypatch):
    def failed_post_json(*_args, **_kwargs):
        raise RuntimeError("api unavailable")

    monkeypatch.setattr(detail_fetcher, "_post_json", failed_post_json)

    text, error = detail_fetcher.search_context_with_status("测试查询")

    assert text == ""
    assert error == "api unavailable"
    assert detail_fetcher.search_context("测试查询") == ""


def test_announcement_detail_marks_background_search_failure(monkeypatch, tmp_path):
    db_path = tmp_path / "monitor.db"
    init_db(str(db_path))
    monkeypatch.setattr(detail_fetcher, "_fetch_us_filing", lambda *_args: "")
    monkeypatch.setattr(
        detail_fetcher,
        "search_context_with_status",
        lambda _query: ("", "api unavailable"),
    )

    detail = detail_fetcher.fetch_announcement_detail(
        str(db_path), "https://xueqiu.com/S/TEST", "10-Q", "TEST.US"
    )

    assert detail == "[EDGAR 原文不可得; 背景搜索失败: api unavailable]"


def test_pdf_announcement_detail_marks_background_search_failure(
    monkeypatch, tmp_path
):
    db_path = tmp_path / "monitor.db"
    init_db(str(db_path))
    monkeypatch.setattr(
        detail_fetcher,
        "fetch_detail_cached",
        lambda _db_path, _url: {"status": "error", "content": ""},
    )
    monkeypatch.setattr(
        detail_fetcher,
        "search_context_with_status",
        lambda _query: ("", "api unavailable"),
    )

    detail = detail_fetcher.fetch_announcement_detail(
        str(db_path), "https://example.com/report.pdf", "年度报告", "600519.SH"
    )

    assert detail == "[原文不可得(error); 背景搜索失败: api unavailable]"
