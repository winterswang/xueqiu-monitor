"""插件源码定位：守卫测试必须读**真正在跑的那份**。

背景（2026-10-12 第二阶段迁移）：插件已独立成仓 `opencli-adapters`，各消费仓不再保留副本。
此前 monitor 的守卫把 ``PLUGIN_DIR`` 指向 `xueqiu-crawler/opencli-adapters/`，而真正在跑的是
独立仓 —— 于是有一类**假绿灯**：改副本「没效果」，测试却读的正是那份副本。

解析顺序（找到即止）：

1. ``OPENCLI_ADAPTERS_DIR`` —— 显式指定，CI 与临时验证用。
2. ``_deps/opencli-adapters`` —— CI 里 checkout 的独立仓。
3. ``~/.opencli/plugins/opencli-adapters`` —— 本机**已安装（正在跑）**的那份。
   语义最强：它就是 opencli 实际加载的源码（指向独立仓的符号链接）。

三条守卫（少任何一条都会退回假绿灯）：

* 解析不到 → **抛错**，不是 skip、不是返回 None。路径断了就该红。
* 解析结果不得落在本仓 `opencli-adapters/` 之下 —— 防副本被重新塞回来。
* 解析结果的 ``opencli-plugin.json`` 里 ``name`` 必须是 ``opencli-adapters`` ——
  防拿到更名前的旧副本（旧 ID 是 ``xueqiu-adapters``）。

本文件与 ``xueqiu-crawler/tests/plugin_source.py`` 是同一份逻辑（两个仓无法共享代码，
只能各留一份）；改动请两边同步。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

PLUGIN_ID = "opencli-adapters"
MANIFEST = "opencli-plugin.json"
ENV_VAR = "OPENCLI_ADAPTERS_DIR"

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IN_REPO_COPY = _REPO_ROOT / PLUGIN_ID


def _candidates() -> list[tuple[str, Path]]:
    env = os.environ.get(ENV_VAR, "").strip()
    out: list[tuple[str, Path]] = []
    if env:
        out.append((ENV_VAR, Path(env).expanduser()))
    out.append(("_deps（CI checkout）", _REPO_ROOT / "_deps" / PLUGIN_ID))
    out.append(
        (
            "已安装的插件（本机在跑的那份）",
            Path.home() / ".opencli" / "plugins" / PLUGIN_ID,
        )
    )
    return out


def resolve_plugin_dir() -> Path:
    """返回插件源码目录。解析不到、或守卫不过，一律抛 AssertionError。"""
    tried: list[str] = []
    for source, path in _candidates():
        tried.append(f"  - {source}：{path}")
        if not path.is_dir() or not (path / MANIFEST).is_file():
            continue

        resolved = path.resolve()
        assert resolved != _IN_REPO_COPY and _IN_REPO_COPY not in resolved.parents, (
            f"守卫被指回了本仓副本 {_IN_REPO_COPY}。插件源码只应存在于独立仓 "
            f"{PLUGIN_ID}；本仓不应有副本。"
        )

        name = json.loads((resolved / MANIFEST).read_text(encoding="utf-8")).get("name")
        assert name == PLUGIN_ID, (
            f"{resolved} 的 {MANIFEST} 里 name={name!r}，期望 {PLUGIN_ID!r}"
            " —— 拿到的是更名前的旧副本？"
        )
        return resolved

    raise AssertionError(
        "找不到插件源码。已尝试：\n"
        + "\n".join(tried)
        + f"\n设 {ENV_VAR} 指向独立仓，或在本机安装插件后重跑。"
        "这条守卫**故意失败而不是跳过**：路径断了必须红，否则又变成假绿灯。"
    )
