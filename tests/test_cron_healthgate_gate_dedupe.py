"""gate 的告警去重与告警回退 (2026-10-15 review).

跑的是真脚本, health_check / notify_card / lark-cli 全是桩。

为什么必须测:
  - `high_failure_rate` / `detect_errors` 是**当日累计**量, 一次瞬时抖动会让
    14:25 / 15:25 … 19:05 每次 gate 各发一张几乎相同的红卡(最多 6 张)。
    去重失效 = 告警疲劳 = 真失败被淹。
  - 「卡片发不出去要回退纯文本」是脚本头注释里承诺的不变量, 原先零覆盖。
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GATE = PROJECT_ROOT / "scripts" / "cron_healthgate.sh"
STATUS_SCRIPT = PROJECT_ROOT / "scripts" / "health_gate_status.py"

WARN_A = ('{"status": "warn", "errors": ["high_failure_rate"], "checks": '
          '[{"check": "crawl_failures", "status": "warn", "detail": "9 stocks skipped"}]}')
# 同一份失败, detail 变了(计数/时长) —— 指纹必须不变
WARN_A_LATER = ('{"status": "warn", "errors": ["high_failure_rate"], "checks": '
                '[{"check": "crawl_failures", "status": "warn", "detail": "11 stocks skipped"}]}')
WARN_B = '{"status": "warn", "errors": ["no_summary"], "checks": []}'

NOTIFY_OK = "import sys\nopen(sys.argv[0] + '.calls', 'a').write('card\\n')\n"
NOTIFY_DOWN = "import sys\nopen(sys.argv[0] + '.calls', 'a').write('card-failed\\n')\nsys.exit(1)\n"
NOTIFY_HEAD = """\
import os, sys
with open(os.environ["GATE_CALLS"], "a", encoding="utf-8") as f:
    f.write("card\\n")
sys.exit(int(os.environ.get("GATE_NOTIFY_RC", "0")))
"""
# 一次调用一行(参数里的换行会被压平, 否则多行正文会把行数算成多次调用)
LARK_STUB = """\
#!/bin/bash
printf 'CALL %s\n' "$(printf '%s' "$*" | tr '\n' ' ')" >> "$GATE_CALLS_LARK"
exit 0
"""


@pytest.fixture
def gate(tmp_path: Path):
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True)
    (tmp_path / "logs").mkdir()          # 生产里也在; STATE 写这里
    shutil.copy(GATE, scripts / "cron_healthgate.sh")
    shutil.copy(STATUS_SCRIPT, scripts / "health_gate_status.py")
    (scripts / "notify_card.py").write_text(NOTIFY_HEAD, encoding="utf-8")
    (scripts / "health_check.py").write_text(
        "import os, sys\nsys.stdout.write(os.environ['GATE_STUB_OUT'])\nsys.exit(0)\n",
        encoding="utf-8")
    lark = tmp_path / "lark-cli"
    lark.write_text(LARK_STUB, encoding="utf-8")
    lark.chmod(lark.stat().st_mode | stat.S_IEXEC)
    calls = tmp_path / "notify.calls"
    lark_calls = tmp_path / "lark.calls"

    def run(out: str, notify_rc: int = 0):
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "GATE_STUB_OUT": out,
            "GATE_CALLS": str(calls),
            "GATE_CALLS_LARK": str(lark_calls),
            "GATE_NOTIFY_RC": str(notify_rc),
            "LARK_CLI": str(lark),
        }
        return subprocess.run(["bash", str(scripts / "cron_healthgate.sh")],
                              capture_output=True, text=True, timeout=60, env=env)

    return run, calls, lark_calls


def _count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0


class TestAlertDeduplication:
    def test_same_failure_alerts_once_per_day(self, gate):
        """detail 变了(计数)但错误码相同 = 同一份失败 → 当天只发一张卡."""
        run, calls, _ = gate
        assert run(WARN_A).returncode == 1
        assert _count(calls) == 1
        assert run(WARN_A_LATER).returncode == 1     # 仍然以失败退出
        assert _count(calls) == 1, "同一份失败重复发卡"

    def test_new_failure_type_alerts_again(self, gate):
        run, calls, _ = gate
        run(WARN_A)
        run(WARN_B)
        assert _count(calls) == 2

    def test_ok_run_does_not_alert_or_write_state(self, gate):
        run, calls, _ = gate
        r = run('{"status": "ok", "errors": [], "checks": []}')
        assert r.returncode == 0 and _count(calls) == 0
        assert not (Path(r.args[1]).parent.parent / "logs" / ".healthgate_last_alert").exists()

    def test_deduped_run_still_reports_failure_frame(self, gate):
        """去重只省卡片, 不能把失败帧改成成功帧."""
        run, _, _ = gate
        run(WARN_A)
        out = run(WARN_A).stdout
        assert '"ok":false' in out


class TestAlertFallback:
    """头注释承诺「卡片发不出去回退纯文本 —— 告警通道不能哑」."""

    def test_card_failure_falls_back_to_text(self, gate):
        run, calls, lark_calls = gate
        r = run(WARN_A, notify_rc=1)
        assert _count(calls) == 1
        assert _count(lark_calls) == 1, "卡片失败后没有回退纯文本"
        text = lark_calls.read_text(encoding="utf-8")
        assert "im +messages-send" in text
        assert "high_failure_rate" in text            # 详情没丢
        assert r.returncode == 1

    def test_text_fallback_is_char_truncated(self, gate):
        """按字节截断会切坏中文 → 卡片侧 UnicodeDecodeError; 按字符才是对的."""
        run, _, lark_calls = gate
        body = '{"status": "warn", "errors": ["x"], "checks": [{"check": "c", "status": "warn", "detail": "%s"}]}' % ("中文" * 900)
        run(body, notify_rc=1)
        raw = lark_calls.read_text(encoding="utf-8")
        assert "…(已截断)" in raw
        assert raw.encode("utf-8").decode("utf-8")    # 非法 UTF-8 会在这里炸
