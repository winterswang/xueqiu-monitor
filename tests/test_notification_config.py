"""Tests for notification config consumption (Task 3).

Before fix: notification.max_retries (=2) and push_timeout (=5) were
defined in config but never consumed — _send_via_lark_cli hardcoded
timeout=30 and never retried.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class TestNotificationConfigConsumption:
    """Task 3: _send_via_lark_cli and dispatch_messages should consume
    push_timeout and max_retries from notification config."""

    def test_send_via_lark_cli_accepts_timeout(self, monkeypatch):
        """_send_via_lark_cli should accept a timeout parameter."""
        from src import notifier

        captured_timeout = {}

        def fake_run(cmd, **kwargs):
            captured_timeout["timeout"] = kwargs.get("timeout")
            # Simulate success

            class FakeResult:
                returncode = 0
                stderr = ""
            return FakeResult()

        monkeypatch.setattr(notifier.subprocess, "run", fake_run)
        notifier._send_via_lark_cli("test", "chat123", push_timeout=5)

        assert captured_timeout["timeout"] == 5, \
            f"Should use push_timeout=5, got {captured_timeout}"

    def test_send_via_lark_cli_retries_on_failure(self, monkeypatch):
        """_send_via_lark_cli should retry max_retries times on failure."""
        from src import notifier

        call_count = 0

        def fake_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1

            class FakeResult:
                returncode = 1  # failure
                stderr = "simulated error"
            return FakeResult()

        monkeypatch.setattr(notifier.subprocess, "run", fake_run)
        monkeypatch.setattr(notifier.time, "sleep", lambda _: None)

        result = notifier._send_via_lark_cli(
            "test", "chat123", push_timeout=5, max_retries=2
        )

        # 1 initial + 2 retries = 3 calls
        assert call_count == 3, f"Expected 3 calls (1+2 retries), got {call_count}"
        assert result is False, "All failures should return False"

    def test_dispatch_messages_passes_config(self, monkeypatch):
        """dispatch_messages should forward push_timeout/max_retries to sender."""
        from src import notifier

        captured = {}

        def fake_send(text, chat_id, push_timeout=30, max_retries=0):
            captured["push_timeout"] = push_timeout
            captured["max_retries"] = max_retries
            return True

        monkeypatch.setattr(notifier, "_send_via_lark_cli", fake_send)
        notifier.dispatch_messages(
            ["msg1"], "/tmp/test_pending.json",
            mode="lark_cli", lark_chat_id="chat123",
            push_timeout=5, max_retries=2,
        )

        assert captured.get("push_timeout") == 5
        assert captured.get("max_retries") == 2
