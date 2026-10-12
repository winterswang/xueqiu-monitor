"""monitor 侧的 opencli 超时必须盖住插件层的最坏等待。

背景：插件层限速器（`xueqiu-crawler/opencli-adapters/`）的等待发生在 **opencli 子进程
内部**，会被调用方 `subprocess.run(timeout=…)` 的墙钟一并计时。它的策略是
「6–12 秒间隔 + 每 30 次一次 45–75 秒长停」，所以单次最坏等待 =
取锁 `LOCK_WAIT_MS` + `max_delay_s` + `long_pause_max_s` + 一次真实执行。
调用方超时若低于它，**长停一命中就必然被误杀**（表现为那一只股票静默降级成失败）。

为什么 monitor 需要这条：crawler 侧早有一条同样的守卫（`xueqiu-crawler/tests/
test_opencli_throttle.py`，会解析插件源码），而 **monitor 侧此前只有一句注释**
（`detail_fetcher.py` 里写着「与 opencli_extractor 的常量同一数值」）—— 插件把长停调大
而 monitor 没跟，crawler 的测试会红、monitor 的不会。这个文件把那半边补上。

数值都从**源码解析**得出，不照抄：改插件或改调用方任一侧，这里都会红。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CRAWLER = Path(__file__).resolve().parent.parent.parent / "xueqiu-crawler"
PLUGIN_DIR = CRAWLER / "opencli-adapters"
MONITOR_DETAIL_FETCHER = Path(__file__).resolve().parent.parent / "src" / "detail_fetcher.py"

# ⚠️ monitor 的 CI 只 checkout 本仓 + xueqiu-analyzer-skill（见 .github/workflows/tests.yml），
# **看不到 crawler 仓的插件源码** —— 所以这条守卫在 CI 里会自动跳过，它的价值在**本地**
# （两个仓并排时）。想让 CI 也守住，需要让 monitor 的 workflow 一并 checkout crawler；
# 那会耦合两个仓的 CI，暂不做，先记在这里。
pytestmark = pytest.mark.skipif(
    not PLUGIN_DIR.is_dir(),
    reason=f"看不到插件源码 {PLUGIN_DIR}（CI 不 checkout crawler 仓）—— 本地跑才有意义",
)

# 单次真实执行的余量（列表/详情的实测耗时量级）。
SINGLE_RUN_HEADROOM_SECONDS = 20


def _plugin_source() -> str:
    files = sorted(PLUGIN_DIR.glob("*.js")) + sorted((PLUGIN_DIR / "lib").glob("*.js"))
    assert files, f"{PLUGIN_DIR} 下没有 .js —— 插件目录结构变了"
    return "\n".join(p.read_text(encoding="utf-8") for p in files)


def _plugin_number(key: str) -> float:
    src = _plugin_source()
    found = re.search(rf"\b{re.escape(key)}\s*:\s*([0-9.]+)", src)
    assert found, f"插件源码里找不到 `{key}` —— 限速策略结构变了，请同步这个测试"
    return float(found.group(1))


def _plugin_const_ms(name: str) -> float:
    src = _plugin_source()
    found = re.search(rf"const {re.escape(name)} = ([\d_]+)", src)
    assert found, f"插件源码里找不到常量 {name}"
    return float(found.group(1).replace("_", "")) / 1000.0


def plugin_worst_wait_seconds() -> float:
    """插件层单次调用可能被等待的最长时间（秒），取 xueqiu 站的策略。"""
    return (
        _plugin_const_ms("LOCK_WAIT_MS")
        + _plugin_number("max_delay_s")
        + _plugin_number("long_pause_max_s")
    )


def _monitor_article_timeout() -> float:
    """`detail_fetcher._opencli_article` 里那个 `subprocess.run(..., timeout=N)`。"""
    src = MONITOR_DETAIL_FETCHER.read_text(encoding="utf-8")
    # 定位到调用 web article 的那一段，避免抓到别的 timeout
    anchor = src.find('cmd = ["opencli", "web", "article"')
    assert anchor > 0, "detail_fetcher 里找不到 `web article` 调用点 —— 请同步这个测试"
    found = re.search(r"timeout=(\d+)", src[anchor : anchor + 1200])
    assert found, "找不到该调用点的 timeout"
    return float(found.group(1))


def test_plugin_worst_wait_is_parsed_not_guessed():
    """先确认解析出来的是一组像样的数 —— 否则下面的断言可能是「空转通过」。"""
    worst = plugin_worst_wait_seconds()
    assert 60 <= worst <= 150, f"解析出的最坏等待 {worst}s 不合常理，解析逻辑可能失效"
    assert _plugin_number("min_delay_s") >= 6, "下限低于 6 秒会破坏 peak.mjs 的判定前提"


def test_monitor_detail_timeout_covers_plugin_worst_wait():
    timeout = _monitor_article_timeout()
    worst = plugin_worst_wait_seconds()
    assert timeout > worst + SINGLE_RUN_HEADROOM_SECONDS, (
        f"detail_fetcher 的超时 {timeout}s 盖不住插件层的最坏等待 {worst}s + 执行余量 "
        f"{SINGLE_RUN_HEADROOM_SECONDS}s —— 长停命中即被误杀"
    )


def test_analyzer_wrapper_timeout_covers_plugin_worst_wait():
    """monitor 的多数 opencli 调用走 analyzer 的共享封装，那份常量也要够。

    （`xueqiu-monitor` 运行时依赖 `xueqiu-analyzer-skill`，CI 里它被装在 `_deps/` 下。）
    """
    analyzer = pytest.importorskip(
        "xueqiu_analyzer.fetcher_opencli",
        reason="当前环境没装 xueqiu-analyzer-skill，跳过（CI 里会装）",
    )
    timeout = getattr(analyzer, "OPENCLI_CALL_TIMEOUT_SECONDS", None)
    assert timeout is not None, "analyzer 的 fetcher_opencli 没有 OPENCLI_CALL_TIMEOUT_SECONDS"
    worst = plugin_worst_wait_seconds()
    assert timeout > worst + SINGLE_RUN_HEADROOM_SECONDS, (
        f"analyzer 封装的超时 {timeout}s 盖不住插件层最坏等待 {worst}s + 余量"
    )
