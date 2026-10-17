"""期权数据融合进逐股分析 (2026-10-15)。

用户口径：期权不该在报告末尾单列一节，而要成为每只个股分析的**输入素材**，
并且 prompt 要让模型知道这段数据**拿来干什么** —— 帖子描述已发生的事，
期权定价未来，用它判断未来一段时间的定价与方向。
"""

from pathlib import Path

from src import db
from src import report_generator as rg
from src.models import OptionDaily

STAT = 1791907200  # 2026-10-14 北京时间 00:00
CODE = "PDD.US"


def _opt(**kw) -> OptionDaily:
    base = dict(
        stock_code=CODE,
        stat_date=STAT,
        pcr_oi=0.73,
        pcr_volume=0.75,
        z_pcr_oi=-0.4,
        atm_iv=32.4,
        iv_percentile=58.0,
        premium_yield=14.1,
        wall_call=80.0,
        wall_put=80.0,
        max_pain=80.0,
        source="cboe",
    )
    base.update(kw)
    return OptionDaily(**base)


def _posts(n: int = 1) -> list[dict]:
    return [
        {
            "content": "内容" * 30,
            "title": f"帖{i}",
            "like_count": 5,
            "comment_count": 1,
            "forward_count": 0,
            "author": "甲",
            "time": "10:00",
            "link": "",
        }
        for i in range(n)
    ]


class TestFetchStockOptions:
    def test_prefers_cboe_over_longbridge_same_day(self, tmp_path: Path):
        """回归: 同一天两源都在时必须选 cboe —— longbridge 行没有 IV/墙/MaxPain。"""
        db_path = str(tmp_path / "t.db")
        db.init_db(db_path)
        db.upsert_option_daily(
            db_path,
            _opt(source="longbridge", atm_iv=None, wall_call=None, max_pain=None),
        )
        db.upsert_option_daily(db_path, _opt(source="cboe"))
        got = rg.fetch_stock_options(db_path, CODE)
        assert got is not None
        assert got["source"] == "cboe"
        assert got["atm_iv"] == 32.4 and got["max_pain"] == 80.0

    def test_none_when_no_rows(self, tmp_path: Path):
        db_path = str(tmp_path / "t.db")
        db.init_db(db_path)
        assert rg.fetch_stock_options(db_path, "AAPL.US") is None


class TestFormatOptionsBlock:
    def test_block_carries_goal_guide_and_readings(self):
        blk = rg._format_options_block(
            {
                "stat_date": STAT,
                "source": "cboe",
                "pcr_oi": 0.73,
                "pcr_volume": 0.75,
                "atm_iv": 32.4,
                "iv_percentile": 58.0,
                "premium_yield": 14.1,
                "wall_call": 80.0,
                "wall_put": 80.0,
                "max_pain": 80.0,
            }
        )
        # 目标说明 —— 用户要求「让 LLM 感知到期权数据的价值和目标」
        assert "未来" in blk and "方向" in blk and "已发生" in blk
        # 逐项解读
        for kw in ("PCR-仓", "IV", "收租率", "MaxPain", "墙"):
            assert kw in blk
        # 实际读数 + 口径
        assert "0.73" in blk and "32.4" in blk and "2026-10-14" in blk
        assert "CBOE" in blk
        # 不该出现嵌套括号那种别扭写法
        assert "（2026-10-14，美股 CBOE 快照, " in blk

    def test_readings_empty_for_degenerate_row(self):
        assert rg._options_readings({"stat_date": STAT, "source": "cboe"}) == ""


class TestPromptInjection:
    def test_options_section_present_only_when_given(self):
        opt = {
            "stat_date": STAT,
            "source": "cboe",
            "pcr_oi": 0.73,
            "wall_call": 80.0,
            "max_pain": 80.0,
        }
        kw = dict(trend={"has_trend": False, "days": 1}, alerts=[], hot_words=[])
        with_opt = rg._build_analysis_prompt(
            "拼多多", CODE, _posts(), options=opt, **kw
        )
        without = rg._build_analysis_prompt("拼多多", CODE, _posts(), **kw)
        assert "## 期权数据" in with_opt
        assert "## 期权数据" not in without

    def test_tier_instructions_demand_forward_looking_read(self):
        for tier in (rg.TIER_DEEP, rg.TIER_STD, rg.TIER_FLAT):
            text = rg._tier_instructions(tier, "今日")
            assert "期权数据" in text, f"{tier} 档没要求用期权"
            assert "不要只复述数字" in text, f"{tier} 档没说别复述数字"


class TestAlertLabels:
    def test_options_alerts_render_readable(self):
        for t in (
            "options_pcr_spike",
            "options_pcr_call_surge",
            "options_volume_spike",
        ):
            out = rg._alert_summary({"type": t, "detail": {}, "priority": "P1"})
            assert out != t, f"{t} 还在原样输出英文类型名"
            assert "期权" in out

    def test_detector_signal_text_wins(self):
        out = rg._alert_summary(
            {
                "type": "options_pcr_spike",
                "priority": "P0",
                "detail": {"signal": "PCR-仓 2.1σ 异常"},
            }
        )
        assert out == "PCR-仓 2.1σ 异常"


class TestZeroPostStillShowsOptions:
    """0 帖标的不调 LLM —— 占位小节是它唯一能看到期权的通道。"""

    def test_placeholder_carries_options_line(self, tmp_path: Path):
        db_path = str(tmp_path / "t.db")
        db.init_db(db_path)
        db.upsert_option_daily(db_path, _opt())
        res = rg.analyze_stock("PDD.US", "拼多多", db_path, "2026-10-15", {})
        assert res["analyzed"] is False
        assert "- 期权读数" in res["section"]
        assert "PCR-仓 0.73" in res["section"]

    def test_placeholder_has_no_options_line_without_data(self, tmp_path: Path):
        db_path = str(tmp_path / "t.db")
        db.init_db(db_path)
        res = rg.analyze_stock("PDD.US", "拼多多", db_path, "2026-10-15", {})
        assert "- 期权读数:" not in res["section"]


class TestTrailingSectionRemoved:
    def test_legacy_section_builder_is_gone(self):
        assert not hasattr(rg, "_build_options_section")

    def test_hk_gap_note_reaches_footnote(self, tmp_path: Path):
        db_path = str(tmp_path / "t.db")
        db.init_db(db_path)
        cache = tmp_path / "hk_option_classes.json"
        cache.write_text('{"map": {"9992": "POP"}}', encoding="utf-8")
        stocks = {"9992.HK": {"name": "泡泡玛特"}, "325.HK": {"name": "布鲁可"}}
        db.upsert_option_daily(
            db_path,
            OptionDaily(
                stock_code="9992.HK", stat_date=STAT, pcr_oi=1.11, source="hkex"
            ),
        )
        have = rg._hk_codes_with_options(db_path, stocks)
        assert have == {"9992.HK"}  # 布鲁可没有行 → 不在 have 里
        note = rg._hk_no_option_note(db_path, stocks, have)
        assert "布鲁可" in note
        assert "泡泡玛特" not in note  # 有数据的不该被列成「无上市期权」


class TestFreshnessGuard:
    """采集侧写着「不得用旧数据充数」, 报告侧原先没有对应的闸 (2026-10-16)。"""

    def _db(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        db.init_db(db_path)
        db.upsert_option_daily(db_path, _opt())  # stat_date = 2026-10-14
        return db_path

    def test_fresh_data_passes(self, tmp_path):
        got = rg.fetch_stock_options(self._db(tmp_path), CODE, ref_date="2026-10-15")
        assert got is not None and got["pcr_oi"] == 0.73

    def test_stale_data_is_treated_as_no_data(self, tmp_path):
        # 报告日比读数晚 10 天 → 超过 OPTIONS_MAX_AGE_DAYS(5)
        got = rg.fetch_stock_options(self._db(tmp_path), CODE, ref_date="2026-10-24")
        assert got is None, "过期的期权读数必须当作无数据, 不能喂给模型"

    def test_boundary_day_still_passes(self, tmp_path):
        got = rg.fetch_stock_options(self._db(tmp_path), CODE, ref_date="2026-10-19")
        assert got is not None, "恰好等于阈值(5 天)时仍应保留"

    def test_no_ref_date_skips_the_guard(self, tmp_path):
        assert rg.fetch_stock_options(self._db(tmp_path), CODE) is not None

    def test_zero_post_placeholder_carries_date_and_caliber(self, tmp_path):
        """0 帖占位行原先只给一串裸数字, 读者无从判断是哪天的。"""
        db_path = self._db(tmp_path)
        res = rg.analyze_stock("PDD.US", "拼多多", db_path, "2026-10-15", {})
        assert res["analyzed"] is False
        assert "期权读数（2026-10-14" in res["section"]
        assert "CBOE" in res["section"]


class TestChainExpiryAnnotation:
    """MaxPain 必须标出到期链 —— 模型此前拿快照日期猜, 5 只里 4 只猜错 (2026-10-16)。"""

    def test_readings_annotate_chain_expiry(self):
        out = rg._options_readings(
            {
                "stat_date": STAT,
                "source": "cboe",
                "max_pain": 200.0,
                "chain_expiry": "2027-01-15",
            }
        )
        assert "MaxPain 200（属 2027-01-15 到期链）" in out

    def test_readings_omit_annotation_when_absent(self):
        out = rg._options_readings(
            {
                "stat_date": STAT,
                "source": "cboe",
                "max_pain": 200.0,
            }
        )
        assert "到期链" not in out

    def test_guide_marks_yield_same_source_as_iv(self):
        """收租率与 IV 同源 —— 指南必须禁止把它当第二证据重复引用。"""
        blk = rg._format_options_block(
            {
                "stat_date": STAT,
                "source": "cboe",
                "pcr_oi": 0.73,
            }
        )
        assert "同源" in blk
        assert "市场愿为这段波动付多少钱" not in blk  # 旧措辞已删

    def test_guide_says_no_iv_verdict_without_percentile(self):
        blk = rg._format_options_block(
            {
                "stat_date": STAT,
                "source": "cboe",
                "pcr_oi": 0.73,
            }
        )
        assert "无基线不可比" in blk


class TestChainExpiryMigration:
    def test_old_schema_gets_column(self, tmp_path):
        """旧库 (无 chain_expiry 列) 经 init_db 迁移后必须有该列。"""
        import sqlite3

        db_path = str(tmp_path / "old.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""CREATE TABLE options_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_code TEXT NOT NULL,
            stat_date INTEGER NOT NULL,
            call_volume INTEGER, put_volume INTEGER, call_oi INTEGER, put_oi INTEGER,
            pcr_volume REAL, pcr_oi REAL, atm_iv REAL, iv_percentile REAL,
            premium_yield REAL, max_pain REAL, wall_call REAL, wall_put REAL,
            z_pcr_oi REAL, z_volume REAL,
            source TEXT NOT NULL DEFAULT 'cboe')""")
        conn.commit()
        conn.close()
        db.init_db(db_path)
        cols = {
            r[1]
            for r in sqlite3.connect(db_path).execute(
                "PRAGMA table_info(options_daily)"
            )
        }
        assert "chain_expiry" in cols


class TestYieldDteAnnotation:
    def test_readings_annotate_yield_dte(self):
        out = rg._options_readings(
            {
                "stat_date": STAT,
                "source": "cboe",
                "premium_yield": 76.0,
                "premium_yield_dte": 2,
            }
        )
        assert "收租率(年化) 76%（2 天期折算）" in out

    def test_readings_no_dte_label_without_value(self):
        out = rg._options_readings(
            {
                "stat_date": STAT,
                "source": "cboe",
                "premium_yield": 76.0,
            }
        )
        assert "天期折算" not in out


class TestOptionAnnotationsBackfill:
    """回填脚本: chain 补缺 + premium_yield 重算为 DTE 版 (×12 旧值是错的)。"""

    def test_backfill_recomputes_yield_and_dte(self, tmp_path):
        import importlib
        import sys as _sys

        scripts = Path(__file__).resolve().parent.parent / "scripts"
        _sys.path.insert(0, str(scripts))
        boa = importlib.import_module("backfill_option_annotations")

        db_path = str(tmp_path / "t.db")
        db.init_db(db_path)
        snap = STAT  # 2026-10-14 北京零点 → US ET 交易日 = 10-13
        db.upsert_option_daily(
            db_path,
            OptionDaily(
                stock_code="PDD.US", stat_date=snap, premium_yield=31.2, source="cboe"
            ),
        )
        db.upsert_option_daily(
            db_path,
            OptionDaily(
                stock_code="LB.US",
                stat_date=snap,
                premium_yield=None,
                source="longbridge",
            ),
        )
        rows = []
        for strike, oi_, pp in ((90, 100, 2.0), (80, 500, 1.375)):
            rows.append(
                {
                    "stock_code": "PDD.US",
                    "snap_date": snap,
                    "contract_id": f"PDD261018C{int(strike * 1000):08d}",
                    "expiry": "2026-10-18",
                    "strike": strike,
                    "cp": "C",
                    "bid": 1.0,
                    "ask": 1.2,
                    "volume": 10,
                    "oi": oi_,
                    "iv": 30.0,
                    "delta": 0.5,
                    "gamma": 0.01,
                    "theta": -0.02,
                    "vega": 0.1,
                    "premium_pct": pp,
                    "moneyness": round((strike / 80 - 1) * 100, 4),
                }
            )
        db.insert_option_contracts(db_path, rows)

        n_chain, n_yield, missed = boa.backfill(db_path)

        row = db.get_latest_option_daily(db_path, "PDD.US", source="cboe")
        assert row.chain_expiry == "2026-10-18"  # OI 500 集中在 80
        # ET 交易日 10-13 → 10-18 到期, DTE=5 (恰过 MIN_YIELD_DTE 门槛);
        # ATM = |moneyness|=0 的 80
        assert row.premium_yield_dte == 5
        assert row.premium_yield == round(1.375 / 5 * 365, 3)
        assert row.premium_yield != 31.2  # ×12 旧值被覆盖
        lb = db.get_latest_option_daily(db_path, "LB.US", source="longbridge")
        assert lb.premium_yield is None  # 无合约行不动
        assert missed == 1
