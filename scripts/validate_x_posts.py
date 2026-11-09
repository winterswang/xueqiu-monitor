#!/usr/bin/env python3
"""X 舆情契约校验器：采集落盘后、日报读取前的强制体检。

它回答三个问题，且只回答这三个：

1. **文件在不在、能不能解析** —— 采集是否真的发生过。
2. **字段全不全、类型对不对** —— 融合层能否安全读取（不靠运气）。
3. **覆盖率够不够** —— 预期 N 只里实际有数据几只，谁缺。

不做语义判断（内容好坏由 x_sources.clean_posts 与 LLM 负责），也不修改
任何文件 —— 校验器永远只读。

用法：

    python3 scripts/validate_x_posts.py --date 2026-11-10
    python3 scripts/validate_x_posts.py --date 2026-11-10 \\
        --tickers META,GOOGL,RDDT --min-posts 1
    python3 scripts/validate_x_posts.py --date 2026-11-10 --coverage-only

退出码：0 全过；1 有 error；2 参数/目录问题。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path("data/x_posts")
VALID_STATUS = {"ok", "partial", "empty", "login_required", "failed"}
REQUIRED_POST_FIELDS = ("tweet_id", "url", "author", "text", "slice", "created_at_utc")


def _is_iso_time(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    s = value.strip().replace("Z", "+00:00")
    try:
        datetime.fromisoformat(s)
    except ValueError:
        return False
    return True


def _is_x_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return value.startswith("https://x.com/") or value.startswith(
        "https://twitter.com/"
    )


def validate_file(path: Path, expected_ticker: str, expected_date: str) -> tuple[list[str], list[str], dict[str, Any]]:
    """返回 (errors, warnings, summary)。"""
    errors: list[str] = []
    warnings: list[str] = []
    summary: dict[str, Any] = {"ticker": expected_ticker, "posts": 0, "status": ""}

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return [f"{path.name}: JSON 解析失败 {type(e).__name__}: {e}"], [], summary

    if not isinstance(payload, dict):
        return [f"{path.name}: 顶层不是对象"], [], summary

    ticker = payload.get("ticker")
    if ticker != expected_ticker:
        errors.append(f"{path.name}: ticker={ticker!r} 与文件名 {expected_ticker!r} 不一致")
    if payload.get("date") != expected_date:
        errors.append(f"{path.name}: date={payload.get('date')!r} 与目录 {expected_date!r} 不一致")

    status = payload.get("status")
    summary["status"] = status
    if status not in VALID_STATUS:
        errors.append(f"{path.name}: status={status!r} 不在 {sorted(VALID_STATUS)}")

    posts = payload.get("posts")
    if not isinstance(posts, list):
        errors.append(f"{path.name}: posts 不是数组")
        return errors, warnings, summary

    summary["posts"] = len(posts)
    seen_ids: set[str] = set()
    for i, post in enumerate(posts):
        where = f"{path.name}#{i}"
        if not isinstance(post, dict):
            errors.append(f"{where}: 不是对象")
            continue
        for f in REQUIRED_POST_FIELDS:
            if not post.get(f):
                errors.append(f"{where}: 缺字段 {f}")
        if post.get("url") and not _is_x_url(post["url"]):
            warnings.append(f"{where}: url 不似 X 链接 {post['url'][:60]}")
        created = post.get("created_at_utc")
        if created and not _is_iso_time(created):
            errors.append(f"{where}: created_at_utc 不是 ISO 时间 {created!r}")
        metrics = post.get("metrics")
        if metrics is not None and not isinstance(metrics, dict):
            errors.append(f"{where}: metrics 不是对象")
        elif isinstance(metrics, dict):
            for k, v in metrics.items():
                if k == "partial":
                    continue
                if not isinstance(v, int) or v < 0:
                    errors.append(f"{where}: metrics.{k}={v!r} 不是非负整数")
        tid = post.get("tweet_id")
        if tid:
            if tid in seen_ids:
                errors.append(f"{where}: tweet_id {tid} 文件内重复")
            seen_ids.add(tid)

    slices = payload.get("slices")
    if not isinstance(slices, list) or not slices:
        warnings.append(f"{path.name}: 缺 slices 元数据（不影响读取，但健康检查无法判断切片完整度）")
    else:
        summary["slices"] = {s.get("name"): s.get("returned") for s in slices if isinstance(s, dict)}

    return errors, warnings, summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="X 舆情契约校验")
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    ap.add_argument("--date", required=True)
    ap.add_argument("--tickers", default="", help="逗号分隔，省略则校验目录下全部文件")
    ap.add_argument("--min-posts", type=int, default=0, help="每票最少帖数（低于记 warning）")
    ap.add_argument("--coverage-only", action="store_true", help="只报覆盖率")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    root = Path(args.root)
    day_dir = root / args.date
    if not day_dir.exists():
        msg = f"目录不存在: {day_dir}"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
        else:
            print(f"ERROR {msg}")
        return 2

    expected = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if not expected:
        expected = sorted(p.stem for p in day_dir.glob("*.json") if p.stem != "_status")

    errors: list[str] = []
    warnings: list[str] = []
    rows: list[dict[str, Any]] = []
    missing: list[str] = []

    status_payload: dict[str, Any] = {}
    status_path = day_dir / "_status.json"
    if status_path.exists():
        try:
            status_payload = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception as e:
            warnings.append(f"_status.json 解析失败: {type(e).__name__}: {e}")

    for ticker in expected:
        path = day_dir / f"{ticker}.json"
        if not path.exists():
            missing.append(ticker)
            recorded = (status_payload.get("tickers") or {}).get(ticker) or {}
            rows.append(
                {
                    "ticker": ticker,
                    "present": False,
                    "status": recorded.get("status") or "missing",
                    "posts": 0,
                    "note": recorded.get("note", ""),
                }
            )
            continue
        if args.coverage_only:
            try:
                n = len(json.loads(path.read_text(encoding="utf-8")).get("posts") or [])
            except Exception:
                n = 0
            rows.append({"ticker": ticker, "present": True, "status": "", "posts": n})
            continue
        e, w, summary = validate_file(path, ticker, args.date)
        errors.extend(e)
        warnings.extend(w)
        if args.min_posts and summary["posts"] < args.min_posts:
            warnings.append(
                f"{ticker}: 仅 {summary['posts']} 条，低于门槛 {args.min_posts}"
            )
        rows.append({"present": True, **summary})

    covered = sum(1 for r in rows if r["present"])
    total_posts = sum(r["posts"] for r in rows)
    ok = not errors

    if args.json:
        print(
            json.dumps(
                {
                    "ok": ok,
                    "date": args.date,
                    "expected": len(expected),
                    "covered": covered,
                    "missing": missing,
                    "total_posts": total_posts,
                    "errors": errors,
                    "warnings": warnings,
                    "rows": rows,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"== X 源契约校验 {args.date} ==")
        print(f"覆盖 {covered}/{len(expected)}，共 {total_posts} 条")
        for r in rows:
            if r["present"]:
                extra = f" status={r.get('status')}" if r.get("status") else ""
                print(f"  OK   {r['ticker']:<8} {r['posts']:>3} 条{extra}")
            else:
                note = f" ({r['note']})" if r.get("note") else ""
                print(f"  MISS {r['ticker']:<8} — {r['status']}{note}")
        for w in warnings:
            print(f"WARN {w}")
        for e in errors:
            print(f"ERROR {e}")
        print("PASS" if ok else "FAIL")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
