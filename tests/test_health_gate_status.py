"""scripts/health_gate_status.py — cron gate 的 status 判定 (2026-10-15).

gate 之前只看 health_check 的退出码, 而 WARN 档退出码是 0 → 真实异常被静默。
改成「退出码 0 且 status=ok」之后, 这一段解析就是告警链路的判定核心:
解析错了要么天天误报, 要么把真失败静默掉。
"""

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import health_gate_status  # noqa: E402


OK_REPORT = '{"status": "ok", "errors": [], "checks": [{"check": "a", "status": "ok"}]}'
WARN_REPORT = '{"status": "warn", "errors": ["no_summary"], "checks": []}'
FAIL_REPORT = '{"status": "fail", "errors": ["db_missing"], "checks": []}'


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
    """cron_healthgate.sh 靠 stdout 单行 + 退出码 0 取结果.

    输入走**参数**而不是 stdin: 本机 cron 有 pipe_to_interpreter 安全扫描
    (把命令输出 pipe 进解释器 → pending_approval, 无人审批即卡死)。
    """

    def _run(self, capsys, *argv):
        assert health_gate_status.main(["health_gate_status.py", *argv]) == 0
        return capsys.readouterr().out.strip()

    def test_status_from_argv(self, capsys):
        assert self._run(capsys, WARN_REPORT) == "warn"

    def test_no_args_is_fail(self, capsys):
        assert self._run(capsys) == "fail"

    def test_multiline_json_survives_argv(self, capsys):
        raw = json.dumps(json.loads(FAIL_REPORT), ensure_ascii=False, indent=2)
        assert self._run(capsys, raw) == "fail"   # 单参传入, 换行不被吞

    def test_fail_report(self, capsys):
        assert self._run(capsys, FAIL_REPORT) == "fail"

    def test_fingerprint_mode(self, capsys):
        assert self._run(capsys, "--fingerprint", WARN_REPORT) == "no_summary"

    def test_truncate_mode_is_char_safe(self, capsys):
        text = "中文" * 10
        out = self._run(capsys, "--truncate", "5", text)
        assert out.startswith("中文中文中")
        assert "…(已截断)" in out


class TestFingerprint:
    """同一份持续失败在当天各次 gate 上要有**相同**指纹, 否则去重失效、每小时一张卡."""

    BASE = '{"status": "warn", "errors": ["high_failure_rate"], "checks": [' \
           '{"check": "crawl_failures", "status": "warn", "detail": "9 stocks skipped"},' \
           '{"check": "db_exists", "status": "ok"}]}'

    def test_detail_changes_do_not_change_fingerprint(self):
        other = self.BASE.replace("9 stocks skipped", "9 stocks skipped, 3 小时前更新")
        assert (health_gate_status.fingerprint(self.BASE)
                == health_gate_status.fingerprint(other))

    def test_new_error_code_changes_fingerprint(self):
        other = self.BASE.replace('"errors": ["high_failure_rate"]',
                                 '"errors": ["high_failure_rate", "no_summary"]')
        assert (health_gate_status.fingerprint(self.BASE)
                != health_gate_status.fingerprint(other))

    def test_ok_checks_do_not_enter_fingerprint(self):
        assert "db_exists" not in health_gate_status.fingerprint(self.BASE)

    def test_unparseable_has_stable_fingerprint(self):
        assert health_gate_status.fingerprint("not json") == "unparseable"
        assert (health_gate_status.fingerprint("also not json")
                == health_gate_status.fingerprint("not json"))


class TestTruncate:
    def test_short_text_untouched(self):
        assert health_gate_status.truncate("短", 5) == "短"

    def test_exactly_limit_untouched(self):
        assert health_gate_status.truncate("abcde", 5) == "abcde"

    def test_cjk_never_split_mid_character(self):
        """按字节切会切坏中文 → notify_card.py 侧 UnicodeDecodeError → 卡片静默降级."""
        text = "中" * 3000
        out = health_gate_status.truncate(text, 2000)
        out.encode("utf-8").decode("utf-8")          # 非法 UTF-8 会在这里炸
        assert len(out) == 2000 + len("\n…(已截断)")
