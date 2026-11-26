"""体检项 git_integrity：索引指向未写出对象时必须在小时巡检里告警。

背景（2026-11-25）：一天之内索引四次出现指向缺失 blob 的条目，表现为
`git commit` 报 `invalid object`，但 HEAD 完好、平时毫无征兆。归因未闭合，
所以把检测接到已有的每小时体检上——比加钩子好：它带时间戳，真复现时能
直接圈定是哪一小时、谁在动 git。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_SCRIPT = PROJECT_ROOT / "scripts" / "health_check.py"
_spec = importlib.util.spec_from_file_location("health_check_mod", _SCRIPT)
hc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hc)


def test_missing_blob_is_reported():
    out = "missing blob ce013625030ba8dba906f756967f9e9ca394464a\n"
    assert hc.git_integrity_bad(out) == [
        "missing blob ce013625030ba8dba906f756967f9e9ca394464a"]


def test_broken_link_and_error_are_reported():
    out = "broken link from tree abc to blob def\nerror: object file is empty\n"
    assert len(hc.git_integrity_bad(out)) == 2


def test_dangling_is_ignored():
    """历史里的悬空对象是正常现象，拦它会把人训成"告警可以忽略"。"""
    out = ("dangling commit 17c00f195761e79f6332e55bd02a6c34284b1f2e\n"
           "dangling tree 3961e65b3c97ba5dc895b193df7b428ccd020622\n")
    assert hc.git_integrity_bad(out) == []


def test_clean_output_is_empty():
    assert hc.git_integrity_bad("") == []
    assert hc.git_integrity_bad("Checking object directories: 100% (256/256), done.") == []
