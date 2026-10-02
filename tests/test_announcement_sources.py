from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from src.announcement_sources import (
    ArchiveSummary,
    SourcePlan,
    archive_day,
    build_filename,
    classify_source,
    ima_target,
    market_from_stock_code,
    upload_pending,
)
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
            "SELECT upload_status, media_id, retry_count FROM announcement_sources"
        ).fetchone()
    assert row == ("uploaded", "media-1", 0)
