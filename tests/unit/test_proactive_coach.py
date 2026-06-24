from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agoracle.domain.types import UserProfile
from agoracle.services.proactive_coach import MAX_ACTIVE_PLANS, ProactiveCoachService


def _make_store(profile: UserProfile) -> MagicMock:
    store = MagicMock()
    store.load = AsyncMock(return_value=profile)
    store.save = AsyncMock()
    return store


def _make_plan(
    *,
    plan_id: str,
    topic: str,
    status: str = "active",
    current_level: int = 1,
    target_level: int = 4,
    difficulty: int = 1,
    challenges_delivered: int = 0,
    challenges_engaged: int = 0,
    last_challenge_date: str = "",
) -> dict:
    now = datetime.now().isoformat()
    return {
      "plan_id": plan_id,
      "topic": topic,
      "status": status,
      "current_level": current_level,
      "target_level": target_level,
      "difficulty": difficulty,
      "challenges_delivered": challenges_delivered,
      "challenges_engaged": challenges_engaged,
      "last_challenge_date": last_challenge_date,
      "milestones": [],
      "created_at": now,
      "updated_at": now,
    }


@pytest.mark.asyncio
async def test_detect_plan_opportunity_returns_proposal_for_repeated_shallow_topic():
    profile = UserProfile(
        topic_frequency={"react": 4},
        topic_depth_map={"react": 1},
    )
    store = _make_store(profile)
    service = ProactiveCoachService(store)

    result = await service.detect_plan_opportunity("想继续学 react", ["react"], user_id=7)

    assert result is not None
    assert result["type"] == "coach_plan_proposal"
    assert result["topic"] == "react"
    assert result["times_explored"] == 4
    assert len(profile.improvement_plans) == 1
    assert profile.improvement_plans[0]["status"] == "proposed"
    store.save.assert_awaited_once_with(profile, 7)


@pytest.mark.asyncio
async def test_detect_plan_opportunity_returns_none_when_frequency_below_threshold():
    profile = UserProfile(
        topic_frequency={"react": 3},
        topic_depth_map={"react": 1},
    )
    store = _make_store(profile)
    service = ProactiveCoachService(store)

    result = await service.detect_plan_opportunity("想继续学 react", ["react"])

    assert result is None
    assert profile.improvement_plans == []
    store.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_detect_plan_opportunity_returns_none_when_max_active_plans_reached():
    profile = UserProfile(
        topic_frequency={"react": 4},
        topic_depth_map={"react": 1},
        improvement_plans=[
            _make_plan(plan_id=f"plan-{idx}", topic=f"topic-{idx}")
            for idx in range(MAX_ACTIVE_PLANS)
        ],
    )
    store = _make_store(profile)
    service = ProactiveCoachService(store)

    result = await service.detect_plan_opportunity("想继续学 react", ["react"])

    assert result is None
    assert len(profile.improvement_plans) == MAX_ACTIVE_PLANS
    store.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_detect_plan_opportunity_returns_none_when_topic_already_has_active_plan():
    profile = UserProfile(
        topic_frequency={"react": 5},
        topic_depth_map={"react": 1},
        improvement_plans=[_make_plan(plan_id="existing", topic="react", status="active")],
    )
    store = _make_store(profile)
    service = ProactiveCoachService(store)

    result = await service.detect_plan_opportunity("想继续学 react", ["react"])

    assert result is None
    assert len(profile.improvement_plans) == 1
    store.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_activate_plan_promotes_proposed_plan_to_active():
    plan = _make_plan(plan_id="plan-1", topic="react", status="proposed")
    profile = UserProfile(improvement_plans=[plan])
    store = _make_store(profile)
    service = ProactiveCoachService(store)

    result = await service.activate_plan("plan-1", user_id=9)

    assert result is plan
    assert plan["status"] == "active"
    store.save.assert_awaited_once_with(profile, 9)


@pytest.mark.asyncio
async def test_activate_plan_returns_none_for_missing_plan():
    profile = UserProfile(improvement_plans=[_make_plan(plan_id="plan-1", topic="react", status="proposed")])
    store = _make_store(profile)
    service = ProactiveCoachService(store)

    result = await service.activate_plan("missing")

    assert result is None
    store.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_abandon_plan_marks_active_plan_as_abandoned():
    plan = _make_plan(plan_id="plan-1", topic="react", status="active")
    profile = UserProfile(improvement_plans=[plan])
    store = _make_store(profile)
    service = ProactiveCoachService(store)

    result = await service.abandon_plan("plan-1", user_id=3)

    assert result is plan
    assert plan["status"] == "abandoned"
    store.save.assert_awaited_once_with(profile, 3)


@pytest.mark.asyncio
async def test_get_capability_map_returns_expected_sections():
    profile = UserProfile(
        topic_frequency={"react": 6, "python": 2},
        topic_depth_map={"react": 3, "python": 1},
        cognitive_quadrant_dist={
            "known_known": 2,
            "known_unknown": 1,
            "unknown_known": 1,
            "unknown_unknown": 0,
        },
        reasoning_improvement_trend=[0.2, 0.5, 0.8],
        average_reasoning_quality=0.76,
        improvement_plans=[
            _make_plan(
                plan_id="plan-1",
                topic="react",
                status="active",
                current_level=2,
                target_level=4,
                challenges_engaged=2,
            ),
            _make_plan(plan_id="plan-2", topic="python", status="proposed"),
            _make_plan(plan_id="plan-3", topic="sql", status="completed"),
        ],
    )
    store = _make_store(profile)
    service = ProactiveCoachService(store)

    result = await service.get_capability_map()

    assert result["has_data"] is True
    assert "topics" in result
    assert "active_plans" in result
    assert "cognitive_quadrant" in result
    assert "reasoning_trend" in result
    assert result["topics"][0]["topic"] == "react"
    assert result["active_plans"][0]["plan_id"] == "plan-1"
    assert result["reasoning_trend"] == [0.2, 0.5, 0.8]


@pytest.mark.asyncio
async def test_record_challenge_engagement_increments_count():
    plan = _make_plan(plan_id="plan-1", topic="react", status="active", challenges_engaged=0)
    profile = UserProfile(
        topic_depth_map={"react": 1},
        improvement_plans=[plan],
    )
    store = _make_store(profile)
    service = ProactiveCoachService(store)

    await service.record_challenge_engagement("plan-1", user_id=12)

    assert plan["challenges_engaged"] == 1
    store.save.assert_awaited_once_with(profile, 12)


@pytest.mark.asyncio
async def test_check_micro_challenge_returns_hint_for_matching_active_plan():
    plan = _make_plan(
        plan_id="plan-1",
        topic="react",
        status="active",
        difficulty=2,
        last_challenge_date=(datetime.now() - timedelta(hours=6)).isoformat(),
    )
    profile = UserProfile(improvement_plans=[plan])
    store = _make_store(profile)
    service = ProactiveCoachService(store)

    with patch("random.choice", return_value="挑战一下：{topic}"):
        result = await service.check_micro_challenge("我想继续聊 react", ["react"], user_id=5)

    assert result is not None
    assert result["type"] == "coach_micro_challenge"
    assert result["plan_id"] == "plan-1"
    assert result["topic"] == "react"
    assert result["challenge"] == "挑战一下：react"
    assert plan["challenges_delivered"] == 1
    store.save.assert_awaited_once_with(profile, 5)
