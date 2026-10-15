"""cron_healthgate.sh 的静默/告警决策 (2026-10-15).

跑的是**真脚本**, 只把 health_check.py / notify_card.py 换成桩:
验的是「health_check 退出码 + JSON status」的组合判定, 以及告警到底有没有发。
解析细节由 test_health_gate_status.py 覆盖。

背景: gate 原先只看退出码, 而 WARN 档 (no_summary / high_failure_rate /
detect_errors / source_failures) 退出 0 → 真实异常被静默, 头部注释却称会告警。
"""

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GATE = PROJECT_ROOT / "scripts" / "cron_healthgate.sh"
STATUS_SCRIPT = PROJECT_ROOT / "scripts" / "health_gate_status.py"

OK_REPORT = '{"status": "ok", "errors": [], "checks": [{"check": "a", "status": "ok"}]}'
WARN_REPORT = '{"status": "warn", "errors": ["no_summary"], "checks": []}'
FAIL_REPORT = '{"status": "fail", "errors": ["db_missing"], "checks": []}'

# 桩 notify_card.py: 记下落盘标记 + stdin 正文, 退出 0(模拟卡片发成功)
STUB_NOTIFY = """\
import os, pathlib, sys
marker = pathlib.Path(os.environ["GATE_MARKER"])
marker.write_text(sys.stdin.read(), encoding="utf-8")
"""


@pytest.fixture
def gate(tmp_path: Path):
    """搭一个临时 project root, 返回 (调用函数, 标记文件路径, health 输出可变字典)."""
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(GATE, scripts / "cron_healthgate.sh")
    shutil.copy(STATUS_SCRIPT, scripts / "health_gate_status.py")
    (scripts / "notify_card.py").write_text(STUB_NOTIFY, encoding="utf-8")
    marker = tmp_path / "alert.marker"
    state = {"out": OK_REPORT, "rc": 0}

    (scripts / "health_check.py").write_text(
        "import os, sys\n"
        "sys.stdout.write(os.environ['GATE_STUB_OUT'])\n"
        "sys.stderr.write(os.environ.get('GATE_STUB_ERR', ''))\n"
        "sys.exit(int(os.environ['GATE_STUB_RC']))\n",
        encoding="utf-8",
    )

    def run(out: str = OK_REPORT, rc: int = 0, noise: str = ""):
        state.update(out=out, rc=rc)
        return subprocess.run(
            ["bash", str(scripts / "cron_healthgate.sh")],
            capture_output=True, text=True, timeout=60,
            env={
                "PATH": "/usr/bin:/bin:/usr/local/bin",
                "HOME": str(tmp_path),
                "GATE_MARKER": str(marker),
                "GATE_STUB_OUT": out,
                "GATE_STUB_RC": str(rc),
                "GATE_STUB_ERR": noise,
            },
        )

    return run, marker, tmp_path


class TestGateDecision:
    def test_ok_is_silent(self, gate):
        run, marker, _ = gate
        r = run(OK_REPORT, 0)
        assert r.returncode == 0, r.stderr
        assert '"type":"complete"' in r.stdout
        assert "ok" not in r.stdout.replace('"complete"', "") or '"ok":false' not in r.stdout
        assert not marker.exists(), "ok 时不该发告警"

    def test_warn_with_rc0_alerts(self, gate):
        """核心修复: WARN 档退出码是 0, 但必须告警 (以前被静默)."""
        run, marker, _ = gate
        r = run(WARN_REPORT, 0)
        assert marker.exists(), "WARN 档被静默了"
        assert "no_summary" in marker.read_text(encoding="utf-8")
        assert '"ok":false' in r.stdout
        assert r.returncode == 1

    def test_fail_alerts(self, gate):
        run, marker, _ = gate
        r = run(FAIL_REPORT, 1)
        assert marker.exists()
        assert "db_missing" in marker.read_text(encoding="utf-8")
        assert r.returncode == 1

    def test_garbage_output_alerts_not_silently_ok(self, gate):
        """输出不可解析(被截断/崩溃)时宁可多报一次."""
        run, marker, _ = gate
        r = run("{not json", 0)
        assert marker.exists(), "解析失败被当成 ok 静默了"
        assert "status=fail" in marker.read_text(encoding="utf-8")
        assert r.returncode == 1

    def test_stderr_noise_does_not_flip_decision(self, gate):
        """health_check 是 2>&1 捕获的, 噪声不能把真失败变成静默."""
        run, marker, _ = gate
        r = run(WARN_REPORT, 0, noise="WARNING: 风控页\n")
        assert marker.exists()
        assert r.returncode == 1

    def test_truncated_report_with_nested_ok_alerts(self, gate):
        """外层 JSON 被截断时, 不能取到 checks 里那个 ok 而静默."""
        truncated = ('{"status": "fail", "errors": ["db_missing"], "checks": '
                     '[{"check": "db_exists", "status": "fail"}, {"check": "other", "status": "ok"')
        run, marker, _ = gate
        r = run(truncated, 1)
        assert marker.exists()
        assert r.returncode == 1
