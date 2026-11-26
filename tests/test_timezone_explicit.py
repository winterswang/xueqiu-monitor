"""日桶必须显式用 CST，不跟随宿主时区（2026-11-25 D2）。

整系统的「日」都是 CST（README 口径定义）。SQL 里原本写的是
`date(...,'unixepoch','localtime')`，Python 侧是 `datetime.fromtimestamp()`
或 `time.localtime()` —— 都跟随宿主时区。机器换到 UTC 后，同一条帖子
会被算进不同的「日」，日报、告警、统计全线错位一格。

本机是 +8，所以替换前后结果必须**逐行一致**；不一致就是改错了。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import db  # noqa: E402

# 活代码：参与运行的模块（死代码与 tests 不在管辖范围）
LIVE_FILES = [
    "src/report_generator.py",
    "src/db.py",
    "src/cli.py",
    "src/notifier.py",
    "scripts/assistant_daily.py",
    "scripts/export_csv.py",
    "scripts/health_check.py",
]


def test_no_sql_localtime_left_in_live_code():
    """SQL 的日桶不许再写 'localtime'。"""
    bad = []
    for rel in LIVE_FILES:
        text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        for m in re.finditer(r"'unixepoch'\s*,\s*'localtime'", text):
            line = text[: m.start()].count("\n") + 1
            bad.append(f"{rel}:{line}")
    assert not bad, "SQL 日桶仍是隐式本地时区：" + ", ".join(bad)


def test_local_day_start_is_cst():
    """Python 日桶同样显式 CST：与 SQL 的 '+8 hours' 必须是同一口径。"""
    import time
    from datetime import datetime
    from datetime import timezone, timedelta

    cst = timezone(timedelta(hours=8))
    now = time.time()
    expect = int(datetime.now(cst).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp())
    assert db.local_day_start(now) == expect


def test_sql_and_python_day_bucket_agree():
    """同一条记录，SQL 算出的日与 Python 算出的日必须相同。"""
    from datetime import datetime
    from datetime import timezone, timedelta

    cst = timezone(timedelta(hours=8))
    # 凌晨 00:30 CST —— 在 UTC 机器上最容易劈叉的时刻
    ts = int(datetime(2026, 11, 24, 0, 30, tzinfo=cst).timestamp())
    py_day = datetime.fromtimestamp(db.local_day_start(ts), cst).strftime("%Y-%m-%d")
    assert py_day == "2026-11-24"


def test_no_python_localtime_left_in_live_code():
    """展示类时间也不许再用 time.localtime()（会跟随宿主时区）。"""
    bad = []
    for rel in LIVE_FILES:
        text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        for m in re.finditer(r"time\.localtime\(", text):
            line = text[: m.start()].count("\n") + 1
            bad.append(f"{rel}:{line}")
    assert not bad, "仍在使用隐式本地时区：" + ", ".join(bad)


def test_display_timestamp_uses_cst():
    """告警/卡片里的时间：CST 凌晨 00:30 必须显示 00:30，不是前一天 16:30。"""
    from datetime import datetime, timezone, timedelta

    cst = timezone(timedelta(hours=8))
    ts = int(datetime(2026, 11, 24, 0, 30, tzinfo=cst).timestamp())
    shown = datetime.fromtimestamp(ts, cst).strftime("%Y-%m-%d %H:%M:%S")
    assert shown == "2026-11-24 00:30:00"
