"""
End-to-end test for realtime question routing.

Validates the complete chain:
  User input "今天比特币价格是多少" 
  → router classifies as QuestionType.REALTIME
  → dispatcher (both main route and fallback) routes to search-enabled single_model
  → NOT routed to pipeline:deep/light

This test prevents regression of the "news fallback to deep" bug (R2-P0-1).
"""

import pytest
from agoracle.domain.router import _classify_question_type
from agoracle.domain.types import QuestionType
from agoracle.services.companion_dispatcher import (
    CompanionDispatcher,
    DispatcherInput,
    SINGLE_MODEL_RECOMMENDATIONS,
)


class TestRealtimeRoutingE2E:
    """End-to-end tests for realtime question routing (news/prices/current events)."""

    @pytest.mark.parametrize("question", [
        "今天比特币价格是多少",
        "最新的AI新闻有哪些",
        "现在黄金价格多少钱一克",
        "今日头条新闻",
        "What's the latest news about Tesla stock?",
        "Current weather in Beijing",
    ])
    def test_realtime_questions_classified_correctly(self, question):
        """Router must classify news/price queries as REALTIME, not FACTUAL/UNKNOWN."""
        q_type = _classify_question_type(question)
        assert q_type == QuestionType.REALTIME, (
            f"Question '{question}' classified as {q_type.value}, expected realtime"
        )

    def test_realtime_has_search_model_recommendations(self):
        """SINGLE_MODEL_RECOMMENDATIONS must include realtime with search-enabled models."""
        assert "realtime" in SINGLE_MODEL_RECOMMENDATIONS
        candidates = SINGLE_MODEL_RECOMMENDATIONS["realtime"]
        assert len(candidates) > 0
        assert candidates[:3] == ["perplexity_sonar_pro", "perplexity_sonar", "kimi"], (
            "realtime recommendation order must be perplexity_sonar_pro, perplexity_sonar, kimi"
        )
        # At least one search-capable model (perplexity or kimi)
        search_models = {"perplexity_sonar", "perplexity_sonar_pro", "kimi"}
        assert any(m in search_models for m in candidates), (
            f"realtime recommendations {candidates} must include search-enabled models"
        )

    def test_dispatcher_fallback_routes_realtime_to_single_model(self):
        """Fallback route must send realtime queries to single_model, not pipeline."""
        # Mock adapter that supports perplexity
        class MockAdapter:
            def supports_model(self, model_id: str) -> bool:
                return model_id in {"perplexity_sonar", "perplexity_sonar_pro", "kimi"}

        dispatcher = CompanionDispatcher(
            model_adapter=MockAdapter(),
            config=None,
            failure_monitor=None,
        )

        dispatcher_input = DispatcherInput(
            question="今天比特币价格是多少",
            question_type="realtime",
        )

        # Trigger fallback route directly
        output = dispatcher._fallback_route(dispatcher_input)

        # Must be single_model strategy, not pipeline
        assert output.strategy == "single_model", (
            f"Realtime fallback routed to {output.strategy}, expected single_model"
        )
        # Must select a search-enabled model
        assert output.single_model_id in {"perplexity_sonar", "perplexity_sonar_pro", "kimi"}, (
            f"Realtime fallback selected {output.single_model_id}, expected search model"
        )

    def test_routing_prompt_includes_realtime_mapping(self):
        """Main routing prompt must include realtime in single_model mapping."""
        from types import SimpleNamespace
        from agoracle.services.companion_dispatcher import CompanionDispatcher

        dispatcher = CompanionDispatcher.__new__(CompanionDispatcher)
        dispatcher._failure_monitor = None
        dispatcher._config = SimpleNamespace(models={})
        prompt = dispatcher._build_route_system_prompt(DispatcherInput(question="latest news"))

        # Prompt must mention realtime in single_model mapping
        assert "realtime" in prompt.lower(), (
            "Routing prompt must include 'realtime' in single_model recommendations"
        )
        # Prompt must mention search models for realtime
        assert ("perplexity" in prompt.lower() or "kimi" in prompt.lower()), (
            "Routing prompt must recommend search models for realtime queries"
        )
        assert "realtime → perplexity_sonar_pro, perplexity_sonar, kimi" in prompt, (
            "Prompt realtime mapping order must match SINGLE_MODEL_RECOMMENDATIONS['realtime']"
        )
        # Prompt must have decision rule for realtime
        assert "新闻" in prompt or "价格" in prompt or "实时" in prompt, (
            "Routing prompt must include decision rule for news/price/realtime queries"
        )
