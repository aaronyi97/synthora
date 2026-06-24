"""Tests for LLMMetadataExtractor parsing and fallback logic."""

from __future__ import annotations

import json

import pytest

from agoracle.adapters.judge.metadata_extractor import LLMMetadataExtractor
from agoracle.domain.types import ConsensusType, ModelResponse, Role


def _resp(model_id: str, content: str, success: bool = True) -> ModelResponse:
    return ModelResponse(
        call_id=f"test-{model_id}",
        model_id=model_id,
        role=Role.CONTRIBUTOR,
        content=content,
        latency_ms=100,
        success=success,
    )


VALID_JSON = json.dumps({
    "confidence": 0.85,
    "has_divergence": False,
    "consensus_type": "independent_verification",
    "key_insights": ["insight1", "insight2"],
    "topic_tags": ["tag1"],
    "model_evaluations": {
        "model_a": {"accuracy": 0.9, "reasoning": 0.8, "uniqueness": 0.5},
        "model_b": {"accuracy": 0.7, "reasoning": 0.7, "uniqueness": 0.6},
    },
})


class TestExtractJson:
    """Test _extract_json with various LLM output formats."""

    def test_clean_json(self):
        result = LLMMetadataExtractor._extract_json(VALID_JSON)
        assert result is not None
        assert result["confidence"] == 0.85

    def test_json_in_markdown_block(self):
        raw = f"Here is the analysis:\n```json\n{VALID_JSON}\n```\nDone."
        result = LLMMetadataExtractor._extract_json(raw)
        assert result is not None
        assert result["confidence"] == 0.85

    def test_json_in_plain_markdown_block(self):
        raw = f"Analysis:\n```\n{VALID_JSON}\n```"
        result = LLMMetadataExtractor._extract_json(raw)
        assert result is not None
        assert result["confidence"] == 0.85

    def test_json_embedded_in_text(self):
        raw = f"I analyzed the responses. {VALID_JSON} That's my evaluation."
        result = LLMMetadataExtractor._extract_json(raw)
        assert result is not None
        assert result["confidence"] == 0.85

    def test_garbage_returns_none(self):
        result = LLMMetadataExtractor._extract_json("This is not JSON at all.")
        assert result is None

    def test_partial_json_returns_none(self):
        result = LLMMetadataExtractor._extract_json('{"confidence": 0.8, "broken')
        assert result is None


class TestBuildFromJson:
    """Test _build_from_json converts dict to MetadataExtraction correctly."""

    def test_full_data(self):
        data = json.loads(VALID_JSON)
        result = LLMMetadataExtractor._build_from_json(data)
        assert result.confidence == 0.85
        assert result.has_divergence is False
        assert result.consensus_type == ConsensusType.INDEPENDENT
        assert len(result.model_evaluations) == 2
        assert result.model_evaluations["model_a"].accuracy == 0.9

    def test_missing_fields_use_defaults(self):
        result = LLMMetadataExtractor._build_from_json({})
        assert result.confidence == 0.5
        assert result.has_divergence is False
        assert len(result.model_evaluations) == 0

    def test_bad_eval_data_skipped(self):
        data = {"model_evaluations": {"good": {"accuracy": 0.9}, "bad": "not_a_dict"}}
        result = LLMMetadataExtractor._build_from_json(data)
        assert "good" in result.model_evaluations
        assert "bad" not in result.model_evaluations

    def test_insight_agreements_from_agreed_models(self):
        """Extractor preserves agreement counts from {text, agreed_models} insight format."""
        data = {
            "confidence": 0.8,
            "key_insights": [
                {"text": "AI transforms education", "agreed_models": ["model_1", "model_2", "model_3"]},
                {"text": "Cost is a barrier", "agreed_models": ["model_1"]},
                {"text": "No agreement info"},
                "Plain string insight",
            ],
        }
        result = LLMMetadataExtractor._build_from_json(data)
        assert result.insight_agreements == {
            "AI transforms education": 3,
            "Cost is a barrier": 1,
        }
        assert len(result.key_insights) == 4

    def test_insight_agreements_empty_when_string_only(self):
        """All-string insights produce empty insight_agreements."""
        data = {"key_insights": ["insight A", "insight B"]}
        result = LLMMetadataExtractor._build_from_json(data)
        assert result.insight_agreements == {}

    def test_insight_agreements_empty_agreed_models_ignored(self):
        """Insight with empty agreed_models list is not counted."""
        data = {"key_insights": [{"text": "no models", "agreed_models": []}]}
        result = LLMMetadataExtractor._build_from_json(data)
        assert result.insight_agreements == {}
        assert len(result.key_insights) == 1


class TestPairwiseToScores:
    """Test _pairwise_to_scores converts pairwise comparisons to model scores."""

    def test_clear_winner(self):
        comparisons = [
            {"model_a": "A", "model_b": "B", "winner_accuracy": "A", "winner_reasoning": "A"},
            {"model_a": "A", "model_b": "C", "winner_accuracy": "A", "winner_reasoning": "A"},
            {"model_a": "B", "model_b": "C", "winner_accuracy": "B", "winner_reasoning": "C"},
        ]
        result = LLMMetadataExtractor._pairwise_to_scores(comparisons)
        assert result["A"].accuracy == 1.0  # won all 2 comparisons
        assert result["A"].reasoning == 1.0
        assert result["C"].accuracy == 0.0  # lost both accuracy comparisons

    def test_all_ties(self):
        comparisons = [
            {"model_a": "A", "model_b": "B", "winner_accuracy": "tie", "winner_reasoning": "tie"},
        ]
        result = LLMMetadataExtractor._pairwise_to_scores(comparisons)
        assert result["A"].accuracy == 0.5
        assert result["B"].accuracy == 0.5

    def test_empty_comparisons(self):
        result = LLMMetadataExtractor._pairwise_to_scores([])
        assert len(result) == 0

    def test_build_from_json_prefers_pairwise(self):
        data = {
            "confidence": 0.9,
            "pairwise_comparisons": [
                {"model_a": "X", "model_b": "Y", "winner_accuracy": "X", "winner_reasoning": "Y"},
            ],
            "model_evaluations": {
                "X": {"accuracy": 0.1, "reasoning": 0.1, "uniqueness": 0.1},
            },
        }
        result = LLMMetadataExtractor._build_from_json(data)
        # Pairwise should override legacy model_evaluations
        assert result.model_evaluations["X"].accuracy == 1.0  # won the only comparison
        assert result.model_evaluations["Y"].reasoning == 1.0


class TestRegexFallback:
    """Test _regex_fallback extracts fields from non-JSON text."""

    def test_extracts_confidence_and_divergence(self):
        raw = 'The confidence is confidence: 0.72 and has_divergence: true in this analysis.'
        responses = [_resp("m1", "answer")]
        result = LLMMetadataExtractor._regex_fallback(raw, responses)
        assert result is not None
        assert abs(result.confidence - 0.72) < 0.01
        assert result.has_divergence is True

    def test_extracts_key_insights(self):
        raw = '''confidence: 0.8, key_insights: ["AI is transforming education", "Cost remains high"]'''
        responses = [_resp("m1", "answer")]
        result = LLMMetadataExtractor._regex_fallback(raw, responses)
        assert result is not None
        assert len(result.key_insights) == 2

    def test_returns_none_without_confidence(self):
        raw = "Some random text without any structured data."
        responses = [_resp("m1", "answer")]
        result = LLMMetadataExtractor._regex_fallback(raw, responses)
        assert result is None

    def test_clamps_confidence(self):
        raw = "confidence: 5.0"
        responses = [_resp("m1", "answer")]
        result = LLMMetadataExtractor._regex_fallback(raw, responses)
        assert result is not None
        assert result.confidence <= 1.0


class TestStatisticalFallback:
    """Test _statistical_fallback computes reasonable confidence from response data."""

    def test_no_responses(self):
        result = LLMMetadataExtractor._statistical_fallback([])
        assert result.confidence == 0.3

    def test_all_failed(self):
        responses = [_resp("m1", "", success=False), _resp("m2", "", success=False)]
        result = LLMMetadataExtractor._statistical_fallback(responses)
        assert result.confidence == 0.3

    def test_all_successful_similar_length(self):
        responses = [
            _resp("m1", "A" * 1000),
            _resp("m2", "B" * 1100),
            _resp("m3", "C" * 950),
        ]
        result = LLMMetadataExtractor._statistical_fallback(responses)
        # High success rate + decent length + low variance → higher confidence
        assert result.confidence >= 0.6

    def test_mixed_success(self):
        responses = [
            _resp("m1", "A" * 500),
            _resp("m2", "", success=False),
            _resp("m3", "C" * 600),
        ]
        result = LLMMetadataExtractor._statistical_fallback(responses)
        # 2/3 success rate → moderate confidence
        assert 0.4 <= result.confidence <= 0.75

    def test_confidence_clamped(self):
        responses = [_resp("m1", "A" * 5000)]
        result = LLMMetadataExtractor._statistical_fallback(responses)
        assert result.confidence <= 0.85
