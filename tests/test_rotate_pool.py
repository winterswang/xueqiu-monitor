"""Unit tests for scripts/rotate_pool.py (v0.9 T2).

Two layers:
- check suite against a synthetic repo fixture (valid configs, dup
  injected, union mismatch, empty group, morning-brief missing coverage,
  ledger replay mismatch)
- plan/apply end-to-end on a synthetic repo + temp sqlite ledger:
  spec validation errors, dry-run simulation, apply writes configs +
  ledger rows, post-apply ledger replay == new pool
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent))

import rotate_pool as rp  # noqa: E402
from src import db as dbmod  # noqa: E402


def make_repo(root: Path, groups: dict[str, list[str]], stocks: dict[str, dict]) -> None:
    """Materialize synthetic etc/ configs."""
    (root / "etc").mkdir(parents=True, exist_ok=True)
    for gname, fname in rp.GROUP_CONFIGS.items():
        (root / "etc" / fname).write_text(json.dumps({
            "db_path": "data/monitor.db",
            "crawler": {"whitelist": groups.get(gname, [])},
        }))
    (root / "etc" / rp.REPORT_CONFIG).write_text(json.dumps({"stocks": stocks}))


@pytest.fixture
def repo():
    """Healthy synthetic repo: every group non-empty, pool == union."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "proj"
        groups = {"g0": ["A.US", "B.US"], "g1": ["C.US"], "g2": ["D.US"],
                  "g3": ["F.US"], "g4": ["G.US"], "g5": ["H.US"]}
        stocks = {c: {"name": c, "sector": "其他"}
                  for c in ["A.US", "B.US", "C.US", "D.US", "F.US", "G.US", "H.US"]}
        make_repo(root, groups, stocks)
        yield root


def names(results):
    return {r.name: r for r in results}


class TestChecks:
    def test_healthy_repo_all_pass(self, repo):
        results = rp.run_checks(repo, cron_jobs_path=None)
        by = names(results)
        assert by["1.json"].status == rp.PASS
        assert by["2.union==pool"].status == rp.PASS
        assert by["3.no-dup"].status == rp.PASS

    def test_empty_groups_fail(self, repo):
        data = json.loads((repo / "etc" / "config.g3.json").read_text())
        data["crawler"]["whitelist"] = []
        (repo / "etc" / "config.g3.json").write_text(json.dumps(data))
        by = names(rp.run_checks(repo, cron_jobs_path=None))
        assert by["4.groups+cron"].status == rp.FAIL
        assert "empty groups" in by["4.groups+cron"].detail

    def test_dup_detected(self, repo):
        data = json.loads((repo / "etc" / "config.g2.json").read_text())
        data["crawler"]["whitelist"].append("A.US")  # cross-group dup
        (repo / "etc" / "config.g2.json").write_text(json.dumps(data))
        by = names(rp.run_checks(repo, cron_jobs_path=None))
        assert by["3.no-dup"].status == rp.FAIL
        assert "A.US" in by["3.no-dup"].detail

    def test_union_mismatch_detected(self, repo):
        data = json.loads((repo / "etc" / "config.report.json").read_text())
        del data["stocks"]["D.US"]  # pool shrinks, crawl union unchanged
        (repo / "etc" / "config.report.json").write_text(json.dumps(data))
        by = names(rp.run_checks(repo, cron_jobs_path=None))
        assert by["2.union==pool"].status == rp.FAIL
        assert "D.US" in by["2.union==pool"].detail

    def test_bad_json_detected(self, repo):
        (repo / "etc" / "config.json").write_text("{not json")
        by = names(rp.run_checks(repo, cron_jobs_path=None))
        assert by["1.json"].status == rp.FAIL

    def test_morning_brief_coverage(self, repo):
        with tempfile.TemporaryDirectory() as tmp:
            mb = Path(tmp) / "mb.db"
            conn = sqlite3.connect(mb)
            conn.execute(
                "CREATE TABLE watchlist (stock_code TEXT PRIMARY KEY, is_active BOOLEAN)"
            )
            conn.executemany(
                "INSERT INTO watchlist VALUES (?, 1)", [("A.US",), ("B.US",), ("C.US",)]
            )  # D.US missing
            conn.commit()
            conn.close()
            by = names(rp.run_checks(repo, mb_db=mb))
            assert by["5.morning-brief"].status == rp.FAIL
            assert "D.US" in by["5.morning-brief"].detail

    def test_ledger_replay_mismatch(self, repo):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "mon.db"
            dbmod.init_db(str(db_path))
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO pool_history (stock_code, action, effective_date) "
                "VALUES ('A.US', 'add', '2026-01-01')"
            )  # only A in ledger, pool has 4
            conn.commit()
            conn.close()
            by = names(rp.run_checks(repo, db_path=db_path))
            assert by["6.ledger"].status == rp.FAIL
            assert "B.US" in by["6.ledger"].detail  # pool-only


class TestSpec:
    def _spec(self, tmp: Path, **overrides) -> Path:
        spec = {
            "effective_date": "2026-09-20",
            "config_version": "vtest",
            "reason": "test rotation",
            "changes": [
                {"action": "remove", "stock_code": "A.US", "avg_daily_posts": 4.6},
                {"action": "add", "stock_code": "E.HK", "group": "g1",
                 "name": "E公司", "sector": "消费"},
            ],
        }
        spec.update(overrides)
        p = tmp / "spec.json"
        p.write_text(json.dumps(spec))
        return p

    def test_load_spec_rejects_bad_action(self, tmp_path):
        p = self._spec(tmp_path)
        data = json.loads(p.read_text())
        data["changes"][0]["action"] = "swap"
        p.write_text(json.dumps(data))
        with pytest.raises(rp.RotationError):
            rp.load_spec(p)

    def test_load_spec_requires_group_for_add(self, tmp_path):
        p = self._spec(tmp_path)
        data = json.loads(p.read_text())
        del data["changes"][1]["group"]
        p.write_text(json.dumps(data))
        with pytest.raises(rp.RotationError):
            rp.load_spec(p)

    def test_remove_nonexistent_fails(self, repo, tmp_path):
        spec = json.loads(self._spec(tmp_path).read_text())
        spec["changes"][0]["stock_code"] = "ZZZ.US"
        rc = rp.load_configs(repo)
        with pytest.raises(rp.RotationError):
            rp.apply_spec_to_configs(rc, spec)

    def test_add_dup_fails(self, repo, tmp_path):
        spec = json.loads(self._spec(tmp_path).read_text())
        spec["changes"][1]["stock_code"] = "C.US"  # already in g1
        rc = rp.load_configs(repo)
        with pytest.raises(rp.RotationError):
            rp.apply_spec_to_configs(rc, spec)


class TestPlanApply:
    def test_end_to_end_apply(self, repo, tmp_path, capsys, monkeypatch):
        # populate ledger so pre-state is consistent
        db_path = repo / "data" / "monitor.db"
        db_path.parent.mkdir(exist_ok=True)
        dbmod.init_db(str(db_path))
        conn = sqlite3.connect(db_path)
        for code in ["A.US", "B.US", "C.US", "D.US", "F.US", "G.US", "H.US"]:
            conn.execute(
                "INSERT INTO pool_history (stock_code, action, effective_date) "
                f"VALUES ('{code}', 'add', '2026-01-01')"
            )
        conn.commit()
        conn.close()

        spec = {
            "effective_date": "2026-09-20",
            "config_version": "vtest",
            "reason": "rotate low volume",
            "changes": [
                {"action": "remove", "stock_code": "A.US", "avg_daily_posts": 4.6},
                {"action": "add", "stock_code": "E.HK", "group": "g1",
                 "name": "E公司", "sector": "消费"},
            ],
        }
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(spec))

        args = type("A", (), {
            "plan": str(spec_path), "apply": True, "root": str(repo),
            "db": str(db_path), "morning_brief_db": "", "cron_jobs": "",
        })()
        rc = rp.plan_mode(args)
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "ALL GREEN" in out or "green" in out

        # configs mutated on disk: A gone, E in g1 + report
        live = rp.load_configs(repo)
        assert "A.US" not in live.report_stocks
        assert "E.HK" in live.report_stocks
        assert "E.HK" in live.groups["g1"]
        assert "A.US" not in live.groups["g0"]

        # ledger: 2 new rows; replay == new pool
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT stock_code, action FROM pool_history WHERE config_version='vtest'"
        ).fetchall()
        conn.close()
        assert sorted(rows) == [("A.US", "remove"), ("E.HK", "add")]

        # commit message includes volume snapshot
        assert "4.6" in out

    def test_dry_run_writes_nothing(self, repo, tmp_path, capsys):
        spec = {
            "effective_date": "2026-09-20",
            "config_version": "vtest",
            "reason": "x",
            "changes": [
                {"action": "remove", "stock_code": "A.US"},
            ],
        }
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(spec))
        args = type("A", (), {
            "plan": str(spec_path), "apply": False, "root": str(repo),
            "db": "", "morning_brief_db": "", "cron_jobs": "",
        })()
        rc = rp.plan_mode(args)
        assert rc == 0
        live = rp.load_configs(repo)
        assert "A.US" in live.report_stocks  # untouched
        assert not (repo / "data").exists()  # no ledger writes

    def test_real_repo_check_green(self):
        """Smoke: the real repo passes --check end-to-end (CI-safe skip)."""
        real = SCRIPTS_DIR.parent
        if not (real / "etc" / "config.report.json").exists():
            pytest.skip("real repo configs unavailable")
        results = rp.run_checks(
            real,
            mb_db=real.parent / "morning-brief" / "data" / "morning-brief.db",
            db_path=real / "data" / "monitor.db",
            cron_jobs_path=Path.home() / ".hermes" / "cron" / "jobs.json",
        )
        fails = [r for r in results if r.status == rp.FAIL]
        assert not fails, [r.detail for r in fails]
