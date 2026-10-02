#!/usr/bin/env python3
"""把已归档公告源文件按类别上传到 IMA 知识库。"""

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

from src.announcement_sources import upload_pending  # noqa: E402
from src.db import init_db  # noqa: E402


def _load_uploader():
    analyzer_path = os.environ.get("XUEQIU_ANALYZER_PATH", "")
    candidates = [
        analyzer_path,
        str(PROJECT_ROOT.parent / "xueqiu-analyzer-skill" / "src"),
    ]
    for candidate in candidates:
        if candidate and candidate not in sys.path:
            sys.path.insert(0, candidate)
        try:
            from xueqiu_analyzer.ima_kb_uploader import upload_file
            return upload_file
        except ImportError:
            continue
    raise RuntimeError("无法导入 xueqiu_analyzer.ima_kb_uploader")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="只上传该公告日期；缺省则处理全部待上传")
    parser.add_argument(
        "--db", default=str(PROJECT_ROOT / "data" / "monitor.db")
    )
    parser.add_argument(
        "--source-root",
        default=str(PROJECT_ROOT / "data" / "announcement_sources"),
    )
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    init_db(args.db)
    summary = upload_pending(
        args.db,
        args.source_root,
        upload_file=_load_uploader(),
        date_str=args.date,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    print(json.dumps(summary.to_dict(), ensure_ascii=False))
    return 1 if summary.failed else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
