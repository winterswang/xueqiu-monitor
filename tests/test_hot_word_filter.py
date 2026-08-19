"""Hot word pre-filter regression tests.

Validates the four-layer noise filtering that prevents false P0/P1 alerts.
Each test case maps to a real historical false positive documented in the
hot-word-*.md references.
"""

from src import detector
from src.detector import compute_tfidf


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


# ════════════════════════════════════════════════════════
# Layer 0: _tokenize jieba segmentation (Issue #22)
# ════════════════════════════════════════════════════════

class TestJiebaTokenize:
    """Verify jieba segments continuous Chinese runs instead of treating
    them as single tokens (the root cause of Issue #22)."""

    def test_company_name_is_segmented(self):
        """7/4 data: '心动公司' was a single token → polluted hot words.
        After jieba: should be split into ['心动', '公司']."""
        tokens = detector._tokenize("心动公司发布财报")
        assert "心动公司" not in tokens, "公司全名不应是单个 token"
        assert "心动" in tokens

    def test_long_company_name_is_segmented(self):
        """7/4 data: '深圳迈瑞生物医疗电子股份有限公司申请一项名为' was ONE token.
        This was the worst pollution case — 19-char string as a 'hot word'."""
        text = "深圳迈瑞生物医疗电子股份有限公司申请一项名为"
        tokens = detector._tokenize(text)
        # The full string must NOT appear as a single token
        assert text not in tokens
        # Should be broken into multiple meaningful tokens
        assert len(tokens) >= 3, f"Expected segmentation, got {tokens}"

    def test_patent_boilerplate_is_segmented(self):
        """7/4 data: '国家知识产权局信息显示' was a hot 'word'."""
        tokens = detector._tokenize("国家知识产权局信息显示")
        assert "国家知识产权局信息显示" not in tokens

    def test_english_tokens_preserved(self):
        """English tokens like PDD, NVDA should still be extracted correctly.
        jieba would split 'PDD.US' on the dot, so we use regex for English."""
        tokens = detector._tokenize("PDD.US 拼多多 Temu 出海")
        assert "pdd" in tokens  # lowercase
        assert "temu" in tokens

    def test_mixed_cn_en_both_extracted(self):
        """Mixed Chinese/English text should yield tokens from both."""
        tokens = detector._tokenize("NVDA GPU 算力 rubin 平台")
        assert "nvda" in tokens
        assert "算力" in tokens

    def test_short_tokens_filtered(self):
        """Single-char tokens (< 2 chars) should be filtered out."""
        tokens = detector._tokenize("a 是 b 的 c")
        for t in tokens:
            assert len(t) >= 2


# ════════════════════════════════════════════════════════
# Layer 1b: Expanded stopwords (Issue #22)
# ════════════════════════════════════════════════════════

class TestExpandedStopwords:
    """Verify new stopwords (media accounts, boilerplate, fragments) are present."""

    def test_media_accounts_in_stoplist(self):
        """7/4 data: 环球市场播报, 新浪证券, 格隆汇 dominated hot words."""
        for word in ["环球市场播报", "新浪证券", "格隆汇", "红岸工作室"]:
            assert word in detector._CN_STOPWORDS, f"{word!r} missing"

    def test_patent_boilerplate_in_stoplist(self):
        """7/4 data: 国家知识产权局, 申请号 etc. from patent announcements."""
        assert "国家知识产权局" in detector._CN_STOPWORDS
        assert "申请号" in detector._CN_STOPWORDS

    def test_generic_finance_in_stoplist(self):
        """同比增长/回购/增持 — high freq across all stocks, no specificity."""
        for word in ["同比增长", "回购", "增持", "减持"]:
            assert word in detector._CN_STOPWORDS, f"{word!r} missing"

    def test_company_fragments_in_stoplist(self):
        """jieba splits 心动公司→[心动,公司]; '公司' is noise."""
        for word in ["公司", "有限公司", "股份", "集团"]:
            assert word in detector._CN_STOPWORDS, f"{word!r} missing"

    def test_real_signal_words_still_pass(self):
        """Core signal words must NOT be in stoplist."""
        for word in ["labubu", "taptap", "算力", "雄安", "专利", "超声"]:
            assert word not in detector._CN_STOPWORDS, f"{word!r} should not be stopword"


# ════════════════════════════════════════════════════════
# Integration: compute_tfidf end-to-end quality (Issue #22)
# ════════════════════════════════════════════════════════

class TestTfidfQuality:
    """End-to-end: verify TF-IDF output is free of known noise patterns."""

    NOISE_PATTERNS = [
        "心动公司",           # stock full name
        "迈瑞医疗",           # stock full name
        "深圳迈瑞生物医疗",   # company legal name fragment
        "环球市场播报",       # media account
        "国家知识产权局信息显示",  # boilerplate
        "新浪证券",           # media account
    ]

    def test_no_company_names_in_tfidf(self):
        """Company full names should not appear as TF-IDF tokens."""
        # Simulate posts that would have triggered company-name pollution
        posts = [
            "心动公司今天发布了新游戏，心动公司股价大涨",
            "心动公司的TapTap平台用户增长",
            "心动公司回购股份，心动公司业绩不错",
            "游戏行业利好，心动公司受益",
        ]
        result = dict(compute_tfidf(posts, min_df=2, max_df=0.8))
        for noise in self.NOISE_PATTERNS:
            assert noise not in result, f"{noise!r} should not be a hot word: {list(result.keys())}"

    def test_meaningful_words_surface(self):
        """After jieba + stopword filtering, meaningful topic words should surface.
        Uses enough posts to meet min_df=2 threshold reliably."""
        posts = [
            "雄安新区建设加速，拼多多入驻",
            "拼多多在雄安设立新公司",
            "雄安成为互联网公司新战场",
            "拼多多雄安布局引发关注",
            "雄安新区政策落地",
            "拼多多雄安招聘启动",
            "雄安商机无限",
            "拼多多扎根雄安",
        ]
        result = compute_tfidf(posts, min_df=2, max_df=0.8)
        result_words = [w for w, _ in result]
        # 雄安 should surface (either as unigram or in a bigram)
        xiongan_found = any("雄安" in w for w in result_words)
        assert xiongan_found, f"雄安 should surface in hot words, got: {result_words}"
        # Stock name fragments should NOT dominate
        for w in result_words:
            assert "心动公司" not in w
            assert "迈瑞医疗" not in w


# ════════════════════════════════════════════════════════
# v0.7 F4: filter_noise_words (storage-path noise filter)
# ════════════════════════════════════════════════════════

class TestFilterNoiseWords:
    """The storage path must apply the same noise filters as the alert path."""

    def test_short_tokens_filtered(self):
        """ASCII ≤3 chars and Chinese ≤2-char short tokens are dropped."""
        words = ["ai", "etf", "ipo", "pe", "市场", "这个", "就是"]
        out = detector.filter_noise_words(words, ["ai 市场 这个 就是 讨论"])
        assert out == []

    def test_real_signal_words_kept(self):
        """Meaningful topic words survive."""
        words = ["碳酸锂", "yoyo", "labubu", "钠电池", "拼多多"]
        out = detector.filter_noise_words(words, ["碳酸锂 yoyo labubu 钠电池 拼多多"])
        assert set(out) == {"碳酸锂", "yoyo", "labubu", "钠电池", "拼多多"}

    def test_username_like_filtered(self):
        """A KOL name appearing mostly in @mentions is dropped."""
        posts_texts = ["回复 @多伦多的大道信徒: 说得对", "@多伦多的大道信徒 看这里"]
        out = detector.filter_noise_words(["多伦多的大道信徒", "碳酸锂"], posts_texts)
        assert out == ["碳酸锂"]

    def test_preserves_order(self):
        """Filtered output keeps input order."""
        words = ["碳酸锂", "ai", "yoyo", "etf"]
        out = detector.filter_noise_words(words, ["碳酸锂 yoyo"])
        assert out == ["碳酸锂", "yoyo"]
