"""0 帖守卫（2026-10-16）: 「没抓到」不是「今天没帖、情绪转中性」。

风控页 / 登录失效 / 超时 / 水位过滤 都产出**空结果**而非异常，而 crawler 把空结果
无条件写成 `status="success"`、`sentiment_avg=0.0`。0.0 一旦进 `detect_sentiment_shift`，
就是拿「抓不到」去比历史（约 +0.3）→ 凭空推出情感突变。

实证：库里有 74 条 `posts_count=0, status='success', sentiment_avg=0.0` 的快照；
其中 9 例当天确实触发了 `sentiment_shift`（实测都是 P2 —— 噪声过滤器压住了等级，
但假告警本身仍然是错的）。
"""

from pathlib import Path

from src.detector import has_activity_signal


class TestHasActivitySignal:
    def test_zero_posts_is_not_a_signal(self):
        assert has_activity_signal(0) is False

    def test_any_post_is_a_signal(self):
        for n in (1, 5, 100):
            assert has_activity_signal(n) is True


class TestProductionPathUsesTheGuard:
    """契约: cli.py 的检测入口必须走这个判定，而不是各写一份。"""

    def test_cli_guards_detection_with_it(self):
        src = (Path(__file__).resolve().parent.parent / "src" / "cli.py").read_text(
            encoding="utf-8"
        )
        assert "has_activity_signal(cr[" in src, \
            "cli.py 的裸计数守卫被移除了 —— 0 帖又会进检测器"
        # 两个检测都必须落在守卫的 else 分支里
        guard = src.index("has_activity_signal(cr[")
        seg = src[guard:guard + 1600]
        assert "detect_post_spike" in seg and "detect_sentiment_shift" in seg
