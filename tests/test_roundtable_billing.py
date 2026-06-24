from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sse_starlette.sse import AppStatus

import agoracle.api.app as app_module
from agoracle.api.routes import misc as misc_module
from agoracle.api.routes import query as query_module
from agoracle.domain.types import Mode, QueryContext, QueryResult
from agoracle.services import roundtable_orchestrator as rt


class _QuotaStub:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    def check_quota(self, user_id: int, mode: str):
        return None

    def record_usage(self, user_id: int, mode: str) -> None:
        self.calls.append((user_id, mode))


class _TrackerModelAdapter:
    def __init__(self, tracker) -> None:
        self._tracker = tracker

    def get_cost_tracker(self):
        return self._tracker


@pytest.fixture(autouse=True)
def _stabilize_sse_exit_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid cross-test loop binding in sse-starlette shutdown watcher."""
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
def _client_and_quota(monkeypatch: pytest.MonkeyPatch):
    @asynccontextmanager
    async def _noop_lifespan(_app):
        yield

    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    quota = _QuotaStub()
    monkeypatch.setattr(app_module, "lifespan", _noop_lifespan)
    monkeypatch.setattr(misc_module, "_get_user_id", lambda _request: 1001)
    monkeypatch.setattr(query_module, "_get_user_id", lambda _request: 1001)
    # sse-starlette keeps AppStatus.should_exit_event as a process-global.
    # Reset it per test so each TestClient loop gets a fresh event binding.
    AppStatus.should_exit = False
    AppStatus.should_exit_event = None

    app_module.state.config = SimpleNamespace(
        features=SimpleNamespace(roundtable_enabled=True)
    )
    app_module.state.quota_service = quota
    app_module.state.model_adapter = object()
    app_module.state.judge = object()
    app_module.state.extractor = object()
    app_module.state.event_bus = None
    app_module.state.search_service = None
    app_module.state.prompt_loader = None
    app_module.state.profile_store = None
    app_module.state.behavior_analytics = None
    app_module.state.conversation_store = None
    app_module.state.user_store = None

    app = app_module.create_app()
    client = TestClient(app)
    try:
        yield client, quota
    finally:
        client.close()
        AppStatus.should_exit = False
        AppStatus.should_exit_event = None


def test_roundtable_early_failure_no_charge(
    monkeypatch: pytest.MonkeyPatch,
    _client_and_quota,
) -> None:
    client, quota = _client_and_quota

    async def _fake_stream(self, question, owner_user_id, **kwargs):
        yield rt.RoundtableError("圆桌讨论需要至少2个可用专家模型", billable=False)

    monkeypatch.setattr(rt.RoundtableOrchestrator, "execute_streaming", _fake_stream)

    res = client.post(
        "/api/roundtable/start",
        json={"question": "要不要辞职？"},
        headers={"accept": "text/event-stream"},
    )

    assert res.status_code == 200
    assert "event: roundtable_error" in res.text
    assert quota.calls == []


def test_roundtable_model_called_then_exception_is_chargeable(
    monkeypatch: pytest.MonkeyPatch,
    _client_and_quota,
) -> None:
    client, quota = _client_and_quota

    async def _fake_stream(self, question, owner_user_id, **kwargs):
        opinion = rt.ExpertOpinion(
            model_id="m1", label="专家1", stance="支持", confidence=0.7
        )
        yield rt.ExpertDone(opinion, done_count=1, total_count=5)
        raise RuntimeError("boom-after-model-call")
        yield  # pragma: no cover

    monkeypatch.setattr(rt.RoundtableOrchestrator, "execute_streaming", _fake_stream)

    res = client.post(
        "/api/roundtable/start",
        json={"question": "要不要辞职？"},
        headers={"accept": "text/event-stream"},
    )

    assert res.status_code == 200
    assert "event: expert_done" in res.text
    assert "event: roundtable_error" in res.text
    assert quota.calls == [(1001, "roundtable")]


def test_roundtable_auto_draft_is_chargeable(
    monkeypatch: pytest.MonkeyPatch,
    _client_and_quota,
) -> None:
    client, quota = _client_and_quota

    async def _fake_stream(self, question, owner_user_id, **kwargs):
        packet = rt.DecisionPacket(
            final_summary="auto draft summary",
            conclusion_type="draft",
            confidence_basis="timeout",
            degraded=True,
            degradation_reason="auto_draft",
        )
        yield rt.AutoDraft(packet)

    monkeypatch.setattr(rt.RoundtableOrchestrator, "execute_streaming", _fake_stream)

    res = client.post(
        "/api/roundtable/start",
        json={"question": "要不要辞职？"},
        headers={"accept": "text/event-stream"},
    )

    assert res.status_code == 200
    assert "event: auto_draft" in res.text
    assert quota.calls == [(1001, "roundtable")]


def test_roundtable_stream_exception_before_model_call_not_chargeable(
    monkeypatch: pytest.MonkeyPatch,
    _client_and_quota,
) -> None:
    client, quota = _client_and_quota

    async def _fake_stream(self, question, owner_user_id, **kwargs):
        raise RuntimeError("boom-before-model-call")
        yield  # pragma: no cover

    monkeypatch.setattr(rt.RoundtableOrchestrator, "execute_streaming", _fake_stream)

    res = client.post(
        "/api/roundtable/start",
        json={"question": "要不要辞职？"},
        headers={"accept": "text/event-stream"},
    )

    assert res.status_code == 200
    assert "event: roundtable_error" in res.text
    assert quota.calls == []


def _fake_context(req, user_id: int = 0) -> QueryContext:
    return QueryContext(
        query_id="q-billing",
        question=req.question,
        mode=Mode.DEEP,
        resolved_mode=Mode.DEEP,
        user_id=user_id,
    )


def test_ask_non_stream_error_without_model_calls_not_chargeable(
    monkeypatch: pytest.MonkeyPatch,
    _client_and_quota,
) -> None:
    client, quota = _client_and_quota
    monkeypatch.setattr(query_module, "_build_context", _fake_context)

    error_result = QueryResult(
        query_id="q1",
        question="test",
        mode="deep",
        resolved_mode="deep",
        final_answer="系统错误: test_error",
        total_model_calls=0,
        estimated_cost_usd=0.0,
    )

    class _FakeOrchestrator:
        def __init__(self, **kwargs) -> None:
            pass

        async def execute(self, context):
            return error_result

    monkeypatch.setattr(query_module, "Orchestrator", _FakeOrchestrator)

    res = client.post("/api/ask", json={"question": "x", "mode": "deep", "skip_preflight": True})
    assert res.status_code == 200
    assert "系统错误" in res.json().get("final_answer", "")
    assert quota.calls == []


def test_ask_non_stream_error_with_model_calls_is_chargeable(
    monkeypatch: pytest.MonkeyPatch,
    _client_and_quota,
) -> None:
    client, quota = _client_and_quota
    monkeypatch.setattr(query_module, "_build_context", _fake_context)

    error_result = QueryResult(
        query_id="q2",
        question="test",
        mode="deep",
        resolved_mode="deep",
        final_answer="系统错误: after_model_call",
        total_model_calls=1,
        estimated_cost_usd=0.01,
    )

    class _FakeOrchestrator:
        def __init__(self, **kwargs) -> None:
            pass

        async def execute(self, context):
            return error_result

    monkeypatch.setattr(query_module, "Orchestrator", _FakeOrchestrator)

    res = client.post("/api/ask", json={"question": "x", "mode": "deep", "skip_preflight": True})
    assert res.status_code == 200
    assert quota.calls == [(1001, "deep")]


def test_ask_non_stream_complete_is_chargeable(
    monkeypatch: pytest.MonkeyPatch,
    _client_and_quota,
) -> None:
    client, quota = _client_and_quota
    monkeypatch.setattr(query_module, "_build_context", _fake_context)

    ok_result = QueryResult(
        query_id="q3",
        question="test",
        mode="deep",
        resolved_mode="deep",
        final_answer="normal answer",
        total_model_calls=1,
        estimated_cost_usd=0.02,
    )

    class _FakeOrchestrator:
        def __init__(self, **kwargs) -> None:
            pass

        async def execute(self, context):
            return ok_result

    monkeypatch.setattr(query_module, "Orchestrator", _FakeOrchestrator)

    res = client.post("/api/ask", json={"question": "x", "mode": "deep", "skip_preflight": True})
    assert res.status_code == 200
    assert quota.calls == [(1001, "deep")]


def test_ask_non_stream_exception_without_model_calls_not_chargeable(
    monkeypatch: pytest.MonkeyPatch,
    _client_and_quota,
) -> None:
    client, quota = _client_and_quota
    monkeypatch.setattr(query_module, "_build_context", _fake_context)
    app_module.state.model_adapter = _TrackerModelAdapter([])

    class _FakeOrchestrator:
        def __init__(self, **kwargs) -> None:
            pass

        async def execute(self, context):
            raise RuntimeError("pipeline_boom")

    monkeypatch.setattr(query_module, "Orchestrator", _FakeOrchestrator)

    with pytest.raises(RuntimeError, match="pipeline_boom"):
        client.post("/api/ask", json={"question": "x", "mode": "deep", "skip_preflight": True})
    assert quota.calls == []


def test_ask_non_stream_exception_with_model_calls_is_chargeable(
    monkeypatch: pytest.MonkeyPatch,
    _client_and_quota,
) -> None:
    client, quota = _client_and_quota
    monkeypatch.setattr(query_module, "_build_context", _fake_context)
    app_module.state.model_adapter = _TrackerModelAdapter([
        ("call_1", "deep_model", 1024, 0.03),
    ])

    class _FakeOrchestrator:
        def __init__(self, **kwargs) -> None:
            pass

        async def execute(self, context):
            raise RuntimeError("pipeline_boom_after_model")

    monkeypatch.setattr(query_module, "Orchestrator", _FakeOrchestrator)

    with pytest.raises(RuntimeError, match="pipeline_boom_after_model"):
        client.post("/api/ask", json={"question": "x", "mode": "deep", "skip_preflight": True})
    assert quota.calls == [(1001, "deep")]


def test_ask_stream_error_without_model_calls_not_chargeable(
    monkeypatch: pytest.MonkeyPatch,
    _client_and_quota,
) -> None:
    client, quota = _client_and_quota
    monkeypatch.setattr(query_module, "_build_context", _fake_context)

    async def _fake_stream(**kwargs):
        yield app_module.PipelineError("pipeline_answer_error", billable=False, billed_mode="deep")

    monkeypatch.setattr(query_module, "execute_streaming", _fake_stream)

    res = client.post(
        "/api/ask/stream",
        json={"question": "x", "mode": "deep", "skip_preflight": True},
        headers={"accept": "text/event-stream"},
    )
    assert res.status_code == 200
    assert "event: error" in res.text
    assert quota.calls == []


def test_ask_stream_error_with_model_calls_is_chargeable(
    monkeypatch: pytest.MonkeyPatch,
    _client_and_quota,
) -> None:
    client, quota = _client_and_quota
    monkeypatch.setattr(query_module, "_build_context", _fake_context)

    async def _fake_stream(**kwargs):
        yield app_module.PipelineError("pipeline_answer_error", billable=True, billed_mode="deep")

    monkeypatch.setattr(query_module, "execute_streaming", _fake_stream)

    res = client.post(
        "/api/ask/stream",
        json={"question": "x", "mode": "deep", "skip_preflight": True},
        headers={"accept": "text/event-stream"},
    )
    assert res.status_code == 200
    assert "event: error" in res.text
    assert quota.calls == [(1001, "deep")]


def test_ask_stream_complete_is_chargeable(
    monkeypatch: pytest.MonkeyPatch,
    _client_and_quota,
) -> None:
    client, quota = _client_and_quota
    monkeypatch.setattr(query_module, "_build_context", _fake_context)

    ok_result = QueryResult(
        query_id="q4",
        question="test",
        mode="deep",
        resolved_mode="deep",
        final_answer="ok",
        total_model_calls=1,
        estimated_cost_usd=0.02,
    )

    async def _fake_stream(**kwargs):
        yield app_module.PipelineComplete(ok_result)

    monkeypatch.setattr(query_module, "execute_streaming", _fake_stream)

    res = client.post(
        "/api/ask/stream",
        json={"question": "x", "mode": "deep", "skip_preflight": True},
        headers={"accept": "text/event-stream"},
    )
    assert res.status_code == 200
    assert "event: complete" in res.text
    assert quota.calls == [(1001, "deep")]
