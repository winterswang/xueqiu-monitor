"""LLM 模型 id 唯一来源的回归测试 (2026-10-08: MiniMax-M3 → DeepSeek-V4.1-Flash)。

借鉴价值投资日报的教训 (PROJECT_LOG D-009): 模型 id 散在多处 → 换模型时漏改,
而兜底值/环境变量会静默启用旧模型。这里钉住三件事:
  ① 只从 etc/config.report.json 的 llm.model 读;
  ② 环境变量不参与 (SENTIMENT_LLM_MODEL 已废弃);
  ③ 配置缺失时响亮报错, 不退回任何兜底模型名。
"""

import json
from pathlib import Path

import pytest

from src import llm_config, sentiment

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
REPO_CONFIG = Path(__file__).resolve().parent.parent / "etc" / "config.report.json"


def test_resolve_model_reads_repo_config():
    cfg = json.loads(REPO_CONFIG.read_text(encoding="utf-8"))
    assert llm_config.resolve_model() == cfg["llm"]["model"]


def test_resolve_model_is_wire_id_not_display_name():
    # 方舟认的是全小写带日期戳的 wire id; 填展示名 (如 DeepSeek-V4.1-Flash)
    # 会直接把 model= 调用打挂
    model = llm_config.resolve_model()
    assert model
    assert model == model.lower()
    assert " " not in model


def test_passed_config_wins():
    cfg = {"llm": {"model": "unit-test-model"}}
    assert llm_config.resolve_model(cfg) == "unit-test-model"


def test_missing_model_raises(tmp_path):
    path = tmp_path / "report.json"
    path.write_text('{"llm": {}}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="llm.model"):
        llm_config.resolve_model(config_path=str(path))


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(RuntimeError):
        llm_config.resolve_model(config_path=str(tmp_path / "nope.json"))


def test_sentiment_module_has_no_env_second_source():
    """SENTIMENT_LLM_MODEL 曾能压过仓库配置 → 已删除, 不能被悄悄加回来。"""
    text = (SRC_DIR / "sentiment.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "SENTIMENT_LLM_MODEL" not in code


class _FakeClient:
    """记录 model= 的最小 OpenAI 兼容桩。"""

    def __init__(self, sink: list):
        self._sink = sink
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self._sink.append(kwargs)
        msg = type("Msg", (), {"content": '[{"i": 0, "s": 0.8}]',
                               "reasoning_content": ""})()
        choice = type("Choice", (), {"message": msg, "finish_reason": "stop"})()
        return type("Resp", (), {"choices": [choice]})()


def test_sentiment_uses_config_model_and_ignores_env(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setenv("SENTIMENT_LLM_MODEL", "minimax-m3")
    monkeypatch.setattr(sentiment, "_get_client", lambda: _FakeClient(calls))

    scores = sentiment.analyze_sentiment_batch([
        {"type": "discussion", "title": "某股讨论", "content": "正文" * 20},
    ])

    assert scores == [0.8]
    assert calls, "未发出 LLM 调用"
    assert calls[0]["model"] == llm_config.resolve_model()


def test_report_generator_has_no_hidden_model_fallback():
    """报告侧曾经写死 .get("model", "minimax-m3") —— 兜底模型名是隐藏的第二来源。"""
    text = (SRC_DIR / "report_generator.py").read_text(encoding="utf-8")
    assert 'get("model"' not in text
