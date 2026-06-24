from __future__ import annotations

from pathlib import Path

import yaml

from agoracle.config.loader import load_config
from agoracle.config.schema import FeatureFlags, ModeConfig


ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config.yaml"
LOADER_PATH = ROOT / "src/agoracle/config/loader.py"
ORCHESTRATOR_PATH = ROOT / "src/agoracle/services/orchestrator.py"


def _raw_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}


def test_load_config_reads_refinement_timeout_seconds_from_config_yaml(monkeypatch) -> None:
    monkeypatch.setenv("ENV", "development")
    cfg = load_config("config.yaml")
    raw = _raw_config()

    expected_deep = int(raw["modes"]["deep"]["refinement_timeout_seconds"])
    expected_research = int(raw["modes"]["research"]["refinement_timeout_seconds"])

    assert cfg.modes["deep"].refinement_timeout_seconds == expected_deep
    assert cfg.modes["research"].refinement_timeout_seconds == expected_research


def test_mode_timeout_invariant_for_deep_and_research(monkeypatch) -> None:
    monkeypatch.setenv("ENV", "development")
    cfg = load_config("config.yaml")

    assert cfg.modes["deep"].max_timeout_seconds >= cfg.modes["deep"].refinement_timeout_seconds
    assert cfg.modes["research"].max_timeout_seconds >= cfg.modes["research"].refinement_timeout_seconds


def test_feature_flags_roundtable_moderator_model_exists_and_is_readable(monkeypatch) -> None:
    monkeypatch.setenv("ENV", "development")
    cfg = load_config("config.yaml")
    raw = _raw_config()

    assert "roundtable_moderator_model" in FeatureFlags.__dataclass_fields__
    assert isinstance(cfg.features.roundtable_moderator_model, str)
    assert cfg.features.roundtable_moderator_model == raw["features"].get("roundtable_moderator_model", "")


def test_feature_flags_types_roundtable_enabled_and_guidance_v1(monkeypatch) -> None:
    monkeypatch.setenv("ENV", "development")
    cfg = load_config("config.yaml")

    assert isinstance(cfg.features.roundtable_enabled, bool)
    assert isinstance(cfg.features.guidance_v1, bool)


def test_schema_loader_consumer_anchor_for_refinement_timeout_seconds() -> None:
    assert "refinement_timeout_seconds" in ModeConfig.__dataclass_fields__

    loader_src = LOADER_PATH.read_text(encoding="utf-8")
    assert 'refinement_timeout_seconds=mode_data.get("refinement_timeout_seconds", 60)' in loader_src

    orchestrator_src = ORCHESTRATOR_PATH.read_text(encoding="utf-8")
    consumer_anchor = 'getattr(mode_config, "refinement_timeout_seconds", 60)'
    assert orchestrator_src.count(consumer_anchor) >= 2
