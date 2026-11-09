"""X (Twitter) 舆情源接入 —— 清洗规则、降级路径、prompt 融合。

样本取自 2026-11-02 META 试点（3 切片 12 条，信噪比 9/12）：
`~/cindy-workspace/素材/x-舆情/META-20261102-试点评估.md`。这里把它固化成
可回归的夹具，重点验证三类清洗：

- 诈骗引流（WhatsApp / wa.me / Message my assistant / $2K→$500K）
- 全文复制 + 「Follow me」的抄袭号（归一化指纹去重）
- live 切片低互动降权（不删除，排到末尾）

以及设计约束：**目录缺失/坏 JSON 一律降级为空，不抛、不阻塞日报**。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import x_sources  # noqa: E402
from src import report_generator  # noqa: E402


# ------------------------------------------------------------------ 夹具 --

def _post(tweet_id, text, *, slice_="top", like=0, reply=0, repost=0,
          created="2026-11-02T03:00:00Z", author="@trader", url=None):
    return {
        "tweet_id": tweet_id,
        "url": url if url is not None else f"https://x.com/trader/status/{tweet_id}",
        "author": author,
        "text": text,
        "lang": "en",
        "slice": slice_,
        "created_at_utc": created,
        "metrics": {"like": like, "reply": reply, "repost": repost},
    }


# 试点样本：9 条实质 + 3 条杂音（1 抄袭 / 1 诈骗 / 1 低质）
META_PILOT = {
    "ticker": "META",
    "date": "2026-11-02",
    "posts": [
        # —— 实质：NVDA 拟购 $5B META 股票传闻 ——
        _post("1", "Kalshi just listed a market on NVDA acquiring $5B of META stock. Unconfirmed but wild.",
              slice_="min_faves", like=820, reply=140, repost=210),
        _post("2", "If NVDA really buys $5B of META it's a strategic alignment, not a buyback.",
              slice_="top", like=310, reply=45, repost=60),
        # —— 实质：Muse AI 补贴争议 ——
        _post("3", "Meta giving away Muse tokens at massive scale is the one advantage nobody can copy.",
              slice_="live", like=44, reply=12, repost=6, created="2026-11-02T05:10:00Z"),
        _post("4", "Subsidizing billions in Muse tokens while revenue from it is a rounding error. Burning cash.",
              slice_="live", like=51, reply=19, repost=7, created="2026-11-02T05:40:00Z"),
        # —— 实质：人形机器人 Max ——
        _post("5", "Meta's humanoid robot Max uses Jetson Thor and was built in 3 months with Fourier.",
              slice_="min_faves", like=1200, reply=90, repost=340),
        # —— 实质：期权结构 ——
        _post("6", "614 heavy put support, dips getting absorbed fast.", slice_="live",
              like=28, reply=4, repost=3),
        # —— 实质：盘面/情绪 ——
        _post("7", "META relative strength vs QQQ today is notable.", slice_="top",
              like=210, reply=11, repost=8),
        _post("8", "Anyone else find it funny that META is now the boring mega cap?", slice_="top",
              like=180, reply=30, repost=5),
        _post("9", "Street estimates for Q4 ad revenue look conservative given Reels monetization.",
              slice_="min_faves", like=430, reply=52, repost=77),
        # —— 杂音①：全文复制 + Follow me（与 #5 同文本，指纹应撞上）——
        _post("10", "Meta's humanoid robot Max uses Jetson Thor and was built in 3 months with Fourier. Follow me for more!",
              slice_="top", like=3, reply=0, repost=0, author="@copycat"),
        # —— 杂音②：诈骗引流 ——
        _post("11", "$2K→$500K in 3 months. Message my assistant on WhatsApp wa.me/1234567",
              slice_="live", like=9, reply=0, repost=0, author="@scammer"),
        # —— 杂音③：live 低互动噪音 ——
        _post("12", "gm everyone, hoping for a green day across the whole market",
              slice_="live", like=1, reply=0, repost=0, author="@noise"),
    ],
}


@pytest.fixture
def x_root(tmp_path):
    return tmp_path / "x_posts"


def _write(root: Path, date_str: str, ticker: str, payload) -> None:
    d = root / date_str
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ticker}.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


# ------------------------------------------------------------ 降级路径 --

def test_missing_root_returns_empty():
    assert x_sources.load_x_posts("/nonexistent/path", "2026-11-02", "META.US") == []


def test_missing_file_returns_empty(x_root):
    x_root.mkdir()
    assert x_sources.load_x_posts(x_root, "2026-11-02", "META.US") == []


def test_broken_json_returns_empty_not_raise(x_root):
    _write(x_root, "2026-11-02", "META", "{not json")
    assert x_sources.load_x_posts(x_root, "2026-11-02", "META.US") == []


def test_posts_not_a_list_returns_empty(x_root):
    _write(x_root, "2026-11-02", "META", {"posts": "oops"})
    assert x_sources.load_x_posts(x_root, "2026-11-02", "META.US") == []


def test_non_us_code_ignored_by_caller_contract():
    """is_us 是接入开关：非美股不读 X 源。"""
    assert x_sources.is_us("META.US") is True
    assert x_sources.is_us("600519.SH") is False
    assert x_sources.is_us("00700.HK") is False


# ------------------------------------------------------------ 清洗规则 --

def test_pilot_sample_cleaning(x_root):
    """12 条试点样本 → 诈骗 1 条丢弃、抄袭 1 条按指纹去重、其余 10 条保留。"""
    _write(x_root, "2026-11-02", "META", META_PILOT)
    posts = x_sources.load_x_posts(x_root, "2026-11-02", "META.US")

    ids = [p["tweet_id"] for p in posts]
    assert "11" not in ids, "诈骗帖必须被丢弃"
    assert "10" not in ids, "全文复制+Follow me 的抄袭号应被指纹去重"
    assert len(posts) == 10

    # 去重保留互动更高的一条（#5 而非 #10）
    assert "5" in ids


def test_scam_patterns_direct():
    for text in [
        "DM me on WhatsApp",
        "check wa.me/9999",
        "Message my assistant now",
        "$2K→$500K guaranteed",
        "$500 aiming for $5,000",
        "join my private group",
    ]:
        assert x_sources._is_scam(text), text


def test_legit_text_not_flagged():
    for text in [
        "META reports Q3 earnings next week",
        "The stock is aiming for a breakout above 620",
    ]:
        assert not x_sources._is_scam(text), text


def test_known_false_positive_is_documented():
    """已知取舍，用例固化：WhatsApp 作为特征词是为了不漏杀猪盘，
    代价是正常讨论里提到 WhatsApp 也会被误杀。X 源用于爬观点，
    漏掉一条正常帖的损失远小于放过一条引流帖。"""
    assert x_sources._is_scam("WhatsApp user growth is a tailwind for Meta") is True


def test_copy_detection_handles_appended_suffix():
    """抄袭号 = 原文 + 追加后缀，前缀包含必须命中。"""
    orig = "Meta's humanoid robot Max uses Jetson Thor and was built in 3 months with Fourier."
    copy = orig + " Follow me for more!"
    assert x_sources._is_copy(x_sources._norm_text(orig), x_sources._norm_text(copy))


def test_copy_detection_ignores_short_fragments():
    """短碎片不参与包含比较，避免 "gm" 把整批吞掉。"""
    assert not x_sources._is_copy("gm", "gm everyone welcome to my timeline today")


def test_live_low_engagement_sorted_last(x_root):
    _write(x_root, "2026-11-02", "META", META_PILOT)
    posts = x_sources.load_x_posts(x_root, "2026-11-02", "META.US")
    # 低互动的 live 帖（#12 gm, #6 互动 35 但>=5 不算弱）应排在最后
    assert posts[-1]["tweet_id"] == "12"
    # 高互动的 min_faves 帖应在前面
    assert posts[0]["tweet_id"] == "5"


def test_url_dedup(x_root):
    same = "https://x.com/a/status/777"
    payload = {"posts": [
        _post("777", "First copy of the story that everyone keeps reposting", url=same, like=100),
        _post("777", "First copy of the story that everyone keeps reposting", url=same, like=100, slice_="live"),
    ]}
    _write(x_root, "2026-11-02", "META", payload)
    assert len(x_sources.load_x_posts(x_root, "2026-11-02", "META.US")) == 1


# -------------------------------------------------------------- 时区 --

def test_utc_converted_to_beijing(x_root):
    payload = {"posts": [_post("1", "late night takeover rumor circulating widely on the timeline", created="2026-11-01T20:00:00Z")]}
    _write(x_root, "2026-11-02", "META", payload)
    p = x_sources.load_x_posts(x_root, "2026-11-02", "META.US")[0]
    # UTC 20:00 → 北京次日 04:00
    assert p["time"] == "11-02 04:00"


def test_missing_time_is_safe(x_root):
    payload = {"posts": [{"text": "no timestamp here but the body is long enough to pass", "metrics": {"like": 5}}]}
    _write(x_root, "2026-11-02", "META", payload)
    p = x_sources.load_x_posts(x_root, "2026-11-02", "META.US")[0]
    assert p["time"] == "时间未知"


# ------------------------------------------------------- prompt 融合 --

def _prompt(posts, x_posts):
    return report_generator._build_analysis_prompt(
        "Meta Platforms", "META.US", posts,
        {"has_trend": False, "days": 0}, [], ["ai"], x_posts=x_posts,
    )


def test_prompt_contains_x_block(x_root):
    _write(x_root, "2026-11-02", "META", META_PILOT)
    raw = x_sources.load_x_posts(x_root, "2026-11-02", "META.US")
    text = _prompt([], x_sources.as_prompt_posts(raw))
    assert "## X 源英文舆情" in text
    assert "[X1]" in text
    assert "未证实" in text


def test_prompt_omits_x_block_when_empty():
    text = _prompt([], [])
    assert "## X 源英文舆情" not in text


def test_x_block_independent_of_xueqiu_truncation(x_root):
    """雪球帖超量截断时，X 段仍完整出现（两源额度独立）。"""

    _write(x_root, "2026-11-02", "META", META_PILOT)
    raw = x_sources.load_x_posts(x_root, "2026-11-02", "META.US")
    many_xueqiu = [
        {"title": f"雪球帖{i}", "content": "内容" * 200, "author": "u",
         "like_count": 1000 - i, "forward_count": 0, "comment_count": 0,
         "link": f"https://xueqiu.com/{i}", "time": "11-02 10:00"}
        for i in range(120)
    ]
    text = _prompt(many_xueqiu, x_sources.as_prompt_posts(raw))
    # 雪球侧确实被截断（条数上限或正文总量上限，两者之一必触发）
    assert ("已截取前" in text) or ("按互动量取前" in text)
    assert "## X 源英文舆情" in text    # X 段仍在


# ------------------------------------------------- analyze_stock 集成 --

def _min_config(x_root):
    return {
        "llm": {"min_post_length": 30, "max_posts_by_tier": {}},
        "tier": {},
        "x_source": {"enabled": True, "root": str(x_root)},
    }


class _FakeCompletions:
    def __init__(self, sink):
        self._sink = sink

    def create(self, **kwargs):
        from types import SimpleNamespace
        self._sink.append(kwargs["messages"][0]["content"])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="#### 分析\nX 源测试内容 [X1]"))]
        )


def _fake_client(sink):
    from types import SimpleNamespace
    return SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions(sink))
    )


@pytest.fixture
def empty_db(tmp_path):
    from src import db as dbmod
    path = str(tmp_path / "monitor.db")
    dbmod.init_db(path)
    return path


def test_analyze_stock_uses_x_when_xueqiu_empty(empty_db, x_root, monkeypatch):
    """雪球当天 0 帖、X 有料时必须走 LLM 分析，而不是「无新增量」占位。"""
    _write(x_root, "2026-11-02", "META", META_PILOT)
    sink = []
    monkeypatch.setattr(report_generator, "_get_llm_client", lambda: _fake_client(sink))
    monkeypatch.setattr(report_generator, "resolve_model", lambda cfg: "fake-model")

    out = report_generator.analyze_stock(
        "META.US", "Meta Platforms", empty_db, "2026-11-02", _min_config(x_root)
    )

    assert out.get("analyzed", True) is True
    assert "（无新增量）" not in out["section"]
    assert "X 源:" in out["section"]          # caliber 带上 X 计数
    assert sink and "## X 源英文舆情" in sink[0]


def test_analyze_stock_degrades_when_x_missing(empty_db, tmp_path, monkeypatch):
    """X 目录不存在 → 纯雪球路径，雪球也没帖则回到「无新增量」，不报错。"""
    sink = []
    monkeypatch.setattr(report_generator, "_get_llm_client", lambda: _fake_client(sink))
    monkeypatch.setattr(report_generator, "resolve_model", lambda cfg: "fake-model")

    out = report_generator.analyze_stock(
        "META.US", "Meta Platforms", empty_db, "2026-11-02",
        _min_config(tmp_path / "no_such_dir"),
    )

    assert out.get("analyzed", True) is False
    assert "（无新增量）" in out["section"]
    assert not sink


def test_non_us_stock_ignores_x(empty_db, x_root, monkeypatch):
    """A 股不读 X 源，即使目录里有同名文件。"""
    _write(x_root, "2026-11-02", "600519", META_PILOT)
    sink = []
    monkeypatch.setattr(report_generator, "_get_llm_client", lambda: _fake_client(sink))
    monkeypatch.setattr(report_generator, "resolve_model", lambda cfg: "fake-model")

    out = report_generator.analyze_stock(
        "600519.SH", "贵州茅台", empty_db, "2026-11-02", _min_config(x_root)
    )
    assert "X 源:" not in out["section"]
    assert not sink


# ------------------------------------ 真实数据（2026-11-02 首批）暴露的问题 --

def test_short_text_filtered(x_root):
    """真实数据里的「يا حبي لج يا $META」(17 字) 与占位串都太短，无分析价值。"""
    payload = {"posts": [
        _post("1", "short noise", like=100),
        _post("2", "this one is long enough to keep because it has substance", like=50),
    ]}
    _write(x_root, "2026-11-02", "META", payload)
    posts = x_sources.load_x_posts(x_root, "2026-11-02", "META.US")
    assert [p["tweet_id"] for p in posts] == ["2"]


def test_days_ago_and_fresh_flag(x_root):
    """热门切片抓的是历史帖：必须标出距日报日的天数，且能区分当日/历史。

    实测 META 首批 12 条里 7 条在 2–7 天前 —— 不区分就会把一周前的旧闻
    写成「今日新增」。
    """
    payload = {"posts": [
        _post("old", "a week old post about the humanoid robot prototype",
              created="2026-10-26T10:00:00Z", like=900),
        _post("new", "today post about the same topic still developing",
              created="2026-11-02T05:00:00Z", like=10),
    ]}
    _write(x_root, "2026-11-02", "META", payload)
    posts = {p["tweet_id"]: p for p in x_sources.load_x_posts(x_root, "2026-11-02", "META.US")}
    assert posts["old"]["is_fresh"] is False
    assert posts["old"]["days_ago"] > 5
    assert posts["new"]["is_fresh"] is True


def test_prompt_separates_fresh_and_historical():
    """历史高互动帖必须单独分组，并明令禁止写成「今日新增」。"""
    fresh = [{"title": "", "content": "today fresh content about earnings",
              "author": "a", "like_count": 5, "forward_count": 0, "comment_count": 0,
              "link": "u1", "time": "11-02 10:00", "source": "x", "slice": "live",
              "days_ago": 0.4, "is_fresh": True}]
    hist = [{"title": "", "content": "week old high engagement post about robot",
             "author": "b", "like_count": 900, "forward_count": 50, "comment_count": 20,
             "link": "u2", "time": "10-26 09:00", "source": "x", "slice": "top",
             "days_ago": 6.9, "is_fresh": False}]
    text = report_generator._build_analysis_prompt(
        "Meta", "META.US", [], {"has_trend": False, "days": 0}, [], ["ai"],
        x_posts=fresh + hist,
    )
    assert "### 当日（1 条）" in text
    assert "### 历史高互动（1 条" in text
    assert "禁止写成「今日新增」" in text
    assert "7天前" in text          # 6.9 天按 0 位小数渲染为 7
    assert "String Metaverse" in text      # ticker 撞名提醒


# ---------------------------------- 采集契约 v2（2026-11-03）：views / partial --

def test_metrics_partial_demoted(x_root):
    """采集侧标记 metrics_partial 的帖互动含义不可信，必须降到末尾而不是当零互动。"""
    good = _post("good", "normal post with reliable engagement numbers", like=10, reply=2, repost=1)
    bad = _post("bad", "partial metrics post but the content itself is substantive", like=0, reply=0, repost=0)
    bad["metrics_partial"] = True
    _write(x_root, "2026-11-02", "META", {"posts": [bad, good]})

    posts = x_sources.load_x_posts(x_root, "2026-11-02", "META.US")
    assert [p["tweet_id"] for p in posts] == ["good", "bad"]
    assert posts[1]["metrics_partial"] is True


def test_views_do_not_inflate_engagement(x_root):
    """单数字被采集侧判为 views：高曝光低讨论的帖不能靠浏览量挤到前面。"""
    viral = _post("viral", "post with huge views but zero real engagement", like=0, reply=0, repost=0)
    viral["metrics"]["views"] = 999_999
    normal = _post("normal", "normal post with modest but real engagement", like=10, reply=2, repost=1)
    _write(x_root, "2026-11-02", "META", {"posts": [viral, normal]})

    posts = x_sources.load_x_posts(x_root, "2026-11-02", "META.US")
    assert posts[0]["tweet_id"] == "normal"
    viral_out = next(p for p in posts if p["tweet_id"] == "viral")
    assert viral_out["views"] == 999_999


def test_prompt_surfaces_partial_and_views():
    """提示词里要显式区分「互动不可信」与「有浏览量」，别让模型自己猜。"""
    partial = {"title": "", "content": "partial metrics post with real content",
               "author": "a", "like_count": 0, "forward_count": 0, "comment_count": 0,
               "link": "u1", "time": "11-02 10:00", "source": "x", "slice": "live",
               "days_ago": 0.3, "is_fresh": True, "views": 0, "metrics_partial": True}
    withviews = {"title": "", "content": "post with views but no likes at all",
                 "author": "b", "like_count": 0, "forward_count": 0, "comment_count": 0,
                 "link": "u2", "time": "11-02 11:00", "source": "x", "slice": "top",
                 "days_ago": 0.3, "is_fresh": True, "views": 1234, "metrics_partial": False}
    text = report_generator._build_analysis_prompt(
        "Meta", "META.US", [], {"has_trend": False, "days": 0}, [], ["ai"],
        x_posts=[partial, withviews],
    )
    assert "互动数不可信" in text
    assert "👁1234" in text
