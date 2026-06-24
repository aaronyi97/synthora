"""
Integration tests for the Research mode pipeline.

Verifies that Research mode:
  - Uses wait-all (no N-of-M skipping)
  - Triggers Answer Critic + Judge Refine (always, like Deep)
  - Overrides BEST_SINGLE to always SYNTHESIZED
  - Uses per-model specialized prompts
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agoracle.config.schema import AppConfig, ModeConfig, ModelConfig
from agoracle.domain.types import (
    Intent,
    JudgeSynthesis,
    MetadataExtraction,
    Mode,
    ModelEvaluation,
    ModelResponse,
    OutputDepth,
    QueryContext,
    QualityGateResult,
    Role,
)


def _make_config() -> AppConfig:
    """Build a test config with Research mode."""
    config = AppConfig()
    for mid in (
        "model_a", "model_b", "model_c", "model_d", "model_e",
        "judge_model", "extractor_model", "critic_model",
    ):
        config.models[mid] = ModelConfig(
            id=mid, name=mid, provider="openai", model_name=mid,
            api_key_env="TEST_KEY", timeout_seconds=10,
        )
    config.modes["research"] = ModeConfig(
        name="research",
        contributors=["model_a", "model_b", "model_c", "model_d", "model_e"],
        judge="judge_model",
        extractor="extractor_model",
        question_critic="critic_model",
        answer_critic="critic_model",
        critique_always_on=True,
        n_of_m=0,              # wait for all
        max_timeout_seconds=300,
        disable_best_single=True,
        smart_routing=False,   # test mode-level behavior, not adaptive aggregation
    )
    return config


def _mock_response(model_id: str, content: str, role: Role = Role.CONTRIBUTOR) -> ModelResponse:
    return ModelResponse(
        call_id=f"test-{model_id}",
        model_id=model_id,
        role=role,
        content=content,
        latency_ms=100,
        success=True,
    )


class TestResearchModePipeline:
    """Test the Research mode pipeline end-to-end."""

    @pytest.mark.asyncio
    async def test_research_waits_for_all_contributors(self):
        """Research mode waits for all contributors (no N-of-M skipping)."""
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config()

        adapter = MagicMock()
        adapter.supports_model.return_value = True

        call_count = 0
        async def mock_call(role_call):
            nonlocal call_count
            if role_call.role == Role.QUESTION_CRITIC:
                return _mock_response(role_call.model_id, '{"has_issues": false}', Role.QUESTION_CRITIC)
            if role_call.role == Role.ANSWER_CRITIC:
                return _mock_response(role_call.model_id, "答案质量良好，无需修正。", Role.ANSWER_CRITIC)
            # v4.1: semantic verification uses METADATA_EXTRACTOR role — don't count as contributor
            if role_call.role == Role.METADATA_EXTRACTOR:
                return _mock_response(role_call.model_id, "A", Role.METADATA_EXTRACTOR)
            # v4.20: DivergenceAnalyzer uses DIVERGENCE_ANALYZER role — don't count as contributor
            if role_call.role == Role.DIVERGENCE_ANALYZER:
                return _mock_response(role_call.model_id, "{}", Role.DIVERGENCE_ANALYZER)
            call_count += 1
            return _mock_response(role_call.model_id, f"Research answer from {role_call.model_id}")
        adapter.call = mock_call

        judge = MagicMock()
        judge.synthesize = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Comprehensive research synthesis.",
            latency_ms=500,
            success=True,
        ))

        extractor = MagicMock()
        extractor.extract = AsyncMock(return_value=MetadataExtraction(
            confidence=0.9,
            has_divergence=False,
            model_evaluations={
                "model_a": ModelEvaluation(model_id="model_a", accuracy=0.8, reasoning=0.8, uniqueness=0.5),
                "model_b": ModelEvaluation(model_id="model_b", accuracy=0.8, reasoning=0.7, uniqueness=0.6),
                "model_c": ModelEvaluation(model_id="model_c", accuracy=0.7, reasoning=0.8, uniqueness=0.5),
                "model_d": ModelEvaluation(model_id="model_d", accuracy=0.9, reasoning=0.8, uniqueness=0.7),
                "model_e": ModelEvaluation(model_id="model_e", accuracy=0.8, reasoning=0.9, uniqueness=0.4),
            },
        ))

        prompt_loader = MagicMock()
        prompt_loader.render.return_value = "system prompt"
        prompt_loader.load.return_value = "system prompt"

        orchestrator = Orchestrator(
            config=config, model_adapter=adapter, judge=judge,
            extractor=extractor, prompt_loader=prompt_loader,
        )

        context = QueryContext(
            question="Comprehensive analysis question",
            mode=Mode.RESEARCH,
            resolved_mode=Mode.RESEARCH,
            critique_enabled=True,
            output_depth=OutputDepth.LEVEL_3,
        )

        result = await orchestrator.execute(context)

        # All 5 contributors should have been called
        assert call_count == 5
        assert result.contributor_count == 5

    @pytest.mark.asyncio
    async def test_research_always_synthesizes_never_best_single(self):
        """Research mode overrides BEST_SINGLE → always SYNTHESIZED."""
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config()

        adapter = MagicMock()
        adapter.supports_model.return_value = True
        async def mock_call(role_call):
            if role_call.call_id.startswith("postcheck-"):
                return _mock_response(role_call.model_id, "KEEP")  # v4.27: non-A/B → keeps synthesis
            if role_call.role == Role.QUESTION_CRITIC:
                return _mock_response(role_call.model_id, '{"has_issues": false}', Role.QUESTION_CRITIC)
            if role_call.role == Role.ANSWER_CRITIC:
                return _mock_response(role_call.model_id, "答案质量良好，无需修正。", Role.ANSWER_CRITIC)
            return _mock_response(role_call.model_id, f"Answer from {role_call.model_id}")
        adapter.call = mock_call

        judge = MagicMock()
        judge.synthesize = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Research synthesis result.",
            latency_ms=300,
            success=True,
        ))

        # Metadata with one dominant model → would normally trigger BEST_SINGLE
        extractor = MagicMock()
        extractor.extract = AsyncMock(return_value=MetadataExtraction(
            confidence=0.95,
            model_evaluations={
                "model_a": ModelEvaluation(model_id="model_a", accuracy=0.95, reasoning=0.95, uniqueness=0.8),
                "model_b": ModelEvaluation(model_id="model_b", accuracy=0.3, reasoning=0.3, uniqueness=0.1),
                "model_c": ModelEvaluation(model_id="model_c", accuracy=0.3, reasoning=0.2, uniqueness=0.1),
                "model_d": ModelEvaluation(model_id="model_d", accuracy=0.2, reasoning=0.3, uniqueness=0.1),
                "model_e": ModelEvaluation(model_id="model_e", accuracy=0.3, reasoning=0.2, uniqueness=0.1),
            },
        ))

        prompt_loader = MagicMock()
        prompt_loader.render.return_value = "prompt"
        prompt_loader.load.return_value = "prompt"

        orchestrator = Orchestrator(
            config=config, model_adapter=adapter, judge=judge,
            extractor=extractor, prompt_loader=prompt_loader,
        )

        context = QueryContext(
            question="Test", mode=Mode.RESEARCH, resolved_mode=Mode.RESEARCH,
            critique_enabled=True,
        )

        result = await orchestrator.execute(context)

        # Should NOT be BEST_SINGLE even though one model dominates
        assert result.quality_gate_result == QualityGateResult.SYNTHESIZED.value
        assert result.final_answer == "Research synthesis result."

    @pytest.mark.asyncio
    async def test_research_refines_when_extractor_signals_issues(self):
        """Research mode triggers refinement when Extractor signals low confidence/divergence."""
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config()

        adapter = MagicMock()
        adapter.supports_model.return_value = True
        async def mock_call(role_call):
            if role_call.call_id.startswith("postcheck-"):
                return _mock_response(role_call.model_id, "KEEP")  # v4.27: non-A/B → keeps synthesis
            if role_call.role == Role.QUESTION_CRITIC:
                return _mock_response(role_call.model_id, '{"has_issues": false}', Role.QUESTION_CRITIC)
            if role_call.role == Role.ANSWER_CRITIC:
                return _mock_response(
                    role_call.model_id,
                    "遗漏了国际比较视角，建议补充。",
                    Role.ANSWER_CRITIC,
                )
            return _mock_response(role_call.model_id, f"Research answer from {role_call.model_id}")
        adapter.call = mock_call

        judge = MagicMock()
        judge.synthesize = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Initial research synthesis.",
            latency_ms=500,
            success=True,
        ))
        judge.refine = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Refined research synthesis with international comparison.",
            latency_ms=400,
            success=True,
        ))

        # Low confidence + divergence → triggers should_trigger_answer_critic
        extractor = MagicMock()
        extractor.extract = AsyncMock(return_value=MetadataExtraction(
            confidence=0.6,
            has_divergence=True,
            model_evaluations={
                "model_a": ModelEvaluation(model_id="model_a", accuracy=0.7, reasoning=0.7, uniqueness=0.5),
                "model_b": ModelEvaluation(model_id="model_b", accuracy=0.65, reasoning=0.6, uniqueness=0.4),
            },
        ))

        prompt_loader = MagicMock()
        prompt_loader.render.return_value = "prompt"
        prompt_loader.load.return_value = "prompt"

        orchestrator = Orchestrator(
            config=config, model_adapter=adapter, judge=judge,
            extractor=extractor, prompt_loader=prompt_loader,
        )

        context = QueryContext(
            question="Analyze the impact of AI on education globally",
            mode=Mode.RESEARCH,
            resolved_mode=Mode.RESEARCH,
            critique_enabled=True,
        )

        result = await orchestrator.execute(context)

        # Refinement triggered by low confidence + divergence
        judge.refine.assert_called_once()
        assert "Refined" in result.final_answer or "international" in result.final_answer

    @pytest.mark.asyncio
    async def test_research_skips_refinement_when_high_confidence(self):
        """Research mode skips refinement when Extractor reports high confidence."""
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config()

        adapter = MagicMock()
        adapter.supports_model.return_value = True
        async def mock_call(role_call):
            if role_call.role == Role.QUESTION_CRITIC:
                return _mock_response(role_call.model_id, '{"has_issues": false}', Role.QUESTION_CRITIC)
            if role_call.role == Role.ANSWER_CRITIC:
                return _mock_response(role_call.model_id, "No issues.", Role.ANSWER_CRITIC)
            return _mock_response(role_call.model_id, f"Research answer from {role_call.model_id}")
        adapter.call = mock_call

        judge = MagicMock()
        judge.synthesize = AsyncMock(return_value=JudgeSynthesis(
            final_answer="High quality research synthesis.",
            latency_ms=500,
            success=True,
        ))
        judge.refine = AsyncMock()

        # High confidence + no divergence → should_trigger_answer_critic returns False
        extractor = MagicMock()
        extractor.extract = AsyncMock(return_value=MetadataExtraction(
            confidence=0.95,
            has_divergence=False,
            model_evaluations={
                "model_a": ModelEvaluation(model_id="model_a", accuracy=0.9, reasoning=0.9, uniqueness=0.5),
                "model_b": ModelEvaluation(model_id="model_b", accuracy=0.85, reasoning=0.85, uniqueness=0.6),
            },
        ))

        prompt_loader = MagicMock()
        prompt_loader.render.return_value = "prompt"
        prompt_loader.load.return_value = "prompt"

        orchestrator = Orchestrator(
            config=config, model_adapter=adapter, judge=judge,
            extractor=extractor, prompt_loader=prompt_loader,
        )

        context = QueryContext(
            question="Analyze the impact of AI on education globally",
            mode=Mode.RESEARCH,
            resolved_mode=Mode.RESEARCH,
            critique_enabled=True,
        )

        result = await orchestrator.execute(context)

        # Refinement skipped — Extractor says quality is good, no need to spend on Critic
        judge.refine.assert_not_called()
        assert result.final_answer == "High quality research synthesis."

    @pytest.mark.asyncio
    async def test_research_uses_per_model_prompts(self):
        """Research mode loads per-model specialized prompts."""
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config()

        adapter = MagicMock()
        adapter.supports_model.return_value = True

        captured_prompts = {}
        async def mock_call(role_call):
            if role_call.role == Role.CONTRIBUTOR:
                captured_prompts[role_call.model_id] = role_call.system_prompt
            if role_call.role == Role.QUESTION_CRITIC:
                return _mock_response(role_call.model_id, '{"has_issues": false}', Role.QUESTION_CRITIC)
            if role_call.role == Role.ANSWER_CRITIC:
                return _mock_response(role_call.model_id, "答案质量良好，无需修正。", Role.ANSWER_CRITIC)
            return _mock_response(role_call.model_id, f"Answer from {role_call.model_id}")
        adapter.call = mock_call

        judge = MagicMock()
        judge.synthesize = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Synthesis.", latency_ms=100, success=True,
        ))

        extractor = MagicMock()
        extractor.extract = AsyncMock(return_value=MetadataExtraction(confidence=0.9))

        # Prompt loader returns different prompts for different template names
        def mock_render(name, **kwargs):
            if name.startswith("contributor_research_"):
                model_id = name.replace("contributor_research_", "")
                return f"Specialized prompt for {model_id}"
            return f"Generic prompt: {name}"

        prompt_loader = MagicMock()
        prompt_loader.render.side_effect = mock_render
        prompt_loader.load.return_value = "judge prompt"

        orchestrator = Orchestrator(
            config=config, model_adapter=adapter, judge=judge,
            extractor=extractor, prompt_loader=prompt_loader,
        )

        context = QueryContext(
            question="Test", mode=Mode.RESEARCH, resolved_mode=Mode.RESEARCH,
            critique_enabled=True,
        )

        await orchestrator.execute(context)

        # Each contributor should have received its specialized prompt
        # (safety_rules are prepended per 原则 #25, so check containment)
        for model_id in config.modes["research"].contributors:
            assert model_id in captured_prompts
            assert f"Specialized prompt for {model_id}" in captured_prompts[model_id]
