"""
Contract tests for v5.2 Canonical Guidance Protocol.

Validates:
  1. GuidanceOutput is the single source of truth
  2. Legacy fields (next_steps, companion_guide) derive correctly from guidance
  3. Dispatcher guidance derives legacy companion_guide while next_steps stays retired
  4. guidance_compat serialization round-trips correctly
  5. _build_guidance priority: Dispatcher > empty
  6. protocol_version present in guidance_compat module
"""
import pytest
from dataclasses import dataclass, field

from agoracle.domain.types import (
    GuidanceOutput,
    GuidanceActionType,
    GuidanceIntensity,
    GuidanceSuggestion,
    QueryResult,
)
from agoracle.services.guidance_compat import (
    CAPABILITIES,
    PROTOCOL_VERSION,
    derive_companion_guide,
    derive_legacy_fields,
    derive_next_steps,
    guidance_to_dict,
)


# ── Fixtures ──────────────────────────────────────────────

def _make_guidance_suggestion(label="深入研究", action_type=GuidanceActionType.QUERY_RESEARCH.value):
    return GuidanceSuggestion(
        label=label,
        action_type=action_type,
        action_payload={"query": "test"},
        rationale="test rationale",
        estimated_seconds=60,
        estimated_cost_usd=0.05,
        requires_confirm=False,
    )


def _make_guidance_dispatcher():
    return GuidanceOutput(
        source="dispatcher",
        confidence_statement="",
        confidence_level="medium",
        message="建议你深入对比这两个方案的长期成本。",
        suggestions=[_make_guidance_suggestion("对比成本", GuidanceActionType.QUERY_RESEARCH.value)],
        intensity=GuidanceIntensity.RICH.value,
        is_folded=False,
        show_dismiss=True,
        route_reason="因为：分析题型 + 高分歧 → 建议深入研究",
        trigger="divergence",
    )

def _make_guidance_none():
    return GuidanceOutput(
        source="none",
        confidence_statement="综合评估：高信心",
        confidence_level="high",
    )


# ── Test: derive_next_steps ──────────────────────────────

class TestDeriveNextSteps:
    def test_dispatcher_source_returns_none(self):
        g = _make_guidance_dispatcher()
        assert derive_next_steps(g) is None

    def test_none_source_returns_none(self):
        g = _make_guidance_none()
        assert derive_next_steps(g) is None


# ── Test: derive_companion_guide ─────────────────────────

class TestDeriveCompanionGuide:
    def test_dispatcher_source_returns_dict(self):
        g = _make_guidance_dispatcher()
        cg = derive_companion_guide(g)
        assert cg is not None
        assert cg["message"] == g.message
        assert cg["trigger"] == "divergence"
        assert len(cg["actions"]) == 1
        assert cg["route_reason"] == g.route_reason

    def test_none_source_returns_none(self):
        g = _make_guidance_none()
        assert derive_companion_guide(g) is None


# ── Test: derive_legacy_fields compatibility ─────────────

class TestDeriveLegacyFields:
    def test_dispatcher_source_derives_companion_only(self):
        g = _make_guidance_dispatcher()
        nsg, cg = derive_legacy_fields(g)
        assert nsg is None, "next_steps must stay retired"
        assert cg is not None, "dispatcher guidance → companion_guide must be dict"

    def test_none_source_both_none(self):
        g = _make_guidance_none()
        nsg, cg = derive_legacy_fields(g)
        assert nsg is None
        assert cg is None


# ── Test: guidance_to_dict serialization ─────────────────

class TestGuidanceToDict:
    def test_dispatcher_serialization(self):
        g = _make_guidance_dispatcher()
        d = guidance_to_dict(g)
        assert d["source"] == "dispatcher"
        assert d["message"] == g.message
        assert len(d["suggestions"]) == 1
        assert d["suggestions"][0]["label"] == "对比成本"
        assert d["trigger"] == "divergence"
        assert d["is_folded"] is False

    def test_none_serialization(self):
        g = _make_guidance_none()
        d = guidance_to_dict(g)
        assert d["source"] == "none"
        assert d["suggestions"] == []
        assert d["message"] == ""

    def test_all_fields_present(self):
        g = _make_guidance_dispatcher()
        d = guidance_to_dict(g)
        required_keys = {
            "source", "confidence_statement", "confidence_level",
            "message", "suggestions", "intensity", "is_folded",
            "show_dismiss", "route_reason", "trigger",
        }
        assert required_keys.issubset(d.keys())


# ── Test: _build_guidance priority ───────────────────────

class TestBuildGuidancePriority:
    """Test Orchestrator._build_guidance static method priority logic."""

    def test_dispatcher_wins_when_present(self):
        from agoracle.services.orchestrator import Orchestrator

        # Simulate DispatcherOutput with content
        @dataclass
        class FakeDispOut:
            companion_message: str = "test dispatcher message"
            suggested_actions: list = field(default_factory=lambda: [
                {"label": "深入", "action_type": "query_research", "action_payload": {}}
            ])
            route_reason: str = "test reason"
            post_guide_trigger: str = "divergence"
            is_silent_route: bool = False

        result = QueryResult(confidence=0.6)
        from agoracle.domain.types import QueryContext
        context = QueryContext()

        guidance = Orchestrator._build_guidance(
            FakeDispOut(), None, result, context,
        )
        assert guidance.source == "dispatcher"
        assert guidance.message == "test dispatcher message"

    def test_empty_when_no_dispatcher(self):
        from agoracle.services.orchestrator import Orchestrator
        from agoracle.domain.types import QueryContext

        result = QueryResult(confidence=0.3)
        context = QueryContext()

        guidance = Orchestrator._build_guidance(
            None, None, result, context,
        )
        assert guidance.source == "none"
        assert guidance.suggestions == []

    def test_empty_dispatcher_returns_none(self):
        """Dispatcher returned but with no message and no actions → empty guidance."""
        from agoracle.services.orchestrator import Orchestrator
        from agoracle.domain.types import QueryContext

        @dataclass
        class EmptyDispOut:
            companion_message: str = ""
            suggested_actions: list = field(default_factory=list)
            route_reason: str = ""
            post_guide_trigger: str = "fold"
            is_silent_route: bool = True

        result = QueryResult()
        context = QueryContext()

        guidance = Orchestrator._build_guidance(
            EmptyDispOut(), None, result, context,
        )
        assert guidance.source == "none"


# ── Test: protocol_version ───────────────────────────────

class TestProtocolVersion:
    def test_version_format(self):
        assert PROTOCOL_VERSION == "2026-03-04"

    def test_capabilities_contains_guidance_v1(self):
        assert "guidance_v1" in CAPABILITIES


# ── Test: QueryResult.guidance field ─────────────────────

class TestQueryResultGuidanceField:
    def test_guidance_field_exists(self):
        r = QueryResult()
        assert hasattr(r, "guidance")
        assert r.guidance is None

    def test_guidance_field_assignable(self):
        r = QueryResult()
        g = _make_guidance_dispatcher()
        r.guidance = g
        assert r.guidance.source == "dispatcher"


# ── Test: Orchestrator main-path produces GuidanceOutput ─

class TestOrchestratorGuidanceCanonical:
    """Verify that _build_guidance + derive_legacy_fields is the only write path.

    Uses a direct call to Orchestrator._build_guidance with a mock dispatcher output
    (simulating what the Light/Deep paths now invoke) and asserts that:
      - result.guidance is a GuidanceOutput instance
      - result.next_steps / result.companion_guide are derived from it (not written directly)
    """

    def test_build_guidance_then_derive_produces_canonical_result(self):
        """_build_guidance → derive_legacy_fields round-trip produces consistent fields."""
        from agoracle.services.orchestrator import Orchestrator
        from agoracle.services.guidance_compat import derive_legacy_fields

        @dataclass
        class _FakeGuideOut:
            companion_message: str = "深入探索这个方向"
            suggested_actions: list = field(default_factory=lambda: [
                {"label": "Deep模式", "action_type": "switch_mode",
                 "action_payload": {"mode": "deep"}, "estimated_seconds": 0,
                 "requires_confirm": False}
            ])
            post_guide_trigger: str = "show"
            is_silent_route: bool = False
            route_reason: str = "divergence≥2"

        result = QueryResult(
            query_id="test-q1",
            question="什么是量子纠缠？",
            final_answer="量子纠缠是...",
            confidence=0.4,
            divergence_points=["观点A", "观点B"],
            key_insights=[],
        )

        guidance = Orchestrator._build_guidance(
            guide_output=_FakeGuideOut(),
            dispatcher_output=None,
            result=result,
            context=None,
        )
        result.guidance = guidance
        result.next_steps, result.companion_guide = derive_legacy_fields(guidance)

        # Canonical field is GuidanceOutput
        assert isinstance(result.guidance, GuidanceOutput)
        assert result.guidance.source == "dispatcher"
        assert result.guidance.message == "深入探索这个方向"

        # Legacy next_steps derives from guidance (dispatcher source → None)
        assert result.next_steps is None

        # Legacy companion_guide derives from guidance
        assert result.companion_guide is not None
        assert result.companion_guide["message"] == "深入探索这个方向"
        assert result.companion_guide["trigger"] == "show"

    def test_no_guidance_input_produces_none_source(self):
        """When dispatcher produces nothing, source='none' and legacy fields are None."""
        from agoracle.services.orchestrator import Orchestrator
        from agoracle.services.guidance_compat import derive_legacy_fields

        result = QueryResult(
            query_id="test-q2",
            question="test",
            final_answer="ok",
            divergence_points=[],
            key_insights=[],
        )
        guidance = Orchestrator._build_guidance(
            guide_output=None,
            dispatcher_output=None,
            result=result,
            context=None,
        )
        result.guidance = guidance
        result.next_steps, result.companion_guide = derive_legacy_fields(guidance)

        assert result.guidance.source == "none"
        assert result.next_steps is None
        assert result.companion_guide is None
