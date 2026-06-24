from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sse_starlette.sse import AppStatus

import agoracle.api.app as app_module
import agoracle.api.routes.query as query_module
from agoracle.domain.types import Mode, QueryContext, QueryResult
from agoracle.services.streaming import PipelineComplete, PipelineError, StageStarted


class _QuotaStub:
    def check_quota(self, user_id: int, mode: str):
        return None

    def record_usage(self, user_id: int, mode: str) -> None:
        return None

    def get_lifetime_usage(self, user_id: int):
        return {}


def _fake_context(req, user_id: int = 1001) -> QueryContext:
    return QueryContext(
        query_id="q-stream-contract",
        question=req.question,
        mode=Mode.LIGHT,
        resolved_mode=Mode.LIGHT,
        user_id=user_id,
    )


def _parse_sse_events(sse_text: str) -> list[dict]:
    events: list[dict] = []
    current_event: str | None = None
    data_lines: list[str] = []

    def _flush() -> None:
        nonlocal current_event, data_lines
        if current_event is None:
            return
        payload_raw = "\n".join(data_lines)
        if payload_raw:
            try:
                payload = json.loads(payload_raw)
            except json.JSONDecodeError:
                payload = payload_raw
        else:
            payload = {}
        events.append({"event": current_event, "data": payload})
        current_event = None
        data_lines = []

    for raw_line in sse_text.splitlines():
        line = raw_line.rstrip("\r")
        if line == "":
            _flush()
            continue
        if line.startswith("event:"):
            if current_event is not None and data_lines:
                _flush()
            current_event = line.split(":", 1)[1].strip()
            continue
        if line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].lstrip(" "))

    _flush()
    return events


def _payloads(events: list[dict], event_name: str) -> list[dict]:
    return [e["data"] for e in events if e["event"] == event_name]


def _terminal_count(events: list[dict]) -> int:
    terminal_events = {"complete", "error"}
    return sum(1 for e in events if e["event"] in terminal_events)


def _fallback_required(events: list[dict]) -> bool:
    """Mirror frontend fallback trigger: no complete/error terminal was received."""
    return _terminal_count(events) == 0


@pytest.fixture(autouse=True)
def _reset_sse_app_status() -> None:
    AppStatus.should_exit = False
    AppStatus.should_exit_event = None
    yield
    AppStatus.should_exit = False
    AppStatus.should_exit_event = None


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    @asynccontextmanager
    async def _noop_lifespan(_app):
        yield

    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    monkeypatch.setattr(app_module, "lifespan", _noop_lifespan)
    monkeypatch.setattr(query_module, "_build_context", _fake_context)
    monkeypatch.setattr(query_module, "_get_user_id", lambda _request: 1001)

    app_module.state.config = type(
        "_Cfg",
        (),
        {
            "features": type(
                "_Features",
                (),
                {
                    "companion_hints": False,
                    "preference_injection": False,
                    "supplement_restart": False,
                    "roundtable_enabled": True,
                },
            )(),
            "modes": {},
        },
    )()
    app_module.state.model_adapter = object()
    app_module.state.judge = object()
    app_module.state.extractor = object()
    app_module.state.prompt_loader = object()
    app_module.state.event_bus = None
    app_module.state.search_service = None
    app_module.state.profile_store = None
    app_module.state.session_store = None
    app_module.state.socratic_orch = None
    app_module.state.user_store = None
    app_module.state.behavior_analytics = None
    app_module.state.quota_service = _QuotaStub()
    app_module.state.proactive_coach = None
    app_module.state.conversation_store = None

    app = app_module.create_app()
    with TestClient(app) as tc:
        yield tc


def test_ask_stream_complete_contract(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    async def _fake_execute_streaming(**_kwargs):
        result = QueryResult(
            query_id="q-stream-complete",
            question="stream complete",
            mode="light",
            resolved_mode="light",
            final_answer="done",
            quality_gate_result="best_single",
            contributor_count=1,
            total_model_calls=1,
        )
        yield PipelineComplete(result)

    monkeypatch.setattr(query_module, "execute_streaming", _fake_execute_streaming)

    res = client.post(
        "/api/ask/stream",
        json={"question": "stream complete", "mode": "light", "skip_preflight": True},
        headers={"accept": "text/event-stream"},
    )

    assert res.status_code == 200
    events = _parse_sse_events(res.text)
    complete_payloads = _payloads(events, "complete")

    assert len(complete_payloads) == 1
    complete_data = complete_payloads[0]
    assert complete_data["query_id"] == "q-stream-complete"
    assert complete_data["mode"] == "light"
    assert complete_data["final_answer"] == "done"
    assert _terminal_count(events) == 1


def test_ask_stream_complete_persists_history(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    async def _fake_execute_streaming(**_kwargs):
        result = QueryResult(
            query_id="q-stream-history",
            question="stream history",
            mode="light",
            resolved_mode="light",
            final_answer="done",
            quality_gate_result="best_single",
            contributor_count=1,
            total_model_calls=1,
        )
        yield PipelineComplete(result)

    save_query = AsyncMock()
    get_by_api_key = AsyncMock(return_value={"id": 1001, "username": "tester", "is_admin": False})
    app_module.state.user_store = type(
        "_UserStore",
        (),
        {"save_query": save_query, "get_by_session_id": AsyncMock(return_value=None), "get_by_api_key": get_by_api_key},
    )()
    monkeypatch.setattr(query_module, "execute_streaming", _fake_execute_streaming)

    res = client.post(
        "/api/ask/stream",
        json={"question": "stream history", "mode": "light", "skip_preflight": True},
        headers={"accept": "text/event-stream", "authorization": "Bearer test-token"},
    )

    assert res.status_code == 200
    events = _parse_sse_events(res.text)
    assert len(_payloads(events, "complete")) == 1
    get_by_api_key.assert_awaited_once_with("test-token")
    save_query.assert_awaited_once()


def test_ask_stream_error_contract(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    async def _fake_execute_streaming(**_kwargs):
        yield PipelineError("pipeline_answer_error", billable=False, billed_mode="light")

    monkeypatch.setattr(query_module, "execute_streaming", _fake_execute_streaming)

    res = client.post(
        "/api/ask/stream",
        json={"question": "stream error", "mode": "light", "skip_preflight": True},
        headers={"accept": "text/event-stream"},
    )

    assert res.status_code == 200
    events = _parse_sse_events(res.text)
    error_payloads = _payloads(events, "error")

    assert len(error_payloads) == 1
    assert error_payloads[0]["error"] == "pipeline_answer_error"
    assert _terminal_count(events) == 1


def test_ask_stream_no_terminal_requires_fallback(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    async def _fake_execute_streaming(**_kwargs):
        yield StageStarted("fan_out", "running")
        await asyncio.sleep(0)

    monkeypatch.setattr(query_module, "execute_streaming", _fake_execute_streaming)

    res = client.post(
        "/api/ask/stream",
        json={"question": "stream no terminal", "mode": "light", "skip_preflight": True},
        headers={"accept": "text/event-stream"},
    )

    assert res.status_code == 200
    events = _parse_sse_events(res.text)
    stage_payloads = _payloads(events, "stage_start")

    assert len(stage_payloads) == 1
    assert stage_payloads[0]["stage"] == "fan_out"
    assert stage_payloads[0]["detail"] == "running"
    assert stage_payloads[0]["query_id"] == "q-stream-contract"
    assert _terminal_count(events) == 0
    assert _fallback_required(events) is True
