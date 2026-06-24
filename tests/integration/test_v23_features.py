"""
Integration tests for v2.3 pipeline features:
  1. Fact-Check (post-gate verification) — SKIPPED (not yet implemented)
  2. Multi-round Answer Critic refinement — ACTIVE
  3. Auto-escalation (Light → Deep on LOW_CONFIDENCE) — ACTIVE
  4. _parse_verification_result — SKIPPED (not yet implemented)
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agoracle.config.schema import AppConfig, JudgeConfig, ModeConfig, ModelConfig
from agoracle.domain.types import (
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


# ════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════

def _make_config(
    mode_name: str = "deep",
    max_refinement_rounds: int = 1,
    auto_escalate: bool = False,
    disable_best_single: bool = False,
) -> AppConfig:
    config = AppConfig()
    for mid in (
        "model_a", "model_b", "model_c",
        "judge_model", "extractor_model", "critic_model",
        "fact_checker_model", "validator_model",
    ):
        config.models[mid] = ModelConfig(
            id=mid, name=mid, provider="openai", model_name=mid,
            api_key_env="TEST_KEY", timeout_seconds=10,
        )
    config.modes[mode_name] = ModeConfig(
        name=mode_name,
        contributors=["model_a", "model_b", "model_c"],
        judge="judge_model",
        extractor="extractor_model",
        question_critic="critic_model",
        answer_critic="critic_model",
        critique_always_on=True,
        n_of_m=0,
        max_timeout_seconds=60,
        max_refinement_rounds=max_refinement_rounds,
        auto_escalate=auto_escalate,
        disable_best_single=disable_best_single,
    )
    # Judge config with thresholds that trigger LOW_CONFIDENCE easily
    config.judge = JudgeConfig(
        quality_gate_enabled=True,
        low_confidence_avg_threshold=0.4,
        low_confidence_meta_threshold=0.3,
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


def _high_confidence_metadata():
    return MetadataExtraction(
        confidence=0.9,
        has_divergence=False,
        model_evaluations={
            "model_a": ModelEvaluation(model_id="model_a", accuracy=0.85, reasoning=0.85, uniqueness=0.5),
            "model_b": ModelEvaluation(model_id="model_b", accuracy=0.80, reasoning=0.80, uniqueness=0.6),
            "model_c": ModelEvaluation(model_id="model_c", accuracy=0.80, reasoning=0.75, uniqueness=0.5),
        },
    )


def _refinement_triggering_metadata():
    """Metadata that triggers should_trigger_answer_critic (confidence < 0.8 or divergence)."""
    return MetadataExtraction(
        confidence=0.6,
        has_divergence=True,
        model_evaluations={
            "model_a": ModelEvaluation(model_id="model_a", accuracy=0.7, reasoning=0.7, uniqueness=0.5),
            "model_b": ModelEvaluation(model_id="model_b", accuracy=0.65, reasoning=0.65, uniqueness=0.5),
            "model_c": ModelEvaluation(model_id="model_c", accuracy=0.60, reasoning=0.60, uniqueness=0.4),
        },
    )


def _low_confidence_metadata():
    return MetadataExtraction(
        confidence=0.15,
        has_divergence=True,
        model_evaluations={
            "model_a": ModelEvaluation(model_id="model_a", accuracy=0.3, reasoning=0.3, uniqueness=0.2),
            "model_b": ModelEvaluation(model_id="model_b", accuracy=0.25, reasoning=0.25, uniqueness=0.2),
            "model_c": ModelEvaluation(model_id="model_c", accuracy=0.2, reasoning=0.2, uniqueness=0.1),
        },
    )


# ════════════════════════════════════════════════════
# 1. Fact-Check tests
# ════════════════════════════════════════════════════

@pytest.mark.skip(reason="Fact-Check not yet implemented")
class TestFactCheck:

    @pytest.mark.asyncio
    async def test_fact_check_runs_only_on_synthesized_path(self):
        """Fact-check should only run when Quality Gate = SYNTHESIZED, not BEST_SINGLE."""
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config(fact_checker="fact_checker_model")

        adapter = MagicMock()
        adapter.supports_model.return_value = True

        fact_check_called = False
        async def mock_call(role_call):
            nonlocal fact_check_called
            if role_call.role == Role.FACT_CHECKER:
                fact_check_called = True
                return _mock_response(role_call.model_id, '{"passed": true, "issues": []}', Role.FACT_CHECKER)
            if role_call.role == Role.QUESTION_CRITIC:
                return _mock_response(role_call.model_id, '{"has_issues": false}', Role.QUESTION_CRITIC)
            if role_call.role == Role.ANSWER_CRITIC:
                return _mock_response(role_call.model_id, "质量良好，无需修正。", Role.ANSWER_CRITIC)
            return _mock_response(role_call.model_id, f"Answer from {role_call.model_id}")
        adapter.call = mock_call

        judge = MagicMock()
        judge.synthesize = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Synthesized answer.", latency_ms=100, success=True,
        ))

        # Metadata where one model dominates → triggers BEST_SINGLE
        extractor = MagicMock()
        extractor.extract = AsyncMock(return_value=MetadataExtraction(
            confidence=0.95,
            model_evaluations={
                "model_a": ModelEvaluation(model_id="model_a", accuracy=0.95, reasoning=0.95, uniqueness=0.9),
                "model_b": ModelEvaluation(model_id="model_b", accuracy=0.2, reasoning=0.2, uniqueness=0.1),
                "model_c": ModelEvaluation(model_id="model_c", accuracy=0.2, reasoning=0.2, uniqueness=0.1),
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
            question="Test", mode=Mode.DEEP, resolved_mode=Mode.DEEP,
            critique_enabled=True,
        )

        result = await orchestrator.execute(context)

        # BEST_SINGLE path → fact-check should NOT have been called
        assert result.quality_gate_result == QualityGateResult.BEST_SINGLE.value
        assert not fact_check_called

    @pytest.mark.asyncio
    async def test_fact_check_runs_on_synthesized_path(self):
        """Fact-check runs when Quality Gate = SYNTHESIZED."""
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config(fact_checker="fact_checker_model")

        adapter = MagicMock()
        adapter.supports_model.return_value = True

        fact_check_called = False
        async def mock_call(role_call):
            nonlocal fact_check_called
            if role_call.role == Role.FACT_CHECKER:
                fact_check_called = True
                return _mock_response(role_call.model_id, '{"passed": true, "issues": []}', Role.FACT_CHECKER)
            if role_call.role == Role.QUESTION_CRITIC:
                return _mock_response(role_call.model_id, '{"has_issues": false}', Role.QUESTION_CRITIC)
            if role_call.role == Role.ANSWER_CRITIC:
                return _mock_response(role_call.model_id, "质量良好，无需修正。", Role.ANSWER_CRITIC)
            return _mock_response(role_call.model_id, f"Answer from {role_call.model_id}")
        adapter.call = mock_call

        judge = MagicMock()
        judge.synthesize = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Synthesized answer.", latency_ms=100, success=True,
        ))

        extractor = MagicMock()
        extractor.extract = AsyncMock(return_value=_high_confidence_metadata())

        prompt_loader = MagicMock()
        prompt_loader.render.return_value = "prompt"
        prompt_loader.load.return_value = "prompt"

        orchestrator = Orchestrator(
            config=config, model_adapter=adapter, judge=judge,
            extractor=extractor, prompt_loader=prompt_loader,
        )

        context = QueryContext(
            question="Test", mode=Mode.DEEP, resolved_mode=Mode.DEEP,
            critique_enabled=True,
        )

        result = await orchestrator.execute(context)

        assert result.quality_gate_result == QualityGateResult.SYNTHESIZED.value
        assert fact_check_called

    @pytest.mark.asyncio
    async def test_fact_check_triggers_refinement_on_issues(self):
        """When fact-check finds issues, Judge refines the answer."""
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config(fact_checker="fact_checker_model")

        adapter = MagicMock()
        adapter.supports_model.return_value = True
        async def mock_call(role_call):
            if role_call.role == Role.FACT_CHECKER:
                return _mock_response(
                    role_call.model_id,
                    '{"passed": false, "issues": [{"claim": "wrong", "evidence": "...", "fix": "correct"}]}',
                    Role.FACT_CHECKER,
                )
            if role_call.role == Role.QUESTION_CRITIC:
                return _mock_response(role_call.model_id, '{"has_issues": false}', Role.QUESTION_CRITIC)
            if role_call.role == Role.ANSWER_CRITIC:
                return _mock_response(role_call.model_id, "质量良好，无需修正。", Role.ANSWER_CRITIC)
            return _mock_response(role_call.model_id, f"Answer from {role_call.model_id}")
        adapter.call = mock_call

        judge = MagicMock()
        judge.synthesize = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Initial synthesis.", latency_ms=100, success=True,
        ))
        judge.refine = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Refined after fact-check.", latency_ms=100, success=True,
        ))

        extractor = MagicMock()
        extractor.extract = AsyncMock(return_value=_high_confidence_metadata())

        prompt_loader = MagicMock()
        prompt_loader.render.return_value = "prompt"
        prompt_loader.load.return_value = "prompt"

        orchestrator = Orchestrator(
            config=config, model_adapter=adapter, judge=judge,
            extractor=extractor, prompt_loader=prompt_loader,
        )

        context = QueryContext(
            question="Test", mode=Mode.DEEP, resolved_mode=Mode.DEEP,
            critique_enabled=True,
        )

        result = await orchestrator.execute(context)

        # Judge.refine should be called: once for Answer Critic, once for Fact-Check
        assert judge.refine.call_count >= 1
        assert "fact-check" in result.final_answer.lower() or "Refined" in result.final_answer

    @pytest.mark.asyncio
    async def test_fact_check_anonymizes_model_ids(self):
        """Fact-check input should use '回答 1', '回答 2' instead of model IDs."""
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config(fact_checker="fact_checker_model")

        adapter = MagicMock()
        adapter.supports_model.return_value = True

        captured_messages = []
        async def mock_call(role_call):
            if role_call.role == Role.FACT_CHECKER:
                captured_messages.append(role_call.messages[0]["content"])
                return _mock_response(role_call.model_id, '{"passed": true, "issues": []}', Role.FACT_CHECKER)
            if role_call.role == Role.QUESTION_CRITIC:
                return _mock_response(role_call.model_id, '{"has_issues": false}', Role.QUESTION_CRITIC)
            if role_call.role == Role.ANSWER_CRITIC:
                return _mock_response(role_call.model_id, "质量良好，无需修正。", Role.ANSWER_CRITIC)
            # Use generic content without model_id to test anonymization
            return _mock_response(role_call.model_id, "This is a contributor response about quantum physics.")
        adapter.call = mock_call

        judge = MagicMock()
        judge.synthesize = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Synthesis.", latency_ms=100, success=True,
        ))

        extractor = MagicMock()
        extractor.extract = AsyncMock(return_value=_high_confidence_metadata())

        prompt_loader = MagicMock()
        prompt_loader.render.return_value = "prompt"
        prompt_loader.load.return_value = "prompt"

        orchestrator = Orchestrator(
            config=config, model_adapter=adapter, judge=judge,
            extractor=extractor, prompt_loader=prompt_loader,
        )

        context = QueryContext(
            question="Test", mode=Mode.DEEP, resolved_mode=Mode.DEEP,
            critique_enabled=True,
        )

        await orchestrator.execute(context)

        assert len(captured_messages) == 1
        msg = captured_messages[0]
        # Should contain anonymized labels
        assert "回答 1" in msg
        # Headers should NOT contain model IDs
        assert "### model_a" not in msg
        assert "### model_b" not in msg


# ════════════════════════════════════════════════════
# 2. Multi-round refinement tests
# ════════════════════════════════════════════════════

class TestMultiRoundRefinement:

    @pytest.mark.asyncio
    async def test_multi_round_runs_configured_rounds(self):
        """Answer Critic runs up to max_refinement_rounds times."""
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config(max_refinement_rounds=3)

        adapter = MagicMock()
        adapter.supports_model.return_value = True

        critic_call_count = 0
        async def mock_call(role_call):
            nonlocal critic_call_count
            if role_call.role == Role.ANSWER_CRITIC:
                critic_call_count += 1
                # Always find issues to keep refining
                return _mock_response(role_call.model_id, "发现逻辑问题，建议修正。", Role.ANSWER_CRITIC)
            if role_call.role == Role.QUESTION_CRITIC:
                return _mock_response(role_call.model_id, '{"has_issues": false}', Role.QUESTION_CRITIC)
            return _mock_response(role_call.model_id, f"Answer from {role_call.model_id}")
        adapter.call = mock_call

        judge = MagicMock()
        judge.synthesize = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Initial.", latency_ms=100, success=True,
        ))
        judge.refine = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Refined.", latency_ms=100, success=True,
        ))

        extractor = MagicMock()
        extractor.extract = AsyncMock(return_value=_refinement_triggering_metadata())

        prompt_loader = MagicMock()
        prompt_loader.render.return_value = "prompt"
        prompt_loader.load.return_value = "prompt"

        orchestrator = Orchestrator(
            config=config, model_adapter=adapter, judge=judge,
            extractor=extractor, prompt_loader=prompt_loader,
        )

        context = QueryContext(
            question="Test", mode=Mode.DEEP, resolved_mode=Mode.DEEP,
            critique_enabled=True,
        )

        await orchestrator.execute(context)

        # Should have been called exactly 3 times (max_refinement_rounds=3)
        assert critic_call_count == 3
        assert judge.refine.call_count == 3

    @pytest.mark.asyncio
    async def test_multi_round_stops_early_on_no_issues(self):
        """Refinement stops early if critic finds no issues."""
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config(max_refinement_rounds=3)

        adapter = MagicMock()
        adapter.supports_model.return_value = True

        critic_call_count = 0
        async def mock_call(role_call):
            nonlocal critic_call_count
            if role_call.role == Role.ANSWER_CRITIC:
                critic_call_count += 1
                if critic_call_count == 1:
                    return _mock_response(role_call.model_id, "发现遗漏。", Role.ANSWER_CRITIC)
                else:
                    return _mock_response(role_call.model_id, "质量良好，无需修正。", Role.ANSWER_CRITIC)
            if role_call.role == Role.QUESTION_CRITIC:
                return _mock_response(role_call.model_id, '{"has_issues": false}', Role.QUESTION_CRITIC)
            return _mock_response(role_call.model_id, f"Answer from {role_call.model_id}")
        adapter.call = mock_call

        judge = MagicMock()
        judge.synthesize = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Initial.", latency_ms=100, success=True,
        ))
        judge.refine = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Refined.", latency_ms=100, success=True,
        ))

        extractor = MagicMock()
        extractor.extract = AsyncMock(return_value=_refinement_triggering_metadata())

        prompt_loader = MagicMock()
        prompt_loader.render.return_value = "prompt"
        prompt_loader.load.return_value = "prompt"

        orchestrator = Orchestrator(
            config=config, model_adapter=adapter, judge=judge,
            extractor=extractor, prompt_loader=prompt_loader,
        )

        context = QueryContext(
            question="Test", mode=Mode.DEEP, resolved_mode=Mode.DEEP,
            critique_enabled=True,
        )

        await orchestrator.execute(context)

        # Round 1: issues found → refine. Round 2: no issues → stop.
        assert critic_call_count == 2
        assert judge.refine.call_count == 1


# ════════════════════════════════════════════════════
# 3. Auto-escalation tests
# ════════════════════════════════════════════════════

class TestAutoEscalation:

    @pytest.mark.asyncio
    async def test_light_escalates_to_deep_on_low_confidence(self):
        """Light mode auto-escalates to Deep on LOW_CONFIDENCE."""
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config(mode_name="light", auto_escalate=True)
        # Also need a Deep mode config for the escalation target
        config.modes["deep"] = ModeConfig(
            name="deep",
            contributors=["model_a", "model_b", "model_c"],
            judge="judge_model",
            extractor="extractor_model",
            answer_critic="critic_model",
            critique_always_on=True,
            n_of_m=0,
        )

        adapter = MagicMock()
        adapter.supports_model.return_value = True
        async def mock_call(role_call):
            if role_call.call_id.startswith("postcheck-"):
                return _mock_response(role_call.model_id, "KEEP")  # v4.27: non-A/B → keeps synthesis
            if role_call.role == Role.QUESTION_CRITIC:
                return _mock_response(role_call.model_id, '{"has_issues": false}', Role.QUESTION_CRITIC)
            if role_call.role == Role.ANSWER_CRITIC:
                return _mock_response(role_call.model_id, "质量良好，无需修正。", Role.ANSWER_CRITIC)
            return _mock_response(role_call.model_id, f"Answer from {role_call.model_id}")
        adapter.call = mock_call

        judge = MagicMock()
        judge.synthesize = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Deep synthesis.", latency_ms=100, success=True,
        ))

        call_count = 0
        original_extract = AsyncMock()
        async def mock_extract(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call (Light) → low confidence → triggers escalation
                return _low_confidence_metadata()
            else:
                # Second call (Deep) → high confidence
                return _high_confidence_metadata()

        extractor = MagicMock()
        extractor.extract = mock_extract

        prompt_loader = MagicMock()
        prompt_loader.render.return_value = "prompt"
        prompt_loader.load.return_value = "prompt"

        orchestrator = Orchestrator(
            config=config, model_adapter=adapter, judge=judge,
            extractor=extractor, prompt_loader=prompt_loader,
        )

        context = QueryContext(
            question="Test", mode=Mode.LIGHT, resolved_mode=Mode.LIGHT,
            critique_enabled=False,
        )

        result = await orchestrator.execute(context)

        # Should have escalated: resolved_mode should be "deep"
        assert result.resolved_mode == "deep"
        # Extractor called twice (once for Light, once for Deep)
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_deep_does_not_re_escalate(self):
        """Deep mode should NOT auto-escalate (only Light does)."""
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config(mode_name="deep", auto_escalate=True)

        adapter = MagicMock()
        adapter.supports_model.return_value = True
        async def mock_call(role_call):
            if role_call.call_id.startswith("postcheck-"):
                return _mock_response(role_call.model_id, "KEEP")  # v4.27: non-A/B → keeps synthesis
            if role_call.role == Role.QUESTION_CRITIC:
                return _mock_response(role_call.model_id, '{"has_issues": false}', Role.QUESTION_CRITIC)
            if role_call.role == Role.ANSWER_CRITIC:
                return _mock_response(role_call.model_id, "质量良好，无需修正。", Role.ANSWER_CRITIC)
            return _mock_response(role_call.model_id, f"Answer from {role_call.model_id}")
        adapter.call = mock_call

        judge = MagicMock()
        judge.synthesize = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Deep synthesis.", latency_ms=100, success=True,
        ))

        extractor = MagicMock()
        extractor.extract = AsyncMock(return_value=_low_confidence_metadata())

        prompt_loader = MagicMock()
        prompt_loader.render.return_value = "prompt"
        prompt_loader.load.return_value = "prompt"

        orchestrator = Orchestrator(
            config=config, model_adapter=adapter, judge=judge,
            extractor=extractor, prompt_loader=prompt_loader,
        )

        context = QueryContext(
            question="Test", mode=Mode.DEEP, resolved_mode=Mode.DEEP,
            critique_enabled=True,
        )

        result = await orchestrator.execute(context)

        # Should NOT escalate — mode == "deep" blocks auto-escalation
        assert result.resolved_mode == "deep"
        assert result.quality_gate_result == QualityGateResult.LOW_CONFIDENCE.value


# ════════════════════════════════════════════════════
# 4. _parse_verification_result tests
# ════════════════════════════════════════════════════

# ════════════════════════════════════════════════════
# 5. Audit fix verification tests
# ════════════════════════════════════════════════════

class TestAuditFixes:

    @pytest.mark.asyncio
    async def test_best_single_skips_refinement(self):
        """🔴-1: BEST_SINGLE path should NOT trigger refinement (saves cost)."""
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config(mode_name="deep", max_refinement_rounds=2)

        adapter = MagicMock()
        adapter.supports_model.return_value = True
        async def mock_call(role_call):
            if role_call.call_id.startswith("postcheck-"):
                return _mock_response(role_call.model_id, "KEEP")  # v4.27: non-A/B → keeps synthesis
            if role_call.role == Role.QUESTION_CRITIC:
                return _mock_response(role_call.model_id, '{"has_issues": false}', Role.QUESTION_CRITIC)
            if role_call.role == Role.ANSWER_CRITIC:
                return _mock_response(role_call.model_id, "发现问题。", Role.ANSWER_CRITIC)
            return _mock_response(role_call.model_id, f"Answer from {role_call.model_id}")
        adapter.call = mock_call

        judge = MagicMock()
        judge.synthesize = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Synthesized.", latency_ms=100, success=True,
        ))
        judge.refine = AsyncMock()

        # One model dominates → triggers BEST_SINGLE
        extractor = MagicMock()
        extractor.extract = AsyncMock(return_value=MetadataExtraction(
            confidence=0.9,
            model_evaluations={
                "model_a": ModelEvaluation(model_id="model_a", accuracy=0.95, reasoning=0.95, uniqueness=0.9),
                "model_b": ModelEvaluation(model_id="model_b", accuracy=0.3, reasoning=0.3, uniqueness=0.1),
                "model_c": ModelEvaluation(model_id="model_c", accuracy=0.3, reasoning=0.2, uniqueness=0.1),
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
            question="Test", mode=Mode.DEEP, resolved_mode=Mode.DEEP,
            critique_enabled=True,
        )

        result = await orchestrator.execute(context)

        assert result.quality_gate_result == QualityGateResult.BEST_SINGLE.value
        # Refinement should NOT have been called — cost saved
        judge.refine.assert_not_called()

    @pytest.mark.asyncio
    async def test_extractor_failure_triggers_low_confidence(self):
        """🔴-2: Extractor failure (no evals, confidence=0) → LOW_CONFIDENCE, not SYNTHESIZED."""
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config(mode_name="deep")

        adapter = MagicMock()
        adapter.supports_model.return_value = True
        async def mock_call(role_call):
            if role_call.call_id.startswith("postcheck-"):
                return _mock_response(role_call.model_id, "KEEP")  # v4.27: non-A/B → keeps synthesis
            if role_call.role == Role.QUESTION_CRITIC:
                return _mock_response(role_call.model_id, '{"has_issues": false}', Role.QUESTION_CRITIC)
            if role_call.role == Role.ANSWER_CRITIC:
                return _mock_response(role_call.model_id, "质量良好。", Role.ANSWER_CRITIC)
            return _mock_response(role_call.model_id, f"Answer from {role_call.model_id}")
        adapter.call = mock_call

        judge = MagicMock()
        judge.synthesize = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Synthesis.", latency_ms=100, success=True,
        ))
        judge.refine = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Refined.", latency_ms=100, success=True,
        ))

        # Extractor fails completely → empty metadata
        extractor = MagicMock()
        extractor.extract = AsyncMock(return_value=MetadataExtraction())

        prompt_loader = MagicMock()
        prompt_loader.render.return_value = "prompt"
        prompt_loader.load.return_value = "prompt"

        orchestrator = Orchestrator(
            config=config, model_adapter=adapter, judge=judge,
            extractor=extractor, prompt_loader=prompt_loader,
        )

        context = QueryContext(
            question="Test", mode=Mode.DEEP, resolved_mode=Mode.DEEP,
            critique_enabled=True,
        )

        result = await orchestrator.execute(context)

        assert result.quality_gate_result == QualityGateResult.LOW_CONFIDENCE.value
        # LOW_CONFIDENCE is communicated via quality_gate_result (low_confidence_actions deprecated → always [])
        assert result.low_confidence_actions == []

    @pytest.mark.asyncio
    async def test_auto_escalation_carries_tokens(self):
        """🔴-3: Auto-escalation should carry Light pipeline tokens via inherited_tokens."""
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config(mode_name="light", auto_escalate=True)
        config.modes["deep"] = ModeConfig(
            name="deep",
            contributors=["model_a", "model_b", "model_c"],
            judge="judge_model",
            extractor="extractor_model",
            answer_critic="critic_model",
            critique_always_on=True,
            n_of_m=0,
        )

        adapter = MagicMock()
        adapter.supports_model.return_value = True
        async def mock_call(role_call):
            if role_call.role == Role.QUESTION_CRITIC:
                return _mock_response(role_call.model_id, '{"has_issues": false}', Role.QUESTION_CRITIC)
            if role_call.role == Role.ANSWER_CRITIC:
                return _mock_response(role_call.model_id, "质量良好。", Role.ANSWER_CRITIC)
            resp = _mock_response(role_call.model_id, f"Answer from {role_call.model_id}")
            # Simulate token usage
            resp.prompt_tokens = 100
            resp.completion_tokens = 50
            return resp
        adapter.call = mock_call

        judge = MagicMock()
        judge.synthesize = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Deep synthesis.", latency_ms=100, success=True,
        ))

        call_count = 0
        async def mock_extract(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _low_confidence_metadata()
            return _high_confidence_metadata()

        extractor = MagicMock()
        extractor.extract = mock_extract

        prompt_loader = MagicMock()
        prompt_loader.render.return_value = "prompt"
        prompt_loader.load.return_value = "prompt"

        orchestrator = Orchestrator(
            config=config, model_adapter=adapter, judge=judge,
            extractor=extractor, prompt_loader=prompt_loader,
        )

        context = QueryContext(
            question="Test", mode=Mode.LIGHT, resolved_mode=Mode.LIGHT,
            critique_enabled=False,
        )

        result = await orchestrator.execute(context)

        assert result.resolved_mode == "deep"
        # total_tokens should include Light pipeline tokens (inherited)
        assert result.total_tokens > 0
        # At minimum: 3 contributors × 150 tokens each = 450 from each pipeline
        assert result.total_tokens >= 450

    @pytest.mark.asyncio
    async def test_synthesized_high_confidence_skips_refinement(self):
        """🟠-1: SYNTHESIZED + high confidence → skip refinement (Extractor independence)."""
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config(mode_name="deep", max_refinement_rounds=2)

        adapter = MagicMock()
        adapter.supports_model.return_value = True
        async def mock_call(role_call):
            if role_call.call_id.startswith("postcheck-"):
                return _mock_response(role_call.model_id, "KEEP")  # v4.27: non-A/B → keeps synthesis
            if role_call.role == Role.QUESTION_CRITIC:
                return _mock_response(role_call.model_id, '{"has_issues": false}', Role.QUESTION_CRITIC)
            if role_call.role == Role.ANSWER_CRITIC:
                return _mock_response(role_call.model_id, "发现问题。", Role.ANSWER_CRITIC)
            return _mock_response(role_call.model_id, f"Answer from {role_call.model_id}")
        adapter.call = mock_call

        judge = MagicMock()
        judge.synthesize = AsyncMock(return_value=JudgeSynthesis(
            final_answer="High quality synthesis.", latency_ms=100, success=True,
        ))
        judge.refine = AsyncMock()

        extractor = MagicMock()
        extractor.extract = AsyncMock(return_value=_high_confidence_metadata())

        prompt_loader = MagicMock()
        prompt_loader.render.return_value = "prompt"
        prompt_loader.load.return_value = "prompt"

        orchestrator = Orchestrator(
            config=config, model_adapter=adapter, judge=judge,
            extractor=extractor, prompt_loader=prompt_loader,
        )

        context = QueryContext(
            question="Test", mode=Mode.DEEP, resolved_mode=Mode.DEEP,
            critique_enabled=True,
        )

        result = await orchestrator.execute(context)

        assert result.quality_gate_result == QualityGateResult.SYNTHESIZED.value
        # Refinement skipped — Extractor says quality is good
        judge.refine.assert_not_called()
        assert result.final_answer == "High quality synthesis."


@pytest.mark.skip(reason="_parse_verification_result not yet implemented")
class TestParseVerificationResult:

    def test_json_passed_true(self):
        from agoracle.services.orchestrator import Orchestrator
        raw = '```json\n{"passed": true, "issues": []}\n```'
        assert Orchestrator._parse_verification_result(raw, "test") is True

    def test_json_passed_false(self):
        from agoracle.services.orchestrator import Orchestrator
        raw = '{"passed": false, "issues": [{"claim": "x", "fix": "y"}]}'
        assert Orchestrator._parse_verification_result(raw, "test") is False

    def test_keyword_pass(self):
        from agoracle.services.orchestrator import Orchestrator
        raw = "核查通过。综合答案的事实陈述与原始回答一致。"
        assert Orchestrator._parse_verification_result(raw, "test") is True

    def test_keyword_fail(self):
        from agoracle.services.orchestrator import Orchestrator
        raw = "发现以下事实错误：\n1. 问题描述：日期不对\n2. 修正建议：改为2025年"
        assert Orchestrator._parse_verification_result(raw, "test") is False

    def test_mixed_pass_and_fail_keywords_returns_fail(self):
        from agoracle.services.orchestrator import Orchestrator
        # Contains both "核查通过" and "事实错误" — fail should win
        raw = "核查通过大部分内容，但发现一处事实错误。"
        assert Orchestrator._parse_verification_result(raw, "test") is False

    def test_ambiguous_returns_false(self):
        from agoracle.services.orchestrator import Orchestrator
        raw = "I reviewed the answer carefully and found some concerns."
        assert Orchestrator._parse_verification_result(raw, "test") is False

    def test_json_without_code_fence(self):
        from agoracle.services.orchestrator import Orchestrator
        raw = '{"passed": true, "issues": []}'
        assert Orchestrator._parse_verification_result(raw, "test") is True
