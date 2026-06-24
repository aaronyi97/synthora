from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from agoracle.domain.events import QueryCompleted
from agoracle.domain.types import UserProfile
from agoracle.services.behavior_analytics import BehaviorAnalytics, MAX_TOPIC_SEQUENCE


def _make_store(profile: UserProfile) -> MagicMock:
    store = MagicMock()
    store.load = AsyncMock(return_value=profile)
    store.save = AsyncMock()
    return store


def _event(
    *,
    query_id: str = "q-1",
    question: str = "react 是什么？",
    mode: str = "light",
    resolved_mode: str = "",
    topic_tags: list[str] | None = None,
    user_id: int = 1,
    timestamp: datetime | None = None,
) -> QueryCompleted:
    return QueryCompleted(
        query_id=query_id,
        question=question,
        mode=mode,
        resolved_mode=resolved_mode,
        topic_tags=topic_tags or ["react"],
        user_id=user_id,
        timestamp=timestamp or datetime(2026, 3, 10, 8, 30, 0),
    )


@pytest.mark.asyncio
async def test_on_query_completed_increments_topic_frequency():
    profile = UserProfile(topic_frequency={"react": 1})
    store = _make_store(profile)
    analytics = BehaviorAnalytics(store, proactive_coach=None, model_adapter=None)

    await analytics.on_query_completed(_event())

    assert profile.topic_frequency["react"] == 2
    store.save.assert_awaited_once_with(profile, 1)


@pytest.mark.asyncio
async def test_on_query_completed_appends_topic_sequence_with_mode_and_timestamp():
    profile = UserProfile()
    store = _make_store(profile)
    analytics = BehaviorAnalytics(store, proactive_coach=None, model_adapter=None)
    ts = datetime(2026, 3, 10, 9, 45, 0)

    await analytics.on_query_completed(_event(mode="deep", topic_tags=["react", "hooks"], timestamp=ts))

    assert len(profile.topic_sequence) == 1
    assert profile.topic_sequence[0] == {
        "tags": ["react", "hooks"],
        "mode": "deep",
        "ts": ts.isoformat(),
    }


@pytest.mark.asyncio
async def test_on_query_completed_increments_mode_usage_history():
    profile = UserProfile(mode_usage_history={"deep": 2})
    store = _make_store(profile)
    analytics = BehaviorAnalytics(store, proactive_coach=None, model_adapter=None)

    await analytics.on_query_completed(_event(mode="deep"))

    assert profile.mode_usage_history["deep"] == 3


@pytest.mark.asyncio
async def test_on_query_completed_upgrades_topic_depth_map_from_l1_to_l2():
    profile = UserProfile(
        topic_frequency={"react": 1},
        topic_depth_map={"react": 1},
    )
    store = _make_store(profile)
    analytics = BehaviorAnalytics(store, proactive_coach=None, model_adapter=None)

    await analytics.on_query_completed(_event(mode="light"))

    assert profile.topic_depth_map["react"] == 2


@pytest.mark.asyncio
async def test_on_query_completed_trims_topic_sequence_to_max_window():
    profile = UserProfile(
        topic_sequence=[
            {"tags": [f"topic-{idx}"], "mode": "light", "ts": f"2026-03-10T00:{idx % 60:02d}:00"}
            for idx in range(MAX_TOPIC_SEQUENCE)
        ]
    )
    store = _make_store(profile)
    analytics = BehaviorAnalytics(store, proactive_coach=None, model_adapter=None)

    await analytics.on_query_completed(_event(topic_tags=["react"], query_id="trim"))

    assert len(profile.topic_sequence) == MAX_TOPIC_SEQUENCE
    assert profile.topic_sequence[-1]["tags"] == ["react"]
    assert profile.topic_sequence[0]["tags"] != ["topic-0"]


@pytest.mark.asyncio
async def test_check_depth_gate_returns_depth_gate_when_repeated_topic_is_shallow():
    profile = UserProfile(
        topic_frequency={"react": 3},
        topic_depth_map={"react": 2},
    )
    store = _make_store(profile)
    analytics = BehaviorAnalytics(store, proactive_coach=None, model_adapter=None)

    result = await analytics.check_depth_gate(["react"], user_id=11)

    assert result is not None
    assert result["type"] == "depth_gate"
    assert result["topic"] == "react"
    store.save.assert_awaited_once_with(profile, 11)


@pytest.mark.asyncio
async def test_check_depth_gate_returns_none_when_no_condition_matches():
    profile = UserProfile()
    store = _make_store(profile)
    analytics = BehaviorAnalytics(store, proactive_coach=None, model_adapter=None)

    result = await analytics.check_depth_gate([], user_id=11)

    assert result is None
    store.save.assert_not_awaited()
