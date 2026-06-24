from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sse_starlette.sse import AppStatus

import agoracle.api.app as app_module
from agoracle.api.routes import misc as misc_module
from agoracle.services import roundtable_orchestrator as rt


class _QuotaStub:
    def check_quota(self, user_id: int, mode: str):
        return None

    def record_usage(self, user_id: int, mode: str) -> None:
        return None


def _parse_sse_events(sse_text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    current_event = ""
    data_lines: list[str] = []

    def _flush() -> None:
        nonlocal current_event, data_lines
        if not current_event:
            data_lines = []
            return
        raw_data = "\n".join(data_lines)
        payload: Any = None
        if raw_data:
            try:
                payload = json.loads(raw_data)
            except json.JSONDecodeError:
                payload = raw_data
        events.append({"event": current_event, "data": payload})
        current_event = ""
        data_lines = []

    for raw_line in sse_text.splitlines():
        line = raw_line.rstrip("\r")
        if line == "":
            _flush()
            continue
        if line.startswith("event: "):
            if data_lines:
                _flush()
            current_event = line[7:].strip()
            continue
        if line.startswith("event:"):
            if data_lines:
                _flush()
            current_event = line[6:].strip()
            continue
        if line.startswith("data: "):
            data_lines.append(line[6:])
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:])
            continue

    _flush()
    return events


def _terminal_count(events: list[dict[str, Any]]) -> int:
    terminal = {"roundtable_complete", "roundtable_error", "auto_draft"}
    return sum(1 for e in events if e["event"] in terminal)


def _decision_packet(summary: str) -> rt.DecisionPacket:
    return rt.DecisionPacket(
        final_summary=summary,
        conclusion_type="recommendation",
        confidence_basis="contract_test",
    )


@pytest.fixture(autouse=True)
def _stabilize_sse_exit_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _wait_until_cancelled(*_args, **_kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    try:
        import sse_starlette.sse as _sse
    except Exception:
        return

    _resp_cls = getattr(_sse, "EventSourceResponse", None)
    if _resp_cls is not None:
        for _name in ("_listen_for_exit_signal", "listen_for_exit_signal"):
            if hasattr(_resp_cls, _name):
                monkeypatch.setattr(_resp_cls, _name, staticmethod(_wait_until_cancelled), raising=False)

    _app_status = getattr(_sse, "AppStatus", None)
    if _app_status is not None and hasattr(_app_status, "should_exit"):
        monkeypatch.setattr(_app_status, "should_exit", False, raising=False)


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

    AppStatus.should_exit = False
    AppStatus.should_exit_event = None
    app = app_module.create_app()
    tc = TestClient(app, raise_server_exceptions=False)
    try:
        yield tc
    finally:
        tc.close()
        AppStatus.should_exit = False
        AppStatus.should_exit_event = None


def test_roundtable_stream_complete_terminal_contract(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    async def _fake_stream(self, question, owner_user_id, **_kwargs):
        yield rt.RoundtableStarted("sid-complete", 2, question)
        result = rt.RoundtableResult(
            session_id="sid-complete",
            question=question,
            rounds_completed=1,
            decision_packet=_decision_packet("complete summary"),
        )
        yield rt.RoundtableComplete(result)

    monkeypatch.setattr(rt.RoundtableOrchestrator, "execute_streaming", _fake_stream)

    res = client.post(
        "/api/roundtable/start",
        json={"question": "Should I switch jobs?"},
        headers={"accept": "text/event-stream"},
    )
    assert res.status_code == 200

    events = _parse_sse_events(res.text)
    names = [e["event"] for e in events]
    assert "roundtable_complete" in names
    assert names.count("roundtable_complete") == 1
    assert _terminal_count(events) == 1
    assert names[-1] == "roundtable_complete"

    payload = next(e["data"] for e in events if e["event"] == "roundtable_complete")
    assert isinstance(payload, dict)
    assert payload.get("session_id") == "sid-complete"
    assert "decision_packet" in payload
    assert payload["decision_packet"].get("final_summary") == "complete summary"


def test_roundtable_stream_error_terminal_contract(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    async def _fake_stream(self, question, owner_user_id, **_kwargs):
        yield rt.RoundtableError(
            "roundtable_contract_error",
            billable=False,
            phase="S2",
            reason="moderator_timeout",
            detail="Moderator dispute mapping exceeded 15s",
        )

    monkeypatch.setattr(rt.RoundtableOrchestrator, "execute_streaming", _fake_stream)

    res = client.post(
        "/api/roundtable/start",
        json={"question": "Should I switch jobs?"},
        headers={"accept": "text/event-stream"},
    )
    assert res.status_code == 200

    events = _parse_sse_events(res.text)
    names = [e["event"] for e in events]
    assert "roundtable_error" in names
    assert names.count("roundtable_error") == 1
    assert _terminal_count(events) == 1
    assert names[-1] == "roundtable_error"

    payload = next(e["data"] for e in events if e["event"] == "roundtable_error")
    assert isinstance(payload, dict)
    assert payload.get("error") == "roundtable_contract_error"
    assert payload.get("code") == "roundtable_contract_error"
    assert payload.get("phase") == "S2"
    assert payload.get("reason") == "moderator_timeout"


def test_roundtable_stream_auto_draft_terminal_contract(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    async def _fake_stream(self, question, owner_user_id, **_kwargs):
        yield rt.AutoDraft(_decision_packet("auto draft summary"))

    monkeypatch.setattr(rt.RoundtableOrchestrator, "execute_streaming", _fake_stream)

    res = client.post(
        "/api/roundtable/start",
        json={"question": "Should I switch jobs?"},
        headers={"accept": "text/event-stream"},
    )
    assert res.status_code == 200

    events = _parse_sse_events(res.text)
    names = [e["event"] for e in events]
    assert "auto_draft" in names
    assert names.count("auto_draft") == 1
    assert _terminal_count(events) == 1
    assert names[-1] == "auto_draft"

    payload = next(e["data"] for e in events if e["event"] == "auto_draft")
    assert isinstance(payload, dict)
    assert "decision_packet" in payload
    assert payload["decision_packet"].get("final_summary") == "auto draft summary"
