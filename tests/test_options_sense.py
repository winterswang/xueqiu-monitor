"""options-sense (方向 8) 单元测试: 采集聚合/过滤闸/Max Pain/detector 冷启动。

网络零依赖 — CBOE/Longbridge 均以合成数据注入。
"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src import db, options_collector, options_detector
from src.models import OptionDaily


SYMBOL = "PDD"
SPOT = 80.0


def _cboe_opt(sym_root: str, ymd: str, cp: str, strike: float, **kw) -> dict:
    code = f"{sym_root}{ymd}{cp}{int(strike * 1000):08d}"
    base = {
        "option": code, "bid": 1.0, "ask": 1.2, "last_trade_price": 1.1,
        "volume": 10, "open_interest": 100, "iv": 0.32,
        "delta": 0.5 if cp == "C" else -0.5,
        "gamma": 0.01, "theta": -0.02, "vega": 0.1,
    }
    base.update(kw)
    return base


def _raw_snapshot(expiry: str) -> dict:
    ymd = expiry.replace("-", "")[2:]
    return {
        "data": {
            "current_price": SPOT,
            "iv30": 32.2,
            "options": [
                _cboe_opt(SYMBOL, ymd, "C", 78),
                _cboe_opt(SYMBOL, ymd, "C", 80, open_interest=500),
                _cboe_opt(SYMBOL, ymd, "C", 82, bid=0, ask=0.01),
                _cboe_opt(SYMBOL, ymd, "P", 70),
                _cboe_opt(SYMBOL, ymd, "P", 75, open_interest=800, bid=0.9, ask=1.0),
                _cboe_opt(SYMBOL, ymd, "P", 80, bid=0.5, ask=3.5),
                _cboe_opt(SYMBOL, ymd, "P", 65),
                _cboe_opt(SYMBOL, ymd, "C", 85),
            ],
        }
    }


class TestQuotableGates:
    def test_placeholder_bid_zero_rejected(self):
        assert not options_collector.is_quotable({"bid": 0, "ask": 0.02})

    def test_dust_ask_rejected(self):
        assert not options_collector.is_quotable({"bid": 0.01, "ask": 0.02})

    def test_wide_spread_rejected(self):
        assert not options_collector.is_quotable({"bid": 0.5, "ask": 3.5})

    def test_normal_quote_passes(self):
        assert options_collector.is_quotable({"bid": 1.0, "ask": 1.2})


class TestParseCboeSymbol:
    def test_roundtrip(self):
        parsed = options_collector._parse_cboe_symbol("PDD261016C00080000", "PDD")
        assert parsed == ("2026-10-16", "C", 80.0)

    def test_garbage_returns_none(self):
        assert options_collector._parse_cboe_symbol("BAD", "PDD") is None


class TestAggregateChain:
    def test_aggregates_and_filters(self):
        expiry = (datetime.now().date() + timedelta(days=7)).isoformat()
        agg = options_collector.aggregate_chain(_raw_snapshot(expiry), SYMBOL)
        assert agg is not None
        assert agg["spot"] == SPOT
        # Max Pain 所属到期链必须随值一起返回，避免丢失期限口径。
        assert agg["chain_expiry"] == expiry
        assert agg["atm_iv"] == pytest.approx(32.2)
        # 占位/空挂 (C82 bid=0, P80 价差 150%) 被闸掉
        assert all(r["ask"] >= 0.03 for r in agg["contracts"])
        assert all(r["bid"] > 0 for r in agg["contracts"])
        # PCR 聚合与保存合约使用同一报价闸；未过闸合约不参与汇总。
        assert agg["contracts_seen"] == 8 and agg["contracts_valid"] == 6
        assert agg["call_volume"] == 30 and agg["put_volume"] == 30
        assert agg["call_oi"] == 700 and agg["put_oi"] == 1000
        assert agg["pcr_oi"] == pytest.approx(1000 / 700, abs=0.00005)
        assert agg["expiry_metrics"][0]["expiry"] == expiry
        assert agg["expiry_metrics"][0]["pcr_oi"] == pytest.approx(1000 / 700, abs=0.00005)
        # 期权墙: 过闸合约里 call OI 最大 K80 / put OI 最大 K75
        assert agg["wall_call"] == 80.0 and agg["wall_put"] == 75.0

    def test_expired_contracts_excluded(self):
        et_today = options_collector._et_now().date()
        past = (et_today - timedelta(days=1)).isoformat()
        future = (et_today + timedelta(days=7)).isoformat()
        raw = _raw_snapshot(future)
        raw["data"]["options"] += [_cboe_opt(SYMBOL, past.replace("-", "")[2:], "C", 50)]
        agg = options_collector.aggregate_chain(raw, SYMBOL)
        assert all(r["expiry"] != past for r in agg["contracts"])

    def test_max_pain_between_walls(self):
        expiry = (datetime.now().date() + timedelta(days=7)).isoformat()
        agg = options_collector.aggregate_chain(_raw_snapshot(expiry), SYMBOL)
        # 合成数据 put OI (800@K75) 远大于 call OI → MaxPain 落在 put 侧价位
        assert agg["max_pain"] is not None
        assert 65.0 <= agg["max_pain"] <= 80.0


class TestMaxPain:
    def test_all_calls_otm_pain_at_top_strike(self):
        calls = [{"strike": 90.0, "oi": 10, "cp": "C"}]
        puts = [{"strike": 70.0, "oi": 100, "cp": "P"},
                {"strike": 60.0, "oi": 100, "cp": "P"}]
        assert options_collector._max_pain(calls, puts) == 70.0


class TestDetector:
    def _seed_history(self, db_path: str, days: int, pcr: float = 0.75, vol: int = 1000):
        base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        for i in range(days):
            d = base - timedelta(days=days - i)
            p = pcr + (0.01 if i % 2 else -0.01)
            db.upsert_option_daily(db_path, OptionDaily(
                stock_code="PDD.US", stat_date=int(d.timestamp()),
                call_volume=vol, put_volume=int(vol * pcr),
                call_oi=10000, put_oi=int(10000 * p),
                pcr_volume=p, pcr_oi=p, source="cboe",
            ))

    def test_cold_start_silent(self, tmp_path: Path):
        db_path = str(tmp_path / "t.db")
        db.init_db(db_path)
        self._seed_history(db_path, 10)
        assert options_detector.detect_options_signals(db_path, "PDD.US") == []

    def test_all_expiry_pcr_does_not_emit_direction_alert(self, tmp_path: Path):
        db_path = str(tmp_path / "t.db")
        db.init_db(db_path)
        self._seed_history(db_path, 40)
        # 全期限兼容字段不能生成方向性 PCR 告警。
        db.upsert_option_daily(db_path, OptionDaily(
            stock_code="PDD.US", stat_date=int(datetime.now().timestamp()),
            call_volume=1000, put_volume=3000, call_oi=10000, put_oi=40000,
            pcr_volume=3.0, pcr_oi=4.0, source="cboe",
        ))
        alerts = options_detector.detect_options_signals(db_path, "PDD.US")
        types = {(a.alert_type, a.priority) for a in alerts}
        assert not any(alert_type.startswith("options_pcr") for alert_type, _ in types)

    def test_stable_history_no_alert(self, tmp_path: Path):
        db_path = str(tmp_path / "t.db")
        db.init_db(db_path)
        self._seed_history(db_path, 40)
        assert options_detector.detect_options_signals(db_path, "PDD.US") == []


class TestBackfillNeverOverwrites:
    def test_insert_or_ignore(self, tmp_path: Path):
        db_path = str(tmp_path / "t.db")
        db.init_db(db_path)
        day = int(datetime(2026, 10, 1).timestamp())
        db.upsert_option_daily(db_path, OptionDaily(
            stock_code="PDD.US", stat_date=day, call_oi=1, pcr_oi=0.5, source="cboe",
        ))
        added = db.backfill_option_daily(db_path, OptionDaily(
            stock_code="PDD.US", stat_date=day, call_oi=999, pcr_oi=9.9,
            source="cboe",
        ))
        assert added is False
        row = db.get_option_daily_history(db_path, "PDD.US")[0]
        assert row.pcr_oi == 0.5


class TestOptionHistoryWindow:
    """回归: ASC + LIMIT 取到的是**最老**的 N 行, 窗口满了 detector 就永久停摆。"""

    def test_returns_latest_n_in_ascending_order(self, tmp_path: Path):
        db_path = str(tmp_path / "t.db")
        db.init_db(db_path)
        for i in range(5):
            db.upsert_option_daily(db_path, OptionDaily(
                stock_code="PDD.US", stat_date=1000 + i, pcr_oi=0.5, source="cboe",
            ))
        hist = db.get_option_daily_history(db_path, "PDD.US", limit=2)
        assert [h.stat_date for h in hist] == [1003, 1004]

    def test_latest_helper_returns_newest(self, tmp_path: Path):
        db_path = str(tmp_path / "t.db")
        db.init_db(db_path)
        for i in range(3):
            db.upsert_option_daily(db_path, OptionDaily(
                stock_code="PDD.US", stat_date=2000 + i, pcr_oi=0.4 + i, source="cboe",
            ))
        row = db.get_latest_option_daily(db_path, "PDD.US", source="cboe")
        assert row is not None and row.stat_date == 2002


class TestPremiumYieldDte:
    """收租率按实际 DTE 年化 (×12 旧算法隐含当月合约, 周期权被低估 ~30 倍)。"""

    def test_yield_uses_actual_dte(self):
        expiry = (datetime.now().date() + timedelta(days=7)).isoformat()
        agg = options_collector.aggregate_chain(_raw_snapshot(expiry), SYMBOL)
        dte = (datetime.fromisoformat(expiry).date()
               - options_collector._et_now().date()).days
        assert agg["premium_yield_dte"] == dte
        # ATM (strike 80): mid=1.1 / spot=80 ×100 = 1.375%
        assert agg["premium_yield"] == round(1.375 / dte * 365, 3)

    def test_same_day_expiry_excluded(self):
        """0DTE 链的权利金是衰减噪声 —— 2026-10-17 PDD「76%」就是它 ×12 的产物。"""
        today = options_collector._et_now().date()
        ymd0 = today.strftime("%y%m%d")
        d7 = today + timedelta(days=7)
        ymd7 = d7.strftime("%y%m%d")
        raw = {"data": {"current_price": SPOT, "iv30": 30.0, "options": [
            _cboe_opt(SYMBOL, ymd0, "C", 80, open_interest=900),
            _cboe_opt(SYMBOL, ymd0, "P", 80, open_interest=900),
            _cboe_opt(SYMBOL, ymd7, "C", 80, open_interest=300),
            _cboe_opt(SYMBOL, ymd7, "P", 80, open_interest=300),
        ]}}
        agg = options_collector.aggregate_chain(raw, SYMBOL)
        assert agg["premium_yield_dte"] == 7
        assert agg["premium_yield"] == round(1.375 / 7 * 365, 3)
