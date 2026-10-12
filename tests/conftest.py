"""整个测试套件共用的夹具。

目前只有一个：``frozen_clock``。
"""

import time

import pytest


@pytest.fixture
def frozen_clock(monkeypatch):
    """把 ``time.time()`` 固定在**当日正午**（宿主本地时区）。

    为什么需要它
    ------------
    夹具里的帖子用**相对时间串**（``"3小时前"`` / ``"5分钟前"`` / ``"1小时前"``）造数据，
    而 ``report_generator.fetch_stock_posts`` 是以读取时的 ``time.time()`` 为参照
    把它们解析成时间戳（``src/report_generator.py:146``），再按 ``date_str`` 的
    **当日窗口**过滤（``max_age_days=1`` ⇒ 只留 ``date_str`` 当天）。

    于是测试结果取决于**跑的时刻**：在宿主本地午夜后约 3 小时内，
    ``"3小时前"`` 会落到**前一个自然日**，被当日窗口滤掉，断言拿到空列表
    → ``IndexError`` / ``assert 0 == 1``。

    **实测**：同一个 commit，北京时间 23:5x 跑是 ``398 passed``，
    00:02 跑是 ``10 failed``；CI 在 ``2026-10-12T16:00Z``（= 北京时间 00:00）触发，
    失败的就是同一批。副作用是这段时间触发的推送在 CI 里都会假红。

    固定在正午之后，``"3小时前"`` 落在当日 09:00，无论几点跑都在当日窗口内。

    用法（模块级显式挂载，不用 autouse，避免影响不在意时钟的测试）::

        pytestmark = pytest.mark.usefixtures("frozen_clock")
    """
    noon = time.mktime(time.strptime(time.strftime("%Y-%m-%d") + " 12:00:00",
                                      "%Y-%m-%d %H:%M:%S"))
    monkeypatch.setattr(time, "time", lambda: noon)
