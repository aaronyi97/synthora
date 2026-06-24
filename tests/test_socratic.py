"""
Tests for Socratic mode components (Phase 3).

Tests:
  - DivergenceAnalyzer: JSON parsing, fallback handling
  - SocraticGuide: guide generation, evaluation parsing
  - Data structures: DivergenceMap, SocraticSession, CognitiveSnapshot
  - Config: Socratic fields in ModeConfig
"""

import json
import re
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agoracle.config.schema import ModeConfig
from agoracle.domain.types import (
    CognitiveSnapshot,
    DivergenceMap,
    DivergencePoint,
    ModelResponse,
    Role,
    SocraticSession,
    SocraticTurn,
)
from agoracle.services.divergence_analyzer import DivergenceAnalyzer
from agoracle.services.socratic_guide import SocraticGuide


# ── Fixtures ──────────────────────────────────────────────

@pytest.fixture
def mock_adapter():
    adapter = AsyncMock()
    adapter.call = AsyncMock()
    return adapter


@pytest.fixture
def sample_responses():
    return [
        ModelResponse(
            call_id="c1", model_id="model_a", role=Role.CONTRIBUTOR,
            content="AI可以辅助创造力但不能取代。技术上看，AI缺乏真正的意识和主观体验。",
            latency_ms=1000, success=True,
        ),
        ModelResponse(
            call_id="c2", model_id="model_b", role=Role.CONTRIBUTOR,
            content="AI已经在某些领域展现出超越人类的创造力，比如音乐和绘画。",
            latency_ms=1200, success=True,
        ),
        ModelResponse(
            call_id="c3", model_id="model_c", role=Role.CONTRIBUTOR,
            content="创造力的定义本身就有争议。如果定义为新颖组合，AI已经做到了。",
            latency_ms=800, success=True,
        ),
    ]


# ── Data Structure Tests ──────────────────────────────────

class TestDataStructures:
    def test_divergence_point_defaults(self):
        dp = DivergencePoint()
        assert dp.topic == ""
        assert dp.consensus_ratio == 0.0
        assert dp.difficulty == "medium"
        assert dp.positions == []

    def test_divergence_map_defaults(self):
        dm = DivergenceMap()
        assert dm.consensus_points == []
        assert dm.divergence_points == []
        assert dm.overall_consensus_score == 0.0
        assert dm.model_count == 0

    def test_socratic_session_defaults(self):
        session = SocraticSession()
        assert session.guide_rounds_used == 0
        assert session.revealed is False
        assert session.completed_naturally is True
        assert session.max_guide_rounds == 5
        assert session.turns == []
        assert session.cognitive_snapshot is None

    def test_cognitive_snapshot_defaults(self):
        cs = CognitiveSnapshot()
        assert cs.anchoring_detected is False
        assert cs.confirmation_bias is False
        assert cs.nuance_recognition == 0.0
        assert cs.reasoning_depth == 0.0
        assert cs.blind_spots == []

    def test_socratic_turn_defaults(self):
        turn = SocraticTurn(role="guide", content="你怎么看？")
        assert turn.role == "guide"
        assert turn.content == "你怎么看？"
        assert turn.divergence_point_id is None


# ── Config Tests ──────────────────────────────────────────

class TestSocraticConfig:
    def test_mode_config_socratic_fields(self):
        mc = ModeConfig(
            name="socratic",
            divergence_analyzer="gemini_3_flash",
            guide_generator="gemini_3_flash",
            max_guide_rounds=5,
            reveal_on_demand=True,
        )
        assert mc.divergence_analyzer == "gemini_3_flash"
        assert mc.guide_generator == "gemini_3_flash"
        assert mc.max_guide_rounds == 5
        assert mc.reveal_on_demand is True

    def test_mode_config_socratic_defaults(self):
        mc = ModeConfig()
        assert mc.divergence_analyzer == ""
        assert mc.guide_generator == ""
        assert mc.max_guide_rounds == 5
        assert mc.reveal_on_demand is True


# ── DivergenceAnalyzer Tests ──────────────────────────────

class TestDivergenceAnalyzer:
    @pytest.mark.asyncio
    async def test_analyze_parses_valid_json(self, mock_adapter, sample_responses):
        json_response = json.dumps({
            "consensus_points": ["AI技术在快速发展", "需要伦理框架"],
            "divergence_points": [
                {
                    "topic": "AI是否具有真正的创造力",
                    "description": "专家们对AI创造力的本质存在根本分歧",
                    "positions": [
                        {"stance": "反对", "summary": "AI缺乏意识", "models": ["专家1"]},
                        {"stance": "支持", "summary": "AI已展现创造力", "models": ["专家2"]},
                    ],
                    "consensus_ratio": 0.3,
                    "difficulty": "hard",
                }
            ],
            "overall_consensus_score": 0.4,
        })
        mock_adapter.call.return_value = ModelResponse(
            call_id="test", model_id="flash", role=Role.DIVERGENCE_ANALYZER,
            content=f"```json\n{json_response}\n```",
            latency_ms=500, success=True,
        )

        analyzer = DivergenceAnalyzer(mock_adapter)
        result = await analyzer.analyze("AI会取代创造力吗？", sample_responses)

        assert len(result.consensus_points) == 2
        assert len(result.divergence_points) == 1
        assert result.divergence_points[0].topic == "AI是否具有真正的创造力"
        assert result.divergence_points[0].difficulty == "hard"
        assert result.overall_consensus_score == 0.4
        assert result.model_count == 3

    @pytest.mark.asyncio
    async def test_analyze_handles_failed_call(self, mock_adapter, sample_responses):
        mock_adapter.call.return_value = ModelResponse(
            call_id="test", model_id="flash", role=Role.DIVERGENCE_ANALYZER,
            content="", latency_ms=500, success=False, error="timeout",
        )

        analyzer = DivergenceAnalyzer(mock_adapter)
        result = await analyzer.analyze("test question", sample_responses)

        # Should return fallback map
        assert result.model_count == 3
        assert len(result.divergence_points) == 1
        assert result.divergence_points[0].topic == "各专家的不同角度"

    @pytest.mark.asyncio
    async def test_analyze_single_response(self, mock_adapter):
        responses = [
            ModelResponse(
                call_id="c1", model_id="model_a", role=Role.CONTRIBUTOR,
                content="Only one response", latency_ms=100, success=True,
            )
        ]

        analyzer = DivergenceAnalyzer(mock_adapter)
        result = await analyzer.analyze("test", responses)

        assert result.model_count == 1
        assert result.overall_consensus_score == 1.0
        mock_adapter.call.assert_not_called()

    @pytest.mark.asyncio
    async def test_analyze_handles_malformed_json(self, mock_adapter, sample_responses):
        mock_adapter.call.return_value = ModelResponse(
            call_id="test", model_id="flash", role=Role.DIVERGENCE_ANALYZER,
            content="This is not JSON at all, just text",
            latency_ms=500, success=True,
        )

        analyzer = DivergenceAnalyzer(mock_adapter)
        result = await analyzer.analyze("test", sample_responses)

        assert result.model_count == 3
        assert result.divergence_points == []


# ── SocraticGuide Tests ──────────────────────────────────

class TestSocraticGuide:
    @pytest.mark.asyncio
    async def test_generate_initial_guide_parses_json(self, mock_adapter):
        guide_json = json.dumps({
            "guide_message": "你觉得AI能否产生真正的创意？为什么？",
            "target_divergence_id": "dp1",
            "hint_level": 0,
        })
        mock_adapter.call.return_value = ModelResponse(
            call_id="test", model_id="flash", role=Role.SOCRATIC_GUIDE,
            content=f"```json\n{guide_json}\n```",
            latency_ms=800, success=True,
        )

        guide = SocraticGuide(mock_adapter)
        dm = DivergenceMap(
            divergence_points=[
                DivergencePoint(
                    point_id="dp1",
                    topic="AI创造力",
                    description="专家分歧",
                    positions=[
                        {"stance": "支持", "summary": "AI已展现创造力", "models": ["专家1"]},
                        {"stance": "反对", "summary": "AI缺乏意识", "models": ["专家2"]},
                    ],
                )
            ],
        )

        turn = await guide.generate_initial_guide("AI会取代创造力吗？", dm)

        assert turn.role == "guide"
        assert "创意" in turn.content or "创造力" in turn.content or "guide_message" not in turn.content
        assert turn.latency_ms >= 0  # measured by time.monotonic(), near-zero in tests

    @pytest.mark.asyncio
    async def test_generate_initial_guide_no_divergence(self, mock_adapter):
        guide = SocraticGuide(mock_adapter)
        dm = DivergenceMap()

        turn = await guide.generate_initial_guide("test", dm)

        assert turn.role == "guide"
        assert "共识" in turn.content
        mock_adapter.call.assert_not_called()

    @pytest.mark.asyncio
    async def test_generate_initial_guide_uses_english_prompt(self, mock_adapter):
        guide_json = json.dumps({
            "guide_message": "What assumption matters most to your conclusion?",
            "target_divergence_id": "dp1",
            "hint_level": 1,
        })
        mock_adapter.call.return_value = ModelResponse(
            call_id="test", model_id="flash", role=Role.SOCRATIC_GUIDE,
            content=guide_json,
            latency_ms=300, success=True,
        )
        guide = SocraticGuide(mock_adapter)
        dm = DivergenceMap(
            divergence_points=[
                DivergencePoint(
                    point_id="dp1",
                    topic="AI creativity",
                    description="Experts disagree on the definition of creativity",
                    positions=[{"stance": "support", "summary": "AI already shows it", "models": ["Expert 1"]}],
                )
            ],
        )

        turn = await guide.generate_initial_guide("Can AI be creative?", dm, language="en-US")

        role_call = mock_adapter.call.call_args.args[0]
        assert "Socratic thinking coach" in role_call.system_prompt
        assert "User question" in role_call.messages[0]["content"]
        assert "What assumption" in turn.content

    @pytest.mark.asyncio
    async def test_generate_initial_guide_no_divergence_in_english(self, mock_adapter):
        guide = SocraticGuide(mock_adapter)
        dm = DivergenceMap()

        turn = await guide.generate_initial_guide("test", dm, language="en-US")

        assert turn.role == "guide"
        assert "experts mostly agree" in turn.content.lower()
        assert not re.search(r"[一-龥]", turn.content)

    @pytest.mark.asyncio
    async def test_generate_initial_guide_fallback_on_failure(self, mock_adapter):
        mock_adapter.call.return_value = ModelResponse(
            call_id="test", model_id="flash", role=Role.SOCRATIC_GUIDE,
            content="", latency_ms=500, success=False, error="timeout",
        )

        guide = SocraticGuide(mock_adapter)
        dm = DivergenceMap(
            divergence_points=[
                DivergencePoint(point_id="dp1", topic="AI创造力"),
            ],
        )

        turn = await guide.generate_initial_guide("test", dm)

        assert turn.role == "guide"
        assert "AI创造力" in turn.content
        assert turn.divergence_point_id == "dp1"

    @pytest.mark.asyncio
    async def test_generate_followup_no_position_fallback_is_english(self, mock_adapter):
        mock_adapter.call.return_value = ModelResponse(
            call_id="test", model_id="flash", role=Role.SOCRATIC_GUIDE,
            content="", latency_ms=500, success=False, error="timeout",
        )
        guide = SocraticGuide(mock_adapter)
        session = SocraticSession(
            question="Should we trust AI copilots?",
            turns=[SocraticTurn(role="guide", content="What matters most to you here?")],
            divergence_map=DivergenceMap(
                divergence_points=[DivergencePoint(point_id="dp1", topic="Trust", positions=[])],
            ),
        )

        turn = await guide.generate_followup(session, "Reliability matters most", language="en-US")

        assert "interesting angle" in turn.content.lower()
        assert not re.search(r"[一-龥]", turn.content)

    @pytest.mark.asyncio
    async def test_evaluate_session_parses_json(self, mock_adapter):
        eval_json = json.dumps({
            "anchoring_detected": True,
            "confirmation_bias": False,
            "nuance_recognition": 0.7,
            "reasoning_depth": 0.6,
            "blind_spots": ["技术细节", "伦理考量"],
            "overall_quality": 0.65,
        })
        mock_adapter.call.return_value = ModelResponse(
            call_id="test", model_id="flash", role=Role.SOCRATIC_GUIDE,
            content=f"```json\n{eval_json}\n```",
            latency_ms=500, success=True,
        )

        guide = SocraticGuide(mock_adapter)
        session = SocraticSession(
            turns=[
                SocraticTurn(role="guide", content="你怎么看？"),
                SocraticTurn(role="user", content="我觉得AI不能取代创造力"),
            ],
            divergence_map=DivergenceMap(
                divergence_points=[DivergencePoint(topic="AI创造力")],
            ),
        )

        snapshot = await guide.evaluate_session(session)

        assert snapshot.anchoring_detected is True
        assert snapshot.confirmation_bias is False
        assert snapshot.nuance_recognition == 0.7
        assert snapshot.reasoning_depth == 0.6
        assert "技术细节" in snapshot.blind_spots

    @pytest.mark.asyncio
    async def test_evaluate_session_empty_turns(self, mock_adapter):
        guide = SocraticGuide(mock_adapter)
        session = SocraticSession()

        snapshot = await guide.evaluate_session(session)

        assert snapshot.reasoning_depth == 0.0
        mock_adapter.call.assert_not_called()

    @pytest.mark.asyncio
    async def test_followup_generation(self, mock_adapter):
        guide_json = json.dumps({
            "guide_message": "有意思。但如果AI能写出获奖小说呢？",
            "target_divergence_id": "dp1",
            "hint_level": 1,
        })
        mock_adapter.call.return_value = ModelResponse(
            call_id="test", model_id="flash", role=Role.SOCRATIC_GUIDE,
            content=f"```json\n{guide_json}\n```",
            latency_ms=600, success=True,
        )

        guide = SocraticGuide(mock_adapter)
        session = SocraticSession(
            turns=[
                SocraticTurn(role="guide", content="你怎么看AI创造力？"),
            ],
            divergence_map=DivergenceMap(
                divergence_points=[
                    DivergencePoint(
                        point_id="dp1", topic="AI创造力",
                        positions=[
                            {"stance": "支持", "summary": "AI已展现创造力", "models": ["专家1"]},
                        ],
                    ),
                ],
            ),
        )

        turn = await guide.generate_followup(session, "我觉得AI只是模仿")

        assert turn.role == "guide"
        assert turn.latency_ms >= 0


# ── Integration-style Tests ───────────────────────────────

class TestSocraticSessionFlow:
    def test_session_turn_tracking(self):
        session = SocraticSession(max_guide_rounds=3)

        # Add guide turn
        session.turns.append(SocraticTurn(role="guide", content="你怎么看？"))

        # Add user turn
        session.turns.append(SocraticTurn(role="user", content="我觉得..."))
        session.guide_rounds_used += 1

        assert len(session.turns) == 2
        assert session.guide_rounds_used == 1
        assert not session.revealed

    def test_session_max_rounds(self):
        session = SocraticSession(max_guide_rounds=2)
        session.guide_rounds_used = 2

        assert session.guide_rounds_used >= session.max_guide_rounds

    def test_session_reveal(self):
        session = SocraticSession()
        session.revealed = True
        session.completed_naturally = False

        assert session.revealed
        assert not session.completed_naturally

    def test_divergence_map_with_points(self):
        dm = DivergenceMap(
            consensus_points=["AI在发展", "需要监管"],
            divergence_points=[
                DivergencePoint(
                    topic="AI意识",
                    positions=[
                        {"stance": "支持", "summary": "可能产生意识", "models": ["m1"]},
                        {"stance": "反对", "summary": "不可能", "models": ["m2", "m3"]},
                    ],
                    consensus_ratio=0.33,
                    difficulty="hard",
                ),
            ],
            overall_consensus_score=0.6,
            model_count=3,
        )

        assert len(dm.consensus_points) == 2
        assert len(dm.divergence_points) == 1
        assert dm.divergence_points[0].consensus_ratio == 0.33
