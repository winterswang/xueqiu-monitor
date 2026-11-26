"""静态检查：活代码里不许有 undefined name。

## 为什么需要这条

2026-11-25 一天里出现两处 **运行时才炸** 的未定义变量：

1. `scripts/assistant_daily.py`：`published_today_count(raw, ...)` 引用了
   `raw`，而 `raw` 只在 `work()` 内部定义，`build()` 的作用域里没有。
   单元测试全绿（build 没有测试覆盖），但 19:00 那轮日报会直接 NameError。
2. `src/cli.py`：`logger` 只在一个函数内定义，`_prewarm_details` 的降级分支
   却用了它 —— 一旦 `enrich_details` 抛异常，那句"预热失败不影响流程"的
   兜底就变成 NameError，把可降级变成崩溃。

这两类都不是逻辑错误，是**作用域错误**：读代码看不出来，跑测试也看不出来。
pyflakes 在静态阶段就能发现，而且几乎零成本。

## 为什么用 subprocess 而不是 import

pyflakes 不是运行时依赖；缺了就跳过，不让测试环境因为一个 lint 工具变红。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 参与运行的模块。测试文件本身、一次性脚本不在此列。
LIVE_TARGETS = [
    "src/",
    "scripts/assistant_daily.py",
    "scripts/health_check.py",
    "scripts/export_csv.py",
    "scripts/x_collect.py",
    "scripts/x_ingest.py",
    "scripts/validate_x_posts.py",
]


def _run_pyflakes(*targets: str) -> list[str]:
    exe = shutil.which("pyflakes")
    cmd = [exe, *targets] if exe else [sys.executable, "-m", "pyflakes", *targets]
    proc = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True)
    if "No module named pyflakes" in (proc.stderr or ""):
        pytest.skip("pyflakes 未安装")
    return [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]


def test_no_undefined_names_in_live_code():
    """undefined name = 运行到这里就 NameError。"""
    lines = _run_pyflakes(*LIVE_TARGETS)
    bad = [ln for ln in lines if "undefined name" in ln]
    assert not bad, "存在未定义名（会在运行时 NameError）：\n  " + "\n  ".join(bad)


def test_pyflakes_actually_detects_the_known_shape():
    """反向验证：这条检查确实抓得住——用 HEAD 之前那种写法造一个临时文件。"""
    import tempfile

    src = (
        "def work(code):\n"
        "    raw = [1, 2]\n"
        "    return len(raw)\n"
        "\n"
        "def build():\n"
        "    # 这里的 raw 在 build 作用域里并不存在\n"
        "    return len(raw)\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "bad_scope.py"
        p.write_text(src, encoding="utf-8")
        lines = _run_pyflakes(str(p))
    assert any("undefined name" in ln and "'raw'" in ln for ln in lines), \
        f"pyflakes 未能识别作用域外引用：{lines}"


def test_prewarm_degrades_instead_of_crashing(monkeypatch):
    """cli 的详情预热失败时应当降级返回，而不是 NameError 崩掉整条 pipeline。"""
    sys.path.insert(0, str(PROJECT_ROOT))
    import src.cli as cli
    import src.report_generator as rg

    class _Cfg:
        detail = {"prewarm": True}

    def _boom(*a, **k):
        raise RuntimeError("模拟 enrich_details 失败")

    monkeypatch.setattr(rg, "enrich_details", _boom)
    out = cli._prewarm_details("dummy.db", _Cfg())
    assert "模拟 enrich_details 失败" in out["error"]
    assert out["news"] == 0 and out["announcements"] == 0
