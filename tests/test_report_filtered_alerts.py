"""舆情#1 回归 — 日报的告警查询必须排除 filtered=1 的规则层噪音.
"""
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import datetime
from src.report_generator import fetch_day_alerts, fetch_stock_alerts
from src import db


def _seed(tmp_path):
    from src import db as db_mod
    db_path = str(tmp_path / "t.db")
    db_mod.init_db(db_path)
    conn = db_mod._connect(db_path)
    today = datetime.date.today().isoformat()
    epoch = int(datetime.datetime.now().timestamp())
    for i, (z, filtered) in enumerate([(5.0, 1), (4.0, 0), (3.5, 1)]):
        conn.execute(
        "INSERT INTO change_alert (stock_code, alert_time, alert_type, z_score, magnitude, detail, priority, filtered, filter_reason) VALUES (:stock_code, :alert_time, :alert_type, :z_score, :magnitude, :detail, :priority, :filtered, :filter_reason)",
        dict(stock_code="LULU.US", alert_time=epoch + i, alert_type="sentiment_shift",
             z_score=z, magnitude=0.4, detail="{}", priority="P1",
             filtered=filtered, filter_reason="规则压掉" if filtered else ""))
    conn.commit()
    conn.close()
    return db_path, today


def test_stock_alerts_exclude_filtered(tmp_path):
    """核心回归: filtered=1 的行不得进入 per-stock 日报查询."""
    db, today = _seed(tmp_path)
    rows = fetch_stock_alerts(db, "LULU.US", today)
    assert [r["z_score"] for r in rows] == [4.0]


def test_day_alerts_exclude_filtered(tmp_path):
    db, today = _seed(tmp_path)
    rows = fetch_day_alerts(db, today)
    assert [r["z_score"] for r in rows] == [4.0]
