"""统计口径统一到**本地自然日**（xueqiu-monitor issue #77）。

原先本模块有两处按 **UTC 桶** 切日：`get_today_alerts` 的
``int(time.time()) // 86400 * 86400``、`get_historical_new_announcement_counts`
的 ``GROUP BY (alert_time / 86400)``；而日报的昨日区间
（``report_generator``）与 ``date(x,'unixepoch','localtime')`` 用的是**本地日**。
在 TZ=Asia/Shanghai 下，本地 00:00–08:00 的记录会落进与本地日期不同的窗口。

**这些用例在旧实现下必红**：本地凌晨的告警在 UTC 桶里属于"昨天"。

（背景：这也是测试套件曾在本地午夜后假红的那一族问题的**生产侧**根因；测试侧的
``frozen_clock`` 夹具只是让测试确定，没有动这里。）
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import db as dbmod  # noqa: E402  (sys.path 先行的仓内惯例)
from src.models import ChangeAlert  # noqa: E402


@pytest.fixture
def tmp_db(tmp_path):
    path = str(tmp_path / "test.db")
    dbmod.init_db(path)
    return path


def _local_ts(hour: int, *, days_ago: int = 0) -> int:
    """今天（或 N 天前）本地 HH:00 的 Unix 秒。"""
    base = datetime.now().replace(hour=hour, minute=0, second=0, microsecond=0)
    return int((base - timedelta(days=days_ago)).timestamp())


def _alert(ts: int, *, kind: str, detail: dict | None = None, code: str = "SH600519") -> ChangeAlert:
    return ChangeAlert(
        stock_code=code,
        alert_time=ts,
        alert_type=kind,
        z_score=3.0,
        magnitude=1.0,
        detail=detail if detail is not None else {},
        priority="P1",
        filtered=0,
        filter_reason="",
    )


class TestLocalDayBoundary:
    def test_early_morning_alert_counts_as_today(self, tmp_db):
        """本地 02:00 的告警必须算「今天」。

        旧实现按 UTC 桶：本地 02:00 = UTC 前一天 18:00 → 被算成昨天、查不到。
        """
        dbmod.insert_alert(tmp_db, _alert(_local_ts(2), kind="sentiment_shift"))

        got = dbmod.get_today_alerts(tmp_db)

        assert [a.alert_time for a in got] == [_local_ts(2)]

    def test_yesterday_late_evening_is_excluded(self, tmp_db):
        """本地昨天 23:00 不属于今天。"""
        dbmod.insert_alert(
            tmp_db, _alert(_local_ts(23, days_ago=1), kind="sentiment_shift")
        )

        assert dbmod.get_today_alerts(tmp_db) == []

    def test_filtered_alerts_still_excluded(self, tmp_db):
        """口径改了，`filtered=0` 的过滤不能被顺手弄丢。"""
        a = _alert(_local_ts(2), kind="sentiment_shift")
        a.filtered = 1
        dbmod.insert_alert(tmp_db, a)

        assert dbmod.get_today_alerts(tmp_db) == []

    def test_local_day_start_is_local_midnight(self):
        noon = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        expected = int(
            datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        )

        assert dbmod.local_day_start(now=noon.timestamp()) == expected

    def test_announcement_counts_bucket_by_local_day(self, tmp_db):
        """同一天的两条公告要落进**同一个**桶。

        本地 02:00 与 09:00 在 UTC 桶里分属两天（前一天 18:00 / 当天 01:00）——
        旧实现会把一天的量劈成两天，Z-score 基线随之失真。
        """
        dbmod.insert_alert(
            tmp_db,
            _alert(_local_ts(2), kind="new_announcement", detail={"dedup_hash": "a"}),
        )
        dbmod.insert_alert(
            tmp_db,
            _alert(_local_ts(9), kind="new_announcement", detail={"dedup_hash": "b"}),
        )

        counts = dbmod.get_historical_new_announcement_counts(
            tmp_db, "SH600519", window_days=14
        )

        assert counts == [2.0], f"同一本地日应为一个桶，实际 {counts}"
