"""整个测试套件共用的夹具。

- ``frozen_clock``：把时钟固定在当日正午。
- ``_no_production_x_reads``（autouse）：任何测试都不得读到仓库里真实的
  ``data/x_posts``，否则"金标准"会被生产数据污染（2026-11-25 P5）。
"""

import time
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PROD_X_DIR = _PROJECT_ROOT / "data" / "x_posts"


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


@pytest.fixture(autouse=True)
def _no_production_x_reads(monkeypatch):
    """测试不许读生产 ``data/x_posts``。

    为什么
    ------
    ``analyze_stock`` 的配置里没有 ``x_source`` 块时，``x_cfg.get("enabled", True)``
    默认打开，root 又是相对路径 ``data/x_posts`` —— 于是一个只想测「雪球 0 帖」
    的用例，实际会把仓库里真实的 X 数据读进来（2026-11-25 实测：TSLA 因此被
    判成 DEEP、调用 LLM，`TestNoDataStock` 2 个用例长期假装失败）。

    P5 把相对路径固定到仓库根之后，"读到生产数据"变成了确定性行为 ——
    必须在这里拦一道，让以后任何新用例不会悄悄被真实数据污染：
    用例要测 X，就传绝对路径（tmp_path）；不想测 X，就显式
    ``"x_source": {"enabled": False}``。

    用 teardown 断言而不是直接抛：``analyze_stock`` 内部会吞掉 X 读取异常
    （那是生产需要的降级），当场抛会被吞掉、什么也看不见。
    """
    from src import x_sources

    hits: list[str] = []
    real = x_sources.load_x_posts

    def guarded(root, date_str, stock_code):
        try:
            p = str(x_sources.posts_path(root, date_str, stock_code))
        except Exception:
            p = ""
        # 只拦「真的读到了生产文件」：路径落在生产目录但文件不存在（用假 ticker
        # 测降级路径的用例）不会造成污染，不该被算作违规。
        if p.startswith(str(_PROD_X_DIR) + "/") and Path(p).exists():
            hits.append(p)
        return real(root, date_str, stock_code)

    monkeypatch.setattr(x_sources, "load_x_posts", guarded)
    yield
    assert not hits, (
        "测试读到了仓库里真实的 X 数据（生产数据会污染断言）：\n  "
        + "\n  ".join(sorted(set(hits)))
        + "\n修法：用例显式传绝对 root（tmp_path），或配置里写 "
        '"x_source": {"enabled": False}。'
    )
