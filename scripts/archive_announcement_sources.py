#!/usr/bin/env python3
"""按日期归档公告原始 PDF/SEC HTML，并写入断点状态。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# detail_fetcher 顶层导入 xueqiu_analyzer,必须在导入 src.* 前先注入 analyzer 路径
ANALYZER_PATH = os.environ.get(
    "XUEQIU_ANALYZER_PATH",
    str(PROJECT_ROOT.parent / "xueqiu-analyzer-skill" / "src"),
)
if ANALYZER_PATH not in sys.path:
    sys.path.insert(0, ANALYZER_PATH)

from src.announcement_sources import archive_day  # noqa: E402
from src.detail_fetcher import resolve_us_filing_url  # noqa: E402
from src.db import init_db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="公告日期 YYYY-MM-DD")
    parser.add_argument(
        "--db", default=str(PROJECT_ROOT / "data" / "monitor.db")
    )
    parser.add_argument(
        "--source-root",
        default=str(PROJECT_ROOT / "data" / "announcement_sources"),
    )
    args = parser.parse_args()

    init_db(args.db)
    summary = archive_day(
        args.db,
        args.source_root,
        args.date,
        us_resolver=resolve_us_filing_url,
    )
    print(json.dumps(summary.to_dict(), ensure_ascii=False))
    return 1 if summary.failed else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
