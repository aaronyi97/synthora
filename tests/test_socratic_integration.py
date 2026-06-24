"""
Socratic mode integration smoke test — validates the full pipeline
without real API calls by mocking the model adapter.

Tests the complete chain:
  CLI → SocraticOrchestrator → Orchestrator(fan-out) → DivergenceAnalyzer →
  SocraticGuide(initial) → respond(user_msg) → reveal → finish(cognitive)

Also validates CognitiveTracker persistence after session.
"""

import asyncio
import json
import re
import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from agoracle.adapters.profile.json_profile import JsonProfileStore
from agoracle.config.loader import load_config
from agoracle.config.schema import AppConfig, ModeConfig
from agoracle.domain.types import (
    CognitiveSnapshot,
    DivergenceMap,
    DivergencePoint,
    JudgeSynthesis,
    MetadataExtraction,
    Mode,
    ModelResponse,
    OutputDepth,
    QueryContext,
    QueryResult,
    QuestionType,
    Role,
    SocraticSession,
    SocraticTurn,
)
from agoracle.services.socratic_orchestrator import SocraticOrchestrator


@pytest.fixture
def config():
    """Load real config but we'll mock all API calls."""
    return load_config()


@pytest.fixture
def mock_adapter():
    adapter = MagicMock()
    adapter.supports_model = MagicMock(return_value=True)
    adapter.available_models = ["gemini_3_flash", "deepseek_reasoner", "kimi"]

    # Mock call to return a plausible ModelResponse
    async def mock_call(role_call):
        model_id = role_call.model_id
        role = role_call.role
        call_id = role_call.call_id

        def _resp(content, latency_ms=2000):
            return ModelResponse(
                call_id=call_id,
                model_id=model_id,
                role=role,
                content=content,
                latency_ms=latency_ms,
                success=True,
            )

        if role == Role.CONTRIBUTOR:
            return _resp(
                f"[{model_id}] AI创造力是一个复杂的话题。AI可以生成新颖的内容，但这是否算'创造力'取决于定义。"
            )
        elif role in (Role.JUDGE, Role.JUDGE_REFINE):
            return _resp(
                "AI创造力的本质在于它能够组合和重构已有知识，产生看似新颖的输出。"
                "但真正的创造力可能需要意识和主观体验，这是当前AI所不具备的。"
                "从实用角度看，AI是强大的创意辅助工具，但不是独立的创造者。",
                latency_ms=5000,
            )
        elif role == Role.METADATA_EXTRACTOR:
            return _resp(json.dumps({
                "scores": {
                    "deepseek_reasoner": {"accuracy": 0.8, "reasoning": 0.7, "uniqueness": 0.6},
                    "kimi": {"accuracy": 0.7, "reasoning": 0.8, "uniqueness": 0.5},
                },
                "confidence": 0.75,
                "has_divergence": True,
                "divergence_summary": "关于AI是否具有'真正的'创造力存在分歧",
            }), latency_ms=1500)
        elif role == Role.QUESTION_CRITIC:
            return _resp(json.dumps({
                "has_issues": False,
            }), latency_ms=500)
        elif role == Role.ANSWER_CRITIC:
            return _resp(
                "答案质量良好，逻辑清晰，无需修改。",
                latency_ms=1000,
            )
        elif role in (Role.DIVERGENCE_ANALYZER, Role.SOCRATIC_GUIDE):
            # Route by system prompt content
            sys_prompt = role_call.system_prompt or ""
            if "认知" in sys_prompt or "evaluat" in sys_prompt.lower():
                return _resp(json.dumps({
                    "reasoning_depth": 0.65,
                    "nuance_recognition": 0.7,
                    "anchoring_detected": False,
                    "confirmation_bias": False,
                    "blind_spots": ["技术实现细节", "哲学基础"],
                }), latency_ms=500)
            elif "苏格拉底" in sys_prompt or "guide" in sys_prompt.lower():
                return _resp(json.dumps({
                    "guide_message": "你提到AI缺乏意识。那么，意识是创造力的必要条件吗？",
                }), latency_ms=600)
            elif "divergence" in sys_prompt.lower() or "分歧" in sys_prompt:
                return _resp(json.dumps({
                    "divergence_points": [
                        {
                            "topic": "AI创造力的定义",
                            "description": "模型对'创造力'的定义不同",
                            "positions": [
                                {"stance": "工具论", "summary": "AI是创意工具", "models": ["deepseek"]},
                                {"stance": "能力论", "summary": "AI具备某种创造力", "models": ["kimi"]},
                            ],
                            "consensus_ratio": 0.4,
                            "difficulty": "medium",
                        }
                    ],
                    "consensus_points": ["AI可以生成新颖内容", "AI缺乏主观体验"],
                    "overall_consensus_score": 0.6,
                }), latency_ms=800)
            else:
                return _resp(json.dumps({
                    "guide_message": "这是一个很好的问题，让我们继续探讨。",
                }), latency_ms=300)
        else:
            return _resp("Generic response", latency_ms=100)

    adapter.call = AsyncMock(side_effect=mock_call)
    return adapter


@pytest.fixture
def mock_judge(mock_adapter):
    from agoracle.adapters.judge.llm_judge import LLMJudge
    from agoracle.services.prompt_loader import PromptLoader
    from agoracle.config.loader import PROJECT_ROOT

    prompt_loader = PromptLoader(PROJECT_ROOT / "prompts")
    judge = LLMJudge(mock_adapter, prompt_loader)
    return judge


@pytest.fixture
def mock_extractor(mock_adapter):
    from agoracle.adapters.judge.metadata_extractor import LLMMetadataExtractor
    from agoracle.services.prompt_loader import PromptLoader
    from agoracle.config.loader import PROJECT_ROOT

    prompt_loader = PromptLoader(PROJECT_ROOT / "prompts")
    return LLMMetadataExtractor(mock_adapter, prompt_loader)


@pytest.fixture
def profile_store(tmp_path):
    import json
    profile_path = tmp_path / "test_profile.json"
    profile_path.write_text(json.dumps({
        "cognitive_tracking_consent": True,
    }), encoding="utf-8")
    return JsonProfileStore(profile_path)


@pytest.fixture
def session_store(tmp_path):
    import asyncio
    from agoracle.adapters.session.sqlite_socratic_store import SQLiteSocraticSessionStore
    store = SQLiteSocraticSessionStore(tmp_path / "test_socratic.db")
    asyncio.get_event_loop().run_until_complete(store.initialize())
    yield store
    asyncio.get_event_loop().run_until_complete(store.close())


@pytest.fixture
def socratic_orch(config, mock_adapter, mock_judge, mock_extractor, profile_store, session_store):
    from agoracle.services.prompt_loader import PromptLoader
    from agoracle.config.loader import PROJECT_ROOT

    return SocraticOrchestrator(
        config=config,
        model_adapter=mock_adapter,
        judge=mock_judge,
        extractor=mock_extractor,
        prompt_loader=PromptLoader(PROJECT_ROOT / "prompts"),
        event_bus=None,
        search_service=None,
        profile_store=profile_store,
        session_store=session_store,
    )


class TestSocraticIntegration:
    """Full Socratic pipeline integration test with mocked API calls."""

    @pytest.mark.asyncio
    async def test_streaming_stage_details_are_english_when_locale_is_en_us(self, socratic_orch):
        details: list[str] = []

        async for event in socratic_orch.start_session_streaming("Can AI be creative?", language="en-US"):
            if hasattr(event, "detail") and getattr(event, "stage", "") != "heartbeat":
                details.append(getattr(event, "detail", ""))
            if event.__class__.__name__ in {"SocraticReady", "SocraticError"}:
                break

        assert details
        assert all(not re.search(r"[一-龥]", detail) for detail in details if detail)

    @pytest.mark.asyncio
    async def test_wrap_up_prompt_is_english_when_max_rounds_reached(self, socratic_orch):
        session = SocraticSession(
            question="Should we trust AI copilots?",
            language="en-US",
            max_guide_rounds=1,
            guide_rounds_used=1,
        )

        turn = await socratic_orch.respond(session, "I still think reliability matters most.")

        assert "Reveal answer" in turn.content
        assert not re.search(r"[一-龥]", turn.content)

    @pytest.mark.asyncio
    async def test_full_session_lifecycle(self, socratic_orch, profile_store):
        """Test complete: start → respond → reveal → finish → cognitive persistence."""

        # Phase 1: Start session
        session = await socratic_orch.start_session("AI能真正拥有创造力吗？")

        assert session.session_id
        assert session.phase1_latency_ms > 0
        assert len(session.turns) >= 1  # at least initial guide question
        assert session.turns[-1].role == "guide"

        # Phase 2: User responds
        guide_turn = await socratic_orch.respond(session, "我觉得AI只是模仿，没有真正的创造力")

        assert guide_turn.role == "guide"
        assert guide_turn.content  # non-empty guide response
        assert session.guide_rounds_used == 1

        # Phase 2: Another round
        guide_turn2 = await socratic_orch.respond(session, "意识确实很重要，但也许创造力不一定需要意识")

        assert session.guide_rounds_used == 2

        # Reveal
        reveal_data = await socratic_orch.reveal(session)

        assert reveal_data["full_answer"]
        assert session.revealed is True

        # Finish (triggers cognitive tracking)
        finished = await socratic_orch.finish(session)

        assert finished.cognitive_snapshot is not None
        assert finished.reasoning_quality_score > 0

        # Verify cognitive data was persisted
        profile = await profile_store.load()
        assert profile.mode_usage_history.get("socratic", 0) == 1
        assert profile.average_reasoning_quality > 0
        assert profile.last_challenge_date != ""

    @pytest.mark.asyncio
    async def test_early_reveal(self, socratic_orch):
        """Test revealing without any dialogue rounds."""
        session = await socratic_orch.start_session("量子计算的原理是什么？")

        # Immediately reveal
        reveal_data = await socratic_orch.reveal(session)
        assert reveal_data["full_answer"]

        finished = await socratic_orch.finish(session)
        assert finished.guide_rounds_used == 0

    @pytest.mark.asyncio
    async def test_max_rounds_respected(self, socratic_orch):
        """Test that max_guide_rounds limits the dialogue."""
        session = await socratic_orch.start_session("什么是意识？")

        # Respond up to max rounds
        for i in range(session.max_guide_rounds + 2):
            if session.guide_rounds_used >= session.max_guide_rounds:
                break
            await socratic_orch.respond(session, f"Round {i}: 我的想法是...")

        assert session.guide_rounds_used <= session.max_guide_rounds

    @pytest.mark.asyncio
    async def test_session_isolation(self, socratic_orch):
        """Two sessions don't interfere with each other."""
        s1 = await socratic_orch.start_session("问题1")
        s2 = await socratic_orch.start_session("问题2")

        assert s1.session_id != s2.session_id
        assert (await socratic_orch.get_session(s1.session_id)) is not None
        assert (await socratic_orch.get_session(s2.session_id)) is not None

        await socratic_orch.finish(s1)
        # Finished session is still persisted (marked inactive) but retrievable
        assert (await socratic_orch.get_session(s2.session_id)) is not None

    @pytest.mark.asyncio
    async def test_cognitive_accumulation_across_sessions(self, socratic_orch, profile_store):
        """Multiple sessions accumulate cognitive data."""
        for i in range(3):
            session = await socratic_orch.start_session(f"问题{i}")
            await socratic_orch.respond(session, "我的回答")
            await socratic_orch.reveal(session)
            await socratic_orch.finish(session)

        profile = await profile_store.load()
        assert profile.mode_usage_history["socratic"] == 3
        assert len(profile.satisfaction_history) == 3
