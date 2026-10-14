"""Tests for send_card_now / scripts/notify_card.py.

背景: cron_healthgate.sh 的失败告警原走 `lark-cli --text`,详情被截到 400 字。
现改走 interactive 卡片(send_card_now),卡片发不出去时 shell 侧再回退纯文本。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class TestSendCardNow:
    """send_card_now 组装的卡片信封与返回语义。"""

    def test_missing_chat_id_returns_false_without_sending(self, monkeypatch):
        from src import notifier

        called = []
        monkeypatch.setattr(notifier, "_send_via_lark_cli",
                            lambda *a, **k: called.append(a) or True)

        assert notifier.send_card_now("标题", "正文", chat_id="") is False
        assert called == []

    def test_builds_interactive_card_envelope(self, monkeypatch):
        from src import notifier

        captured = {}

        def fake_send(text, chat_id, push_timeout=30, max_retries=0, card=None):
            captured.update(text=text, chat_id=chat_id, card=card, timeout=push_timeout)
            return True

        monkeypatch.setattr(notifier, "_send_via_lark_cli", fake_send)

        ok = notifier.send_card_now("⛔ gate 失败", "health_check 异常(rc=1)", color="red",
                                    chat_id="oc_test", push_timeout=12)

        assert ok is True
        assert captured["chat_id"] == "oc_test"
        assert captured["timeout"] == 12
        assert captured["text"] == "health_check 异常(rc=1)"

        card = captured["card"]
        assert card["msg_type"] == "interactive"
        assert card["card"]["header"]["title"]["content"] == "⛔ gate 失败"
        assert card["card"]["header"]["template"] == "red"
        assert card["card"]["elements"][0]["text"]["content"] == "health_check 异常(rc=1)"

    def test_returns_false_when_send_fails(self, monkeypatch):
        from src import notifier

        monkeypatch.setattr(notifier, "_send_via_lark_cli", lambda *a, **k: False)

        assert notifier.send_card_now("标题", "正文", chat_id="oc_test") is False


class TestNotifyCardCli:
    """scripts/notify_card.py 的命令行契约(shell wrapper 依赖退出码)。"""

    def _run(self, args, stdin_text, monkeypatch, send_result=True):
        import importlib.util

        captured = {}
        monkeypatch.setattr(
            "src.notifier.send_card_now",
            lambda title, message, color="red", chat_id="", push_timeout=30:
                captured.update(title=title, message=message, color=color, chat_id=chat_id)
                or send_result,
        )

        spec = importlib.util.spec_from_file_location(
            "notify_card", PROJECT_ROOT / "scripts" / "notify_card.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        monkeypatch.setattr(sys, "argv", ["notify_card.py"] + args)
        monkeypatch.setattr(sys, "stdin", type("S", (), {"read": staticmethod(lambda: stdin_text)})())
        return module.main(), captured

    def test_sends_card_from_stdin(self, monkeypatch):
        code, captured = self._run(
            ["--title", "⛔ 标题", "--color", "red", "--chat", "oc_test"],
            "第一行\n第二行\n", monkeypatch)

        assert code == 0
        assert captured["title"] == "⛔ 标题"
        assert captured["message"] == "第一行\n第二行"
        assert captured["chat_id"] == "oc_test"

    def test_failed_send_returns_nonzero(self, monkeypatch):
        code, _ = self._run(["--title", "t", "--chat", "oc_x"], "正文", monkeypatch,
                            send_result=False)

        assert code == 1

    def test_empty_stdin_is_not_sent(self, monkeypatch):
        code, captured = self._run(["--title", "t", "--chat", "oc_x"], "   \n", monkeypatch)

        assert code == 2
        assert captured == {}


class TestHealthgateWiring:
    """cron_healthgate.sh 必须走卡片入口,且保留纯文本回退。"""

    def test_healthgate_uses_card_entry_and_text_fallback(self):
        script = (PROJECT_ROOT / "scripts" / "cron_healthgate.sh").read_text(encoding="utf-8")

        assert "notify_card.py" in script
        assert "--msg-type interactive" not in script  # 卡片组装只在 python 侧
        assert "--text" in script, "卡片失败时必须保留纯文本兜底"
        assert '{"protocol":"cindy-script/1","type":"complete"}' in script
