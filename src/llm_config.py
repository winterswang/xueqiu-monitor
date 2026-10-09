#!/usr/bin/env python3
"""LLM 模型 id 的唯一解析入口。

为什么要单独一个模块
--------------------
模型 id 曾经散在多处: ``etc/config.report.json``、``report_generator`` 的两处
``.get("model", "minimax-m3")`` 兜底、``sentiment.py`` 的 ``SENTIMENT_LLM_MODEL``
默认值。换模型时改一处漏一处, 而且兜底值会静默启用一个没人维护的模型 ——
2026-10-08 从 MiniMax-M3 换到字节 coding plan 的 DeepSeek-V4.1-Flash 时,
正是靠这三处各改一次才对上。教训与做法照搬价值投资日报
(``xueqiu-crawler/scripts/llm_config.py``, PROJECT_LOG D-009)。

唯一来源
--------
``etc/config.report.json`` 的 ``llm.model``。

  - **不读环境变量**: 残留的 ``SENTIMENT_LLM_MODEL`` 会静默压过仓库配置,
    模型悄悄退回旧版且没人发现。要让模型跟着仓库走, 就不能有第二来源。
  - **找不到就抛错**: 兜底模型名本身就是隐藏的第二来源, 出问题时查不出来。
    宁可响亮地失败。

注意: 值必须是方舟接口认的 wire model id (形如 ``deepseek-v4-1-flash-260910``,
全小写带日期戳), 不是展示名 (``DeepSeek-V4.1-Flash`` 会直接把调用打挂)。
查当前可用 id::

    curl -H "Authorization: Bearer $ARK_API_KEY" $ARK_CODING_BASE_URL/models
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "etc" / "config.report.json"


@lru_cache(maxsize=None)
def _load_config(path_str: str) -> dict:
    """读配置文件; 文件缺失/损坏返回 {} (由 resolve_model 报错)。"""
    path = Path(path_str)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError) as e:
        logger.error(f"LLM 配置读取失败 {path}: {e}")
        return {}


def resolve_model(
    config: dict | None = None, config_path: str | None = None
) -> str:
    """解析 LLM 模型 id。

    config: 调用方已加载的 report config; 传入时优先用它 (同一轮内保持口径一致)。
    config_path: 未传 config 时读哪个文件, 默认 etc/config.report.json。
    """
    cfg = config if config is not None else _load_config(
        str(config_path or DEFAULT_CONFIG_PATH)
    )
    model = str(((cfg.get("llm") or {}).get("model") or "")).strip()
    if not model:
        raise RuntimeError(
            "无法解析 LLM 模型 id: "
            f"{config_path or DEFAULT_CONFIG_PATH} 里没有 llm.model"
        )
    return model
