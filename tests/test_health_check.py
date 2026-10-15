"""Unit tests for scripts/health_check.py — data freshness sentinel (v0.7 F2).

Verifies the three semantic checks added in v0.7:
- crawl_freshness: last_crawl_time > 48h → FAIL when pipeline broadly stalled
- time_parse_rate: post time parse failure > 30% → FAIL (8/8 accident signature)
- post_freshness: > 50% posts older than 7d → WARN

crawl_freshness scope = whitelist union of etc/config*.json when configs are
readable (v0.8.4: decommissioned meta rows no longer warn); falls back to the
full meta table in bare environments (tmp roots without etc/).

Runs against a temp project root so the real monitor.db is never touched.
"""

import json
import datetime
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent))  # project root for `from src import ...`

import health_check


@pytest.fixture
def fake_project(tmp_path: Path):
    """Build a temp project root with a monitor.db seeded for tests."""
    db = tmp_path / "data" / "monitor.db"
    db.parent.mkdir(parents=True)
    import sqlite3

    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE crawl_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, stock_code TEXT,
            crawl_time REAL, posts_count INTEGER, posts_data TEXT,
            sentiment_avg REAL, status TEXT
        );
        CREATE TABLE xueqiu_monitor_meta (
            stock_code TEXT UNIQUE, last_crawl_time REAL, last_post_time REAL
        );
        """
    )
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "cron.log").write_text(
        "[SUMMARY] stocks=1/1 success", encoding="utf-8"
    )
    (tmp_path / "data" / "watchlist.json").write_text("[]", encoding="utf-8")
    conn.commit()
    return conn, tmp_path


def _run_check(tmp_path: Path, require_daily_report: bool = False) -> dict:
    """Run health_check.check() against a fake project root, return parsed report."""
    with mock.patch.object(health_check, "PROJECT_ROOT", tmp_path), \
         mock.patch.object(health_check, "LOG_DIR", tmp_path / "logs"), \
         mock.patch.object(health_check, "WATCHLIST_PATH", tmp_path / "data" / "watchlist.json"):
        import io
        from contextlib import redirect_stdout

        out = io.StringIO()
        with redirect_stdout(out):
            with pytest.raises(SystemExit):
                health_check.check(require_daily_report=require_daily_report)
        return json.loads(out.getvalue())


def test_latest_pipeline_window_uses_run_marker_and_summary():
    """Source-failure window must end at SUMMARY, excluding later manual smoke tests."""
    text = "\n".join([
        "2026-10-01 13:00:00 [INFO] src.crawler: 从 morning-brief 加载 69 只自选股",
        "2026-10-01 13:11:20 [INFO] __main__: [SUMMARY] stocks=7/7 success=7",
        "2026-10-01 19:00:00 [INFO] manual smoke test",
    ])
    assert health_check._latest_pipeline_window(text) == (
        datetime.datetime(2026, 10, 1, 13, 0, 0),
        datetime.datetime(2026, 10, 1, 13, 11, 20),
    )


def test_source_failures_before_latest_run_are_ignored(tmp_path):
    """A morning failure must not keep the afternoon health check warned."""
    log = tmp_path / "source_failures.jsonl"
    records = [
        {"ts": "2026-10-01T11:00:00", "source": "news", "reason": "风控验证页"},
        {"ts": "2026-10-01T13:01:00", "source": "news", "reason": "风控验证页"},
    ]
    log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")

    rows = health_check._read_source_failures_in_window(
        log,
        (
            datetime.datetime(2026, 10, 1, 13, 0, 0),
            datetime.datetime(2026, 10, 1, 13, 11, 20),
        ),
    )

    assert len(rows) == 1
    assert rows[0]["ts"] == "2026-10-01T13:01:00"


def test_source_failure_window_falls_back_to_first_and_last_timestamps():
    text = "2026-10-01 13:00:00 [INFO] legacy startup\n2026-10-01 13:05:00 [INFO] done"
    assert health_check._latest_pipeline_window(text) == (
        datetime.datetime(2026, 10, 1, 13, 0, 0),
        datetime.datetime(2026, 10, 1, 13, 5, 0),
    )


def test_no_window_falls_back_to_today_not_empty(tmp_path):
    """No pipeline log → fall back to today's records (old behaviour).

    Returning [] would report "no source failures" with no evidence behind it —
    exactly the silent-OK mode this check exists to catch.
    """
    log = tmp_path / "source_failures.jsonl"
    today = datetime.date.today().isoformat()
    old = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
    records = [
        {"ts": "%sT09:00:00" % today, "source": "news", "reason": "风控验证页"},
        {"ts": "%sT09:00:00" % old, "source": "news", "reason": "风控验证页"},
    ]
    log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")

    rows = health_check._read_source_failures_in_window(log, (None, None))

    assert len(rows) == 1
    assert rows[0]["ts"].startswith(today)


def test_no_latest_log_does_not_crash(tmp_path):
    """_get_latest_log() can return None; source_failures must not NameError."""
    assert health_check._latest_pipeline_window("") == (None, None)


def test_no_pipeline_log_does_not_report_read_failure(fake_project):
    """Regression: source_window used to be bound only inside `if log:`.

    With no pipeline log, section 3.5 raised NameError; the blanket except turned
    that into a permanent WARN "读取失败: ..." on every run.
    """
    conn, tmp_path = fake_project
    report = _run_check(tmp_path)
    checks = {c["check"]: c for c in report["checks"]}
    assert "source_failures" in checks
    assert not checks["source_failures"]["detail"].startswith("读取失败"), checks["source_failures"]


def test_fresh_db_status_ok(fake_project):
    conn, tmp_path = fake_project
    now = time.time()
    conn.execute(
        "INSERT INTO xueqiu_monitor_meta VALUES ('AAA.HK', ?, 0)", (now - 3600,)
    )
    posts = json.dumps(
        [{"time": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.localtime(now - 600))}]
    )
    conn.execute(
        "INSERT INTO crawl_snapshots VALUES (1, 'AAA.HK', ?, 1, ?, 0.1, 'success')",
        (now - 3600, posts),
    )
    conn.commit()
    report = _run_check(tmp_path)
    checks = {c["check"]: c["status"] for c in report["checks"]}
    assert checks["crawl_freshness"] == "ok"
    assert checks["time_parse_rate"] == "ok"
    assert checks["post_freshness"] == "ok"
    assert report["status"] == "ok"


def test_stale_crawl_time_fails(fake_project):
    conn, tmp_path = fake_project
    now = time.time()
    # Both stocks stale 5 days → pipeline broadly stalled → FAIL
    for sc in ("AAA.HK", "BBB.US"):
        conn.execute(
            "INSERT INTO xueqiu_monitor_meta VALUES (?, ?, 0)", (sc, now - 5 * 86400)
        )
    posts = json.dumps([{"time": "2026-08-12T04:57:37.000Z"}])
    conn.execute(
        "INSERT INTO crawl_snapshots VALUES (1, 'AAA.HK', ?, 1, ?, 0.1, 'success')",
        (now - 3600, posts),
    )
    conn.commit()
    report = _run_check(tmp_path)
    checks = {c["check"]: c["status"] for c in report["checks"]}
    assert checks["crawl_freshness"] == "fail"
    assert "stale_last_crawl_time" in report["errors"]
    assert report["status"] == "fail"


def test_legacy_stale_stock_is_warn_not_fail(fake_project):
    """A few decommissioned rows must WARN, not FAIL (no false alarm)."""
    conn, tmp_path = fake_project
    now = time.time()
    # 1 fresh + 1 stale → majority fresh → WARN
    conn.execute("INSERT INTO xueqiu_monitor_meta VALUES ('AAA.HK', ?, 0)", (now - 3600,))
    conn.execute("INSERT INTO xueqiu_monitor_meta VALUES ('BBB.US', ?, 0)", (now - 5 * 86400,))
    posts = json.dumps([{"time": "2026-08-12T04:57:37.000Z"}])
    conn.execute(
        "INSERT INTO crawl_snapshots VALUES (1, 'AAA.HK', ?, 1, ?, 0.1, 'success')",
        (now - 3600, posts),
    )
    conn.commit()
    report = _run_check(tmp_path)
    checks = {c["check"]: c["status"] for c in report["checks"]}
    assert checks["crawl_freshness"] == "warn"
    assert "stale_last_crawl_time" not in report["errors"]
    assert report["status"] == "ok"


def test_high_time_parse_failure_fails(fake_project):
    """Garbage time fields (>30% unparseable) trigger FAIL — 8/8 signature."""
    conn, tmp_path = fake_project
    now = time.time()
    conn.execute("INSERT INTO xueqiu_monitor_meta VALUES ('AAA.HK', ?, 0)", (now - 3600,))
    posts = json.dumps([{"time": f"garbage-{i}"} for i in range(10)])
    conn.execute(
        "INSERT INTO crawl_snapshots VALUES (1, 'AAA.HK', ?, 10, ?, 0.1, 'success')",
        (now - 3600, posts),
    )
    conn.commit()
    report = _run_check(tmp_path)
    checks = {c["check"]: c["status"] for c in report["checks"]}
    assert checks["time_parse_rate"] == "fail"
    assert "high_time_parse_failure" in report["errors"]
    assert report["status"] == "fail"


# ── v0.8.4: crawl_freshness scopes to the whitelist union, not the full table ──


@pytest.fixture
def with_config(tmp_path: Path):
    """Make _load_monitor_whitelist() resolve: tmp etc/ with one group config."""
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "config.json").write_text(
        json.dumps({"crawler": {"whitelist": ["AAA.HK", "ZZZ.US"]}}), encoding="utf-8"
    )
    return tmp_path


def test_decommissioned_meta_rows_do_not_warn(fake_project, with_config):
    """Stale rows for stocks outside the whitelist are invisible to the check.

    This is the 2026-08-25 incident: 4 legacy rows (06-08) kept WARNing forever
    because the check scanned the full meta table."""
    conn, tmp_path = fake_project
    now = time.time()
    conn.execute("INSERT INTO xueqiu_monitor_meta VALUES ('AAA.HK', ?, 0)", (now - 3600,))
    # 3 decommissioned stocks — stale, but outside the whitelist union
    for sc in ("BBB.US", "CCC.SZ", "DDD.HK"):
        conn.execute("INSERT INTO xueqiu_monitor_meta VALUES (?, ?, 0)", (sc, now - 60 * 86400))
    posts = json.dumps([{"time": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.localtime(now - 600))}])
    conn.execute(
        "INSERT INTO crawl_snapshots VALUES (1, 'AAA.HK', ?, 1, ?, 0.1, 'success')",
        (now - 3600, posts),
    )
    conn.commit()
    report = _run_check(tmp_path)
    checks = {c["check"]: c["status"] for c in report["checks"]}
    assert checks["crawl_freshness"] == "ok"
    detail = next(c["detail"] for c in report["checks"] if c["check"] == "crawl_freshness")
    assert "1 whitelisted" in detail
    assert report["status"] == "ok"


def test_whitelisted_stale_stock_still_warns(fake_project, with_config):
    """The fix must not mask real signals: a stale IN-whitelist stock still WARNs
    (single group's cron failing while others run)."""
    conn, tmp_path = fake_project
    now = time.time()
    conn.execute("INSERT INTO xueqiu_monitor_meta VALUES ('AAA.HK', ?, 0)", (now - 5 * 86400,))
    conn.execute("INSERT INTO xueqiu_monitor_meta VALUES ('ZZZ.US', ?, 0)", (now - 3600,))
    posts = json.dumps([{"time": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.localtime(now - 600))}])
    conn.execute(
        "INSERT INTO crawl_snapshots VALUES (1, 'ZZZ.US', ?, 1, ?, 0.1, 'success')",
        (now - 3600, posts),
    )
    conn.commit()
    report = _run_check(tmp_path)
    checks = {c["check"]: c["status"] for c in report["checks"]}
    assert checks["crawl_freshness"] == "warn"
    detail = next(c["detail"] for c in report["checks"] if c["check"] == "crawl_freshness")
    assert "AAA.HK" in detail
    assert report["status"] == "ok"


# =============================================================================
# 2026-10-15: 两个 gate 判定的回归 —— 「批次还在跑」不能算失败;
# --require-daily-report 不再空转。
# =============================================================================


def _write_pipeline_log(tmp_path: Path, name: str, text: str, age_seconds: float = 0.0) -> Path:
    log = tmp_path / "logs" / name
    log.write_text(text, encoding="utf-8")
    if age_seconds:
        ts = time.time() - age_seconds
        import os as _os
        _os.utime(log, (ts, ts))
    return log


class TestRunningBatchIsNotAFailure:
    """14:25–18:25 每小时的批间巡检跑的是同一个 gate。

    health_check 会把运行中的批次标成「(运行中)」——那是正常态。若它同时
    计进 errors, status 就是 warn, 新 gate 会每小时发一张红卡片说「未通过」。
    实测批次耗时 11–24 分钟而 gate 在 :25 跑, g1/g2 正好压在边界上。
    """

    def test_running_batch_warns_but_does_not_error(self, fake_project, monkeypatch):
        conn, tmp_path = fake_project
        _write_pipeline_log(tmp_path, "pipeline-g9.log", "爬取中…没有 SUMMARY\n")
        monkeypatch.setattr(health_check, "_is_pipeline_running", lambda: True)

        report = _run_check(tmp_path)

        checks = {c["check"]: c for c in report["checks"]}
        assert checks["pipeline_completed"]["status"] == "warn"      # 仍可见
        assert "运行中" in checks["pipeline_completed"]["detail"]
        assert "no_summary" not in report["errors"]                   # 但不算错误
        assert report["status"] == "ok"                               # → gate 静默

    def test_stalled_batch_still_errors(self, fake_project, monkeypatch):
        """进程活着但一小时没写日志 = 真停滞, 必须照旧计错误并告警."""
        conn, tmp_path = fake_project
        _write_pipeline_log(tmp_path, "pipeline-g9.log", "挂住了\n", age_seconds=7200)
        monkeypatch.setattr(health_check, "_is_pipeline_running", lambda: True)

        report = _run_check(tmp_path)

        assert "no_summary" in report["errors"]
        assert report["status"] == "warn"                             # → gate 告警

    def test_finished_batch_without_summary_errors(self, fake_project, monkeypatch):
        """没有进程在跑、日志里也没有 SUMMARY = 真没跑完."""
        conn, tmp_path = fake_project
        _write_pipeline_log(tmp_path, "pipeline-g9.log", "没跑完就退了\n")
        monkeypatch.setattr(health_check, "_is_pipeline_running", lambda: False)

        report = _run_check(tmp_path)

        assert "no_summary" in report["errors"]
        assert report["status"] == "warn"


class TestRequireDailyReport:
    """`--require-daily-report` 之前是空转 flag (脚本不读 sys.argv、也没有这一项),
    cron 巡检 prompt 却声称「日报缺失或为空视为失败」→ 只能人工补行。

    判据抄 cron-ops.md:96-99: 日报在 **xueqiu-crawler** 项目里(不是本仓库),
    三连 = 存在 → 非空 → 不是「今日无新增」占位形态。
    """

    def _fake_crawler(self, tmp_path: Path) -> Path:
        d = tmp_path / "xueqiu-crawler" / "data" / "daily_reports"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _run_with_report_dir(self, tmp_path, report_dir, require=True):
        with mock.patch.object(health_check, "DAILY_REPORT_DIR", report_dir):
            return _run_check(tmp_path, require_daily_report=require)

    def test_default_does_not_check_daily_report(self, fake_project):
        conn, tmp_path = fake_project
        report = _run_check(tmp_path)
        assert "daily_report" not in {c["check"] for c in report["checks"]}

    def test_present_and_real_content_is_ok(self, fake_project):
        conn, tmp_path = fake_project
        d = self._fake_crawler(tmp_path)
        today = datetime.date.today().isoformat()
        (d / f"{today}.md").write_text("# 今日新增 37 篇\n\n## 优先级分类\n…", encoding="utf-8")

        report = self._run_with_report_dir(tmp_path, d)

        checks = {c["check"]: c for c in report["checks"]}
        assert checks["daily_report"]["status"] == "ok"
        assert checks["daily_report"]["detail"].startswith(f"{today}.md")
        assert report["status"] == "ok"

    def test_missing_is_fail(self, fake_project):
        conn, tmp_path = fake_project
        d = self._fake_crawler(tmp_path)

        report = self._run_with_report_dir(tmp_path, d)

        checks = {c["check"]: c for c in report["checks"]}
        assert checks["daily_report"]["status"] == "fail"
        assert "daily_report_missing" in report["errors"]
        assert report["status"] == "fail"          # → 退出码 1

    def test_empty_is_fail(self, fake_project):
        conn, tmp_path = fake_project
        d = self._fake_crawler(tmp_path)
        today = datetime.date.today().isoformat()
        (d / f"{today}.md").write_text("", encoding="utf-8")

        report = self._run_with_report_dir(tmp_path, d)

        assert "daily_report_invalid" in report["errors"]
        assert report["status"] == "fail"

    def test_placeholder_minimal_report_is_fail(self, fake_project):
        """「今日无新增」最小日报 = 有文件没内容 —— cron-ops.md 判无交付."""
        conn, tmp_path = fake_project
        d = self._fake_crawler(tmp_path)
        today = datetime.date.today().isoformat()
        (d / f"{today}.md").write_text(
            "# 雪球舆情日报\n\n今日无新增文章，已生成最小日报。\n", encoding="utf-8")

        report = self._run_with_report_dir(tmp_path, d)

        checks = {c["check"]: c for c in report["checks"]}
        assert checks["daily_report"]["status"] == "fail"
        assert "daily_report_placeholder" in report["errors"]
        assert report["status"] == "fail"

    def test_absent_crawler_dir_is_warn_not_fail(self, fake_project):
        """跨仓路径不存在 = 本机没配好, 不是「今天没交付」—— 报 WARN 免得天天红灯."""
        conn, tmp_path = fake_project
        report = self._run_with_report_dir(tmp_path, tmp_path / "no-such-crawler")

        checks = {c["check"]: c for c in report["checks"]}
        assert checks["daily_report"]["status"] == "warn"
        assert "daily_report_missing" not in report["errors"]
        assert "XUEQIU_DAILY_REPORT_DIR" in checks["daily_report"]["detail"]

    def test_env_override_wins(self, monkeypatch, tmp_path):
        """容器/换机器部署靠 XUEQIU_DAILY_REPORT_DIR 指路."""
        import importlib

        monkeypatch.setenv("XUEQIU_DAILY_REPORT_DIR", str(tmp_path / "elsewhere"))
        reloaded = importlib.reload(health_check)
        try:
            assert reloaded.DAILY_REPORT_DIR == tmp_path / "elsewhere"
        finally:
            monkeypatch.delenv("XUEQIU_DAILY_REPORT_DIR", raising=False)
            importlib.reload(health_check)


class TestExitCodeContract:
    """skills 文档的巡检 prompt 靠退出码判读, 且明写「exit 0≠ok」的坑。"""

    def _exit_code(self, tmp_path: Path, require_daily_report: bool = False,
                   report_dir: Path | None = None) -> int:
        patches = [
            mock.patch.object(health_check, "PROJECT_ROOT", tmp_path),
            mock.patch.object(health_check, "LOG_DIR", tmp_path / "logs"),
            mock.patch.object(health_check, "WATCHLIST_PATH", tmp_path / "data" / "watchlist.json"),
        ]
        if report_dir is not None:
            patches.append(mock.patch.object(health_check, "DAILY_REPORT_DIR", report_dir))
        import io
        from contextlib import ExitStack, redirect_stdout

        with ExitStack() as stack:
            for pt in patches:
                stack.enter_context(pt)
            out = io.StringIO()
            with redirect_stdout(out):
                with pytest.raises(SystemExit) as exc:
                    health_check.check(require_daily_report=require_daily_report)
            return exc.value.code

    def test_fail_status_exits_1(self, fake_project):
        _, tmp_path = fake_project
        report_dir = tmp_path / "xueqiu-crawler" / "data" / "daily_reports"
        report_dir.mkdir(parents=True)          # 目录在但今日文件缺失 → FAIL
        assert self._exit_code(tmp_path, require_daily_report=True,
                               report_dir=report_dir) == 1

    def test_missing_db_exits_1(self, fake_project):
        _, tmp_path = fake_project
        (tmp_path / "data" / "monitor.db").unlink()
        assert self._exit_code(tmp_path) == 1

    def test_warn_status_exits_0_so_gate_must_read_status(self, fake_project, monkeypatch):
        """WARN 档退出 0 —— 这正是 gate 必须按 status 判定的原因."""
        conn, tmp_path = fake_project
        _write_pipeline_log(tmp_path, "pipeline-g9.log", "没跑完就退了\n")
        monkeypatch.setattr(health_check, "_is_pipeline_running", lambda: False)
        assert self._exit_code(tmp_path) == 0


class TestRunningExemptionIsPerLog:
    """review 复现的漏洞: 豁免原先只看全局进程, 于是「任一批次活着」会掩盖
    当天所有未收尾日志 —— 上一组崩溃(日志停在 45 分钟前)会被静默放行。"""

    def test_crashed_batch_is_not_masked_by_another_running_batch(self, fake_project, monkeypatch):
        conn, tmp_path = fake_project
        _write_pipeline_log(tmp_path, "pipeline-g2.log", "崩溃在这\n", age_seconds=2700)
        _write_pipeline_log(tmp_path, "pipeline-g3.log", "正在跑…\n", age_seconds=5)
        monkeypatch.setattr(health_check, "_is_pipeline_running", lambda: True)

        report = _run_check(tmp_path)

        checks = {c["check"]: c for c in report["checks"]}
        assert "pipeline-g2.log" in checks["pipeline_completed"]["detail"]   # 未标运行中
        assert "no_summary" in report["errors"]                              # → 告警
        assert report["status"] == "warn"

    def test_batch_still_writing_is_exempt(self, fake_project, monkeypatch):
        conn, tmp_path = fake_project
        _write_pipeline_log(tmp_path, "pipeline-g9.log", "正在跑…\n", age_seconds=30)
        monkeypatch.setattr(health_check, "_is_pipeline_running", lambda: True)

        report = _run_check(tmp_path)

        checks = {c["check"]: c for c in report["checks"]}
        assert "运行中" in checks["pipeline_completed"]["detail"]
        assert "no_summary" not in report["errors"]
        assert report["status"] == "ok"

    def test_quiet_beyond_threshold_is_not_exempt(self, fake_project, monkeypatch):
        """阈值边界: 15 分钟没写日志(进程还在) 视作停滞."""
        conn, tmp_path = fake_project
        _write_pipeline_log(tmp_path, "pipeline-g9.log", "卡住了\n",
                            age_seconds=health_check.RUNNING_FRESH_SECONDS + 60)
        monkeypatch.setattr(health_check, "_is_pipeline_running", lambda: True)

        report = _run_check(tmp_path)
        assert "no_summary" in report["errors"]
