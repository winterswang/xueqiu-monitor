"""options-sense 港股通道 (HKEX DTOP) 单元测试。

网络零依赖 — DTOP/类代码表均以合成数据注入。
"""

import io
import json
import zipfile
from datetime import date, datetime

import openpyxl

from src import db, options_hk
from src import report_generator as rg


def _dtop_row(
    cls: str,
    day: int,
    mon: str,
    year: int,
    strike: float,
    call=(0, 0, 0.0),
    put=(0, 0, 0.0),
) -> str:
    """合成一行 DTOP *_opt_dtl_all.raw (22 字段, 全引号)。"""
    c_oi, c_to, c_settle = call
    p_oi, p_to, p_settle = put
    fields = [
        "01",
        "SOM",
        "STOCK OPTIONS",
        cls,
        str(day),
        mon,
        str(year),
        f"{strike}",
        str(c_oi),
        "0",
        "0",
        str(c_to),
        "0",
        str(c_settle),
        "0",
        str(p_oi),
        "0",
        "0",
        str(p_to),
        "0",
        str(p_settle),
        "0",
    ]
    return ",".join(f'"{f}"' for f in fields)


_POP_RAW = "\n".join(
    [
        _dtop_row("POP", 29, "OCT", 26, 150, call=(100, 10, 5.0), put=(200, 20, 3.0)),
        _dtop_row("POP", 29, "OCT", 26, 155, call=(300, 0, 2.0)),
        _dtop_row("SMC", 29, "OCT", 26, 70, call=(1, 1, 1.0), put=(1, 1, 1.0)),
    ]
)

SPOT = 149.0
STAT = 1791907200


class TestSehkCode:
    def test_strips_suffix(self):
        assert options_hk._sehk_code("9992.HK") == 9992

    def test_non_numeric_returns_none(self):
        assert options_hk._sehk_code("ABC.HK") is None
        assert options_hk._sehk_code("PDD.US") is None


class TestMapSymbols:
    def test_splits_mapped_and_unmapped(self):
        by_class, unmapped = options_hk._map_symbols(
            ["9992.HK", "325.HK", "700.HK"], {9992: "POP", 700: "TCH"}
        )
        assert by_class == {"POP": "9992.HK", "TCH": "700.HK"}
        assert unmapped == ["325.HK"]


class TestParseDtopRaw:
    def test_field_mapping_and_class_filter(self):
        out = options_hk.parse_dtop_raw(_POP_RAW, {"POP"})
        assert set(out) == {"POP"}  # SMC 不在 wanted 里, 被丢掉
        assert len(out["POP"]) == 4  # 2 行 × (call+put)
        call150 = next(r for r in out["POP"] if r["cp"] == "C" and r["strike"] == 150.0)
        assert call150["expiry"] == "2026-10-29"
        assert call150["oi"] == 100  # gross OI
        assert call150["volume"] == 10  # T/O
        assert call150["settle"] == 5.0
        assert call150["iv"] is None  # DTOP 无 IV

    def test_contract_id_synthesized(self):
        out = options_hk.parse_dtop_raw(_POP_RAW, {"POP"})
        ids = {r["contract_id"] for r in out["POP"]}
        assert "POP-2026-10-29-C150" in ids
        assert "POP-2026-10-29-P150" in ids

    def test_malformed_rows_skipped(self):
        bad = "\n".join(
            [
                '"01","SOM","STOCK OPTIONS","POP","29","XXX","26",150,0,0,0,0,0,0,0,0,0,0,0,0,0,0',
                '"H","DTOP","DCASS","20261013","20261013192409","SEOCH",1',
                "garbage",
            ]
        )
        assert options_hk.parse_dtop_raw(bad, {"POP"}) == {"POP": []}


class TestAggregateHkChain:
    def _agg(self, spot=SPOT):
        contracts = options_hk.parse_dtop_raw(_POP_RAW, {"POP"})["POP"]
        return options_hk.aggregate_hk_chain(contracts, spot, STAT)

    def test_pcr(self):
        agg = self._agg()
        assert agg["call_oi"] == 400 and agg["put_oi"] == 200
        assert agg["pcr_oi"] == 0.5

    def test_chain_expiry_recorded(self):
        agg = self._agg()
        assert agg["chain_expiry"] == "2026-10-29"  # _POP_RAW 里唯一的到期
        assert agg["call_volume"] == 10 and agg["put_volume"] == 20
        assert agg["pcr_volume"] == 2.0

    def test_walls_and_max_pain(self):
        agg = self._agg()
        assert agg["wall_call"] == 155.0  # C155 OI 300 > C150 OI 100
        assert agg["wall_put"] == 150.0
        assert agg["max_pain"] == 150.0

    def test_premium_yield_annualized(self):
        agg = self._agg()
        # ATM call 150: settle 5.0 / spot 149 × 100 = 3.3557% → ×12
        assert agg["premium_yield"] == round(5.0 / 149.0 * 100 * 12, 3)

    def test_atm_iv_is_none_by_design(self):
        assert self._agg()["atm_iv"] is None

    def test_zero_settle_rows_gated_out(self):
        # P155 settle=0 → 不进合约行, 也不参与墙/权利金
        agg = self._agg()
        assert all(r["strike"] != 155.0 or r["cp"] != "P" for r in agg["contracts"])

    def test_moneyness_band_excludes_deep_otm(self):
        contracts = options_hk.parse_dtop_raw(_POP_RAW, {"POP"})["POP"]
        contracts.append(
            {
                "contract_id": "POP-x-C500",
                "expiry": "2026-10-29",
                "strike": 500.0,
                "cp": "C",
                "last": None,
                "bid": None,
                "ask": None,
                "volume": 5,
                "oi": 99999,
                "iv": None,
                "delta": None,
                "gamma": None,
                "theta": None,
                "vega": None,
                "settle": 1.0,
            }
        )
        agg = options_hk.aggregate_hk_chain(contracts, SPOT, STAT)
        assert agg["wall_call"] == 155.0  # 500 远离现价, 被 ±20% 带挡掉

    def test_no_spot_leaves_structure_metrics_empty(self):
        """拿不到正股价时 PCR 照算, 但墙/MaxPain/收租率留空。

        缺了 ±20% 价内外带, 全链取 OI 最大会落到远离现价的远期行权价上 ——
        读数看着正常其实是脏值, 宁可留空。
        """
        agg = self._agg(spot=None)
        assert agg["pcr_oi"] == 0.5  # 不需要正股价的指标照常
        assert agg["premium_yield"] is None
        assert agg["wall_call"] is None
        assert agg["wall_put"] is None
        assert agg["max_pain"] is None
        assert all(r["moneyness"] is None for r in agg["contracts"])

    def test_empty_contracts_returns_none(self):
        assert options_hk.aggregate_hk_chain([], SPOT, STAT) is None


class TestContractRows:
    def test_insert_has_no_extra_columns(self, tmp_path):
        """回归: settle 是聚合内部字段, 曾误当表列导致 insert 报错。"""
        db_path = str(tmp_path / "t.db")
        db.init_db(db_path)
        contracts = options_hk.parse_dtop_raw(_POP_RAW, {"POP"})["POP"]
        agg = options_hk.aggregate_hk_chain(contracts, SPOT, STAT)
        rows = options_hk.to_contract_rows("9992.HK", STAT, agg)
        assert "settle" not in rows[0]
        assert db.insert_option_contracts(db_path, rows) == len(rows)
        n = (
            db._connect(db_path)
            .execute(
                "SELECT COUNT(*) FROM options_contracts WHERE stock_code='9992.HK'"
            )
            .fetchone()[0]
        )
        assert n == len(rows)


class TestParseClassXlsx:
    def _xlsx(self) -> bytes:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Eng"
        ws.append(["title", None, None, None])
        ws.append([None, None, None, None])
        ws.append(["a) ...", None, None, None])
        ws.append(["Last update", None, None, None])
        ws.append(["No.", "SEHK Code", "Underlying Stock Name", "HKATS Code"])
        ws.append([1, 981, "Semiconductor Manufacturing International Corp.", "SMC"])
        ws.append([2, 9992, "Pop Mart International Group Limited", "POP"])
        ws.append([3, None, "no code row", "XXX"])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def test_parses_code_pairs(self):
        assert options_hk.parse_class_xlsx(self._xlsx()) == {981: "SMC", 9992: "POP"}


class TestHkNoOptionNote:
    def _stocks(self) -> dict:
        return {
            "9992.HK": {"name": "泡泡玛特"},
            "981.HK": {"name": "中芯国际"},
            "325.HK": {"name": "布鲁可"},
            "PDD.US": {"name": "拼多多"},
        }

    def test_lists_only_stocks_absent_from_class_map(self, tmp_path):
        (tmp_path / "hk_option_classes.json").write_text(
            json.dumps({"map": {"9992": "POP", "981": "SMC"}}), encoding="utf-8"
        )
        note = rg._hk_no_option_note(
            str(tmp_path / "t.db"), self._stocks(), {"9992.HK"}
        )
        assert "布鲁可" in note  # 类代码表里查无 → 无上市期权
        assert "中芯国际" not in note  # 有期权, 只是本轮没采到 → 不算「无期权」
        assert "泡泡玛特" not in note
        assert "拼多多" not in note  # 非港股

    def test_missing_cache_returns_empty(self, tmp_path):
        assert (
            rg._hk_no_option_note(str(tmp_path / "t.db"), self._stocks(), set()) == ""
        )


def _fresh_cache(path, mapping: dict) -> None:
    path.write_text(
        json.dumps(
            {
                "as_of": "20261005",
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
                "map": mapping,
            }
        ),
        encoding="utf-8",
    )


class TestFetchClassMapFallback:
    def test_stale_cache_used_when_payload_is_not_zip(self, tmp_path, monkeypatch):
        """站点返回 HTML 错误页 (200 + 非 zip) 时必须降级, 不能炸掉整轮采集。"""
        cache = tmp_path / "hk_option_classes.json"
        cache.write_text(
            json.dumps(
                {
                    "as_of": "20200101",
                    "fetched_at": "2020-01-01T00:00:00",
                    "map": {"981": "SMC"},
                }
            ),
            encoding="utf-8",
        )

        def fake_get(url, timeout=60):
            if "Stock-Options?" in url:
                return (
                    b'<a href="The-List-of-Stock-Option-Classes-Available'
                    b'-for-Trading_20261005.xlsx">x</a>'
                )
            return b"<html>not a zip</html>"

        monkeypatch.setattr(options_hk, "_http_get", fake_get)
        assert options_hk.fetch_class_map(str(cache)) == {981: "SMC"}

    def test_raises_without_any_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            options_hk, "_http_get", lambda url, timeout=60: b"<html>x</html>"
        )
        try:
            options_hk.fetch_class_map(str(tmp_path / "none.json"))
        except RuntimeError:
            return
        raise AssertionError("无缓存且拉取失败时应抛错, 不能静默返回空表")


class TestFetchSpotDegradation:
    def test_unexpected_json_shape_returns_empty(self, monkeypatch):
        """CLI 输出结构变化时必须降级成 {} (收租率 None), 不能带崩整轮采集。"""

        class R:
            returncode = 0
            stdout = '{"data": []}'
            stderr = ""

        monkeypatch.setattr(options_hk.subprocess, "run", lambda *a, **k: R())
        assert options_hk.fetch_spot(["9992.HK"]) == {}

    def test_nonzero_exit_returns_empty(self, monkeypatch):
        class R:
            returncode = 1
            stdout = ""
            stderr = "not logged in"

        monkeypatch.setattr(options_hk.subprocess, "run", lambda *a, **k: R())
        assert options_hk.fetch_spot(["9992.HK"]) == {}


class TestCollectHk:
    def _prep(self, tmp_path, monkeypatch) -> str:
        db_path = str(tmp_path / "t.db")
        db.init_db(db_path)
        cache = tmp_path / "hk_option_classes.json"
        _fresh_cache(cache, {"9992": "POP"})
        monkeypatch.setattr(options_hk, "class_cache_path", lambda p: str(cache))
        return db_path

    def _dtop(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("20261013_1_dtop_o_seoch_opt_dtl_all.raw", _POP_RAW)
        return buf.getvalue()

    def test_no_dtop_is_a_skip_not_a_crash(self, tmp_path, monkeypatch):
        db_path = self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr(options_hk, "latest_dtop", lambda *a, **k: None)
        out = options_hk.collect_hk(db_path, ["9992.HK"])
        assert out["skipped"] == "no_dtop"
        assert out["results"] == {}

    def test_unmapped_symbols_reported(self, tmp_path, monkeypatch):
        db_path = self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr(options_hk, "latest_dtop", lambda *a, **k: None)
        out = options_hk.collect_hk(db_path, ["9992.HK", "325.HK"])
        assert out["unmapped"] == ["325.HK"]

    def test_dry_run_aggregates_without_writing(self, tmp_path, monkeypatch):
        db_path = self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr(
            options_hk,
            "latest_dtop",
            lambda *a, **k: (date(2026, 10, 13), self._dtop()),
        )
        monkeypatch.setattr(
            options_hk, "fetch_spot", lambda syms, cli="longbridge": {"9992.HK": SPOT}
        )
        out = options_hk.collect_hk(db_path, ["9992.HK"], dry_run=True)
        assert out["unmapped"] == []
        assert out["results"]["9992.HK"]["pcr_oi"] == 0.5
        assert db.get_option_daily_history(db_path, "9992.HK") == []  # dry-run 不落库

    def test_real_run_writes_daily_and_contracts(self, tmp_path, monkeypatch):
        db_path = self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr(
            options_hk,
            "latest_dtop",
            lambda *a, **k: (date(2026, 10, 13), self._dtop()),
        )
        monkeypatch.setattr(options_hk, "fetch_spot", lambda syms, cli="longbridge": {})
        options_hk.collect_hk(db_path, ["9992.HK"])
        row = db.get_latest_option_daily(db_path, "9992.HK", source="hkex")
        assert row is not None and row.pcr_oi == 0.5
        # 正股价拿不到 → 墙/MaxPain 留空, 而不是用未过滤的全链算出的脏值
        assert row.max_pain is None and row.wall_call is None
        assert row.stat_date > 0
