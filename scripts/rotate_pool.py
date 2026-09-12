#!/usr/bin/env python3
"""Pool rotation tool (v0.9 T2): validate configs, plan/apply rotations.

Two modes:

  --check              Health-check the current repo: 6 validations distilled
                       from the 9/12 manual verification chain. Exit 0 = all
                       hard checks green.
  --plan FILE [--apply]
                       Apply a rotation spec (JSON) to the group configs and
                       report pool. Dry-run by default (validates a simulated
                       result and prints the suggested commit message);
                       --apply writes configs + ledger rows. Never commits —
                       git stays a manual gate.

Rotation spec format:
  {
    "effective_date": "2026-09-09",
    "config_version": "v0.9.1",
    "reason":         "low-volume rotation",
    "changes": [
      {"action": "remove", "stock_code": "NVO.US", "avg_daily_posts": 4.6},
      {"action": "add", "stock_code": "981.HK", "group": "g0",
       "name": "中芯国际", "sector": "半导体"}
    ]
  }
  remove: group auto-detected from live configs; avg_daily_posts optional
          snapshot for the ledger.
  add:    target group required; name/sector land in config.report.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GROUP_CONFIGS = {  # canonical group -> config file name
    "g0": "config.json",
    "g1": "config.g1.json",
    "g2": "config.g2.json",
    "g3": "config.g3.json",
    "g4": "config.g4.json",
    "g5": "config.g5.json",
}
REPORT_CONFIG = "config.report.json"
CRON_JOB_NAMES = {  # expected hermes cron jobs (soft check)
    "g0": "xueqiu-pipeline",
    "g1": "xueqiu-pipeline-g1",
    "g2": "xueqiu-pipeline-g2",
    "g3": "xueqiu-pipeline-g3",
    "g4": "xueqiu-pipeline-g4",
    "g5": "xueqiu-pipeline-g5",
}

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


class RotationError(Exception):
    """Invalid rotation spec or inapplicable change."""


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""


@dataclass
class RepoConfigs:
    """Live (or simulated) view of the 7 config files."""
    groups: dict[str, list[str]] = field(default_factory=dict)   # g0..g5 -> whitelist
    report_stocks: dict[str, dict] = field(default_factory=dict)  # code -> {name, sector}


def load_configs(root: Path) -> RepoConfigs:
    """Parse all group configs + report config under root/etc. Raises on bad JSON."""
    rc = RepoConfigs()
    for gname, fname in GROUP_CONFIGS.items():
        data = json.loads((root / "etc" / fname).read_text())
        rc.groups[gname] = list(data.get("crawler", {}).get("whitelist", []))
    rc.report_stocks = json.loads(
        (root / "etc" / REPORT_CONFIG).read_text()
    ).get("stocks", {})
    return rc


# ── checks ─────────────────────────────────────────────────────────────

def check_json(root: Path) -> CheckResult:
    bad = []
    for fname in [*GROUP_CONFIGS.values(), REPORT_CONFIG]:
        try:
            json.loads((root / "etc" / fname).read_text())
        except (json.JSONDecodeError, OSError) as e:
            bad.append(f"{fname}: {e}")
    if bad:
        return CheckResult("1.json", FAIL, "; ".join(bad))
    return CheckResult("1.json", PASS, "7 files parse OK")


def check_union(rc: RepoConfigs) -> CheckResult:
    union: set[str] = set()
    for wl in rc.groups.values():
        union.update(wl)
    pool = set(rc.report_stocks)
    if union == pool:
        return CheckResult("2.union==pool", PASS, f"{len(union)} == {len(pool)}")
    return CheckResult(
        "2.union==pool", FAIL,
        f"crawl union {len(union)} != report pool {len(pool)}; "
        f"union-only={sorted(union - pool)}, pool-only={sorted(pool - union)}",
    )


def check_no_dup(rc: RepoConfigs) -> CheckResult:
    seen: dict[str, str] = {}
    dups = []
    for gname, wl in rc.groups.items():
        for code in wl:
            if code in seen:
                dups.append(f"{code} in {seen[code]}+{gname}")
            seen[code] = gname
    if dups:
        return CheckResult("3.no-dup", FAIL, "; ".join(dups))
    return CheckResult("3.no-dup", PASS, f"{len(seen)} stocks, no cross-group dup")


def check_groups(rc: RepoConfigs, cron_jobs_path: Path | None) -> CheckResult:
    empty = [g for g, wl in rc.groups.items() if not wl]
    missing = [g for g in GROUP_CONFIGS if g not in rc.groups]
    problems = []
    if missing:
        problems.append(f"missing groups: {missing}")
    if empty:
        problems.append(f"empty groups: {empty}")
    cron_note = ""
    if cron_jobs_path and cron_jobs_path.exists():
        try:
            jobs = json.loads(cron_jobs_path.read_text())
            names = {j.get("name") for j in jobs.get("jobs", [])}
            absent = [n for n in CRON_JOB_NAMES.values() if n not in names]
            if absent:
                problems.append(f"cron jobs missing: {absent}")
            else:
                cron_note = "; cron jobs 6/6 present"
        except (json.JSONDecodeError, OSError) as e:
            cron_note = f"; cron jobs file unreadable ({e}) — skipped"
    else:
        cron_note = "; cron jobs file not found — correspondence skipped"
    if problems:
        return CheckResult("4.groups+cron", FAIL, "; ".join(problems) + cron_note)
    return CheckResult("4.groups+cron", PASS, f"G0-G5 all non-empty{cron_note}")


def check_morning_brief(rc: RepoConfigs, mb_db: Path | None) -> CheckResult:
    if not mb_db or not mb_db.exists():
        return CheckResult("5.morning-brief", WARN, "DB not found — skipped")
    conn = sqlite3.connect(f"file:{mb_db}?mode=ro", uri=True)
    try:
        active = {
            r[0] for r in conn.execute(
                "SELECT stock_code FROM watchlist WHERE is_active = 1"
            )
        }
    except sqlite3.Error as e:
        return CheckResult("5.morning-brief", WARN, f"DB unreadable ({e}) — skipped")
    finally:
        conn.close()
    pool = set(rc.report_stocks)
    uncovered = pool - active
    if uncovered:
        return CheckResult(
            "5.morning-brief", FAIL,
            f"pool not covered: {sorted(uncovered)} (active={len(active)})",
        )
    return CheckResult("5.morning-brief", PASS, f"{len(pool)}/{len(pool)} covered (active={len(active)})")


def check_ledger(rc: RepoConfigs, db_path: Path | None) -> CheckResult:
    if not db_path or not db_path.exists():
        return CheckResult("6.ledger", WARN, "monitor.db not found — skipped")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT stock_code, action FROM pool_history ORDER BY effective_date, id"
        ).fetchall()
    except sqlite3.Error as e:
        return CheckResult("6.ledger", WARN, f"ledger unreadable ({e}) — skipped")
    finally:
        conn.close()
    state: dict[str, bool] = {}
    for code, action in rows:
        state[code] = action == "add"
    ledger_pool = {c for c, on in state.items() if on}
    live_pool = set(rc.report_stocks)
    if ledger_pool != live_pool:
        return CheckResult(
            "6.ledger", FAIL,
            f"replay {len(ledger_pool)} != pool {len(live_pool)}; "
            f"ledger-only={sorted(ledger_pool - live_pool)}, "
            f"pool-only={sorted(live_pool - ledger_pool)}",
        )
    return CheckResult("6.ledger", PASS, f"replay == pool ({len(live_pool)})")


def run_checks(
    root: Path,
    mb_db: Path | None = None,
    db_path: Path | None = None,
    cron_jobs_path: Path | None = None,
) -> list[CheckResult]:
    try:
        rc = load_configs(root)
    except (json.JSONDecodeError, OSError, KeyError) as e:
        # Configs unparseable: 1.json FAIL, everything downstream cannot run.
        return [CheckResult("1.json", FAIL, str(e))] + [
            CheckResult(n, WARN, "skipped — configs unparseable")
            for n in ("2.union==pool", "3.no-dup", "4.groups+cron",
                      "5.morning-brief", "6.ledger")
        ]
    return [
        check_json(root),
        check_union(rc),
        check_no_dup(rc),
        check_groups(rc, cron_jobs_path),
        check_morning_brief(rc, mb_db),
        check_ledger(rc, db_path),
    ]


# ── plan/apply ─────────────────────────────────────────────────────────

def load_spec(path: Path) -> dict:
    spec = json.loads(path.read_text())
    for key in ("effective_date", "config_version", "changes"):
        if key not in spec:
            raise RotationError(f"spec missing required key: {key}")
    for ch in spec["changes"]:
        if ch.get("action") not in ("add", "remove"):
            raise RotationError(f"bad action: {ch.get('action')!r}")
        if not ch.get("stock_code"):
            raise RotationError("change missing stock_code")
        if ch["action"] == "add" and ch.get("group") not in GROUP_CONFIGS:
            raise RotationError(
                f"add {ch['stock_code']}: group must be one of {sorted(GROUP_CONFIGS)}"
            )
    return spec


def apply_spec_to_configs(rc: RepoConfigs, spec: dict) -> RepoConfigs:
    """Pure: apply spec changes onto configs in memory, returning a new view."""
    groups = {g: list(wl) for g, wl in rc.groups.items()}
    stocks = {c: dict(v) for c, v in rc.report_stocks.items()}
    for ch in spec["changes"]:
        code, action = ch["stock_code"], ch["action"]
        if action == "remove":
            if code not in stocks:
                raise RotationError(f"remove {code}: not in report pool")
            del stocks[code]
            owner = next((g for g, wl in groups.items() if code in wl), None)
            if owner is None:
                raise RotationError(
                    f"remove {code}: in report pool but no group crawls it "
                    "(manual config surgery needed)"
                )
            groups[owner].remove(code)
        else:  # add
            if code in stocks:
                raise RotationError(f"add {code}: already in report pool")
            g = ch["group"]
            if code in groups[g]:
                raise RotationError(f"add {code}: already in {g} whitelist")
            groups[g].append(code)
            stocks[code] = {"name": ch.get("name", code), "sector": ch.get("sector", "其他")}
    return RepoConfigs(groups=groups, report_stocks=stocks)


def _dump_preserving_trailing_newline(template_path: Path, data: dict, out_path: Path) -> None:
    """Serialize data, matching the template file's trailing-newline convention.

    Existing etc/ configs lack a trailing newline; rewriting with one would
    add a stray '\\ No newline' diff hunk to every config on the first --apply.
    Match whatever the template (pre-rotation) file does, byte for byte.
    """
    template_raw = template_path.read_text()
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if template_raw.endswith("\n"):
        text += "\n"
    out_path.write_text(text)


def write_configs(root: Path, rc: RepoConfigs, template_root: Path) -> None:
    """Materialize a RepoConfigs into root/etc/, preserving all non-pool keys
    from the template configs (template_root holds the pre-rotation files)."""
    for gname, fname in GROUP_CONFIGS.items():
        data = json.loads((template_root / "etc" / fname).read_text())
        data.setdefault("crawler", {})["whitelist"] = rc.groups[gname]
        _dump_preserving_trailing_newline(
            template_root / "etc" / fname, data, root / "etc" / fname)
    data = json.loads((template_root / "etc" / REPORT_CONFIG).read_text())
    data["stocks"] = rc.report_stocks
    _dump_preserving_trailing_newline(
        template_root / "etc" / REPORT_CONFIG, data, root / "etc" / REPORT_CONFIG)


def ledger_rows(spec: dict) -> list[dict]:
    rows = []
    for ch in spec["changes"]:
        rows.append({
            "stock_code": ch["stock_code"],
            "action": ch["action"],
            "effective_date": spec["effective_date"],
            "reason": spec.get("reason", ""),
            "avg_daily_posts": ch.get("avg_daily_posts"),
            "config_version": spec["config_version"],
        })
    return rows


def commit_message(spec: dict, before: int, after: int) -> str:
    removes = [c for c in spec["changes"] if c["action"] == "remove"]
    adds = [c for c in spec["changes"] if c["action"] == "add"]
    lines = [
        f"feat(config): pool rotation {before} -> {after} ({spec['config_version']})",
        "",
        f"Reason: {spec.get('reason', '')}",
        f"Effective date: {spec['effective_date']}",
        "",
    ]
    if removes:
        lines.append(f"Drop {len(removes)}:")
        for c in removes:
            vol = f" (14d avg {c['avg_daily_posts']} posts/day)" if c.get("avg_daily_posts") is not None else ""
            lines.append(f"- {c['stock_code']}{vol}")
        lines.append("")
    if adds:
        lines.append(f"Add {len(adds)}:")
        for c in adds:
            lines.append(f"- {c['stock_code']} {c.get('name', '')} -> {c['group']}")
        lines.append("")
    lines.append("Ledger rows written to monitor.db pool_history by rotate_pool.py.")
    lines.append("Generated by scripts/rotate_pool.py — review diff before pushing.")
    return "\n".join(lines)


def plan_mode(args: argparse.Namespace) -> int:
    spec = load_spec(Path(args.plan))
    root = Path(args.root) if args.root else PROJECT_ROOT
    live = load_configs(root)
    rotated = apply_spec_to_configs(live, spec)

    # Simulate: materialize into a temp root, run the full check suite there.
    with tempfile.TemporaryDirectory() as tmp:
        virtual = Path(tmp) / "sim"
        (virtual / "etc").mkdir(parents=True)
        write_configs(virtual, rotated, root)
        results = run_checks(
            virtual,
            mb_db=Path(args.morning_brief_db) if args.morning_brief_db else _default_mb_db(root),
            db_path=None,  # ledger can't validate pre-insert; check after apply
            cron_jobs_path=Path(args.cron_jobs) if args.cron_jobs else _default_cron_jobs(),
        )
    hard_fail = [r for r in results if r.status == FAIL]
    print(f"[plan] rotation {len(live.report_stocks)} -> {len(rotated.report_stocks)} stocks; "
          f"simulated checks: {'ALL GREEN' if not hard_fail else 'FAILED'}")
    for r in results:
        mark = {"PASS": "✔", "WARN": "△", "FAIL": "✘"}[r.status]
        print(f"  {mark} {r.name}: {r.detail}")
    if hard_fail:
        print("[plan] aborted — fix spec or configs first")
        return 1

    msg = commit_message(spec, len(live.report_stocks), len(rotated.report_stocks))
    print("\n----- suggested commit message -----")
    print(msg)
    print("------------------------------------\n")

    if not args.apply:
        print("[plan] dry-run only — re-run with --apply to write configs + ledger")
        return 0

    # Apply: overwrite real configs, insert ledger rows, verify final state.
    write_configs(root, rotated, root)
    db_path = Path(args.db) if args.db else root / "data" / "monitor.db"
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        try:
            inserted = 0
            for row in ledger_rows(spec):
                cur = conn.execute(
                    "INSERT OR IGNORE INTO pool_history "
                    "(stock_code, action, effective_date, reason, avg_daily_posts, config_version) "
                    "VALUES (:stock_code, :action, :effective_date, :reason, "
                    ":avg_daily_posts, :config_version)",
                    row,
                )
                inserted += cur.rowcount
            conn.commit()
        finally:
            conn.close()
        print(f"[apply] configs written; {inserted} ledger rows inserted into {db_path}")
    else:
        print(f"[apply] configs written; ledger DB not found at {db_path} — rows NOT recorded")

    final = run_checks(
        root, mb_db=Path(args.morning_brief_db) if args.morning_brief_db else _default_mb_db(root),
        db_path=db_path,
        cron_jobs_path=Path(args.cron_jobs) if args.cron_jobs else _default_cron_jobs(),
    )
    hard_fail = [r for r in final if r.status == FAIL]
    for r in final:
        mark = {"PASS": "✔", "WARN": "△", "FAIL": "✘"}[r.status]
        print(f"  {mark} {r.name}: {r.detail}")
    if hard_fail:
        print("[apply] WARNING — post-apply checks failed; inspect git diff, do not push blind")
        return 1
    print("[apply] green. Next (manual gate): git add etc/ && git commit (message above)")
    return 0


def _default_mb_db(root: Path) -> Path:
    env = os.environ.get("MORNING_BRIEF_DB", "").strip()
    if env:
        return Path(env)
    return root.parent / "morning-brief" / "data" / "morning-brief.db"


def _default_cron_jobs() -> Path:
    return Path.home() / ".hermes" / "cron" / "jobs.json"


def check_mode(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else PROJECT_ROOT
    try:
        results = run_checks(
            root,
            mb_db=Path(args.morning_brief_db) if args.morning_brief_db else _default_mb_db(root),
            db_path=Path(args.db) if args.db else root / "data" / "monitor.db",
            cron_jobs_path=Path(args.cron_jobs) if args.cron_jobs else _default_cron_jobs(),
        )
    except (json.JSONDecodeError, OSError, KeyError) as e:
        print(f"✘ 1.json: cannot load configs — {e}")
        return 1
    hard_fail = False
    for r in results:
        mark = {"PASS": "✔", "WARN": "△", "FAIL": "✘"}[r.status]
        print(f"  {mark} {r.name}: {r.detail}")
        hard_fail = hard_fail or r.status == FAIL
    print("ALL GREEN" if not hard_fail else "CHECKS FAILED")
    return 1 if hard_fail else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Pool rotation: check / plan / apply")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="validate current repo state")
    mode.add_argument("--plan", metavar="SPEC.json", help="rotation spec file")
    parser.add_argument("--apply", action="store_true", help="with --plan: write configs + ledger")
    parser.add_argument("--root", help="project root (default: this repo)")
    parser.add_argument("--db", help="monitor.db path (default: <root>/data/monitor.db)")
    parser.add_argument("--morning-brief-db", help="morning-brief.db path")
    parser.add_argument("--cron-jobs", help="hermes cron jobs.json path")
    args = parser.parse_args()
    if args.check:
        return check_mode(args)
    return plan_mode(args)


if __name__ == "__main__":
    sys.exit(main())
