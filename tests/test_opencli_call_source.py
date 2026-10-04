from __future__ import annotations

import importlib
import os


def test_pipeline_sets_exact_opencli_source(monkeypatch):
    monkeypatch.setenv("XUEQIU_CALL_SOURCE", "old")
    cli = importlib.import_module("src.cli")

    class StopBeforeConfig(Exception):
        pass

    monkeypatch.setattr(cli.Config, "from_file", lambda _path: (_ for _ in ()).throw(StopBeforeConfig()))
    try:
        cli.run_pipeline("etc/config.g1.json")
    except StopBeforeConfig:
        pass

    assert os.environ["XUEQIU_CALL_SOURCE"] == "xueqiu-monitor:pipeline:config.g1"
