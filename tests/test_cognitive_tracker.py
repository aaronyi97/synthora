"""
Tests for CognitiveTracker — cross-session cognitive profile accumulation.
"""

import json
import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

from agoracle.adapters.profile.json_profile import JsonProfileStore
from agoracle.domain.types import (
    CognitiveSnapshot,
    DivergenceMap,
    DivergencePoint,
    SocraticSession,
    SocraticTurn,
    UserProfile,
)
from agoracle.services.cognitive_tracker import CognitiveTracker


@pytest.fixture
def tmp_profile_path(tmp_path):
    return tmp_path / "profile.json"


@pytest.fixture
def profile_store(tmp_profile_path):
    # Pre-grant consent for tests (opt-in required since v2.6)
    tmp_profile_path.write_text(json.dumps({
        "cognitive_tracking_consent": True,
    }), encoding="utf-8")
    return JsonProfileStore(tmp_profile_path)


@pytest.fixture
def tracker(profile_store):
    return CognitiveTracker(profile_store)


@pytest.fixture
def completed_session():
    """A Socratic session that completed naturally with cognitive data."""
    return SocraticSession(
        session_id="test-session-001",
        guide_rounds_used=3,
        completed_naturally=True,
        revealed=False,
        reasoning_quality_score=0.7,
        cognitive_snapshot=CognitiveSnapshot(
            anchoring_detected=False,
            confirmation_bias=False,
            nuance_recognition=0.8,
            reasoning_depth=0.7,
            blind_spots=["技术细节", "伦理考量"],
            position_change_count=1,
        ),
        turns=[
            SocraticTurn(role="guide", content="你怎么看？"),
            SocraticTurn(role="user", content="我觉得AI不能取代创造力"),
            SocraticTurn(role="guide", content="为什么？"),
            SocraticTurn(role="user", content="因为AI缺乏意识"),
        ],
        divergence_map=DivergenceMap(
            divergence_points=[DivergencePoint(topic="AI创造力")],
        ),
    )


@pytest.fixture
def revealed_session():
    """A session where user revealed early."""
    return SocraticSession(
        session_id="test-session-002",
        guide_rounds_used=1,
        completed_naturally=False,
        revealed=True,
        reasoning_quality_score=0.3,
        cognitive_snapshot=CognitiveSnapshot(
            anchoring_detected=True,
            confirmation_bias=True,
            nuance_recognition=0.2,
            reasoning_depth=0.3,
            blind_spots=["多角度思考"],
        ),
        turns=[
            SocraticTurn(role="guide", content="你怎么看？"),
            SocraticTurn(role="user", content="直接告诉我答案"),
        ],
    )


class TestCognitiveTracker:
    @pytest.mark.asyncio
    async def test_record_first_session(self, tracker, profile_store, completed_session):
        profile = await tracker.record_session(completed_session)

        assert profile.mode_usage_history["socratic"] == 1
        assert profile.socratic_completion_rate == 1.0  # first session, completed
        assert profile.average_reasoning_quality == 0.7
        assert profile.last_challenge_date != ""
        assert "技术细节" in profile.growth_zone_topics
        assert "伦理考量" in profile.growth_zone_topics

    @pytest.mark.asyncio
    async def test_record_multiple_sessions_ema(self, tracker, completed_session, revealed_session):
        # First session: completed, quality=0.7
        await tracker.record_session(completed_session)

        # Second session: revealed early, quality=0.3
        profile = await tracker.record_session(revealed_session)

        assert profile.mode_usage_history["socratic"] == 2
        # EMA: 0.3 * 0 + 0.7 * 1.0 = 0.7 (completion rate)
        assert profile.socratic_completion_rate < 1.0
        assert profile.socratic_completion_rate > 0.0
        # EMA: quality should blend
        assert 0.3 < profile.average_reasoning_quality < 0.7

    @pytest.mark.asyncio
    async def test_quadrant_known_known(self, tracker, profile_store):
        """High reasoning + high nuance → known_known."""
        session = SocraticSession(
            session_id="kk",
            cognitive_snapshot=CognitiveSnapshot(
                reasoning_depth=0.8, nuance_recognition=0.9,
            ),
        )
        profile = await tracker.record_session(session)
        assert profile.cognitive_quadrant_dist["known_known"] == 1

    @pytest.mark.asyncio
    async def test_quadrant_unknown_unknown(self, tracker, profile_store):
        """Low reasoning + biases → unknown_unknown."""
        session = SocraticSession(
            session_id="uu",
            cognitive_snapshot=CognitiveSnapshot(
                reasoning_depth=0.2, nuance_recognition=0.1,
                anchoring_detected=True,
            ),
        )
        profile = await tracker.record_session(session)
        assert profile.cognitive_quadrant_dist["unknown_unknown"] == 1

    @pytest.mark.asyncio
    async def test_growth_zone_accumulation(self, tracker):
        """Blind spots from multiple sessions accumulate in growth_zone_topics."""
        s1 = SocraticSession(
            session_id="s1",
            cognitive_snapshot=CognitiveSnapshot(blind_spots=["逻辑推理"]),
        )
        s2 = SocraticSession(
            session_id="s2",
            cognitive_snapshot=CognitiveSnapshot(blind_spots=["逻辑推理", "历史背景"]),
        )
        await tracker.record_session(s1)
        profile = await tracker.record_session(s2)

        assert "逻辑推理" in profile.growth_zone_topics
        assert "历史背景" in profile.growth_zone_topics
        # No duplicates
        assert profile.growth_zone_topics.count("逻辑推理") == 1

    @pytest.mark.asyncio
    async def test_satisfaction_history_records_events(self, tracker, completed_session):
        profile = await tracker.record_session(completed_session)

        assert len(profile.satisfaction_history) == 1
        event = profile.satisfaction_history[0]
        assert event["type"] == "socratic_session"
        assert event["session_id"] == "test-session-001"
        assert event["reasoning_depth"] == 0.7
        assert event["completed_naturally"] is True

    @pytest.mark.asyncio
    async def test_persistence_across_loads(self, tracker, profile_store, completed_session):
        """Verify data persists to disk and can be reloaded."""
        await tracker.record_session(completed_session)

        # Create a new tracker with the same store
        new_tracker = CognitiveTracker(profile_store)
        profile = await profile_store.load()

        assert profile.mode_usage_history["socratic"] == 1
        assert profile.average_reasoning_quality == 0.7

    @pytest.mark.asyncio
    async def test_no_snapshot_still_records(self, tracker):
        """Session without cognitive snapshot still records basic data."""
        session = SocraticSession(
            session_id="no-snapshot",
            guide_rounds_used=2,
            completed_naturally=True,
            cognitive_snapshot=None,
        )
        profile = await tracker.record_session(session)

        assert profile.mode_usage_history["socratic"] == 1
        assert profile.socratic_completion_rate == 1.0

    @pytest.mark.asyncio
    async def test_cognitive_summary(self, tracker, completed_session):
        await tracker.record_session(completed_session)
        summary = await tracker.get_cognitive_summary()

        assert "苏格拉底对话次数: 1" in summary
        assert "推理质量" in summary

    @pytest.mark.asyncio
    async def test_cognitive_summary_new_user(self, tracker):
        summary = await tracker.get_cognitive_summary()
        assert "新用户" in summary
