"""Hot word pre-filter regression tests.

Validates the four-layer noise filtering that prevents false P0/P1 alerts.
Each test case maps to a real historical false positive documented in the
hot-word-*.md references.
"""

from src import detector


# ════════════════════════════════════════════════════════
# Layer 1: _CN_STOPWORDS (fixed stoplist, filtered during TF-IDF)
# ════════════════════════════════════════════════════════

class TestCnStopwords:
    """Unit words and rendered placeholders must be in the stoplist."""

    def test_unit_words_in_stoplist(self):
        """6/24 300750.SZ「万元」z=9.84 P0 — the original missing word."""
        assert "万元" in detector._CN_STOPWORDS
        assert "亿元" in detector._CN_STOPWORDS
        assert "万亿" in detector._CN_STOPWORDS

    def test_rendered_placeholders_in_stoplist(self):
        """6/27 300750.SZ「网页链接」z=3.78 P1."""
        assert "网页链接" in detector._CN_STOPWORDS
        assert "图片" in detector._CN_STOPWORDS

    def test_exchange_codes_in_stoplist(self):
        """hk/sz/sh suffixes from stock codes like 00068.HK."""
        assert "hk" in detector._CN_STOPWORDS
        assert "sz" in detector._CN_STOPWORDS
        assert "sh" in detector._CN_STOPWORDS

    def test_real_signal_words_not_in_stoplist(self):
        """Words that were real P1 signals must NOT be filtered."""
        for word in ["yoyo", "molly", "labubu", "钠电池", "碳酸锂"]:
            assert word not in detector._CN_STOPWORDS, f"{word!r} should not be stopword"


# ════════════════════════════════════════════════════════
# Layer 2a: _is_short_token
# ════════════════════════════════════════════════════════

class TestIsShortToken:
    """Short tokens like pe/ai/etf hit 80-100% of posts but carry no signal."""

    def test_filters_english_short_tokens(self):
        """6/24 9992.HK「pe」z=5.00, 6/22 300750.SZ「ai」z=3.40."""
        assert detector._is_short_token("pe") is True
        assert detector._is_short_token("ai") is True
        assert detector._is_short_token("etf") is True
        assert detector._is_short_token("ipo") is True

    def test_preserves_english_signal_words(self):
        """Real signal words must pass (longer tokens)."""
        assert detector._is_short_token("yoyo") is False  # 4 chars
        assert detector._is_short_token("molly") is False  # 5 chars
        assert detector._is_short_token("labubu") is False  # 6 chars

    def test_filters_short_chinese_tokens(self):
        """2-char Chinese tokens like 万元, 亿."""
        assert detector._is_short_token("万元") is True

    def test_preserves_chinese_signal_words(self):
        """Real Chinese topic words must pass (longer)."""
        assert detector._is_short_token("钠电池") is False  # 3 cn chars, len 3
        assert detector._is_short_token("碳酸锂") is False
        assert detector._is_short_token("泡泡玛特") is False  # 4 cn chars


# ════════════════════════════════════════════════════════
# Layer 2b: _is_username_like
# ════════════════════════════════════════════════════════

class TestIsUsernameLike:
    """Usernames in @mention patterns must be filtered."""

    def test_detects_username_in_reply_pattern(self):
        """6/23 PDD.US「多伦多的大道信徒」z=5.44 — 8/8 in @mentions."""
        word = "多伦多的大道信徒"
        posts = [
            "回复 @多伦多的大道信徒 : 分析得很好",
            "// @多伦多的大道信徒 : 拼多多护城河深",
            "回复 @多伦多的大道信徒 : 同意",
            "@多伦多的大道信徒 你怎么看",
        ]
        assert detector._is_username_like(word, posts) is True

    def test_detects_username_partial_mention_ratio(self):
        """70% threshold: 3/4 mentions → username, 2/4 → not."""
        word = "某用户名"
        # 3/4 = 75% > 70% → username
        posts_75 = [
            "@某用户名 : 说得对",
            "@某用户名 : 同意",
            "@某用户名 : 分析到位",
            "某用户名的观点值得商榷",  # not @mention
        ]
        assert detector._is_username_like(word, posts_75) is True

        # 2/4 = 50% < 70% → not username (topic word)
        posts_50 = [
            "@某用户名 : 说得对",
            "@某用户名 : 同意",
            "今天讨论某用户名的最新观点",
            "某用户名这个词火了",
        ]
        assert detector._is_username_like(word, posts_50) is False

    def test_preserves_real_topic_words(self):
        """6/28 9992.HK「yoyo」z=4.98 — appears in topic discussion, not @mentions."""
        word = "yoyo"
        posts = [
            "名创 yoyo，一年不到，就开始抢泡泡市场",
            "名创已经游过了泡泡的护城河，开始抢星星人的粉丝了",
            "yoyo 这个 IP 有潜力",
            "对比一下 yoyo 和 labubu 的设计",
        ]
        assert detector._is_username_like(word, posts) is False

    def test_returns_false_for_zero_occurrences(self):
        """Edge case: word not in any post text."""
        assert detector._is_username_like("不存在", ["无关内容"]) is False
