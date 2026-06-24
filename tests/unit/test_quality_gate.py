"""Tests for the Quality Gate."""

from agoracle.domain.quality_gate import (
    evaluate_gate,
    get_best_response,
    should_trigger_answer_critic,
)
from agoracle.domain.types import (
    MetadataExtraction,
    ModelEvaluation,
    ModelResponse,
    QualityGateResult,
    Role,
)


def _make_response(model_id: str, content: str = "test") -> ModelResponse:
    return ModelResponse(
        call_id=f"call_{model_id}",
        model_id=model_id,
        role=Role.CONTRIBUTOR,
        content=content,
        latency_ms=1000,
    )


def _make_metadata(**kwargs) -> MetadataExtraction:
    return MetadataExtraction(**kwargs)


class TestQualityGate:
    """Test quality gate decisions."""

    def test_normal_synthesis_when_models_are_close(self):
        meta = _make_metadata(
            confidence=0.8,
            model_evaluations={
                "a": ModelEvaluation(model_id="a", accuracy=0.8, reasoning=0.7, uniqueness=0.3),
                "b": ModelEvaluation(model_id="b", accuracy=0.75, reasoning=0.8, uniqueness=0.4),
                "c": ModelEvaluation(model_id="c", accuracy=0.7, reasoning=0.75, uniqueness=0.5),
            },
        )
        result = evaluate_gate([], meta)
        assert result == QualityGateResult.SYNTHESIZED

    def test_best_single_when_one_dominates(self):
        meta = _make_metadata(
            confidence=0.9,
            model_evaluations={
                "a": ModelEvaluation(model_id="a", accuracy=0.95, reasoning=0.9, uniqueness=0.5),
                "b": ModelEvaluation(model_id="b", accuracy=0.4, reasoning=0.3, uniqueness=0.2),
                "c": ModelEvaluation(model_id="c", accuracy=0.3, reasoning=0.4, uniqueness=0.1),
            },
        )
        result = evaluate_gate([], meta)
        assert result == QualityGateResult.BEST_SINGLE

    def test_low_confidence_when_all_weak(self):
        meta = _make_metadata(
            confidence=0.2,
            model_evaluations={
                "a": ModelEvaluation(model_id="a", accuracy=0.3, reasoning=0.2, uniqueness=0.1),
                "b": ModelEvaluation(model_id="b", accuracy=0.2, reasoning=0.3, uniqueness=0.1),
            },
        )
        result = evaluate_gate([], meta)
        assert result == QualityGateResult.LOW_CONFIDENCE

    def test_low_confidence_on_divergence_with_low_conf(self):
        meta = _make_metadata(
            confidence=0.4,
            has_divergence=True,
            model_evaluations={
                "a": ModelEvaluation(model_id="a", accuracy=0.6, reasoning=0.5),
                "b": ModelEvaluation(model_id="b", accuracy=0.5, reasoning=0.6),
            },
        )
        result = evaluate_gate([], meta)
        assert result == QualityGateResult.LOW_CONFIDENCE

    def test_no_evals_low_confidence_returns_low_confidence(self):
        meta = _make_metadata()  # default confidence=0.0
        result = evaluate_gate([], meta)
        assert result == QualityGateResult.LOW_CONFIDENCE

    def test_no_evals_high_confidence_returns_low_confidence(self):
        # v3.6: no evals = no quality signal = always LOW_CONFIDENCE
        # Blind synthesis without Extractor data risks diluting a good answer.
        meta = _make_metadata(confidence=0.8)
        result = evaluate_gate([], meta)
        assert result == QualityGateResult.LOW_CONFIDENCE


class TestPairwiseGate:
    """Test pairwise-mode quality gate paths (Path 1a and 1c)."""

    def test_path1a_unanimous_winner_triggers_best_single(self):
        # Path 1a: max_score == 1.0 (won all 4 comparisons in 5-model setup)
        meta = _make_metadata(
            confidence=0.8,
            pairwise_evaluated=True,
            model_evaluations={
                "a": ModelEvaluation(model_id="a", accuracy=1.0, reasoning=1.0, uniqueness=1.0),
                "b": ModelEvaluation(model_id="b", accuracy=0.5, reasoning=0.5, uniqueness=0.5),
                "c": ModelEvaluation(model_id="c", accuracy=0.25, reasoning=0.25, uniqueness=0.25),
                "d": ModelEvaluation(model_id="d", accuracy=0.0, reasoning=0.0, uniqueness=0.0),
            },
        )
        from agoracle.domain.quality_gate import QualityGateThresholds
        result = evaluate_gate([], meta, QualityGateThresholds(best_single_gap_threshold=0.06))
        assert result == QualityGateResult.BEST_SINGLE

    def test_path1c_strong_majority_with_low_threshold_triggers_best_single(self):
        # Path 1c: max_score=0.75 (won 3/4), gap=0.25 ≥ threshold=0.06 → BEST_SINGLE
        # Simulates cultural/factual question type with gap_override=0.06
        meta = _make_metadata(
            confidence=0.8,
            pairwise_evaluated=True,
            model_evaluations={
                "a": ModelEvaluation(model_id="a", accuracy=0.75, reasoning=0.75, uniqueness=0.75),
                "b": ModelEvaluation(model_id="b", accuracy=0.5, reasoning=0.5, uniqueness=0.5),
                "c": ModelEvaluation(model_id="c", accuracy=0.25, reasoning=0.25, uniqueness=0.25),
                "d": ModelEvaluation(model_id="d", accuracy=0.0, reasoning=0.0, uniqueness=0.0),
            },
        )
        from agoracle.domain.quality_gate import QualityGateThresholds
        result = evaluate_gate([], meta, QualityGateThresholds(best_single_gap_threshold=0.06))
        assert result == QualityGateResult.BEST_SINGLE

    def test_path1c_strong_majority_with_high_threshold_falls_through(self):
        # Path 1c: max_score=0.75, gap=0.25, but threshold=0.5 (analytical) → NOT triggered
        meta = _make_metadata(
            confidence=0.8,
            pairwise_evaluated=True,
            model_evaluations={
                "a": ModelEvaluation(model_id="a", accuracy=0.75, reasoning=0.75, uniqueness=0.75),
                "b": ModelEvaluation(model_id="b", accuracy=0.5, reasoning=0.5, uniqueness=0.5),
                "c": ModelEvaluation(model_id="c", accuracy=0.25, reasoning=0.25, uniqueness=0.25),
            },
        )
        from agoracle.domain.quality_gate import QualityGateThresholds
        result = evaluate_gate([], meta, QualityGateThresholds(best_single_gap_threshold=0.5))
        assert result == QualityGateResult.SYNTHESIZED

    def test_path1c_below_075_does_not_trigger(self):
        # Path 1c: max_score=0.50 (won 2/4) — not strong enough, even with low threshold
        # Use 5 models so avg stays above LOW_CONFIDENCE threshold (0.4)
        meta = _make_metadata(
            confidence=0.8,
            pairwise_evaluated=True,
            model_evaluations={
                "a": ModelEvaluation(model_id="a", accuracy=0.5, reasoning=0.5, uniqueness=0.5),
                "b": ModelEvaluation(model_id="b", accuracy=0.5, reasoning=0.5, uniqueness=0.5),
                "c": ModelEvaluation(model_id="c", accuracy=0.5, reasoning=0.5, uniqueness=0.5),
                "d": ModelEvaluation(model_id="d", accuracy=0.25, reasoning=0.25, uniqueness=0.25),
                "e": ModelEvaluation(model_id="e", accuracy=0.25, reasoning=0.25, uniqueness=0.25),
            },
        )
        from agoracle.domain.quality_gate import QualityGateThresholds
        result = evaluate_gate([], meta, QualityGateThresholds(best_single_gap_threshold=0.06))
        assert result == QualityGateResult.SYNTHESIZED


class TestAnswerCriticTrigger:
    """Test conditional answer critic triggering."""

    def test_triggers_when_low_confidence(self):
        meta = _make_metadata(confidence=0.6)
        assert should_trigger_answer_critic(meta) is True

    def test_triggers_when_divergence(self):
        meta = _make_metadata(confidence=0.9, has_divergence=True)
        assert should_trigger_answer_critic(meta) is True

    def test_skips_when_high_confidence_no_divergence(self):
        meta = _make_metadata(confidence=0.9, has_divergence=False)
        assert should_trigger_answer_critic(meta) is False


class TestGetBestResponse:
    """Test best single response selection."""

    def test_selects_highest_scored_model(self):
        responses = [
            _make_response("a", "answer a"),
            _make_response("b", "answer b is much better and longer"),
        ]
        meta = _make_metadata(
            model_evaluations={
                "a": ModelEvaluation(model_id="a", accuracy=0.5, reasoning=0.5),
                "b": ModelEvaluation(model_id="b", accuracy=0.9, reasoning=0.9),
            }
        )
        best = get_best_response(responses, meta)
        assert best is not None
        assert best.model_id == "b"

    def test_fallback_to_first_successful_when_no_evals(self):
        """When no evaluations exist, return first successful response (参考A: 不用长度等代理指标)."""
        responses = [
            _make_response("a", "short"),
            _make_response("b", "this is a much longer response"),
        ]
        meta = _make_metadata()
        best = get_best_response(responses, meta)
        assert best is not None
        assert best.model_id == "a"
