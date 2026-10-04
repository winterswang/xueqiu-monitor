from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from src.announcement_sources import (
    MAX_DOWNLOAD_BYTES,
    SourcePlan,
    DownloadTooLargeError,
    archive_day,
    build_filename,
    classify_source,
    ima_target,
    market_from_stock_code,
    upload_pending,
)
from src.announcement_sources import _fetch_bytes
from src.db import init_db


def _timestamp(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d").astimezone().timestamp())


def _seed_db(path: Path) -> None:
    init_db(str(path))
    with sqlite3.connect(path) as conn:
        snapshot_id = conn.execute(
            "INSERT INTO crawl_snapshots(stock_code, crawl_time) VALUES (?, ?)",
            ("600519.SH", _timestamp("2026-10-02")),
        ).lastrowid
        conn.executemany(
            """INSERT INTO announcements(
                   snapshot_id, stock_code, ann_title, ann_date, ann_type, ann_link
               ) VALUES (?,?,?,?,?,?)""",
            [
                (
                    snapshot_id,
                    "600519.SH",
                    "2026年半年度报告",
                    _timestamp("2026-10-02"),
                    "公告",
                    "https://example.com/report.pdf",
                ),
                (
                    snapshot_id,
                    "600519.SH",
                    "2026年半年度报告（重复快照）",
                    _timestamp("2026-10-02") + 3600,
                    "公告",
                    "https://example.com/report.pdf",
                ),
                (
                    snapshot_id,
                    "600519.SH",
                    "翌日披露报表",
                    _timestamp("2026-10-02"),
                    "公告",
                    "https://example.com/routine.pdf",
                ),
            ],
        )


def test_classification_routes_financial_and_event_announcements():
    assert classify_source("2026年半年度报告") == SourcePlan(
        "financial_report", "interim"
    )
    assert classify_source("10-Q Quarterly report") == SourcePlan(
        "financial_report", "10-Q"
    )
    assert classify_source(
        "6-K Announcement of acquisition Accession Number: 000-000-000"
    ) == SourcePlan("other_announcement", "6-K")
    assert classify_source("4 Statement of changes in beneficial ownership") is None
    assert classify_source("翌日披露报表") is None


def test_market_and_ima_routing():
    assert market_from_stock_code("600519.SH") == "CN"
    assert market_from_stock_code("09926.HK") == "HK"
    assert market_from_stock_code("NVDA.US") == "US"
    assert ima_target(
        {"doc_category": "financial_report", "market": "HK"}
    )[1].endswith("6997086")
    assert ima_target(
        {"doc_category": "other_announcement", "market": "US"}
    )[1].endswith("9223514")


def test_archive_day_downloads_skips_noise_and_is_idempotent(tmp_path):
    db_path = tmp_path / "monitor.db"
    source_root = tmp_path / "sources"
    _seed_db(db_path)

    calls = []

    def fetch_bytes(url):
        calls.append(url)
        return b"%PDF-1.4 test", "application/pdf"

    first = archive_day(
        db_path,
        source_root,
        "2026-10-02",
        fetch_bytes=fetch_bytes,
    )
    second = archive_day(
        db_path,
        source_root,
        "2026-10-02",
        fetch_bytes=fetch_bytes,
    )

    assert first.selected == 3
    assert first.downloaded == 1
    assert first.skipped == 1
    assert second.unchanged == 1
    assert calls == ["https://example.com/report.pdf"]
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT local_path, sha256, download_status, upload_status "
            "FROM announcement_sources WHERE upload_status='pending'"
        ).fetchone()
    assert row[2:] == ("downloaded", "pending")
    assert row[0].endswith(
        build_filename(
            "600519.SH",
            "2026-10-02",
            SourcePlan("financial_report", "interim"),
            "https://example.com/report.pdf",
        )
    )
    assert len(row[1]) == 64


def test_duplicate_source_url_is_uploaded_once(tmp_path):
    db_path = tmp_path / "monitor.db"
    source_root = tmp_path / "sources"
    _seed_db(db_path)

    summary = archive_day(
        db_path,
        source_root,
        "2026-10-02",
        fetch_bytes=lambda url: (b"%PDF-1.4 duplicate", "application/pdf"),
    )
    upload = upload_pending(
        db_path,
        source_root,
        upload_file=lambda path, kb, folder, title: "media-once",
    )
    with sqlite3.connect(db_path) as conn:
        states = conn.execute(
            "SELECT download_status, upload_status FROM announcement_sources "
            "ORDER BY announcement_id"
        ).fetchall()

    assert (summary.downloaded, summary.skipped) == (1, 1)
    assert upload.uploaded == 1
    assert states == [("downloaded", "uploaded"), ("skipped", "skipped")]


def test_upload_pending_updates_success_and_missing_file_failure(tmp_path):
    db_path = tmp_path / "monitor.db"
    source_root = tmp_path / "sources"
    _seed_db(db_path)
    archive_day(
        db_path,
        source_root,
        "2026-10-02",
        fetch_bytes=lambda url: (b"%PDF-1.4 test", "application/pdf"),
    )

    uploaded = upload_pending(
        db_path,
        source_root,
        upload_file=lambda path, kb, folder, title: "media-1",
    )
    missing = upload_pending(
        db_path,
        source_root,
        upload_file=lambda path, kb, folder, title: "media-2",
    )

    assert uploaded.to_dict() == {"selected": 1, "uploaded": 1, "failed": 0, "skipped": 0}
    assert missing.selected == 0
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT upload_status, media_id, upload_retry_count "
            "FROM announcement_sources"
        ).fetchone()
    assert row == ("uploaded", "media-1", 0)


def test_legacy_retry_count_is_split_by_phase(tmp_path):
    db_path = tmp_path / "monitor.db"
    _seed_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DROP TABLE announcement_sources")
        conn.execute(
            """CREATE TABLE announcement_sources (
                announcement_id INTEGER PRIMARY KEY,
                source_url TEXT NOT NULL,
                market TEXT NOT NULL,
                doc_category TEXT NOT NULL,
                report_form TEXT NOT NULL DEFAULT '',
                local_path TEXT NOT NULL DEFAULT '',
                sha256 TEXT NOT NULL DEFAULT '',
                mime_type TEXT NOT NULL DEFAULT '',
                download_status TEXT NOT NULL DEFAULT 'pending',
                download_error TEXT NOT NULL DEFAULT '',
                downloaded_at INTEGER NOT NULL DEFAULT 0,
                upload_status TEXT NOT NULL DEFAULT 'pending',
                upload_error TEXT NOT NULL DEFAULT '',
                media_id TEXT NOT NULL DEFAULT '',
                uploaded_at INTEGER NOT NULL DEFAULT 0,
                retry_count INTEGER NOT NULL DEFAULT 0
            )"""
        )
        ann_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM announcements ORDER BY id"
            ).fetchall()
        ]
        conn.executemany(
            "INSERT INTO announcement_sources(announcement_id, source_url, market, "
            "doc_category, download_status, upload_status, retry_count) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (ann_ids[0], "url", "CN", "financial_report", "failed", "pending", 2),
                (ann_ids[1], "url", "CN", "financial_report", "downloaded", "uploaded", 1),
            ],
        )
    init_db(str(db_path))
    init_db(str(db_path))
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT download_status, upload_status, download_retry_count, "
            "upload_retry_count FROM announcement_sources ORDER BY announcement_id"
        ).fetchall()
    assert rows == [
        ("failed", "pending", 2, 0),
        ("downloaded", "uploaded", 0, 1),
    ]


def test_sec_resolution_failure_is_retryable_not_permanently_skipped(tmp_path):
    db_path = tmp_path / "monitor.db"
    source_root = tmp_path / "sources"
    _seed_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM announcements")
        conn.execute(
            "INSERT INTO announcements(snapshot_id, stock_code, ann_title, ann_date, ann_link) "
            "VALUES ((SELECT MAX(id) FROM crawl_snapshots), 'NVDA.US', "
            "'10-Q Quarterly report', ?, 'https://xueqiu.com/S/NVDA')",
            (_timestamp("2026-10-02"),),
        )

    resolver_calls = []

    def failing_resolver(title, stock_code):
        resolver_calls.append((title, stock_code))
        return ""

    for _ in range(3):
        summary = archive_day(
            db_path,
            source_root,
            "2026-10-02",
            us_resolver=failing_resolver,
        )
    stopped = archive_day(
        db_path,
        source_root,
        "2026-10-02",
        us_resolver=failing_resolver,
    )
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT download_status, upload_status, download_retry_count, "
            "upload_retry_count, download_error FROM announcement_sources"
        ).fetchone()

    assert summary.failed == 1
    assert stopped.selected == 0
    assert len(resolver_calls) == 3
    assert row[:4] == ("failed", "skipped", 3, 0)
    assert "SEC source resolution failed" in row[4]


def test_fetch_bytes_rejects_oversized_content_before_download(monkeypatch):
    class Response:
        headers = {
            "Content-Length": str(MAX_DOWNLOAD_BYTES + 1),
            "Content-Type": "application/pdf",
        }

        def read(self, *_args, **_kwargs):
            raise AssertionError("oversized response must not be read")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *_args, **_kwargs: Response()
    )
    with pytest.raises(DownloadTooLargeError):
        _fetch_bytes("https://example.com/report.pdf")


def test_upload_retries_use_independent_counter_and_stop_at_limit(tmp_path):
    db_path = tmp_path / "monitor.db"
    source_root = tmp_path / "sources"
    _seed_db(db_path)
    archive_day(
        db_path,
        source_root,
        "2026-10-02",
        fetch_bytes=lambda url: (b"%PDF-1.4 test", "application/pdf"),
    )

    def failing_upload(path, kb, folder, title):
        raise RuntimeError("ima unavailable")

    for _ in range(3):
        summary = upload_pending(
            db_path,
            source_root,
            upload_file=failing_upload,
        )
    stopped = upload_pending(
        db_path,
        source_root,
        upload_file=failing_upload,
    )
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT upload_status, download_retry_count, upload_retry_count "
            "FROM announcement_sources WHERE download_status='downloaded'"
        ).fetchone()

    assert summary.failed == 1
    assert stopped.selected == 0
    assert row == ("failed", 0, 3)


def test_no_source_is_permanently_skipped_and_not_retried(tmp_path):
    db_path = tmp_path / "monitor.db"
    source_root = tmp_path / "sources"
    _seed_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM announcements")
        conn.execute(
            "INSERT INTO announcements(snapshot_id, stock_code, ann_title, ann_date, ann_link) "
            "VALUES ((SELECT MAX(id) FROM crawl_snapshots), '600519.SH', "
            "'关于回购公司股份的公告', ?, '')",
            (_timestamp("2026-10-02"),),
        )

    first = archive_day(
        db_path,
        source_root,
        "2026-10-02",
        fetch_bytes=lambda url: (b"%PDF-1.4 test", "application/pdf"),
    )
    second = archive_day(
        db_path,
        source_root,
        "2026-10-02",
        fetch_bytes=lambda url: (b"%PDF-1.4 test", "application/pdf"),
    )
    dry_run = upload_pending(
        db_path,
        source_root,
        upload_file=lambda path, kb, folder, title: "media-dry",
        dry_run=True,
    )
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT download_status, upload_status, download_retry_count, "
            "upload_retry_count FROM announcement_sources"
        ).fetchone()

    assert (first.skipped, second.selected, dry_run.skipped) == (1, 0, 0)
    assert row == ("skipped", "skipped", 0, 0)


def test_dry_run_does_not_mutate_missing_file_state(tmp_path):
    db_path = tmp_path / "monitor.db"
    source_root = tmp_path / "sources"
    _seed_db(db_path)
    archive_day(
        db_path,
        source_root,
        "2026-10-02",
        fetch_bytes=lambda url: (b"%PDF-1.4 test", "application/pdf"),
    )
    with sqlite3.connect(db_path) as conn:
        relative = conn.execute(
            "SELECT local_path FROM announcement_sources "
            "WHERE download_status='downloaded'"
        ).fetchone()[0]
    (source_root / relative).unlink()

    summary = upload_pending(
        db_path,
        source_root,
        upload_file=lambda path, kb, folder, title: "media-dry",
        dry_run=True,
    )
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT upload_status, upload_error, upload_retry_count "
            "FROM announcement_sources WHERE download_status='downloaded'"
        ).fetchone()

    assert (summary.selected, summary.skipped, summary.failed) == (1, 1, 0)
    assert row == ("pending", "", 0)


# ── SEC User-Agent ────────────────────────────────────────────────────────


def test_sec_urls_use_the_declared_sec_user_agent(monkeypatch):
    """sec.gov 必须用带联系方式的 UA。

    回归：归档代码曾自己写了个浏览器样式 UA，结果**每一条美股公告都下不下来**。
    SEC 对「未声明的自动化工具」在 TLS 层就掐断，表现为 SSL UNEXPECTED_EOF，
    很容易被误判成 TLS 怪癖（detail_fetcher 的注释就这么记的）。2026-10-04
    实测同一 URL：浏览器 UA → SSL EOF / 403，_SEC_UA → 200。
    """
    from src import announcement_sources as mod

    seen: dict[str, str] = {}

    class _Resp:
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def __init__(self):
            self._payload = b"<html>ok</html>"
            self._sent = False

        def read(self, size=None):
            # 真实现按 1MB 分块读，这里忽略 size，只保证「读完返回空」的约定
            if self._sent:
                return b""
            self._sent = True
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        seen[request.full_url] = request.get_header("User-agent")
        return _Resp()

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)

    sec_url = "https://www.sec.gov/Archives/edgar/data/1318605/1/tsla.htm"
    assert mod._fetch_bytes(sec_url)[0] == b"<html>ok</html>"
    assert seen[sec_url] == mod._SEC_UA

    other_url = "https://example.com/report.pdf"
    mod._fetch_bytes(other_url)
    assert seen[other_url] == mod._BROWSER_UA
