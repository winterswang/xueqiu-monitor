"""回归: options 采集脚本必须输出 cindy-script/1 的 complete 帧。

2026-10-15 实测：缺这个帧时调度器把**每一轮**都记成 failed ——
    schedule fire failed: 'script exited without a complete frame;
    it must finish with a cindy-script/1 "complete" frame'
哪怕采集其实干成了（当日美股 19 只照常入库）。US 07:05 与 HK 20:00 两轮
都是这个症状，而 stdout 上本来干干净净、只有诊断日志全走 stderr 这件事实
让问题更隐蔽：跑对了、看着却是红的。
"""

import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent))  # 让 `from src import ...` 可解析

import collect_options  # noqa: E402


def _frame(capsys) -> dict:
    out = capsys.readouterr().out
    assert out.endswith("\n"), "帧必须以换行结尾"
    assert out.strip().count("\n") == 0, f"stdout 只允许一帧, 实际多行: {out!r}"
    return json.loads(out.strip())


def test_success_frame(capsys):
    collect_options.emit_complete(True)
    assert _frame(capsys) == {"protocol": "cindy-script/1", "type": "complete"}


def test_failure_frame_carries_ok_false(capsys):
    collect_options.emit_complete(False, "boom")
    frame = _frame(capsys)
    assert frame["protocol"] == "cindy-script/1"
    assert frame["type"] == "complete"
    assert frame["ok"] is False
    assert frame["error"] == "boom"


def test_failure_frame_defaults_error_text(capsys):
    collect_options.emit_complete(False)
    assert _frame(capsys)["error"] == "failed"


def test_disabled_early_return_still_emits_frame(capsys, monkeypatch, tmp_path):
    """options.enabled=false 的早退路径同样必须发帧, 否则那一轮又会被记 failed。"""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"options": {"enabled": False}}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["collect_options.py", "-c", str(cfg_path)])

    assert collect_options.main() == 0
    assert _frame(capsys)["type"] == "complete"
