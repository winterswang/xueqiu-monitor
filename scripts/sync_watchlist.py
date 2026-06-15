#!/usr/bin/env python3
"""Sync watchlist from Longbridge to data/watchlist.json.

Thin wrapper around the `longbridge` CLI which manages OAuth + token refresh
internally. We just call `longbridge watchlist --format json`, flatten the
groups, and write the project-canonical schema {stock_code, stock_name}.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = PROJECT_ROOT / "data" / "watchlist.json"


def fetch_watchlist() -> list[dict]:
    """Run `longbridge watchlist --format json` and flatten to [{stock_code, stock_name}]."""
    cli = shutil.which("longbridge")
    if not cli:
        print("❌ 未找到 longbridge CLI，请先 `cargo install --path .` 或 `brew install`", file=sys.stderr)
        sys.exit(1)

    result = subprocess.run(
        [cli, "watchlist", "--format", "json"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print(f"❌ longbridge CLI 失败 (exit={result.returncode}): {result.stderr.strip()[:200]}", file=sys.stderr)
        sys.exit(1)

    groups = json.loads(result.stdout)
    return [
        {"stock_code": sec["symbol"], "stock_name": sec["name"]}
        for g in groups for sec in g.get("securities", [])
    ]


def main() -> None:
    stocks = fetch_watchlist()
    if not stocks:
        print("⚠️  watchlist 为空")
        sys.exit(1)

    # Deduplicate (preserve first-seen order)
    seen: set[str] = set()
    unique: list[dict] = []
    for s in stocks:
        if s["stock_code"] not in seen:
            seen.add(s["stock_code"])
            unique.append(s)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(unique, ensure_ascii=False, indent=2) + "\n")
    print(f"✅ 自选股同步完成: {len(unique)} 只 → {OUTPUT_PATH}")
    for s in unique[:20]:
        print(f"   {s['stock_code']:20s} {s['stock_name']}")
    if len(unique) > 20:
        print(f"   ... 等 {len(unique)} 只")


if __name__ == "__main__":
    main()
