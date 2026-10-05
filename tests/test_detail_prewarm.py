from __future__ import annotations

from src import cli, report_generator
from src.config import Config


def test_pipeline_config_enables_incremental_detail_prewarm():
    cfg = Config.from_file("etc/config.g1.json")

    assert cfg.detail["prewarm"] is True
    assert cfg.detail["concurrency"] == 1
    assert cfg.detail["news_per_stock"] == 5


def test_detail_prewarm_reports_cached_counts(monkeypatch, tmp_path):
    cfg = Config.default()
    cfg.detail["prewarm"] = True

    def fake_enrich(_db_path, _date_str, config):
        assert config["detail"]["prewarm"] is True
        return {}, 3, 2

    monkeypatch.setattr(report_generator, "enrich_details", fake_enrich)

    result = cli._prewarm_details(str(tmp_path / "monitor.db"), cfg)

    assert result == {"enabled": True, "news": 3, "announcements": 2}


def test_detail_prewarm_disabled_by_default(tmp_path):
    cfg = Config.default()

    result = cli._prewarm_details(str(tmp_path / "monitor.db"), cfg)

    assert result == {"enabled": False, "news": 0, "announcements": 0}
