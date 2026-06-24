"""Sentiment LLM configuration regression tests."""

from src import sentiment


def test_load_ark_env_prefers_ark_api_key(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "ark-key")
    monkeypatch.setenv("ARKCODE_API_KEY", "arkcode-key")
    monkeypatch.setenv("ARK_CODING_BASE_URL", "https://example.test/api/coding/v3")

    api_key, base_url = sentiment._load_ark_env_config()

    assert api_key == "ark-key"
    assert base_url == "https://example.test/api/coding/v3"


def test_load_ark_env_supports_arkcode_api_key(monkeypatch):
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.setenv("ARKCODE_API_KEY", "arkcode-key")
    monkeypatch.delenv("ARK_CODING_BASE_URL", raising=False)

    api_key, base_url = sentiment._load_ark_env_config()

    assert api_key == "arkcode-key"
    assert base_url == "https://ark.cn-beijing.volces.com/api/coding/v3"


def test_load_ark_env_ignores_legacy_minimax_vars(monkeypatch):
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.delenv("ARKCODE_API_KEY", raising=False)
    monkeypatch.setenv("MINIMAX_API_KEY", "legacy-key")
    monkeypatch.setenv("MINIMAX_BASE_URL", "https://api.minimaxi.com/anthropic")

    api_key, base_url = sentiment._load_ark_env_config()

    assert api_key == ""
    assert base_url == "https://ark.cn-beijing.volces.com/api/coding/v3"


def test_llm_timeouts_allow_minimax_long_batches():
    """All LLM timeouts must be >= 300s to handle minimax-m3 thinking-heavy batches.

    Regression for 2026-06-24 incident: batches with 80+ posts took 107-281s on
    ark coding plan minimax-m3; the previous 180s total cap caused fallback to
    0.0 even though HTTP returned 200 OK. Docstring/intent was always "300s";
    this test pins the actual values so any future change is a deliberate edit.
    """
    assert sentiment.LLM_CLIENT_TIMEOUT >= 300.0, (
        f"LLM_CLIENT_TIMEOUT={sentiment.LLM_CLIENT_TIMEOUT}s < 300s; "
        "large batches will be cut off and fall back to 0.0"
    )
    assert sentiment.LLM_CALL_TIMEOUT >= 300.0, (
        f"LLM_CALL_TIMEOUT={sentiment.LLM_CALL_TIMEOUT}s < 300s"
    )
    assert sentiment.SENTIMENT_TOTAL_TIMEOUT >= 300.0, (
        f"SENTIMENT_TOTAL_TIMEOUT={sentiment.SENTIMENT_TOTAL_TIMEOUT}s < 300s; "
        "thread-level cap will trip before LLM finishes"
    )
