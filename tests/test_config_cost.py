"""
Tests for config loader — cost field parsing and ModelConfig pricing.
"""

import pytest

from agoracle.config.loader import load_config
from agoracle.config.schema import ModelConfig


class TestModelConfigCostFields:
    def test_cost_fields_default_zero(self):
        mc = ModelConfig()
        assert mc.cost_per_1m_input == 0.0
        assert mc.cost_per_1m_output == 0.0

    def test_cost_fields_set(self):
        mc = ModelConfig(cost_per_1m_input=3.0, cost_per_1m_output=15.0)
        assert mc.cost_per_1m_input == 3.0
        assert mc.cost_per_1m_output == 15.0


class TestConfigLoaderCostParsing:
    @pytest.fixture(scope="class")
    def config(self):
        return load_config()

    def test_all_models_have_cost_fields(self, config):
        for model_id, mc in config.models.items():
            assert hasattr(mc, "cost_per_1m_input"), f"{model_id} missing cost_per_1m_input"
            assert hasattr(mc, "cost_per_1m_output"), f"{model_id} missing cost_per_1m_output"

    def test_opus_is_most_expensive(self, config):
        # Opus is the highest-tier model; expect input >= 4.0 and output >= 20.0.
        opus = config.models.get("claude_opus_thinking")
        assert opus is not None
        assert opus.cost_per_1m_input >= 4.0
        assert opus.cost_per_1m_output >= 20.0

    def test_flash_is_cheapest(self, config):
        flash = config.models.get("gemini_3_flash")
        assert flash is not None
        for model_id, mc in config.models.items():
            if model_id == "gemini_3_flash":
                continue
            if mc.cost_per_1m_output > 0:
                assert flash.cost_per_1m_output <= mc.cost_per_1m_output, \
                    f"Flash should be cheapest, but {model_id} output is cheaper"

    def test_deepseek_affordable(self, config):
        ds = config.models.get("deepseek_reasoner")
        assert ds is not None
        assert ds.cost_per_1m_input < 1.0  # very affordable input
        assert ds.cost_per_1m_output < 5.0

    def test_all_models_have_nonzero_pricing(self, config):
        """Every model in config.yaml should have pricing configured."""
        for model_id, mc in config.models.items():
            assert mc.cost_per_1m_input > 0 or mc.cost_per_1m_output > 0, \
                f"{model_id} has no pricing configured"
