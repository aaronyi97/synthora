from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from agoracle.services import roundtable_orchestrator as rt


def _opinion(model_id: str, label: str, *, success: bool = True) -> rt.ExpertOpinion:
    return rt.ExpertOpinion(
        model_id=model_id,
        label=label,
        stance=f"{label} 立场",
        confidence=0.7,
        success=success,
    )


def _rebuttal(round_num: int, model_id: str, label: str) -> rt.Rebuttal:
    return rt.Rebuttal(
        model_id=model_id,
        label=label,
        role="main_debater",
        target_dispute=f"争议点 {round_num}",
        response=f"第 {round_num} 轮回应",
        success=True,
    )


async def _collect_events(stream, session_id_holder: dict[str, str], choice_plan: list[rt.UserChoice]):
    events: list[rt.RoundtableEvent] = []
    async for event in stream:
        events.append(event)
        if isinstance(event, rt.RoundtableStarted):
            session_id_holder["value"] = event.session_id
        if isinstance(event, rt.AwaitingUserChoice):
            session = rt.get_session(session_id_holder["value"])
            assert session is not None
            await session._choice_queue.put(choice_plan.pop(0))
    return events


@pytest.mark.asyncio
async def test_execute_streaming_s2_timeout_falls_back_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = rt.RoundtableOrchestrator(
        model_adapter=SimpleNamespace(supports_model=lambda _model_id: True),
        config=SimpleNamespace(),
    )

    async def _fake_fan_out(*_args, **_kwargs):
        yield _opinion("m1", "专家A")
        yield _opinion("m2", "专家B")

    async def _slow_map(*_args, **_kwargs):
        await asyncio.sleep(0.02)
        return rt.DisputeMap()

    async def _fake_packet(*_args, **_kwargs):
        return rt.DecisionPacket(
            final_summary="fallback path completed",
            conclusion_type="recommendation",
            confidence_basis="ok",
        )

    monkeypatch.setattr(orch, "_fan_out_experts_stream", _fake_fan_out)
    monkeypatch.setattr(orch, "_map_disputes", _slow_map)
    monkeypatch.setattr(orch, "_build_decision_packet", _fake_packet)

    session_id_holder: dict[str, str] = {}
    events = await _collect_events(
        orch.execute_streaming(
            "该不该投火星计划？",
            owner_user_id="u1",
            rt_config=rt.RoundtableConfig(
                moderator_s2_timeout_s=0.01,
                total_timeout_s=5,
                interactive=True,
            ),
        ),
        session_id_holder,
        [rt.UserChoice(action="conclude", choice_point="A")],
    )

    disputes = next(event for event in events if isinstance(event, rt.DisputesMapped))
    assert disputes.dispute_map.degraded is True
    assert disputes.dispute_map.contention_points or disputes.dispute_map.consensus_points
    assert any(isinstance(event, rt.AwaitingUserChoice) and event.choice_point == "A" for event in events)

    last = events[-1]
    assert isinstance(last, rt.RoundtableComplete)
    assert last.result.decision_packet.final_summary == "fallback path completed"


@pytest.mark.asyncio
async def test_execute_streaming_s4_timeout_falls_back_and_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = rt.RoundtableOrchestrator(
        model_adapter=SimpleNamespace(supports_model=lambda _model_id: True),
        config=SimpleNamespace(),
    )

    async def _fake_fan_out(*_args, **_kwargs):
        yield _opinion("m1", "专家A")
        yield _opinion("m2", "专家B")

    async def _fake_map(*_args, **_kwargs):
        return rt.DisputeMap(
            contention_points=[
                rt.ContentionPoint(topic="争议点 1", severity="high", suggested_focus=True),
            ]
        )

    async def _slow_packet(*_args, **_kwargs):
        await asyncio.sleep(0.02)
        return rt.DecisionPacket(final_summary="should timeout before this")

    monkeypatch.setattr(orch, "_fan_out_experts_stream", _fake_fan_out)
    monkeypatch.setattr(orch, "_map_disputes", _fake_map)
    monkeypatch.setattr(orch, "_build_decision_packet", _slow_packet)

    session_id_holder: dict[str, str] = {}
    events = await _collect_events(
        orch.execute_streaming(
            "该不该投火星计划？",
            owner_user_id="u1",
            rt_config=rt.RoundtableConfig(
                moderator_s4_timeout_s=0.01,
                total_timeout_s=5,
                interactive=True,
            ),
        ),
        session_id_holder,
        [rt.UserChoice(action="conclude", choice_point="A")],
    )

    last = events[-1]
    assert isinstance(last, rt.RoundtableComplete)
    assert last.result.decision_packet.degraded is True
    assert last.result.decision_packet.degradation_reason == "s4_moderator_timeout"
    assert last.result.decision_packet.conclusion_type == "draft"
    assert "主持人未能在时限内完成决策综合" in last.result.decision_packet.final_summary


@pytest.mark.asyncio
async def test_non_interactive_s2_phase_error_falls_back_and_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = rt.RoundtableOrchestrator(
        model_adapter=SimpleNamespace(supports_model=lambda _model_id: True),
        config=SimpleNamespace(),
    )

    async def _fake_fan_out(*_args, **_kwargs):
        yield _opinion("m1", "专家A")
        yield _opinion("m2", "专家B")

    async def _failing_map(*_args, **_kwargs):
        raise rt.RoundtablePhaseError(
            "roundtable_s2_upstream_model_failure",
            phase="S2",
            reason="upstream_model_retry_exhausted",
            detail="moderator returned structured error",
        )

    async def _fake_packet(*_args, **_kwargs):
        return rt.DecisionPacket(
            final_summary="fallback dispute map completed",
            conclusion_type="recommendation",
            confidence_basis="ok",
        )

    monkeypatch.setattr(orch, "_fan_out_experts_stream", _fake_fan_out)
    monkeypatch.setattr(orch, "_map_disputes", _failing_map)
    monkeypatch.setattr(orch, "_build_decision_packet", _fake_packet)

    events: list[rt.RoundtableEvent] = []
    async for event in orch.execute_streaming(
        "该不该投火星计划？",
        owner_user_id="u1",
        rt_config=rt.RoundtableConfig(total_timeout_s=5),
    ):
        events.append(event)

    disputes = next(event for event in events if isinstance(event, rt.DisputesMapped))
    assert disputes.dispute_map.degraded is True
    assert any(isinstance(event, rt.RoundtableComplete) for event in events)
    assert not any(isinstance(event, rt.RoundtableError) for event in events)


@pytest.mark.asyncio
async def test_choice_b_s4_timeout_falls_back_and_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = rt.RoundtableOrchestrator(
        model_adapter=SimpleNamespace(supports_model=lambda _model_id: True),
        config=SimpleNamespace(),
    )

    async def _fake_fan_out(*_args, **_kwargs):
        yield _opinion("m1", "专家A")
        yield _opinion("m2", "专家B")

    async def _fake_map(*_args, **_kwargs):
        return rt.DisputeMap(
            contention_points=[
                rt.ContentionPoint(
                    topic="争议点 1",
                    severity="high",
                    suggested_focus=True,
                    sides=[rt.ContentionSide(position="支持", lead_expert="专家A")],
                ),
            ]
        )

    async def _fake_debate(*_args, round_num: int, **_kwargs):
        yield rt.DebateStarted(round_num=round_num, assignments={"m1": "main_debater", "m2": "reviewer"})
        rebuttal = _rebuttal(round_num, "m1", "专家A")
        yield rt.RebuttalDone(rebuttal, done_count=1, total_count=2)
        done = rt.DebateComplete(round_num=round_num, stance_changes=[])
        done._rebuttals = [rebuttal]  # type: ignore[attr-defined]
        yield done

    async def _slow_packet(*_args, **_kwargs):
        await asyncio.sleep(0.02)
        return rt.DecisionPacket(final_summary="should timeout before this")

    monkeypatch.setattr(orch, "_fan_out_experts_stream", _fake_fan_out)
    monkeypatch.setattr(orch, "_map_disputes", _fake_map)
    monkeypatch.setattr(orch, "_run_debate_round", _fake_debate)
    monkeypatch.setattr(orch, "_build_decision_packet", _slow_packet)

    session_id_holder: dict[str, str] = {}
    events = await _collect_events(
        orch.execute_streaming(
            "该不该投火星计划？",
            owner_user_id="u1",
            rt_config=rt.RoundtableConfig(
                moderator_s4_timeout_s=0.01,
                total_timeout_s=5,
                interactive=True,
            ),
        ),
        session_id_holder,
        [
            rt.UserChoice(action="deepen", choice_point="A"),
            rt.UserChoice(action="conclude", choice_point="B"),
        ],
    )

    last = events[-1]
    assert isinstance(last, rt.RoundtableComplete)
    assert last.result.rounds_completed == 2
    assert last.result.decision_packet.degraded is True
    assert last.result.decision_packet.degradation_reason == "s4_moderator_timeout"


@pytest.mark.asyncio
async def test_insufficient_successful_experts_returns_friendly_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orch = rt.RoundtableOrchestrator(
        model_adapter=SimpleNamespace(supports_model=lambda _model_id: True),
        config=SimpleNamespace(),
    )

    async def _fake_fan_out(*_args, **_kwargs):
        yield _opinion("m1", "专家A")
        yield _opinion("m2", "专家B", success=False)
        yield _opinion("m3", "专家C", success=False)

    monkeypatch.setattr(orch, "_fan_out_experts_stream", _fake_fan_out)

    events: list[rt.RoundtableEvent] = []
    async for event in orch.execute_streaming(
        "该不该投火星计划？",
        owner_user_id="u1",
        rt_config=rt.RoundtableConfig(total_timeout_s=5),
    ):
        events.append(event)

    last = events[-1]
    assert isinstance(last, rt.RoundtableError)
    assert last.error == "部分 AI 专家暂时不可用，请稍后重试"
    assert last.phase == "S1"
    assert last.reason == "insufficient_responses_after_fanout"
    assert last.billable is False


@pytest.mark.asyncio
async def test_non_interactive_skips_choice_points(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = rt.RoundtableOrchestrator(
        model_adapter=SimpleNamespace(supports_model=lambda _model_id: True),
        config=SimpleNamespace(),
    )

    async def _fake_fan_out(*_args, **_kwargs):
        yield _opinion("m1", "专家A")
        yield _opinion("m2", "专家B")
        yield _opinion("m3", "专家C")

    async def _fake_map(*_args, **_kwargs):
        return rt.DisputeMap(
            contention_points=[
                rt.ContentionPoint(topic="争议点 1", severity="high", suggested_focus=True),
            ],
        )

    captured: dict[str, object] = {}

    async def _fake_packet(*_args, rebuttals=None, user_preferences=None, eligibility=None, interactive=True, **_kwargs):
        captured["rebuttals"] = list(rebuttals or [])
        captured["user_preferences"] = list(user_preferences or [])
        captured["eligibility"] = dict(eligibility or {})
        captured["interactive"] = interactive
        return rt.DecisionPacket(
            final_summary="auto conclude path completed",
            conclusion_type="recommendation",
            confidence_basis="ok",
        )

    monkeypatch.setattr(orch, "_fan_out_experts_stream", _fake_fan_out)
    monkeypatch.setattr(orch, "_map_disputes", _fake_map)
    monkeypatch.setattr(orch, "_build_decision_packet", _fake_packet)

    events: list[rt.RoundtableEvent] = []
    async for event in orch.execute_streaming(
        "该不该投火星计划？",
        owner_user_id="u1",
        rt_config=rt.RoundtableConfig(total_timeout_s=5),
    ):
        events.append(event)

    assert [type(event).__name__ for event in events] == [
        "RoundtableStarted",
        "ExpertDone",
        "ExpertDone",
        "ExpertDone",
        "ModeratorStarted",
        "DisputesMapped",
        "RoundtableComplete",
    ]
    assert not any(isinstance(event, rt.AwaitingUserChoice) for event in events)
    assert not any(isinstance(event, rt.DebateStarted) for event in events)

    last = events[-1]
    assert isinstance(last, rt.RoundtableComplete)
    assert last.result.rounds_completed == 1
    assert last.result.rebuttals == []
    assert captured["rebuttals"] == []
    assert captured["user_preferences"] == []
    assert captured["interactive"] is False
    assert captured["eligibility"] == {
        "conclusion_type": "recommendation",
        "r1_ok": True,
        "r1_uncovered": [],
        "r2_ok": True,
        "r2_missing_dimensions": [],
        "r3_ok": True,
        "r4_ok": True,
    }


@pytest.mark.asyncio
async def test_choice_b_deepen_runs_second_round_then_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = rt.RoundtableOrchestrator(
        model_adapter=SimpleNamespace(supports_model=lambda _model_id: True),
        config=SimpleNamespace(),
    )

    async def _fake_fan_out(*_args, **_kwargs):
        yield _opinion("m1", "专家A")
        yield _opinion("m2", "专家B")

    async def _fake_map(*_args, **_kwargs):
        return rt.DisputeMap(
            contention_points=[
                rt.ContentionPoint(
                    topic="争议点 1",
                    severity="high",
                    suggested_focus=True,
                    sides=[rt.ContentionSide(position="支持", lead_expert="专家A")],
                ),
            ]
        )

    async def _fake_debate(*_args, round_num: int, **_kwargs):
        yield rt.DebateStarted(round_num=round_num, assignments={"m1": "main_debater", "m2": "reviewer"})
        rebuttal = _rebuttal(round_num, "m1", "专家A")
        yield rt.RebuttalDone(rebuttal, done_count=1, total_count=2)
        done = rt.DebateComplete(round_num=round_num, stance_changes=[])
        done._rebuttals = [rebuttal]  # type: ignore[attr-defined]
        yield done

    async def _fake_packet(*_args, rebuttals=None, **_kwargs):
        return rt.DecisionPacket(
            final_summary=f"rebuttals={len(rebuttals or [])}",
            conclusion_type="recommendation",
            confidence_basis="ok",
        )

    monkeypatch.setattr(orch, "_fan_out_experts_stream", _fake_fan_out)
    monkeypatch.setattr(orch, "_map_disputes", _fake_map)
    monkeypatch.setattr(orch, "_run_debate_round", _fake_debate)
    monkeypatch.setattr(orch, "_build_decision_packet", _fake_packet)

    session_id_holder: dict[str, str] = {}
    events = await _collect_events(
        orch.execute_streaming(
            "该不该投火星计划？",
            owner_user_id="u1",
            rt_config=rt.RoundtableConfig(total_timeout_s=5, interactive=True),
        ),
        session_id_holder,
        [
          rt.UserChoice(action="deepen", choice_point="A"),
          rt.UserChoice(action="deepen", choice_point="B"),
        ],
    )

    debate_rounds = [event.round_num for event in events if isinstance(event, rt.DebateStarted)]
    assert debate_rounds == [1, 2]

    last = events[-1]
    assert isinstance(last, rt.RoundtableComplete)
    assert last.result.rounds_completed == 3
    assert last.result.decision_packet.final_summary == "rebuttals=2"


@pytest.mark.asyncio
async def test_resume_streaming_from_awaiting_a_reconnects_choice_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orch = rt.RoundtableOrchestrator(
        model_adapter=SimpleNamespace(supports_model=lambda _model_id: True),
        config=SimpleNamespace(),
    )

    session = rt.RoundtableSession("sid-resume-a", owner_user_id="u1")
    session.question = "火星计划值得继续投吗？"
    session.expert_count = 2
    session.experts = [_opinion("m1", "专家A"), _opinion("m2", "专家B")]
    session.s1_success_count = 2
    session.dispute_map = rt.DisputeMap(
        contention_points=[
            rt.ContentionPoint(
                topic="争议点 1",
                severity="high",
                suggested_focus=True,
                sides=[rt.ContentionSide(position="支持", lead_expert="专家A")],
            ),
        ]
    )
    session._state = rt.SessionState.AWAITING_A
    session.choice_point = "A"
    rt._sessions[session.session_id] = session

    async def _fake_debate(*_args, round_num: int, **_kwargs):
        yield rt.DebateStarted(round_num=round_num, assignments={"m1": "main_debater", "m2": "reviewer"})
        rebuttal = _rebuttal(round_num, "m1", "专家A")
        yield rt.RebuttalDone(rebuttal, done_count=1, total_count=2)
        done = rt.DebateComplete(round_num=round_num, stance_changes=[])
        done._rebuttals = [rebuttal]  # type: ignore[attr-defined]
        yield done

    async def _fake_packet(*_args, rebuttals=None, **_kwargs):
        return rt.DecisionPacket(
            final_summary=f"resume rebuttals={len(rebuttals or [])}",
            conclusion_type="recommendation",
            confidence_basis="ok",
        )

    monkeypatch.setattr(orch, "_run_debate_round", _fake_debate)
    monkeypatch.setattr(orch, "_build_decision_packet", _fake_packet)

    events: list[rt.RoundtableEvent] = []
    try:
        async for event in orch.execute_streaming(
            session.question,
            owner_user_id="u1",
            session_id=session.session_id,
            rt_config=rt.RoundtableConfig(total_timeout_s=5, interactive=True),
        ):
            events.append(event)
            if isinstance(event, rt.AwaitingUserChoice) and event.choice_point == "A":
                await session._choice_queue.put(rt.UserChoice(action="deepen", choice_point="A"))
            elif isinstance(event, rt.AwaitingUserChoice) and event.choice_point == "B":
                await session._choice_queue.put(rt.UserChoice(action="conclude", choice_point="B"))
    finally:
        rt._sessions.pop(session.session_id, None)

    assert any(isinstance(event, rt.DebateStarted) and event.round_num == 1 for event in events)
    last = events[-1]
    assert isinstance(last, rt.RoundtableComplete)
    assert last.result.decision_packet.final_summary == "resume rebuttals=1"


@pytest.mark.asyncio
async def test_decision_packet_allows_majority_experts_without_forced_degrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orch = rt.RoundtableOrchestrator(
        model_adapter=SimpleNamespace(supports_model=lambda _model_id: True),
        config=SimpleNamespace(),
    )

    opinions = [
        _opinion("m1", "专家A"),
        _opinion("m2", "专家B"),
        _opinion("m3", "专家C"),
        _opinion("m4", "专家D", success=False),
        _opinion("m5", "专家E", success=False),
    ]
    dispute_map = rt.DisputeMap()
    eligibility = orch._check_recommendation_eligibility(opinions, dispute_map, [], [])

    async def _fake_call_model(**_kwargs):
        return SimpleNamespace(
            success=True,
            content=json.dumps(
                {
                    "conclusion_type": "recommendation",
                    "confidence_basis": "three experts are enough",
                    "final_summary": "可以直接给出建议",
                    "stance_evolution": [],
                    "options": [],
                    "unresolved": [],
                    "what_changes_my_mind": "",
                    "recommended_action": "继续推进",
                    "value_disputes_to_user": [],
                },
                ensure_ascii=False,
            ),
        )

    monkeypatch.setattr(orch, "_call_model", _fake_call_model)

    packet = await orch._build_decision_packet(
        "火星计划值得继续投吗？",
        opinions,
        dispute_map,
        [],
        "sid-three-experts",
        [],
        [],
        eligibility,
    )

    assert rt.DEFAULT_EXPERT_COUNT == len(rt.ROUNDTABLE_EXPERTS) == 5
    assert rt.MIN_SUCCESSFUL_EXPERTS == 3
    assert eligibility["r4_ok"] is True
    assert eligibility["conclusion_type"] == "recommendation"
    assert packet.degraded is False
    assert packet.degradation_reason == ""
    assert packet.conclusion_type == "recommendation"


def test_recommendation_eligibility_drops_to_draft_below_majority() -> None:
    orch = rt.RoundtableOrchestrator(
        model_adapter=SimpleNamespace(supports_model=lambda _model_id: True),
        config=SimpleNamespace(),
    )

    opinions = [
        _opinion("m1", "专家A"),
        _opinion("m2", "专家B"),
        _opinion("m3", "专家C", success=False),
        _opinion("m4", "专家D", success=False),
        _opinion("m5", "专家E", success=False),
    ]

    eligibility = orch._check_recommendation_eligibility(opinions, rt.DisputeMap(), [], [])

    assert rt.MIN_SUCCESSFUL_EXPERTS == 3
    assert eligibility["r4_ok"] is False
    assert eligibility["conclusion_type"] == "draft"


def test_roundtable_expert_pool_expands_to_five_models() -> None:
    assert list(rt.ROUNDTABLE_EXPERTS) == [
        "claude_opus_thinking",
        "deepseek_reasoner",
        "kimi",
        "gemini_31_pro_thinking",
        "perplexity_sonar_pro",
    ]
    assert rt.DEFAULT_EXPERT_COUNT == 5
    assert rt.MIN_SUCCESSFUL_EXPERTS == 3
    assert rt.ROUNDTABLE_EXPERTS["deepseek_reasoner"]["style"] == "analytical"
    assert rt.ROUNDTABLE_EXPERTS["kimi"]["style"] == "research"
    assert [key for key in rt._STYLE_HINTS if key in {"analytical", "research"}] == [
        "analytical",
        "research",
    ]


def test_should_auto_draft_uses_min_successful_experts_threshold() -> None:
    session = rt.RoundtableSession("sid-auto-draft", owner_user_id="u1")
    session._state = rt.SessionState.AWAITING_A
    session._last_event_at = time.monotonic() - rt.AUTO_DRAFT_TIMEOUT_S - 1
    session.s1_success_count = rt.MIN_SUCCESSFUL_EXPERTS - 1

    assert session.should_auto_draft() is False

    session.s1_success_count = rt.MIN_SUCCESSFUL_EXPERTS

    assert session.should_auto_draft() is True
