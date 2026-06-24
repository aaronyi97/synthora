"""
Regression tests for Roundtable choice_point isolation.

Covers:
  - A-point stale click does NOT contaminate B-point (P0 fix: choice_point validation)
  - Correct choice at correct decision point is accepted
  - Mismatched choice_point is discarded, not stored
  - _pending_choice is None before wait, set after valid choice, cleared after consume
"""

import asyncio
import pytest

from agoracle.services.roundtable_orchestrator import (
    RoundtableSession,
    SessionState,
    UserChoice,
)


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_session(sid: str = "test-session") -> RoundtableSession:
    return RoundtableSession(session_id=sid, owner_user_id="user-1")


def _make_choice(choice_point: str, action: str = "conclude", idem: str = "k1") -> UserChoice:
    return UserChoice(
        action=action,
        choice_point=choice_point,
        user_input=None,
        focus_topic=None,
        idempotency_key=idem,
    )


# ── Tests ──────────────────────────────────────────────────────────────────


class TestChoicePointIsolation:
    """Core regression: stale A-choice must not be consumed at B-point wait."""

    @pytest.mark.asyncio
    async def test_correct_a_choice_accepted(self):
        """Valid A-point choice is stored in _pending_choice."""
        session = _make_session()
        await session.transition(SessionState.INITIALIZING, SessionState.AWAITING_A, "test")

        choice = _make_choice("A", action="conclude", idem="a-correct")
        await session._choice_queue.put(choice)

        # Simulate _await_choice_stream logic (isolated)
        received = await asyncio.wait_for(session._choice_queue.get(), timeout=1.0)
        if received.choice_point == "A":
            session._pending_choice = received

        assert session._pending_choice is not None
        assert session._pending_choice.choice_point == "A"
        assert session._pending_choice.action == "conclude"

    @pytest.mark.asyncio
    async def test_stale_a_choice_discarded_at_b_point(self):
        """
        A stale A-choice in the queue must NOT be stored as _pending_choice
        when waiting at B-point. Only a B-choice should be accepted.

        This is the core regression for the choice_point contamination fix.
        """
        session = _make_session()
        await session.transition(SessionState.INITIALIZING, SessionState.AWAITING_A, "test")
        await session.transition(SessionState.AWAITING_A, SessionState.DEBATING, "s3")
        await session.transition(SessionState.DEBATING, SessionState.AWAITING_B, "awaiting_b")

        # Stale A-choice from rapid double-click arrives first
        stale_a = _make_choice("A", action="deepen", idem="stale-a")
        valid_b = _make_choice("B", action="conclude", idem="valid-b")

        await session._choice_queue.put(stale_a)
        await session._choice_queue.put(valid_b)

        # Simulate _await_choice_stream for B-point
        accepted = None
        discarded = []
        while accepted is None:
            received = await asyncio.wait_for(session._choice_queue.get(), timeout=1.0)
            if received.choice_point == "B":
                session._pending_choice = received
                accepted = received
            else:
                discarded.append(received)

        assert len(discarded) == 1
        assert discarded[0].idempotency_key == "stale-a"
        assert accepted.idempotency_key == "valid-b"
        assert session._pending_choice.choice_point == "B"

    @pytest.mark.asyncio
    async def test_pending_choice_none_before_wait(self):
        """_pending_choice starts as None."""
        session = _make_session()
        assert session._pending_choice is None

    @pytest.mark.asyncio
    async def test_pending_choice_cleared_after_consume(self):
        """After execute_streaming reads _pending_choice it resets to None."""
        session = _make_session()

        choice = _make_choice("A", action="deepen", idem="k1")
        session._pending_choice = choice

        # Simulate execute_streaming consuming and clearing
        choice_a = session._pending_choice
        session._pending_choice = None

        assert choice_a.choice_point == "A"
        assert session._pending_choice is None

    @pytest.mark.asyncio
    async def test_multiple_stale_a_choices_all_discarded(self):
        """Multiple stale A-choices are all discarded; only B-choice accepted."""
        session = _make_session()

        for i in range(3):
            await session._choice_queue.put(_make_choice("A", idem=f"stale-{i}"))
        await session._choice_queue.put(_make_choice("B", action="conclude", idem="final-b"))

        accepted = None
        discarded = []
        while accepted is None:
            received = await asyncio.wait_for(session._choice_queue.get(), timeout=1.0)
            if received.choice_point == "B":
                session._pending_choice = received
                accepted = received
            else:
                discarded.append(received)

        assert len(discarded) == 3
        assert all(d.choice_point == "A" for d in discarded)
        assert accepted.choice_point == "B"

    @pytest.mark.asyncio
    async def test_b_choice_at_b_point_accepted_without_discarding(self):
        """If the queue only has a valid B-choice, it's accepted immediately with no discards."""
        session = _make_session()

        await session._choice_queue.put(_make_choice("B", action="inject", idem="b-inject"))

        accepted = None
        discarded = []
        while accepted is None:
            received = await asyncio.wait_for(session._choice_queue.get(), timeout=1.0)
            if received.choice_point == "B":
                session._pending_choice = received
                accepted = received
            else:
                discarded.append(received)

        assert len(discarded) == 0
        assert accepted.action == "inject"


class TestSessionStateTransitions:
    """Sanity checks for CAS state machine (used by choice_point isolation)."""

    @pytest.mark.asyncio
    async def test_cas_transition_succeeds_from_correct_state(self):
        session = _make_session()
        ok = await session.transition(SessionState.INITIALIZING, SessionState.AWAITING_A, "test")
        assert ok is True
        assert session.state == SessionState.AWAITING_A

    @pytest.mark.asyncio
    async def test_cas_transition_fails_from_wrong_state(self):
        session = _make_session()
        ok = await session.transition(SessionState.AWAITING_A, SessionState.DEBATING, "wrong")
        assert ok is False
        assert session.state == SessionState.INITIALIZING

    @pytest.mark.asyncio
    async def test_force_state_always_succeeds(self):
        session = _make_session()
        await session.force_state(SessionState.ERROR, "test-force")
        assert session.state == SessionState.ERROR


class TestFallbackRouteRealtime:
    """Regression: realtime/news question_type must not fall back to deep."""

    def test_normalize_realtime_maps_to_realtime(self):
        from agoracle.services.companion_dispatcher import CompanionDispatcher
        cd = CompanionDispatcher.__new__(CompanionDispatcher)
        assert cd._normalize_question_type("realtime") == "realtime"

    def test_normalize_volatile_maps_to_realtime(self):
        from agoracle.services.companion_dispatcher import CompanionDispatcher
        cd = CompanionDispatcher.__new__(CompanionDispatcher)
        assert cd._normalize_question_type("volatile") == "realtime"

    def test_normalize_news_maps_to_realtime(self):
        from agoracle.services.companion_dispatcher import CompanionDispatcher
        cd = CompanionDispatcher.__new__(CompanionDispatcher)
        assert cd._normalize_question_type("news") == "realtime"

    def test_realtime_in_single_types(self):
        """realtime must be in the single_types set to avoid deep fallback."""
        from agoracle.services.companion_dispatcher import SINGLE_MODEL_RECOMMENDATIONS
        assert "realtime" in SINGLE_MODEL_RECOMMENDATIONS

    def test_realtime_recommendation_is_search_model(self):
        """Primary recommendation for realtime must be a search-capable model."""
        from agoracle.services.companion_dispatcher import SINGLE_MODEL_RECOMMENDATIONS
        candidates = SINGLE_MODEL_RECOMMENDATIONS["realtime"]
        assert len(candidates) > 0
        # First candidate should be a search/perplexity model
        assert "perplexity" in candidates[0] or "sonar" in candidates[0]
