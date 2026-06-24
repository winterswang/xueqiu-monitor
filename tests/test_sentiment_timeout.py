"""Timeout behavior regression tests for sentiment analysis.

Pins the 2026-06-24 incident fix: SENTIMENT_TOTAL_TIMEOUT bumped 180s -> 300s
to accommodate minimax-m3 thinking-heavy batches (which took 107-281s on the
ark coding plan). We use a lowered timeout + real sleep to simulate the
old/new cap behavior in milliseconds instead of waiting minutes.
"""

import time
from unittest.mock import MagicMock

import pytest

from src import sentiment


class _SlowChoices:
    """Mimics OpenAI response.choices[0] with the minimum surface we read."""

    def __init__(self, content: str, finish_reason: str = "stop"):
        self.message = MagicMock()
        self.message.content = content
        self.message.reasoning_content = ""
        self.finish_reason = finish_reason


def _make_mock_client(delay_s: float, n_items: int):
    """Build a stub client that sleeps `delay_s` then returns a valid JSON response."""
    client = MagicMock()

    def _create(**kwargs):
        time.sleep(delay_s)
        body = ",".join(f'{{"i":{i},"s":0.5}}' for i in range(n_items))
        response = MagicMock()
        response.choices = [_SlowChoices(f"[{body}]")]
        return response

    client.chat.completions.create.side_effect = _create
    return client


def _make_posts(n: int) -> list[dict]:
    return [{"type": "discussion", "title": f"post {i}", "content": ""} for i in range(n)]


def test_total_timeout_triggers_fallback_when_exceeded(monkeypatch):
    """Simulate the OLD bug: a 200ms call under a 50ms cap -> fallback 0.0.

    This proves SENTIMENT_TOTAL_TIMEOUT is *actually* enforced (i.e. the
    thread-level cap is wired correctly). Same wiring, just scaled down.
    """
    monkeypatch.setattr(sentiment, "SENTIMENT_TOTAL_TIMEOUT", 0.05)
    monkeypatch.setattr(sentiment, "_client", _make_mock_client(delay_s=0.5, n_items=20))
    monkeypatch.setattr(sentiment, "_get_client", lambda: sentiment._client)

    scores = sentiment.analyze_sentiment_batch(_make_posts(20))

    assert scores == [0.0] * 20, "batch should fall back to 0.0 when cap exceeded"


def test_total_timeout_does_not_trigger_when_within_cap(monkeypatch):
    """Inverse: a 30ms call under a 5s cap -> real scores returned.

    Proves the cap doesn't trigger false positives — if a real LLM call
    returns in <300s (the new cap), scores make it through unmolested.
    """
    monkeypatch.setattr(sentiment, "SENTIMENT_TOTAL_TIMEOUT", 5.0)
    monkeypatch.setattr(sentiment, "_client", _make_mock_client(delay_s=0.03, n_items=20))
    monkeypatch.setattr(sentiment, "_get_client", lambda: sentiment._client)

    scores = sentiment.analyze_sentiment_batch(_make_posts(20))

    assert len(scores) == 20
    assert all(s == 0.5 for s in scores), (
        f"expected all 0.5 from mock, got {scores!r}"
    )


def test_new_default_cap_at_least_300s():
    """Pin the production value: 2026-06-24 incident fix sets the cap to 300s.

    If you intentionally lower this, update the test comment so the regression
    is visible in code review.
    """
    assert sentiment.SENTIMENT_TOTAL_TIMEOUT >= 300.0, (
        f"SENTIMENT_TOTAL_TIMEOUT={sentiment.SENTIMENT_TOTAL_TIMEOUT}s. "
        "2026-06-24: minimax-m3 batches took 107-281s; 180s cap caused "
        "false fallback to 0.0. Do not lower without re-measuring batch latency."
    )
    assert sentiment.LLM_CALL_TIMEOUT >= 300.0, (
        f"LLM_CALL_TIMEOUT={sentiment.LLM_CALL_TIMEOUT}s; per-call cap too low"
    )
