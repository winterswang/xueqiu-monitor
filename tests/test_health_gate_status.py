"""scripts/health_gate_status.py — cron gate 的 status 判定 (2026-10-15).

gate 之前只看 health_check 的退出码, 而 WARN 档退出码是 0 → 真实异常被静默。
改成「退出码 0 且 status=ok」之后, 这一段解析就是告警链路的判定核心:
解析错了要么天天误报, 要么把真失败静默掉。
"""

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import health_gate_status  # noqa: E402


OK_REPORT = '{"status": "ok", "errors": [], "checks": [{"check": "a", "status": "ok"}]}'
WARN_REPORT = '{"status": "warn", "errors": ["no_summary"], "checks": []}'


class TestReportStatus:
    def test_ok(self):
        assert health_gate_status.report_status(OK_REPORT) == "ok"

    def test_warn(self):
        assert health_gate_status.report_status(WARN_REPORT) == "warn"

    def test_stderr_noise_before_json(self):
        """health_check 用 2>&1 捕获, 输出前面可能混着告警/日志行."""
        raw = 'Traceback line\nWARNING: something\n' + WARN_REPORT
        assert health_gate_status.report_status(raw) == "warn"

    def test_pretty_printed_multiline(self):
        raw = '{\n  "status": "fail",\n  "errors": ["x"],\n  "checks": []\n}\n'
        assert health_gate_status.report_status(raw) == "fail"

    def test_truncated_outer_json_is_fail_not_a_nested_ok(self):
        """关键对抗用例: 进程被杀截断外层 JSON 时, 不能取到 checks 里某个 ok."""
        raw = ('{"status": "fail", "errors": ["db_missing"], "checks": '
               '[{"check": "db_exists", "status": "fail"}, '
               '{"check": "pipeline_completed", "status": "ok"')
        assert health_gate_status.report_status(raw) == "fail"

    def test_nested_check_status_alone_is_not_a_report(self):
        """只有 check 元素、没有整体报告对象 → fail (宁可多报一次)."""
        raw = '[{"check": "pipeline_completed", "status": "ok"}]'
        assert health_gate_status.report_status(raw) == "fail"

    def test_missing_errors_key_is_not_a_report(self):
        raw = '{"status": "ok", "checks": []}'
        assert health_gate_status.report_status(raw) == "fail"

    def test_first_valid_report_wins(self):
        raw = OK_REPORT + "\n" + WARN_REPORT
        assert health_gate_status.report_status(raw) == "ok"

    @pytest.mark.parametrize("raw", ["", "   ", "not json at all", "{", "null", '{"a": 1}'])
    def test_unparseable_is_fail(self, raw):
        assert health_gate_status.report_status(raw) == "fail"

    def test_case_and_space_normalized(self):
        assert health_gate_status.report_status('{"status": " OK ", "errors": [], "checks": []}') == "ok"

    def test_empty_status_value_is_fail(self):
        assert health_gate_status.report_status('{"status": "", "errors": [], "checks": []}') == "fail"


class TestCliContract:
    """cron_healthgate.sh 靠 stdout 单行 + 退出码 0 取结果."""

    def test_prints_single_line_and_exits_zero(self, capsys, monkeypatch):
        import io

        monkeypatch.setattr(sys, "stdin", io.StringIO(WARN_REPORT))
        assert health_gate_status.main() == 0
        assert capsys.readouterr().out == "warn\n"
