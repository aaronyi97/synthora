"""
Integration tests for the Orchestrator pipeline.

Uses mock model responses to test the full pipeline flow without real API calls.
"""

from __future__ import annotations

import asyncio
import inspect
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
    QuestionType,
    QualityGateResult,
    Role,
)
from agoracle.services.companion_dispatcher import DispatcherOutput


def _make_config() -> AppConfig:
    """Build a test config with mock models."""
    config = AppConfig()
    for mid in ("model_a", "model_b", "model_c", "judge_model", "extractor_model", "critic_model"):
        config.models[mid] = ModelConfig(
            id=mid, name=mid, provider="openai", model_name=mid,
            api_key_env="TEST_KEY", timeout_seconds=10,
        )
    config.modes["light"] = ModeConfig(
        name="light",
        contributors=["model_a", "model_b", "model_c"],
        judge="judge_model",
        extractor="extractor_model",
        n_of_m=2,
        max_timeout_seconds=10,
    )
    config.modes["deep"] = ModeConfig(
        name="deep",
        contributors=["model_a", "model_b", "model_c"],
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
        latency_ms=100,
        success=True,
    )


class TestLightModePipeline:
    """Test the Light mode pipeline end-to-end."""

    def test_execute_delegates_dispatcher_preroute_and_fast_path(self):
        from agoracle.services.orchestrator import Orchestrator

        execute_source = inspect.getsource(Orchestrator.execute)
        dispatcher_source = inspect.getsource(Orchestrator._run_dispatcher_pre_route)
        fanout_source = inspect.getsource(Orchestrator._run_fan_out_stage)
        all_failed_source = inspect.getsource(Orchestrator._build_all_failed_result)
        fast_path_source = inspect.getsource(Orchestrator._complete_light_fast_path)

        assert "_run_dispatcher_pre_route(" in execute_source
        assert "_run_fan_out_stage(" in execute_source
        assert "_build_all_failed_result(" in execute_source
        assert "_complete_light_fast_path(" in execute_source
        assert "await self._companion_dispatcher.dispatch_route(_disp_input)" not in execute_source
        assert "responses, question_critique = await self._fan_out(" not in execute_source
        assert "await self._companion_dispatcher.quality_check(" not in execute_source
        assert "当前所有模型暂时不可用" not in execute_source
        assert "await self._companion_dispatcher.dispatch_route(_disp_input)" in dispatcher_source
        assert "await self._fan_out(" in fanout_source
        assert "apply_strategy(" in fanout_source
        assert "return QueryResult(" in all_failed_source
        assert "await self._companion_dispatcher.quality_check(" in fast_path_source
        assert "QueryResult(" in fast_path_source

    @pytest.mark.asyncio
    async def test_auto_mode_dispatcher_clarify_returns_early(self):
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config()

        adapter = MagicMock()
        adapter.supports_model.return_value = True
        adapter.reset_cost_tracker = MagicMock()

        judge = MagicMock()
        judge.synthesize = AsyncMock()

        extractor = MagicMock()
        extractor.extract = AsyncMock()

        prompt_loader = MagicMock()
        prompt_loader.render.return_value = "prompt"
        prompt_loader.load.return_value = "prompt"

        orchestrator = Orchestrator(
            config=config,
            model_adapter=adapter,
            judge=judge,
            extractor=extractor,
            prompt_loader=prompt_loader,
        )
        orchestrator._companion_dispatcher.dispatch_route = AsyncMock(
            return_value=DispatcherOutput(
                strategy="clarify",
                companion_message="请先补充上下文",
            )
        )

        context = QueryContext(
            question="上下文不足的问题",
            mode=Mode.AUTO,
            resolved_mode=Mode.LIGHT,
            output_depth=OutputDepth.LEVEL_1,
        )

        result = await orchestrator.execute(context)

        assert result.final_answer == "请先补充上下文"
        assert result.quality_gate_result == QualityGateResult.LOW_CONFIDENCE.value
        adapter.reset_cost_tracker.assert_not_called()
        judge.synthesize.assert_not_called()
        extractor.extract.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_mode_dispatcher_single_model_keeps_fast_path_behavior(self):
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config()
        config.modes["light"].extractor = ""

        adapter = MagicMock()
        adapter.supports_model.return_value = True

        async def mock_call(role_call):
            return _mock_response(role_call.model_id, f"Answer from {role_call.model_id}")

        adapter.call = mock_call

        judge = MagicMock()
        judge.synthesize = AsyncMock(return_value=JudgeSynthesis(
            final_answer="unused",
            latency_ms=1,
            success=True,
        ))

        extractor = MagicMock()
        extractor.extract = AsyncMock(return_value=MetadataExtraction(confidence=0.9))

        prompt_loader = MagicMock()
        prompt_loader.render.return_value = "prompt"
        prompt_loader.load.return_value = "prompt"

        orchestrator = Orchestrator(
            config=config,
            model_adapter=adapter,
            judge=judge,
            extractor=extractor,
            prompt_loader=prompt_loader,
        )
        orchestrator._companion_dispatcher.dispatch_route = AsyncMock(
            return_value=DispatcherOutput(
                strategy="single_model",
                single_model_id="model_a",
                companion_message="走单模型直通",
                route_reason="fast_path",
            )
        )
        orchestrator._companion_dispatcher.quality_check = AsyncMock(return_value=0.83)

        context = QueryContext(
            question="Test",
            mode=Mode.AUTO,
            resolved_mode=Mode.LIGHT,
            output_depth=OutputDepth.LEVEL_1,
        )

        result = await orchestrator.execute(context)

        assert result.final_answer == "Answer from model_a"
        assert result.fast_path is True
        assert result.quality_gate_result == QualityGateResult.BEST_SINGLE.value
        assert result.confidence == 0.83
        assert result.contributor_count == 1
        judge.synthesize.assert_not_called()
        orchestrator._companion_dispatcher.quality_check.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_all_models_fail_returns_degraded_answer(self):
        """Pipeline gracefully degrades when all contributors fail (v4.8)."""
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config()

        adapter = MagicMock()
        adapter.supports_model.return_value = True
        adapter.get_cost_tracker.return_value = []

        async def mock_fail(role_call):
            return ModelResponse(
                call_id="fail", model_id=role_call.model_id, role=Role.CONTRIBUTOR,
                content="", latency_ms=0, success=False, error="timeout",
            )

        adapter.call = mock_fail

        judge = MagicMock()
        extractor = MagicMock()
        prompt_loader = MagicMock()
        prompt_loader.render.return_value = "prompt"
        prompt_loader.load.return_value = "prompt"

        orchestrator = Orchestrator(
            config=config, model_adapter=adapter, judge=judge,
            extractor=extractor, prompt_loader=prompt_loader,
        )

        context = QueryContext(
            question="Test", mode=Mode.LIGHT, resolved_mode=Mode.LIGHT,
        )

        result = await orchestrator.execute(context)

        assert result.final_answer.startswith(
            "⚠️ 当前所有模型暂时不可用，无法生成完整回答。\n\n"
            "**建议：**\n"
            "1. 稍等片刻后重试（模型服务可能正在恢复）\n"
            "2. 尝试切换模式（如 Light 模式通常更稳定）\n"
            "3. 如果问题持续，请联系管理员检查服务状态\n\n"
        )
        assert "4 个模型均返回失败" in result.final_answer
        assert "trace:" in result.final_answer
        assert context.query_id in result.final_answer
        assert result.confidence == 0.0
        assert result.quality_gate_result == QualityGateResult.LOW_CONFIDENCE.value
        assert result.contributor_count == 0
        assert result.total_model_calls == 4
        assert result.estimated_cost_usd == 0.0
        assert result.output_depth == OutputDepth.LEVEL_1.value

    def test_build_all_failed_result_keeps_exact_general_message(self):
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config()
        adapter = MagicMock()
        adapter.get_cost_tracker.return_value = []
        orchestrator = Orchestrator(
            config=config,
            model_adapter=adapter,
            judge=MagicMock(),
            extractor=MagicMock(),
            prompt_loader=MagicMock(),
        )
        context = QueryContext(question="Test", mode=Mode.LIGHT, resolved_mode=Mode.LIGHT)
        responses = [
            ModelResponse(
                call_id="fail-a", model_id="model_a", role=Role.CONTRIBUTOR,
                content="", latency_ms=0, success=False, error="timeout",
            ),
            ModelResponse(
                call_id="fail-b", model_id="model_b", role=Role.CONTRIBUTOR,
                content="", latency_ms=0, success=False, error="timeout",
            ),
            ModelResponse(
                call_id="fail-c", model_id="model_c", role=Role.CONTRIBUTOR,
                content="", latency_ms=0, success=False, error="timeout",
            ),
        ]

        result = orchestrator._build_all_failed_result(
            context=context,
            responses=responses,
            mode="light",
            start=0.0,
        )

        assert result.final_answer == (
            "⚠️ 当前所有模型暂时不可用，无法生成完整回答。\n\n"
            "**建议：**\n"
            "1. 稍等片刻后重试（模型服务可能正在恢复）\n"
            "2. 尝试切换模式（如 Light 模式通常更稳定）\n"
            "3. 如果问题持续，请联系管理员检查服务状态\n\n"
            f"（技术信息：3 个模型均返回失败，trace: {context.query_id}）"
        )
        assert result.quality_gate_result == QualityGateResult.LOW_CONFIDENCE.value
        assert result.contributor_count == 0
        assert result.total_model_calls == 3
        assert result.estimated_cost_usd == 0.0

    @pytest.mark.asyncio
    async def test_all_models_fail_quota_exhausted_keeps_quota_message(self):
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config()

        adapter = MagicMock()
        adapter.supports_model.return_value = True
        adapter.get_cost_tracker.return_value = []

        judge = MagicMock()
        extractor = MagicMock()
        prompt_loader = MagicMock()
        prompt_loader.render.return_value = "prompt"
        prompt_loader.load.return_value = "prompt"

        orchestrator = Orchestrator(
            config=config, model_adapter=adapter, judge=judge,
            extractor=extractor, prompt_loader=prompt_loader,
        )

        context = QueryContext(question="Test", mode=Mode.LIGHT, resolved_mode=Mode.LIGHT)
        responses = [
            ModelResponse(
                call_id="fail-a", model_id="model_a", role=Role.CONTRIBUTOR,
                content="", latency_ms=0, success=False, error="QUOTA_EXHAUSTED: a",
            ),
            ModelResponse(
                call_id="fail-b", model_id="model_b", role=Role.CONTRIBUTOR,
                content="", latency_ms=0, success=False, error="QUOTA_EXHAUSTED: b",
            ),
        ]

        result = orchestrator._build_all_failed_result(
            context=context,
            responses=responses,
            mode="light",
            start=0.0,
        )

        assert result.final_answer == (
            "⚠️ API 额度暂时不足，无法生成回答。请稍后重试或联系管理员充值。\n\n"
            "你也可以尝试切换到 Light 模式（消耗更少额度）。"
        )
        assert result.quality_gate_result == QualityGateResult.LOW_CONFIDENCE.value
        assert result.contributor_count == 0
        assert result.total_model_calls == 2
        assert result.estimated_cost_usd == 0.0

    @pytest.mark.asyncio
    async def test_run_fan_out_stage_keeps_classifier_and_adaptive_handoff(self):
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config()
        config.modes["deep"].smart_routing = True
        mode_config = config.modes["deep"]
        mode_config.contributors = list(mode_config.contributors)

        adapter = MagicMock()
        judge = MagicMock()
        extractor = MagicMock()
        prompt_loader = MagicMock()
        prompt_loader.render.return_value = "prompt"
        prompt_loader.load.return_value = "prompt"

        orchestrator = Orchestrator(
            config=config,
            model_adapter=adapter,
            judge=judge,
            extractor=extractor,
            prompt_loader=prompt_loader,
        )
        fan_out_responses = [
            _mock_response("model_a", "Answer from model_a"),
            _mock_response("model_b", "Answer from model_b"),
        ]
        orchestrator._fan_out = AsyncMock(return_value=(fan_out_responses, None))
        draft_answers = []

        context = QueryContext(
            question="Deep test",
            mode=Mode.DEEP,
            resolved_mode=Mode.DEEP,
            question_type=QuestionType.UNKNOWN,
        )

        with patch(
            "agoracle.services.orchestrator.classify_question_type_async",
            new=AsyncMock(return_value=QuestionType.REASONING),
        ), patch(
            "agoracle.services.orchestrator.get_strategy",
            return_value="reasoning_strategy",
        ), patch(
            "agoracle.services.orchestrator.apply_strategy",
            return_value={
                "max_refinement_rounds": 0,
                "disable_best_single": True,
                "strategy_name": "reasoning_strategy",
            },
        ):
            responses, question_critique, adaptive_overrides, successful, under_target_n = await orchestrator._run_fan_out_stage(
                context=context,
                mode="deep",
                mode_config=mode_config,
                planner_result={"sub_questions": ["sub q"]},
                rag_section="",
                search_attempted=False,
                session_context="",
                progress=None,
                draft_answers=draft_answers,
            )

        assert responses == fan_out_responses
        assert question_critique is None
        assert successful == fan_out_responses
        assert under_target_n is False
        assert context.question_type == QuestionType.REASONING
        assert adaptive_overrides["disable_best_single"] is True
        assert adaptive_overrides["max_refinement_rounds"] == 0
        assert mode_config.max_refinement_rounds == 0
        assert mode_config.answer_critic == ""
        assert draft_answers == [{
            "stage": "fan_out_best",
            "model_id": "model_a",
            "content": "Answer from model_a",
        }]

    @pytest.mark.asyncio
    async def test_light_mode_basic_flow(self):
        """Light mode: fan-out → judge → quality gate → result."""
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config()

        # Mock adapters
        adapter = MagicMock()
        adapter.supports_model.return_value = True

        call_count = 0
        async def mock_call(role_call):
            nonlocal call_count
            call_count += 1
            # v4.27: semantic check uses postcheck- call_id.
            # Return non-A/B content so verdict[:1] is "" → falls to else branch → keeps synthesis
            if role_call.call_id.startswith("postcheck-"):
                return _mock_response(role_call.model_id, "KEEP")
            return _mock_response(role_call.model_id, f"Answer from {role_call.model_id}")
        adapter.call = mock_call

        judge = MagicMock()
        judge.synthesize = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Synthesized answer combining all perspectives.",
            latency_ms=200,
            success=True,
        ))

        extractor = MagicMock()
        extractor.extract = AsyncMock(return_value=MetadataExtraction(
            key_insights=["insight1", "insight2"],
            topic_tags=["python", "testing"],
            confidence=0.85,
            has_divergence=False,
            model_evaluations={
                "model_a": ModelEvaluation(model_id="model_a", accuracy=0.8, reasoning=0.7, uniqueness=0.5),
                "model_b": ModelEvaluation(model_id="model_b", accuracy=0.7, reasoning=0.8, uniqueness=0.6),
            },
        ))

        prompt_loader = MagicMock()
        prompt_loader.render.return_value = "system prompt"
        prompt_loader.load.return_value = "system prompt"

        orchestrator = Orchestrator(
            config=config,
            model_adapter=adapter,
            judge=judge,
            extractor=extractor,
            prompt_loader=prompt_loader,
        )

        context = QueryContext(
            question="What is Python?",
            mode=Mode.LIGHT,
            resolved_mode=Mode.LIGHT,
            intent=Intent.ANSWER,
            output_depth=OutputDepth.LEVEL_1,
        )

        result = await orchestrator.execute(context)

        assert result.final_answer == "Synthesized answer combining all perspectives."
        assert result.quality_gate_result == QualityGateResult.SYNTHESIZED.value
        assert result.confidence == 0.85
        assert result.contributor_count >= 2  # N-of-M: at least 2
        assert result.latency_ms >= 0  # mock calls may complete in <1ms

    @pytest.mark.asyncio
    async def test_light_mode_best_single_gate(self):
        """Light mode: quality gate selects best single model."""
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config()

        adapter = MagicMock()
        adapter.supports_model.return_value = True
        async def mock_call(role_call):
            return _mock_response(role_call.model_id, f"Answer from {role_call.model_id}")
        adapter.call = mock_call

        judge = MagicMock()
        judge.synthesize = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Synthesized (should be overridden)",
            latency_ms=200,
            success=True,
        ))

        extractor = MagicMock()
        extractor.extract = AsyncMock(return_value=MetadataExtraction(
            confidence=0.9,
            model_evaluations={
                "model_a": ModelEvaluation(model_id="model_a", accuracy=0.95, reasoning=0.95, uniqueness=0.8),
                "model_b": ModelEvaluation(model_id="model_b", accuracy=0.3, reasoning=0.3, uniqueness=0.2),
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
            question="Test", mode=Mode.LIGHT, resolved_mode=Mode.LIGHT,
        )

        result = await orchestrator.execute(context)
        assert result.quality_gate_result == QualityGateResult.BEST_SINGLE.value

    @pytest.mark.asyncio
    async def test_all_models_fail_returns_degraded_answer(self):
        """Pipeline gracefully degrades when all contributors fail (v4.8)."""
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config()

        adapter = MagicMock()
        adapter.supports_model.return_value = True
        adapter.get_cost_tracker.return_value = []
        async def mock_fail(role_call):
            return ModelResponse(
                call_id="fail", model_id=role_call.model_id, role=Role.CONTRIBUTOR,
                content="", latency_ms=0, success=False, error="timeout",
            )
        adapter.call = mock_fail

        judge = MagicMock()
        extractor = MagicMock()
        prompt_loader = MagicMock()
        prompt_loader.render.return_value = "prompt"
        prompt_loader.load.return_value = "prompt"

        orchestrator = Orchestrator(
            config=config, model_adapter=adapter, judge=judge,
            extractor=extractor, prompt_loader=prompt_loader,
        )

        context = QueryContext(
            question="Test", mode=Mode.LIGHT, resolved_mode=Mode.LIGHT,
        )

        result = await orchestrator.execute(context)
        # v4.8: Returns degraded friendly answer, not system error
        assert "不可用" in result.final_answer or "重试" in result.final_answer or "额度" in result.final_answer
        assert result.confidence == 0.0
        assert result.quality_gate_result == QualityGateResult.LOW_CONFIDENCE.value


class TestDeepModePipeline:
    """Test Deep mode with Answer Critic + Judge refinement."""

    @pytest.mark.asyncio
    async def test_deep_mode_with_critic(self):
        """Deep mode triggers answer critic when metadata shows divergence."""
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config()

        adapter = MagicMock()
        adapter.supports_model.return_value = True
        async def mock_call(role_call):
            if role_call.role == Role.QUESTION_CRITIC:
                return _mock_response(
                    role_call.model_id,
                    '{"has_issues": false}',
                    Role.QUESTION_CRITIC,
                )
            if role_call.role == Role.ANSWER_CRITIC:
                return _mock_response(
                    role_call.model_id,
                    "答案中遗漏了性能方面的考虑。建议补充。",
                    Role.ANSWER_CRITIC,
                )
            return _mock_response(role_call.model_id, f"Deep answer from {role_call.model_id}")
        adapter.call = mock_call

        judge = MagicMock()
        judge.synthesize = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Initial deep synthesis.",
            latency_ms=500,
            success=True,
        ))
        judge.refine = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Refined deep synthesis with performance considerations.",
            latency_ms=400,
            success=True,
        ))

        extractor = MagicMock()
        extractor.extract = AsyncMock(return_value=MetadataExtraction(
            confidence=0.6,  # Below threshold → triggers answer critic
            has_divergence=True,
            model_evaluations={
                "model_a": ModelEvaluation(model_id="model_a", accuracy=0.8, reasoning=0.7, uniqueness=0.5),
                "model_b": ModelEvaluation(model_id="model_b", accuracy=0.7, reasoning=0.8, uniqueness=0.6),
                "model_c": ModelEvaluation(model_id="model_c", accuracy=0.75, reasoning=0.75, uniqueness=0.5),
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
            question="Why does the GIL matter?",
            mode=Mode.DEEP,
            resolved_mode=Mode.DEEP,
            critique_enabled=True,
        )

        result = await orchestrator.execute(context)

        # Should have triggered refinement
        assert "Refined" in result.final_answer or "performance" in result.final_answer
        judge.refine.assert_called_once()


class TestNofMStrategy:
    """Test the N-of-M racing strategy."""

    @pytest.mark.asyncio
    async def test_n_of_m_returns_when_n_ready(self):
        """N-of-M returns as soon as N successful responses arrive."""
        from agoracle.services.orchestrator import Orchestrator

        config = _make_config()

        adapter = MagicMock()
        adapter.supports_model.return_value = True

        call_times = {"model_a": 0.01, "model_b": 0.01, "model_c": 5.0}  # model_c is slow

        async def mock_call(role_call):
            delay = call_times.get(role_call.model_id, 0.01)
            await asyncio.sleep(delay)
            return _mock_response(role_call.model_id, f"Answer from {role_call.model_id}")
        adapter.call = mock_call

        judge = MagicMock()
        judge.synthesize = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Fast synthesis.", latency_ms=100, success=True,
        ))

        extractor = MagicMock()
        extractor.extract = AsyncMock(return_value=MetadataExtraction(confidence=0.8))

        prompt_loader = MagicMock()
        prompt_loader.render.return_value = "prompt"
        prompt_loader.load.return_value = "prompt"

        orchestrator = Orchestrator(
            config=config, model_adapter=adapter, judge=judge,
            extractor=extractor, prompt_loader=prompt_loader,
        )

        context = QueryContext(
            question="Quick test", mode=Mode.LIGHT, resolved_mode=Mode.LIGHT,
        )

        result = await orchestrator.execute(context)

        # Should complete quickly (not wait for model_c's 5s)
        assert result.latency_ms < 3000
        assert result.contributor_count >= 2


class TestSemanticCheckSwapLogic:
    """v4.27 P0-1: A/B swap verdict translation correctness.

    Tests the four deterministic paths of _synthesis_lost:
      swap=True,  verdict=A → synthesis_lost=True  (A=best_single won)
      swap=False, verdict=B → synthesis_lost=True  (B=best_single won)
      swap=True,  verdict=B → synthesis_lost=False (B=synthesis won)
      swap=False, verdict=A → synthesis_lost=False (A=synthesis won)
    """

    def _make_deep_config(self) -> "AppConfig":
        config = _make_config()
        return config

    @pytest.mark.asyncio
    @pytest.mark.parametrize("swap,verdict,expect_fallback", [
        (True,  "A", True),   # swap=T: A=best_single → best_single won → fallback
        (False, "B", True),   # swap=F: B=best_single → best_single won → fallback
        (True,  "B", False),  # swap=T: B=synthesis  → synthesis won  → keep
        (False, "A", False),  # swap=F: A=synthesis  → synthesis won  → keep
    ])
    async def test_swap_verdict_translation(self, swap, verdict, expect_fallback):
        """Semantic check verdict is correctly translated accounting for swap position."""
        from agoracle.services.orchestrator import Orchestrator

        config = self._make_deep_config()

        adapter = MagicMock()
        adapter.supports_model.return_value = True

        async def mock_call(role_call):
            if role_call.call_id.startswith("postcheck-"):
                return _mock_response(role_call.model_id, verdict)
            return _mock_response(role_call.model_id, f"Answer from {role_call.model_id}")
        adapter.call = mock_call

        judge = MagicMock()
        judge.synthesize = AsyncMock(return_value=JudgeSynthesis(
            final_answer="Synthesized answer.", latency_ms=100, success=True,
        ))

        extractor = MagicMock()
        extractor.extract = AsyncMock(return_value=MetadataExtraction(
            confidence=0.85,
            model_evaluations={
                "model_a": ModelEvaluation(model_id="model_a", accuracy=0.8, reasoning=0.75, uniqueness=0.5),
                "model_b": ModelEvaluation(model_id="model_b", accuracy=0.75, reasoning=0.7, uniqueness=0.5),
                "model_c": ModelEvaluation(model_id="model_c", accuracy=0.7, reasoning=0.65, uniqueness=0.5),
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
            question="Test semantic check swap logic.",
            mode=Mode.LIGHT,
            resolved_mode=Mode.LIGHT,
        )

        # swap = (random.random() < 0.5): rv<0.5 → swap=True, rv>=0.5 → swap=False
        rv = 0.1 if swap else 0.9
        with patch("agoracle.services.orchestrator.random.random", return_value=rv):
            result = await orchestrator.execute(context)

        if expect_fallback:
            assert result.quality_gate_result == QualityGateResult.BEST_SINGLE.value, (
                f"swap={swap}, verdict={verdict}: expected BEST_SINGLE (synthesis lost), "
                f"got {result.quality_gate_result}"
            )
        else:
            assert result.quality_gate_result == QualityGateResult.SYNTHESIZED.value, (
                f"swap={swap}, verdict={verdict}: expected SYNTHESIZED (synthesis kept), "
                f"got {result.quality_gate_result}"
            )
