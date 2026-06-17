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


class WatchlistError(Exception):
    """Non-recoverable error during watchlist fetch."""


def fetch_watchlist() -> list[dict]:
    """Run `longbridge watchlist --format json` and flatten to [{stock_code, stock_name}]."""
    cli = shutil.which("longbridge")
    if not cli:
        raise WatchlistError(
            "未找到 longbridge CLI，请先 `cargo install --path .` 或 `brew install`"
        )

    try:
        result = subprocess.run(
            [cli, "watchlist", "--format", "json"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise WatchlistError("longbridge CLI 超时 (30s)")

    if result.returncode != 0:
        raise WatchlistError(
            f"longbridge CLI 失败 (exit={result.returncode}): {result.stderr.strip()[:200]}"
        )

    try:
        groups = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise WatchlistError("longbridge CLI 输出非 JSON 格式")

    if not isinstance(groups, list):
        raise WatchlistError(
            f"longbridge CLI 输出格式异常（期望数组，实际 {type(groups).__name__}）"
        )

    return [
        {
            "stock_code": sec.get("symbol", ""),
            "stock_name": (
                sec.get("name", "")
                or sec.get("name_cn", "")
                or sec.get("name_en", "")
            ),
        }
        for g in groups
        for sec in g.get("securities", [])
    ]


def main() -> None:
    try:
        stocks = fetch_watchlist()
    except WatchlistError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)
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
