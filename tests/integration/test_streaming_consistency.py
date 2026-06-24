"""
Streaming ↔ Batch consistency tests.

Verifies that streaming mode delegates to the Orchestrator and produces
the same quality results (including Deep refinement, question_critique
passthrough, and event emission) as batch mode.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

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
    Role,
)
from agoracle.services.streaming import (
    ContributorDone,
    JudgeToken,
    PipelineComplete,
    PipelineError,
    StageCompleted,
    StageStarted,
    execute_streaming,
)


def _make_config() -> AppConfig:
    config = AppConfig()
    for mid in ("model_a", "model_b", "judge_model", "extractor_model", "critic_model"):
        config.models[mid] = ModelConfig(
            id=mid, name=mid, provider="openai", model_name=mid,
            api_key_env="TEST_KEY", timeout_seconds=10,
        )
    config.modes["light"] = ModeConfig(
        name="light",
        contributors=["model_a", "model_b"],
        judge="judge_model",
        extractor="extractor_model",
        n_of_m=0,
        max_timeout_seconds=10,
    )
    config.modes["deep"] = ModeConfig(
        name="deep",
        contributors=["model_a", "model_b"],
        judge="judge_model",
        extractor="extractor_model",
        question_critic="critic_model",
        answer_critic="critic_model",
        critique_always_on=True,
        n_of_m=0,
        max_timeout_seconds=30,
    )
    return config


def _mock_response(model_id: str, content: str, role: Role = Role.CONTRIBUTOR) -> ModelResponse:
    return ModelResponse(
        call_id=f"test-{model_id}",
        model_id=model_id,
        role=role,
        content=content,
        latency_ms=50,
        success=True,
    )


class TestStreamingBatchConsistency:
    """Streaming and batch modes should produce equivalent results."""

    @pytest.mark.asyncio
    async def test_streaming_yields_all_stage_events(self):
        """Streaming yields StageStarted, ContributorDone, JudgeToken, PipelineComplete."""
        config = _make_config()

        adapter = MagicMock()
        adapter.supports_model.return_value = True
        async def mock_call(role_call):
            if role_call.call_id.startswith("postcheck-"):
                return _mock_response(role_call.model_id, "KEEP")  # v4.27: non-A/B → keeps synthesis
            return _mock_response(role_call.model_id, f"Answer from {role_call.model_id}")
        adapter.call = mock_call

        judge = MagicMock()
        judge.synthesize = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Synthesized.", latency_ms=100, success=True,
        ))

        # synthesize_stream yields tokens one by one
        async def mock_stream(**kwargs):
            for word in ["Syn", "the", "sized", "."]:
                yield word
        judge.synthesize_stream = mock_stream

        extractor = MagicMock()
        extractor.extract = AsyncMock(return_value=MetadataExtraction(confidence=0.8))

        prompt_loader = MagicMock()
        prompt_loader.render.return_value = "prompt"
        prompt_loader.load.return_value = "prompt"

        context = QueryContext(
            question="Test Q",
            mode=Mode.LIGHT,
            resolved_mode=Mode.LIGHT,
            intent=Intent.ANSWER,
            output_depth=OutputDepth.LEVEL_1,
        )

        events = []
        async for event in execute_streaming(
            context=context,
            config=config,
            model_adapter=adapter,
            judge=judge,
            extractor=extractor,
            prompt_loader=prompt_loader,
        ):
            events.append(event)

        # Check event sequence
        event_types = [type(e).__name__ for e in events]

        assert "StageStarted" in event_types
        assert "ContributorDone" in event_types
        assert "JudgeToken" in event_types
        assert "PipelineComplete" in event_types

        # Verify judge tokens contain the answer
        tokens = [e.token for e in events if isinstance(e, JudgeToken)]
        assert "".join(tokens) == "Synthesized."

        # Verify final result
        complete = [e for e in events if isinstance(e, PipelineComplete)]
        assert len(complete) == 1
        assert complete[0].result.question == "Test Q"

    @pytest.mark.asyncio
    async def test_streaming_deep_mode_includes_refinement(self):
        """Streaming Deep mode executes Answer Critic + refinement like batch."""
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config()

        adapter = MagicMock()
        adapter.supports_model.return_value = True
        async def mock_call(role_call):
            if role_call.role == Role.QUESTION_CRITIC:
                return _mock_response(role_call.model_id, '{"has_issues": false}', Role.QUESTION_CRITIC)
            if role_call.role == Role.ANSWER_CRITIC:
                return _mock_response(role_call.model_id, "缺少性能分析", Role.ANSWER_CRITIC)
            return _mock_response(role_call.model_id, f"Deep answer from {role_call.model_id}")
        adapter.call = mock_call

        judge = MagicMock()
        judge.synthesize = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Initial synthesis.", latency_ms=200, success=True,
        ))
        judge.refine = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Refined synthesis with perf.", latency_ms=150, success=True,
        ))

        # For streaming, synthesize_stream produces initial tokens
        async def mock_stream(**kwargs):
            for word in ["Initial", " ", "synthesis", "."]:
                yield word
        judge.synthesize_stream = mock_stream

        extractor = MagicMock()
        extractor.extract = AsyncMock(return_value=MetadataExtraction(
            confidence=0.5,  # Below threshold → triggers critic
            has_divergence=True,
            model_evaluations={
                "model_a": ModelEvaluation(model_id="model_a", accuracy=0.7, reasoning=0.6, uniqueness=0.5),
                "model_b": ModelEvaluation(model_id="model_b", accuracy=0.6, reasoning=0.7, uniqueness=0.4),
            },
        ))

        prompt_loader = MagicMock()
        prompt_loader.render.return_value = "prompt"
        prompt_loader.load.return_value = "prompt"

        context = QueryContext(
            question="Why GIL?",
            mode=Mode.DEEP,
            resolved_mode=Mode.DEEP,
            critique_enabled=True,
        )

        # ── Batch mode ──
        orchestrator = Orchestrator(
            config=config, model_adapter=adapter, judge=judge,
            extractor=extractor, prompt_loader=prompt_loader,
        )
        batch_result = await orchestrator.execute(context)

        # Reset mocks for streaming run
        judge.refine.reset_mock()

        # ── Streaming mode ──
        stream_result = None
        async for event in execute_streaming(
            context=context,
            config=config,
            model_adapter=adapter,
            judge=judge,
            extractor=extractor,
            prompt_loader=prompt_loader,
        ):
            if isinstance(event, PipelineComplete):
                stream_result = event.result

        assert stream_result is not None
        # Both modes should trigger refinement
        assert judge.refine.called, "Streaming Deep mode must also trigger refinement"
        # Both should have the refined answer
        assert "Refined" in stream_result.final_answer or "perf" in stream_result.final_answer

    @pytest.mark.asyncio
    async def test_streaming_handles_all_failures(self):
        """Streaming yields PipelineComplete with degraded answer when all contributors fail (v4.8)."""
        config = _make_config()

        adapter = MagicMock()
        adapter.supports_model.return_value = True
        async def mock_fail(role_call):
            return ModelResponse(
                call_id="fail", model_id=role_call.model_id, role=Role.CONTRIBUTOR,
                content="", latency_ms=0, success=False, error="timeout",
            )
        adapter.call = mock_fail
        adapter.get_cost_tracker.return_value = []

        judge = MagicMock()
        extractor = MagicMock()
        prompt_loader = MagicMock()
        prompt_loader.render.return_value = "prompt"
        prompt_loader.load.return_value = "prompt"

        context = QueryContext(
            question="Fail test",
            mode=Mode.LIGHT,
            resolved_mode=Mode.LIGHT,
        )

        events = []
        async for event in execute_streaming(
            context=context,
            config=config,
            model_adapter=adapter,
            judge=judge,
            extractor=extractor,
            prompt_loader=prompt_loader,
        ):
            events.append(event)

        # v4.8: Should end with PipelineComplete containing degraded answer (not PipelineError)
        complete_events = [e for e in events if isinstance(e, PipelineComplete)]
        assert len(complete_events) == 1
        assert "不可用" in complete_events[0].result.final_answer or "重试" in complete_events[0].result.final_answer
