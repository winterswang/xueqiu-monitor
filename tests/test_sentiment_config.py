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
