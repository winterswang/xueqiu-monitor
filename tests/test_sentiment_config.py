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


# ════════════════════════════════════════════════════════
# score_news_post (2026-09-20: opencli news 并入路径的关键词打分)
# ════════════════════════════════════════════════════════


class TestScoreNewsPost:
    """Public single-headline scorer must match _analyze_news semantics."""

    def test_bullish_headline(self):
        assert sentiment.score_news_post("公司宣布大额回购股份") == 0.5

    def test_bearish_headline(self):
        assert sentiment.score_news_post("股价暴跌, 股东拟减持") == -0.5

    def test_mixed_signal_neutral(self):
        # 同时含看涨与看跌词 → 0.0 (与 _analyze_news 的 mixed 分支一致)
        title = "暴涨之后又暴跌"
        assert sentiment.score_news_post(title) == 0.0

    def test_no_signal_neutral(self):
        assert sentiment.score_news_post("公司发布中期报告") == 0.0

    def test_content_used_as_fallback(self):
        # 标题无信号但正文前 100 字含看跌词 → -0.5
        assert sentiment.score_news_post("中报出炉", "业绩暴雷") == -0.5
