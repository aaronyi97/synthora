from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import agoracle.api.app as app_module
from agoracle.api.routes import misc as misc_module
from agoracle.services import roundtable_orchestrator as rt


class _QuotaStub:
    def check_quota(self, user_id: int, mode: str):
        return None

    def record_usage(self, user_id: int, mode: str) -> None:
        return None


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    @asynccontextmanager
    async def _noop_lifespan(_app):
        yield

    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    monkeypatch.setattr(app_module, "lifespan", _noop_lifespan)
    monkeypatch.setattr(misc_module, "_get_user_id", lambda _request: 1001)

    app_module.state.config = SimpleNamespace(
        features=SimpleNamespace(roundtable_enabled=True),
    )
    app_module.state.quota_service = _QuotaStub()
    app_module.state.model_adapter = object()
    app_module.state.judge = object()
    app_module.state.extractor = object()
    app_module.state.prompt_loader = None
    app_module.state.event_bus = None
    app_module.state.search_service = None
    app_module.state.profile_store = None
    app_module.state.session_store = None
    app_module.state.user_store = None
    app_module.state.behavior_analytics = None
    app_module.state.proactive_coach = None
    app_module.state.conversation_store = None

    app = app_module.create_app()
    tc = TestClient(app, raise_server_exceptions=False)
    try:
        yield tc
    finally:
        tc.close()


def _make_session(session_id: str, owner_user_id: int, state: rt.SessionState) -> rt.RoundtableSession:
    session = rt.RoundtableSession(session_id=session_id, owner_user_id=owner_user_id)
    session._state = state
    return session


def test_roundtable_resume_auto_draft_available(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _make_session("sid-auto-draft", owner_user_id=1001, state=rt.SessionState.AUTO_DRAFT_SENT)
    session._pre_draft_state = rt.SessionState.AWAITING_B
    session.auto_draft_packet = rt.DecisionPacket(
        final_summary="auto draft summary",
        degraded=True,
        degradation_reason="auto_draft",
    )

    monkeypatch.setattr(rt, "get_session", lambda _sid: session)

    res = client.get("/api/roundtable/sid-auto-draft/resume")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "auto_draft_available"
    assert body["session_id"] == "sid-auto-draft"
    assert body["choice_point"] == "B"
    assert "decision_packet" in body
    assert body["decision_packet"]["final_summary"] == "auto draft summary"


def test_roundtable_resume_non_terminal_session_active(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _make_session("sid-active", owner_user_id=1001, state=rt.SessionState.AWAITING_A)
    monkeypatch.setattr(rt, "get_session", lambda _sid: session)

    res = client.get("/api/roundtable/sid-active/resume")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "session_active"
    assert body["state"] == "awaiting_A"


def test_roundtable_resume_terminal_session_returns_410(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _make_session("sid-complete", owner_user_id=1001, state=rt.SessionState.COMPLETE)
    monkeypatch.setattr(rt, "get_session", lambda _sid: session)

    res = client.get("/api/roundtable/sid-complete/resume")
    assert res.status_code == 410
    assert res.json()["detail"] == "session_ended"


def test_roundtable_resume_owner_mismatch_returns_403(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    session = _make_session("sid-owner-mismatch", owner_user_id=2002, state=rt.SessionState.AWAITING_A)
    monkeypatch.setattr(rt, "get_session", lambda _sid: session)

    res = client.get("/api/roundtable/sid-owner-mismatch/resume")
    assert res.status_code == 403
    assert res.json()["detail"] == "forbidden"


def test_roundtable_resume_session_not_found_returns_403(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    monkeypatch.setattr(rt, "get_session", lambda _sid: None)

    res = client.get("/api/roundtable/sid-not-found/resume")
    assert res.status_code == 403
    assert res.json()["detail"] == "forbidden"
