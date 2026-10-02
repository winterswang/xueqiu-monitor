"""公告原始文件的分类、归档与 IMA 上传状态机。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .detail_fetcher import classify_announcement


DOWNLOAD_PENDING = "pending"
DOWNLOAD_DOWNLOADED = "downloaded"
DOWNLOAD_SKIPPED = "skipped"
DOWNLOAD_FAILED = "failed"
UPLOAD_PENDING = "pending"
UPLOAD_UPLOADED = "uploaded"
UPLOAD_SKIPPED = "skipped"
UPLOAD_FAILED = "failed"
MAX_RETRIES = 3

_FINANCIAL_REPORT_PATTERNS = (
    (re.compile(r"中期报告|半年度报告|半年报|中期业绩公告", re.I), "interim"),
    (re.compile(r"年度报告|年度审计报告|年报", re.I), "annual"),
    (re.compile(r"第一季度报告|季度报告|第三季度报告|季报|季度业绩公告", re.I), "quarterly"),
)
_US_FINANCIAL_FORMS = frozenset({"10-K", "10-Q", "20-F", "40-F"})
_US_FINANCIAL_REPORT_PAT = re.compile(
    r"annual\s+report|quarterly\s+report|interim\s+report", re.I
)
_US_FORM_RE = re.compile(r"^\s*(\d+(?:-[A-Z])?)\b")
_SAFE_NAME_RE = re.compile(r"[^0-9A-Za-z_.-]+")


@dataclass(frozen=True)
class SourcePlan:
    doc_category: str
    report_form: str


@dataclass
class ArchiveSummary:
    selected: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    unchanged: int = 0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class UploadSummary:
    selected: int = 0
    uploaded: int = 0
    failed: int = 0
    skipped: int = 0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def classify_source(title: str) -> SourcePlan | None:
    """返回值得归档的公告类型；财报优先，其次高价值事件公告。"""
    text = (title or "").strip()
    if not text:
        return None

    us_form = _US_FORM_RE.match(text)
    us_form_name = us_form.group(1).upper() if us_form else ""
    if us_form_name in _US_FINANCIAL_FORMS:
        return SourcePlan("financial_report", us_form_name)
    if us_form_name == "6-K" and _US_FINANCIAL_REPORT_PAT.search(text):
        return SourcePlan("financial_report", us_form_name)
    for pattern, report_form in _FINANCIAL_REPORT_PATTERNS:
        if pattern.search(text):
            return SourcePlan("financial_report", report_form)
    if classify_announcement(text) == "high":
        return SourcePlan("other_announcement", us_form_name)
    return None


def market_from_stock_code(stock_code: str) -> str:
    code = (stock_code or "").upper()
    if code.endswith((".SH", ".SZ", ".BJ")):
        return "CN"
    if code.endswith(".HK"):
        return "HK"
    if code.endswith(".US"):
        return "US"
    return "OTHER"


def _safe_component(value: str) -> str:
    value = _SAFE_NAME_RE.sub("_", value.strip())
    return value.strip("._-")[:60] or "unknown"


def build_filename(
    stock_code: str,
    announced_on: str,
    plan: SourcePlan,
    source_url: str,
) -> str:
    suffix = ".html" if source_url.lower().rstrip("?").endswith(
        (".htm", ".html")
    ) else ".pdf"
    kind = plan.report_form or plan.doc_category
    digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:8]
    return (
        f"{_safe_component(stock_code)}_{announced_on}_"
        f"{_safe_component(kind)}_{digest}{suffix}"
    )


def _mime_for_content(data: bytes, source_url: str) -> str:
    if data.startswith(b"%PDF"):
        return "application/pdf"
    if source_url.lower().rstrip("?").endswith((".htm", ".html")):
        return "text/html"
    return "application/octet-stream"


def _fetch_bytes(url: str, timeout: int = 60) -> tuple[bytes, str]:
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "xueqiu-monitor/1.0"
            )
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read()
        content_type = response.headers.get("Content-Type", "")
    mime = content_type.split(";", 1)[0].strip().lower()
    return data, mime or _mime_for_content(data, url)


def _resolve_source_url(
    row: sqlite3.Row,
    plan: SourcePlan,
    us_resolver: Callable[[str, str], str] | None,
) -> str:
    url = (row["ann_link"] or "").strip()
    if re.search(r"\.pdf(?:\?|$)", url, re.I):
        return url
    if (
        market_from_stock_code(row["stock_code"]) == "US"
        and "/S/" in url
        and us_resolver is not None
    ):
        return us_resolver(row["ann_title"], row["stock_code"])
    return ""


def _local_path(
    source_root: Path,
    row: sqlite3.Row,
    plan: SourcePlan,
    source_url: str,
) -> Path:
    announced = datetime.fromtimestamp(row["ann_date"]).astimezone()
    relative_dir = (
        Path(announced.strftime("%Y"))
        / announced.strftime("%m")
        / announced.strftime("%d")
        / market_from_stock_code(row["stock_code"])
        / _safe_component(row["stock_code"])
    )
    return source_root / relative_dir / build_filename(
        row["stock_code"],
        announced.strftime("%Y-%m-%d"),
        plan,
        source_url,
    )


def _write_record(
    conn: sqlite3.Connection,
    announcement_id: int,
    source_url: str,
    market: str,
    plan: SourcePlan,
    *,
    local_path: str = "",
    sha256: str = "",
    mime_type: str = "",
    download_status: str = DOWNLOAD_PENDING,
    download_error: str = "",
    upload_status: str = UPLOAD_PENDING,
) -> None:
    downloaded_at = int(time.time()) if download_status == DOWNLOAD_DOWNLOADED else 0
    conn.execute(
        """INSERT INTO announcement_sources (
               announcement_id, source_url, market, doc_category, report_form,
               local_path, sha256, mime_type, download_status, download_error,
               downloaded_at, upload_status, upload_error, media_id,
               uploaded_at, retry_count
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
           ON CONFLICT(announcement_id) DO UPDATE SET
               source_url=excluded.source_url,
               market=excluded.market,
               doc_category=excluded.doc_category,
               report_form=excluded.report_form,
               local_path=CASE WHEN excluded.local_path != '' THEN excluded.local_path ELSE announcement_sources.local_path END,
              sha256=CASE WHEN excluded.sha256 != '' THEN excluded.sha256 ELSE announcement_sources.sha256 END,
              mime_type=CASE WHEN excluded.mime_type != '' THEN excluded.mime_type ELSE announcement_sources.mime_type END,
              download_status=excluded.download_status,
              download_error=excluded.download_error,
              downloaded_at=excluded.downloaded_at,
              upload_status=excluded.upload_status,
              upload_error='',
              media_id='',
              uploaded_at=0,
              retry_count=CASE WHEN excluded.download_status='downloaded' THEN 0 ELSE announcement_sources.retry_count END""",
        (
            announcement_id,
            source_url,
            market,
            plan.doc_category,
            plan.report_form,
            local_path,
            sha256,
            mime_type,
            download_status,
            download_error,
            downloaded_at,
            upload_status,
            "",
            "",
            0,
        ),
    )


def archive_day(
    db_path: str | Path,
    source_root: str | Path,
    date_str: str,
    *,
    fetch_bytes: Callable[[str], tuple[bytes, str]] = _fetch_bytes,
    us_resolver: Callable[[str, str], str] | None = None,
    max_retries: int = MAX_RETRIES,
) -> ArchiveSummary:
    """把某天的目标公告下载为原始文件并写入断点状态。"""
    summary = ArchiveSummary()
    day_start = int(
        datetime.strptime(date_str, "%Y-%m-%d")
        .astimezone()
        .timestamp()
    )
    day_end = day_start + 86_400
    root = Path(source_root)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=3000")
    try:
        rows = conn.execute(
            """SELECT a.*, s.download_status, s.local_path, s.sha256,
                      s.retry_count AS source_retry_count
               FROM announcements a
               LEFT JOIN announcement_sources s
                 ON s.announcement_id = a.id
               WHERE a.ann_date >= ? AND a.ann_date < ?
                 AND (
                       s.announcement_id IS NULL
                       OR s.download_status IN ('pending', 'failed', 'downloaded')
                   )
               ORDER BY a.id""",
            (day_start, day_end),
        ).fetchall()
        summary.selected = len(rows)

        for row in rows:
            plan = classify_source(row["ann_title"])
            if plan is None:
                continue

            if (
                row["download_status"] == DOWNLOAD_DOWNLOADED
                and row["local_path"]
                and (root / row["local_path"]).is_file()
                and row["sha256"]
                and hashlib.sha256(
                    (root / row["local_path"]).read_bytes()
                ).hexdigest() == row["sha256"]
            ):
                summary.unchanged += 1
                continue
            if row["source_retry_count"] is not None and row["source_retry_count"] >= max_retries:
                continue

            market = market_from_stock_code(row["stock_code"])
            source_url = _resolve_source_url(row, plan, us_resolver)
            if not source_url:
                _write_record(
                    conn,
                    row["id"],
                    (row["ann_link"] or ""),
                    market,
                    plan,
                    download_status=DOWNLOAD_SKIPPED,
                    download_error="source_url_unavailable",
                    upload_status=UPLOAD_SKIPPED,
                )
                summary.skipped += 1
                conn.commit()
                continue

            existing = conn.execute(
                """SELECT local_path, sha256 FROM announcement_sources
                   WHERE source_url=? AND download_status=?
                     AND announcement_id != ?
                   ORDER BY downloaded_at DESC LIMIT 1""",
                (source_url, DOWNLOAD_DOWNLOADED, row["id"]),
            ).fetchone()
            if (
                existing
                and existing["local_path"]
                and (root / existing["local_path"]).is_file()
                and hashlib.sha256(
                    (root / existing["local_path"]).read_bytes()
                ).hexdigest() == existing["sha256"]
            ):
                _write_record(
                    conn,
                    row["id"],
                    source_url,
                    market,
                    plan,
                    local_path=existing["local_path"],
                    sha256=existing["sha256"],
                    mime_type=_mime_for_content(
                        (root / existing["local_path"]).read_bytes(),
                        source_url,
                    ),
                    download_status=DOWNLOAD_SKIPPED,
                    download_error="duplicate_source",
                    upload_status=UPLOAD_SKIPPED,
                )
                summary.skipped += 1
                conn.commit()
                continue

            target = _local_path(root, row, plan, source_url)
            try:
                data, mime = fetch_bytes(source_url)
                if not data:
                    raise ValueError("empty download")
                if mime == "application/pdf" and not data.startswith(b"%PDF"):
                    raise ValueError("non-pdf response")
                digest = hashlib.sha256(data).hexdigest()
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(target.suffix + ".part")
                temporary.write_bytes(data)
                temporary.replace(target)
                relative = str(target.relative_to(root))
                _write_record(
                    conn,
                    row["id"],
                    source_url,
                    market,
                    plan,
                    local_path=relative,
                    sha256=digest,
                    mime_type=mime or _mime_for_content(data, source_url),
                    download_status=DOWNLOAD_DOWNLOADED,
                )
                summary.downloaded += 1
            except (
                urllib.error.URLError,
                urllib.error.HTTPError,
                OSError,
                ValueError,
            ) as exc:
                _mark_download_failed(conn, row["id"], source_url, market, plan, str(exc))
                summary.failed += 1
            conn.commit()
    finally:
        conn.close()
    return summary


def _mark_download_failed(
    conn: sqlite3.Connection,
    announcement_id: int,
    source_url: str,
    market: str,
    plan: SourcePlan,
    error: str,
) -> None:
    _write_record(
        conn,
        announcement_id,
        source_url,
        market,
        plan,
                    download_status=DOWNLOAD_FAILED,
                    download_error=error[:1000],
                    upload_status=UPLOAD_SKIPPED,
                )
    conn.execute(
        "UPDATE announcement_sources SET retry_count=retry_count+1 "
        "WHERE announcement_id=?",
        (announcement_id,),
    )


FINANCIAL_KB_ID = "V-Zh0gSNxdBzOpedPdFwA5_BDn7H4FdOZPGq4jTI-X0="
OTHER_KB_ID = "GxGYu5owKy6lKmLRT3rzAuCyljDYXPo_a5SW-SSHgAY="
FINANCIAL_FOLDERS = {
    "CN": "folder_7497872887000987",
    "HK": "folder_7497872886997086",
    "US": "folder_7497872891195268",
}
OTHER_FOLDER_ID = "folder_7477622699223514"


def ima_target(row: sqlite3.Row | dict) -> tuple[str, str]:
    if row["doc_category"] == "financial_report":
        market = row["market"]
        if market not in FINANCIAL_FOLDERS:
            raise ValueError(f"unsupported financial report market: {market}")
        return FINANCIAL_KB_ID, FINANCIAL_FOLDERS[market]
    return OTHER_KB_ID, OTHER_FOLDER_ID


def upload_pending(
    db_path: str | Path,
    source_root: str | Path,
    *,
    upload_file: Callable[[str, str, str, str], str],
    date_str: str | None = None,
    limit: int = 50,
    max_retries: int = MAX_RETRIES,
    dry_run: bool = False,
) -> UploadSummary:
    """串行上传已归档源文件；每次成功/失败立即落库。"""
    summary = UploadSummary()
    parameters: list[object] = [DOWNLOAD_DOWNLOADED, UPLOAD_UPLOADED, max_retries, limit]
    date_filter = ""
    if date_str:
        day_start = int(
            datetime.strptime(date_str, "%Y-%m-%d").astimezone().timestamp()
        )
        date_filter = "AND a.ann_date >= ? AND a.ann_date < ?"
        parameters = [
            DOWNLOAD_DOWNLOADED,
            UPLOAD_UPLOADED,
            max_retries,
            day_start,
            day_start + 86_400,
            limit,
        ]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=3000")
    root = Path(source_root)
    try:
        sql = f"""
            SELECT s.*, a.ann_date, a.stock_code, a.ann_title
            FROM announcement_sources s
            JOIN announcements a ON a.id = s.announcement_id
            WHERE s.download_status = ?
              AND s.upload_status != ?
              AND s.retry_count < ?
              {date_filter}
            ORDER BY a.ann_date, s.announcement_id
            LIMIT ?
        """
        rows = conn.execute(sql, parameters).fetchall()
        summary.selected = len(rows)
        for row in rows:
            local_path = root / row["local_path"]
            if not local_path.is_file():
                conn.execute(
                    """UPDATE announcement_sources
                       SET upload_status=?, upload_error=?, retry_count=retry_count+1
                       WHERE announcement_id=?""",
                    (UPLOAD_FAILED, "local file missing", row["announcement_id"]),
                )
                conn.commit()
                summary.failed += 1
                continue

            if dry_run:
                summary.skipped += 1
                continue
            try:
                knowledge_base_id, folder_id = ima_target(row)
                media_id = upload_file(
                    str(local_path),
                    knowledge_base_id,
                    folder_id,
                    local_path.name,
                )
                conn.execute(
                    """UPDATE announcement_sources
                       SET upload_status=?, upload_error='', media_id=?,
                           uploaded_at=?
                       WHERE announcement_id=?""",
                    (
                        UPLOAD_UPLOADED,
                        media_id,
                        int(time.time()),
                        row["announcement_id"],
                    ),
                )
                summary.uploaded += 1
            except Exception as exc:
                conn.execute(
                    """UPDATE announcement_sources
                       SET upload_status=?, upload_error=?, retry_count=retry_count+1
                       WHERE announcement_id=?""",
                    (
                        UPLOAD_FAILED,
                        str(exc)[:1000],
                        row["announcement_id"],
                    ),
                )
                summary.failed += 1
            conn.commit()
    finally:
        conn.close()
    return summary
