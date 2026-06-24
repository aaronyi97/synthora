"""
Tests for adaptive aggregation — question_type-driven synthesis strategies.
"""

import pytest

from agoracle.domain.types import QuestionType
from agoracle.services.adaptive_aggregation import (
    AggregationStrategy,
    STRATEGIES,
    get_strategy,
    apply_strategy,
)


class TestStrategySelection:
    def test_factual_strategy(self):
        s = get_strategy(QuestionType.FACTUAL)
        assert s.name == "vote"
        assert s.moa_enabled is False
        assert s.max_refinement_override == 1  # safety net: 1 critic round
        assert s.judge_prompt_key == "judge_adaptive_factual"

    def test_analytical_strategy(self):
        s = get_strategy(QuestionType.ANALYTICAL)
        assert s.name == "debate"
        assert s.moa_enabled is True
        assert s.disable_best_single is False
        assert s.best_single_gap_override == 0.25  # v4.27 S1: 0.5→0.25, pairwise 3/4 win triggers BEST_SINGLE

    def test_technical_strategy(self):
        s = get_strategy(QuestionType.TECHNICAL)
        assert s.name == "debate"
        assert s.moa_enabled is True

    def test_controversial_strategy(self):
        s = get_strategy(QuestionType.CONTROVERSIAL)
        assert s.name == "multi_perspective"
        assert s.moa_enabled is False  # MoA converges, bad for controversial
        assert s.judge_prompt_key == "judge_adaptive_controversial"

    def test_creative_strategy(self):
        s = get_strategy(QuestionType.CREATIVE)
        assert s.name == "best_single"
        assert s.moa_enabled is False
        assert s.max_refinement_override == 0
        assert s.best_single_gap_override == 0.1

    def test_unknown_strategy(self):
        s = get_strategy(QuestionType.UNKNOWN)
        assert s.name == "default"
        assert s.judge_prompt_key == ""  # use mode default

    def test_all_question_types_have_strategies(self):
        for qt in QuestionType:
            s = get_strategy(qt)
            assert isinstance(s, AggregationStrategy)


class TestApplyStrategy:
    def test_factual_overrides(self):
        s = get_strategy(QuestionType.FACTUAL)
        overrides = apply_strategy(s, None, None, "test-query")

        assert overrides["judge_prompt_key"] == "judge_adaptive_factual"
        assert overrides["moa_enabled"] is False
        assert overrides["max_refinement_rounds"] == 1  # safety net
        assert overrides["best_single_gap_threshold"] == 0.08  # v4.0: 0.15→0.08

    def test_analytical_no_refinement_override(self):
        s = get_strategy(QuestionType.ANALYTICAL)
        overrides = apply_strategy(s, None, None, "test-query")

        assert "max_refinement_rounds" not in overrides  # uses config default
        assert overrides["disable_best_single"] is False
        assert overrides["best_single_gap_threshold"] == 0.25  # v4.27 S1: 0.5→0.25

    def test_creative_low_gap(self):
        s = get_strategy(QuestionType.CREATIVE)
        overrides = apply_strategy(s, None, None, "test-query")

        assert overrides["best_single_gap_threshold"] == 0.1
        assert overrides["best_single_min_score"] == 0.5

    def test_unknown_empty_prompt(self):
        s = get_strategy(QuestionType.UNKNOWN)
        overrides = apply_strategy(s, None, None, "test-query")

        assert overrides["judge_prompt_key"] == ""  # falls back to mode default


class TestStrategyConsistency:
    def test_moa_disabled_for_convergence_sensitive_types(self):
        """MoA should be disabled for types where convergence hurts."""
        for qt in (QuestionType.FACTUAL, QuestionType.CONTROVERSIAL, QuestionType.CREATIVE):
            s = get_strategy(qt)
            assert s.moa_enabled is False, f"{qt.value} should have MoA disabled"

    def test_moa_enabled_for_depth_types(self):
        """MoA should be enabled for types where depth helps."""
        for qt in (QuestionType.ANALYTICAL, QuestionType.TECHNICAL):
            s = get_strategy(qt)
            assert s.moa_enabled is True, f"{qt.value} should have MoA enabled"

    def test_creative_no_refinement(self):
        """Creative doesn't benefit from refinement."""
        s = get_strategy(QuestionType.CREATIVE)
        assert s.max_refinement_override == 0, "creative should skip refinement"

    def test_factual_has_safety_net(self):
        """Factual has 1 critic round as safety net against all-models-wrong."""
        s = get_strategy(QuestionType.FACTUAL)
        assert s.max_refinement_override == 1, "factual should have 1 safety round"

    def test_depth_types_use_high_threshold_not_hard_disable(self):
        """ANALYTICAL/TECHNICAL/CONTROVERSIAL use thresholds instead of hard disable.
        v4.27 S1: gap lowered 0.5→0.25 to fix synthesis dilution — pairwise 3/4 win triggers BEST_SINGLE.
        Threshold still higher than FACTUAL (0.08) / CULTURAL (0.06) / CREATIVE (0.1).
        """
        for qt in (QuestionType.ANALYTICAL, QuestionType.TECHNICAL, QuestionType.CONTROVERSIAL):
            s = get_strategy(qt)
            assert s.disable_best_single is False, f"{qt.value} should not hard-disable"
            assert s.best_single_gap_override >= 0.2, f"{qt.value} should still require meaningful gap"
            assert s.best_single_gap_override < 0.5, f"{qt.value} should not use near-impossible threshold"
