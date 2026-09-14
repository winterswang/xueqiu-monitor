"""Unit tests for scripts/export_csv.py — CSV export + KB upload logic.

Tests cover:
- Data fetching (posts + announcements from monitor.db)
- Reply/short post filtering
- CSV generation (format, encoding, headers)
- Announcement timestamp conversion

Network calls (IMA API, COS upload) are mocked — no real external requests.
"""

import csv
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# scripts/ is not a package — import via sys.path
import sys
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import export_csv


# ════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════


class TmpDB:
    """Temporary SQLite DB helper — mirrors test_report_generator.TmpDB."""

    def __init__(self, path: str, date_str: str):
        self.path = Path(path)
        self.date_str = date_str
        import sqlite3
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        from src import db as dbmod
        dbmod.init_db(str(self.path))

    def insert_snapshot(self, stock_code: str, posts: list[dict]):
        """Insert a crawl snapshot with posts_data."""
        ts = int(time.time())
        self.conn.execute(
            """INSERT INTO crawl_snapshots
               (stock_code, crawl_time, posts_count, posts_data, sentiment_avg, status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (stock_code, ts, len(posts), json.dumps(posts), 0.0, "success"),
        )
        self.conn.commit()
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def insert_announcement(self, snapshot_id: int, stock_code: str,
                            ann_title: str, ann_date: int, ann_type: str = ""):
        """Insert an announcement row."""
        self.conn.execute(
            """INSERT OR IGNORE INTO announcements
               (snapshot_id, stock_code, ann_title, ann_date, ann_type, is_new)
               VALUES (?, ?, ?, ?, ?, 1)""",
            (snapshot_id, stock_code, ann_title, ann_date, ann_type),
        )
        self.conn.commit()

    def cleanup(self):
        self.conn.close()


@pytest.fixture
def tmp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = TmpDB(str(db_path), time.strftime("%Y-%m-%d"))
        yield db
        db.cleanup()


STOCKS_CFG = {
    "TEST.HK": {"name": "测试股", "sector": "消费"},
    "DEMO.US": {"name": "演示股", "sector": "AI/科技"},
}


# ════════════════════════════════════════════════════════
# fetch_all_posts tests
# ════════════════════════════════════════════════════════


class TestFetchAllPosts:
    """Test fetch_all_posts data extraction."""

    def test_normal_posts(self, tmp_db):
        """Normal discussion posts are fetched correctly."""
        # Use a recent time so the recency filter (default 7d) keeps the post.
        recent_ts = int(time.time()) - 3600  # 1h ago
        recent_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(recent_ts))
        posts = [
            {
                "type": "discussion",
                "title": "这是一个正常讨论帖子的标题",
                "content": "这是足够长度的帖子内容，用于测试数据获取逻辑是否正确工作",
                "author": "用户A",
                "time": recent_time,
                "like_count": 10,
                "comment_count": 5,
                "forward_count": 2,
                "sentiment_score": 0.5,
                "link": "https://xueqiu.com/test/1",
            },
        ]
        tmp_db.insert_snapshot("TEST.HK", posts)
        result = export_csv.fetch_all_posts(
            str(tmp_db.path), STOCKS_CFG, tmp_db.date_str, min_length=10
        )
        assert len(result) == 1
        row = result[0]
        assert row["stock_code"] == "TEST.HK"
        assert row["stock_name"] == "测试股"
        assert row["post_type"] == "discussion"
        assert row["like_count"] == 10
        assert row["sentiment_score"] == 0.5

    def test_filters_stale_posts(self, tmp_db):
        """Posts older than max_age_days are dropped from the CSV feed (R1)."""
        posts = [
            {
                "type": "discussion",
                "title": "这是一个旧帖标题，长度足够长可以用于测试",
                "content": "这是30天前的旧帖内容，知识库不应再收录",
                "author": "用户A",
                "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(int(time.time()) - 30 * 86400)),
                "like_count": 10,
                "comment_count": 5,
                "forward_count": 2,
            },
            {
                "type": "discussion",
                "title": "这是新帖标题，长度足够长可以用于测试",
                "content": "这是今天的帖子内容，应该保留在知识库里",
                "author": "用户B",
                "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(int(time.time()) - 3600)),
                "like_count": 3,
                "comment_count": 1,
                "forward_count": 0,
            },
        ]
        tmp_db.insert_snapshot("TEST.HK", posts)
        result = export_csv.fetch_all_posts(
            str(tmp_db.path), STOCKS_CFG, tmp_db.date_str, min_length=10
        )
        assert len(result) == 1
        assert result[0]["author"] == "用户B"  # stale post dropped

    def test_filters_reply_posts(self, tmp_db):
        """Reply posts (回复@ prefix) must be filtered out."""
        posts = [
            {
                "type": "discussion",
                "title": "正常帖子标题有足够长度",
                "content": "内容也足够长用于测试过滤逻辑",
                "author": "A",
            },
            {
                "type": "discussion",
                "title": "回复@某人: 哈哈",
                "content": "回复内容",
                "author": "B",
            },
        ]
        tmp_db.insert_snapshot("TEST.HK", posts)
        result = export_csv.fetch_all_posts(
            str(tmp_db.path), STOCKS_CFG, tmp_db.date_str, min_length=5
        )
        assert len(result) == 1
        assert "回复@" not in result[0]["title"]

    def test_filters_short_posts(self, tmp_db):
        """Posts shorter than min_length must be filtered."""
        posts = [
            {"type": "news", "title": "短", "content": "也短"},
            {
                "type": "news",
                "title": "正常长度的新闻标题",
                "content": "这是足够长的新闻内容用于测试过滤功能是否正常",
            },
        ]
        tmp_db.insert_snapshot("TEST.HK", posts)
        result = export_csv.fetch_all_posts(
            str(tmp_db.path), STOCKS_CFG, tmp_db.date_str, min_length=30
        )
        assert len(result) == 1

    def test_multiple_stocks(self, tmp_db):
        """Posts from different stocks are all fetched."""
        posts_a = [{"type": "discussion", "title": "A股讨论帖标题足够长",
                     "content": "内容内容内容内容内容内容"}]
        posts_b = [{"type": "article", "title": "B股文章标题也够长用于测试",
                     "content": "内容内容内容内容内容内容"}]
        tmp_db.insert_snapshot("TEST.HK", posts_a)
        tmp_db.insert_snapshot("DEMO.US", posts_b)
        result = export_csv.fetch_all_posts(
            str(tmp_db.path), STOCKS_CFG, tmp_db.date_str, min_length=5
        )
        codes = {r["stock_code"] for r in result}
        assert codes == {"TEST.HK", "DEMO.US"}

    def test_takes_latest_snapshot_only(self, tmp_db):
        """Same stock crawled twice a day → union of both snapshots.

        2026-09-14 contract change: crawl_single_stock trims each snapshot to
        "posts newer than the last watermark", so later snapshots only carry
        incremental posts. Taking just the latest snapshot would silently
        drop everything captured earlier the same day (9/13 live data: 197
        posts missed). Post-level dedup by post_id handles real duplicates.
        """
        posts_old = [{"type": "discussion", "title": "旧帖标题标题标题标题",
                      "content": "旧帖内容内容内容内容内容"}]
        posts_new = [{"type": "discussion", "title": "新帖标题标题标题标题",
                      "content": "新帖内容内容内容内容内容"}]
        tmp_db.insert_snapshot("TEST.HK", posts_old)
        time.sleep(0.01)  # ensure newer timestamp
        tmp_db.insert_snapshot("TEST.HK", posts_new)
        result = export_csv.fetch_all_posts(
            str(tmp_db.path), STOCKS_CFG, tmp_db.date_str, min_length=5
        )
        titles = {r["title"] for r in result}
        assert titles == {"旧帖标题标题标题标题", "新帖标题标题标题标题"}

    def test_same_post_across_snapshots_deduped(self, tmp_db):
        """Same post_id re-captured in a later snapshot → only first kept."""
        shared = {"type": "discussion",
                  "post_id": "https://xueqiu.com/999/111",
                  "title": "重复抓到的帖子标题标题标题",
                  "content": "内容内容内容内容内容内容内容"}
        tmp_db.insert_snapshot("TEST.HK", [shared])
        time.sleep(0.01)
        tmp_db.insert_snapshot("TEST.HK", [shared])
        result = export_csv.fetch_all_posts(
            str(tmp_db.path), STOCKS_CFG, tmp_db.date_str, min_length=5
        )
        assert len(result) == 1

    def test_content_capped(self, tmp_db):
        """Content longer than CONTENT_MAX_CHARS is truncated."""
        long_content = "X" * (export_csv.CONTENT_MAX_CHARS + 500)
        posts = [
            {
                "type": "article",
                "title": "长内容文章标题标题标题",
                "content": long_content,
                "author": "A",
            }
        ]
        tmp_db.insert_snapshot("TEST.HK", posts)
        result = export_csv.fetch_all_posts(
            str(tmp_db.path), STOCKS_CFG, tmp_db.date_str, min_length=5
        )
        assert len(result[0]["content"]) == export_csv.CONTENT_MAX_CHARS

    def test_empty_snapshot(self, tmp_db):
        """No data for the date → empty list."""
        result = export_csv.fetch_all_posts(
            str(tmp_db.path), STOCKS_CFG, "2020-01-01", min_length=10
        )
        assert result == []

    def test_dedup_same_post_across_stocks(self, tmp_db):
        """Same post_id in different stocks → only first occurrence kept."""
        shared_post = {
            "type": "discussion",
            "post_id": "https://xueqiu.com/12345/67890",
            "title": "跨股票重复帖子标题标题标题",
            "content": "这个帖子同时讨论了多只股票内容内容",
        }
        tmp_db.insert_snapshot("TEST.HK", [shared_post])
        tmp_db.insert_snapshot("DEMO.US", [shared_post])
        result = export_csv.fetch_all_posts(
            str(tmp_db.path), STOCKS_CFG, tmp_db.date_str, min_length=5
        )
        assert len(result) == 1

    def test_dedup_same_post_within_snapshot(self, tmp_db):
        """Duplicate post_id within the same snapshot → only first kept."""
        post = {
            "type": "discussion",
            "post_id": "https://xueqiu.com/111/222",
            "title": "重复帖子标题标题标题标题",
            "content": "内容内容内容内容内容内容",
        }
        tmp_db.insert_snapshot("TEST.HK", [post, post])
        result = export_csv.fetch_all_posts(
            str(tmp_db.path), STOCKS_CFG, tmp_db.date_str, min_length=5
        )
        assert len(result) == 1

    def test_no_post_id_not_dropped(self, tmp_db):
        """Posts without post_id are still included (no dedup possible)."""
        posts = [
            {
                "type": "news",
                "title": "没有post_id的新闻标题标题",
                "content": "内容内容内容内容内容",
            },
            {
                "type": "news",
                "title": "另一条没有post_id的新闻标题",
                "content": "内容内容内容内容内容",
            },
        ]
        tmp_db.insert_snapshot("TEST.HK", posts)
        result = export_csv.fetch_all_posts(
            str(tmp_db.path), STOCKS_CFG, tmp_db.date_str, min_length=5
        )
        assert len(result) == 2


# ════════════════════════════════════════════════════════
# fetch_announcements tests
# ════════════════════════════════════════════════════════


class TestFetchAnnouncements:
    """Test announcement data extraction."""

    def test_basic_announcement(self, tmp_db):
        """Announcements are fetched with correct fields."""
        posts = [{"type": "discussion", "title": "帖子", "content": "内容"}]
        snap_id = tmp_db.insert_snapshot("TEST.HK", posts)
        ann_ts = int(time.time())
        tmp_db.insert_announcement(snap_id, "TEST.HK", "季度财报公告标题", ann_ts, "financial")
        result = export_csv.fetch_announcements(
            str(tmp_db.path), STOCKS_CFG, tmp_db.date_str
        )
        assert len(result) == 1
        ann = result[0]
        assert ann["post_type"] == "announcement"
        assert ann["title"] == "季度财报公告标题"
        assert ann["stock_name"] == "测试股"
        assert ann["time"] != ""  # timestamp was converted

    def test_no_announcements(self, tmp_db):
        """No announcements → empty list."""
        posts = [{"type": "discussion", "title": "帖子", "content": "内容"}]
        tmp_db.insert_snapshot("TEST.HK", posts)
        result = export_csv.fetch_announcements(
            str(tmp_db.path), STOCKS_CFG, tmp_db.date_str
        )
        assert result == []


# ════════════════════════════════════════════════════════
# generate_csv tests
# ════════════════════════════════════════════════════════


class TestGenerateCsv:
    """Test CSV file generation."""

    def test_csv_headers(self, tmp_path):
        """CSV has correct headers in correct order."""
        csv_path = tmp_path / "test.csv"
        export_csv.generate_csv([], csv_path)
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            headers = next(reader)
        assert headers == export_csv.CSV_HEADERS

    def test_data_rows_written(self, tmp_path):
        """Data rows are correctly written."""
        rows = [
            {
                "date": "2026-07-09",
                "stock_code": "TEST.HK",
                "stock_name": "测试股",
                "post_type": "discussion",
                "title": "测试标题",
                "content": "测试内容",
                "author": "用户",
                "time": "10:00",
                "like_count": 5,
                "comment_count": 2,
                "forward_count": 1,
                "sentiment_score": 0.3,
                "link": "https://example.com",
            },
            {
                "date": "2026-07-09",
                "stock_code": "DEMO.US",
                "stock_name": "演示股",
                "post_type": "announcement",
                "title": "公告标题",
                "content": "",
                "author": "",
                "time": "12:00",
                "like_count": 0,
                "comment_count": 0,
                "forward_count": 0,
                "sentiment_score": 0.0,
                "link": "",
            },
        ]
        csv_path = tmp_path / "test.csv"
        count = export_csv.generate_csv(rows, csv_path)
        assert count == 2

        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            data = list(reader)
        assert len(data) == 2
        assert data[0]["stock_code"] == "TEST.HK"
        assert data[1]["post_type"] == "announcement"

    def test_utf8_bom(self, tmp_path):
        """CSV file has UTF-8 BOM for Excel compatibility."""
        csv_path = tmp_path / "bom_test.csv"
        export_csv.generate_csv([], csv_path)
        raw = csv_path.read_bytes()
        assert raw[:3] == b"\xef\xbb\xbf"  # UTF-8 BOM

    def test_chinese_content_preserved(self, tmp_path):
        """Chinese characters survive the CSV round-trip."""
        rows = [
            {
                "date": "2026-07-09",
                "stock_code": "TEST.HK",
                "stock_name": "中文公司名",
                "post_type": "discussion",
                "title": "中文标题包含特殊字符「」【】",
                "content": "中文内容，包含逗号、句号。",
                "author": "中文名",
                "time": "10:00",
                "like_count": 0,
                "comment_count": 0,
                "forward_count": 0,
                "sentiment_score": 0.0,
                "link": "",
            },
        ]
        csv_path = tmp_path / "chinese_test.csv"
        export_csv.generate_csv(rows, csv_path)

        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            data = list(reader)
        assert data[0]["stock_name"] == "中文公司名"
        assert "「」【】" in data[0]["title"]

    def test_extras_ignored(self, tmp_path):
        """Extra keys in row dict are silently ignored (extrasaction='ignore')."""
        rows = [
            {
                "date": "2026-07-09",
                "stock_code": "TEST.HK",
                "stock_name": "测试",
                "post_type": "news",
                "title": "标题",
                "content": "内容",
                "author": "A",
                "time": "",
                "like_count": 0,
                "comment_count": 0,
                "forward_count": 0,
                "sentiment_score": 0.0,
                "link": "",
                "extra_field": "should_not_crash",
            },
        ]
        csv_path = tmp_path / "test.csv"
        count = export_csv.generate_csv(rows, csv_path)
        assert count == 1


# ════════════════════════════════════════════════════════
# find_knowledge_base_id tests (mocked)
# ════════════════════════════════════════════════════════


class TestFindKnowledgeBaseId:
    """Test knowledge base search (API mocked)."""

    @patch.object(export_csv, "_ima_api")
    def test_found(self, mock_api):
        """Returns kb_id when name matches."""
        mock_api.return_value = {
            "code": 0,
            "data": {
                "info_list": [
                    {"kb_id": "kb123", "kb_name": "雪球内容数据"},
                ]
            },
        }
        result = export_csv.find_knowledge_base_id("雪球内容数据")
        assert result == "kb123"

    @patch.object(export_csv, "_ima_api")
    def test_not_found(self, mock_api):
        """Returns None when name doesn't match."""
        mock_api.return_value = {
            "code": 0,
            "data": {
                "info_list": [
                    {"kb_id": "kb999", "kb_name": "其他知识库"},
                ]
            },
        }
        result = export_csv.find_knowledge_base_id("雪球内容数据")
        assert result is None

    @patch.object(export_csv, "_ima_api")
    def test_api_error(self, mock_api):
        """Returns None on API error."""
        mock_api.return_value = {"code": 110001, "msg": "参数非法"}
        result = export_csv.find_knowledge_base_id("雪球内容数据")
        assert result is None


# ════════════════════════════════════════════════════════
# upload_csv_to_kb tests (mocked)
# ════════════════════════════════════════════════════════


class TestUploadCsvToKb:
    """Test upload flow (API + COS mocked)."""

    @patch.object(export_csv.subprocess, "run")
    @patch.object(export_csv, "_ima_api")
    def test_successful_upload(self, mock_api, mock_subproc):
        """Full happy path: check → create → COS → add → media_id."""
        mock_api.side_effect = [
            {"code": 0, "data": {"results": [{"name": "test.csv", "is_repeated": False}]}},
            {
                "code": 0,
                "data": {
                    "media_id": "mid_001",
                    "cos_credential": {
                        "secret_id": "sid",
                        "secret_key": "skey",
                        "token": "tok",
                        "bucket_name": "bucket",
                        "region": "ap-gz",
                        "cos_key": "key/path",
                        "start_time": 1000,
                        "expired_time": 5000,
                    },
                },
            },
            {"code": 0, "data": {"media_id": "mid_001"}},
        ]
        mock_subproc.return_value = MagicMock(returncode=0, stdout="OK", stderr="")

        csv_path = Path("/tmp/fake_test_export_csv.csv")
        csv_path.write_text("dummy", encoding="utf-8")
        try:
            result = export_csv.upload_csv_to_kb(csv_path, "kb123")
            assert result == "mid_001"
            assert mock_api.call_count == 3  # check + create + add
            assert mock_subproc.call_count == 1  # COS upload
        finally:
            csv_path.unlink(missing_ok=True)

    @patch.object(export_csv, "_ima_api")
    def test_create_media_failure(self, mock_api):
        """Returns None when create_media fails."""
        mock_api.side_effect = [
            {"code": 0, "data": {"results": [{"is_repeated": False}]}},
            {"code": 110001, "msg": "参数非法"},
        ]
        csv_path = Path("/tmp/fake_test2_export_csv.csv")
        csv_path.write_text("dummy", encoding="utf-8")
        try:
            result = export_csv.upload_csv_to_kb(csv_path, "kb123")
            assert result is None
        finally:
            csv_path.unlink(missing_ok=True)

    @patch.object(export_csv.subprocess, "run")
    @patch.object(export_csv, "_ima_api")
    def test_cos_upload_failure(self, mock_api, mock_subproc):
        """Returns None when COS upload fails."""
        mock_api.side_effect = [
            {"code": 0, "data": {"results": [{"is_repeated": False}]}},
            {
                "code": 0,
                "data": {
                    "media_id": "mid_002",
                    "cos_credential": {
                        "secret_id": "sid", "secret_key": "skey", "token": "tok",
                        "bucket_name": "b", "region": "r", "cos_key": "k",
                        "start_time": 1, "expired_time": 2,
                    },
                },
            },
        ]
        mock_subproc.return_value = MagicMock(returncode=1, stdout="", stderr="upload error")

        csv_path = Path("/tmp/fake_test3_export_csv.csv")
        csv_path.write_text("dummy", encoding="utf-8")
        try:
            result = export_csv.upload_csv_to_kb(csv_path, "kb123")
            assert result is None
            # Should NOT call add_knowledge (only check + create)
            assert mock_api.call_count == 2
        finally:
            csv_path.unlink(missing_ok=True)
