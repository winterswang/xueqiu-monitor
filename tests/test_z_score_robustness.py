"""z-score 的两处稳健性缺陷（2026-10-16）。

实证背景：库里 5 条 SPOT.US `hot_word_surge` 告警 `z=6.5069e11`（P0，09-03~09-08），
**5 次都真的推送了出去**，还占了日报「今日要点」的头名（按 z 降序取前 N）。

复现出来的机制是「分子与分母各自坏掉一半」：

- **分母**：历史值在存库时 `round(x, 4)`，若这批语料完全相同，历史就是 N 个
  逐位相等的值 → σ 精确为 0。但只要序列里混进一个未 round 的原始浮点，它与其余值
  只差 1 个 ULP → σ ≈ 1e-17 而不是 0，`sigma == 0` 这道守卫抓不住。
- **分子**：调用点拿**未 round 的当前值**去比**round 过的历史** → 凭空多出一个
  round 误差量级的差（~5e-5）。

两者相除：5e-5 / 5.78e-17 ≈ 1e12 —— 与库里 6.5069e11 完全吻合。
"""

import datetime
import math

import numpy as np
import pytest

from src.detector import (
    HOT_WORD_MIN_HISTORY_DAYS,
    compute_z_score,
    hot_word_history_days,
)
from src.models import HotWordEvent

BASE = 0.4387                      # 库里真实存的值
ULP = float(np.nextafter(BASE, 1.0))  # 差 1 个 ULP 的邻居


class TestSigmaFloor:
    def test_ulp_noise_sigma_is_not_a_signal(self):
        """历史只差 1 个 ULP（σ≈1e-17）时必须判为「无波动」。

        旧实现只挡 `sigma == 0`，而这里是 1.4e-17 —— 非零、却毫无意义。
        """
        hist = [BASE] * 15 + [ULP]
        assert compute_z_score(BASE, hist) == 0.0

    def test_the_exact_production_combo_gives_zero(self):
        """未 round 的当前值 × round 过的历史 —— 修前会得到 ~6.5e11。"""
        hist = [BASE] * 16
        raw_curr = 0.4387376  # 原始 tfidf，比历史大一个 round 误差量级
        # 先确认这确实是「坏分子 + 坏分母」的组合
        sigma = float(np.std(hist, ddof=1))
        assert sigma == 0.0  # 分母在没有 ULP 噪声时确实是 0 —— 旧守卫能挡
        # 而混入 ULP 噪声后（真实故障形态）就不行了
        assert compute_z_score(raw_curr, [BASE] * 15 + [ULP]) == 0.0

    def test_real_variance_still_produces_z(self):
        """真波动不能被误伤 —— 这是防「修过头」。"""
        hist = [0.4387, 0.4417, 0.4387, 0.4417, 0.4387]
        z = compute_z_score(0.47, hist)
        assert z > 10, f"真突变应给出大 z, 实得 {z}"

    def test_small_but_real_variance_passes(self):
        z = compute_z_score(0.4387, [BASE, ULP * 1.0001, BASE, ULP * 1.0002])
        assert math.isfinite(z)

    def test_identical_history_still_returns_zero(self):
        """σ 精确为 0 的原有语义保持不变（这是既有行为，不是本次引入）。"""
        assert compute_z_score(0.9, [0.1] * 4) == 0.0

    def test_insufficient_points(self):
        assert compute_z_score(1.0, []) == 0.0
        assert compute_z_score(1.0, [1.0]) == 0.0


class TestHistoryDays:
    def _ev(self, word: str, day: str, score: float = 0.4) -> HotWordEvent:
        ts = datetime.datetime.strptime(day, "%Y-%m-%d").timestamp()
        return HotWordEvent(stock_code="X.US", word=word, tfidf_score=score,
                            event_time=int(ts), z_score=0.0)

    def test_counts_distinct_days_not_rows(self):
        """同一天跑多轮会各写一行 —— 门必须按「天」而不是「行」。"""
        evs = [self._ev("pe pe", "2026-09-01") for _ in range(5)]  # 5 行 1 天
        assert hot_word_history_days(evs) == {"pe pe": 1}
        evs += [self._ev("pe pe", "2026-09-02"), self._ev("pe pe", "2026-09-03")]
        assert hot_word_history_days(evs) == {"pe pe": 3}

    def test_per_word(self):
        evs = [self._ev("a", "2026-09-01"), self._ev("b", "2026-09-01"),
               self._ev("b", "2026-09-02")]
        assert hot_word_history_days(evs) == {"a": 1, "b": 2}

    def test_gate_constant_is_at_least_three(self):
        assert HOT_WORD_MIN_HISTORY_DAYS >= 3


class TestCallSitesUseTheFixes:
    """契约: 两处热词检测都必须按天设门、且同精度比较。"""

    @pytest.mark.parametrize("path", ["src/detector.py", "src/cli.py"])
    def test_sources_compare_same_precision_and_count_days(self, path):
        from pathlib import Path
        src = Path(path).read_text(encoding="utf-8")
        if path.endswith("detector.py"):
            assert "hist_days.get(word, 0) < HOT_WORD_MIN_HISTORY_DAYS" in src
        else:
            # cli.py 内联算天数，至少要出现按天去重的构造
            assert "date.fromtimestamp(e.event_time)" in src
        assert "compute_z_score(round(score, 4), hist)" in src, \
            f"{path} 又拿未 round 的当前值去比 round 过的历史了"
