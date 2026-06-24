from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import agoracle.api.app as app_module
import agoracle.api.routes.query as query_module
from agoracle.domain.types import Mode, OutputDepth, QueryContext, QueryResult, QuestionType
from agoracle.services.streaming import PipelineComplete, PipelineError


class _QuotaStub:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    def check_quota(self, user_id: int, mode: str):
        return None

    def record_usage(self, user_id: int, mode: str) -> None:
        self.calls.append((user_id, mode))

    def get_lifetime_usage(self, _user_id: int) -> dict[str, int]:
        return {}


def _make_error_result(model_called: bool) -> QueryResult:
    return QueryResult(
        query_id="q_bill_error",
        question="最新新闻怎么样？",
        mode="light",
        resolved_mode="light",
        final_answer="系统错误: pipeline failed",
        quality_gate_result="low_confidence",
        contributor_count=1 if model_called else 0,
        total_model_calls=1 if model_called else 0,
        estimated_cost_usd=0.01 if model_called else 0.0,
    )


def _make_success_result() -> QueryResult:
    return QueryResult(
        query_id="q_bill_ok",
        question="最新新闻怎么样？",
        mode="light",
        resolved_mode="light",
        final_answer="正常完成",
        quality_gate_result="best_single",
        contributor_count=1,
        total_model_calls=1,
        estimated_cost_usd=0.01,
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


@pytest.fixture
def _client_and_quota(monkeypatch: pytest.MonkeyPatch):
    @asynccontextmanager
    async def _noop_lifespan(_app):
        yield

    def _fake_build_context(req, user_id: int = 0) -> QueryContext:
        return QueryContext(
            query_id="q_billing_semantics",
            question=req.question,
            mode=Mode.LIGHT,
            resolved_mode=Mode.LIGHT,
            web_search_enabled=req.web_search,
            output_depth=OutputDepth.LEVEL_1,
            question_type=QuestionType.UNKNOWN,
        )

    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    quota = _QuotaStub()
    monkeypatch.setattr(app_module, "lifespan", _noop_lifespan)
    monkeypatch.setattr(query_module, "_get_user_id", lambda _request: 1001)
    monkeypatch.setattr(query_module, "_build_context", _fake_build_context)

    app_module.state.config = SimpleNamespace(
        modes={"light": SimpleNamespace(preflight_clarity_check=False)},
        features=SimpleNamespace(
            companion_hints=False,
            supplement_restart=False,
            roundtable_enabled=False,
        ),
    )
    app_module.state.quota_service = quota
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
    client = TestClient(app)
    try:
        yield client, quota
    finally:
        client.close()


def test_ask_error_without_model_calls_not_billed(
    monkeypatch: pytest.MonkeyPatch,
    _client_and_quota,
) -> None:
    client, quota = _client_and_quota

    class _FakeOrchestrator:
        def __init__(self, **kwargs):
            pass

        async def execute(self, context):
            return _make_error_result(model_called=False)

    monkeypatch.setattr(query_module, "Orchestrator", _FakeOrchestrator)

    resp = client.post("/api/ask", json={"question": "最新新闻怎么样？", "mode": "light", "web_search": True})
    assert resp.status_code == 200
    assert quota.calls == []


def test_ask_error_with_model_calls_billed(
    monkeypatch: pytest.MonkeyPatch,
    _client_and_quota,
) -> None:
    client, quota = _client_and_quota

    class _FakeOrchestrator:
        def __init__(self, **kwargs):
            pass

        async def execute(self, context):
            return _make_error_result(model_called=True)

    monkeypatch.setattr(query_module, "Orchestrator", _FakeOrchestrator)

    resp = client.post("/api/ask", json={"question": "最新新闻怎么样？", "mode": "light", "web_search": True})
    assert resp.status_code == 200
    assert quota.calls == [(1001, "light")]


def test_ask_normal_complete_billed(
    monkeypatch: pytest.MonkeyPatch,
    _client_and_quota,
) -> None:
    client, quota = _client_and_quota

    class _FakeOrchestrator:
        def __init__(self, **kwargs):
            pass

        async def execute(self, context):
            return _make_success_result()

    monkeypatch.setattr(query_module, "Orchestrator", _FakeOrchestrator)

    resp = client.post("/api/ask", json={"question": "最新新闻怎么样？", "mode": "light", "web_search": True})
    assert resp.status_code == 200
    assert quota.calls == [(1001, "light")]


def test_ask_stream_error_without_model_calls_not_billed(
    monkeypatch: pytest.MonkeyPatch,
    _client_and_quota,
) -> None:
    client, quota = _client_and_quota

    async def _fake_execute_streaming(**kwargs):
        yield PipelineError(
            "pipeline_answer_error",
            billable=True,  # should be overridden by result-based gate
            billed_mode="light",
            result=_make_error_result(model_called=False),
        )

    monkeypatch.setattr(query_module, "execute_streaming", _fake_execute_streaming)

    resp = client.post(
        "/api/ask/stream",
        json={"question": "最新新闻怎么样？", "mode": "light", "web_search": True},
        headers={"accept": "text/event-stream"},
    )
    assert resp.status_code == 200
    assert "event: error" in resp.text
    assert quota.calls == []


def test_ask_stream_error_with_model_calls_billed(
    monkeypatch: pytest.MonkeyPatch,
    _client_and_quota,
) -> None:
    client, quota = _client_and_quota

    async def _fake_execute_streaming(**kwargs):
        yield PipelineError(
            "pipeline_answer_error",
            billable=False,  # should be overridden by result-based gate
            billed_mode="light",
            result=_make_error_result(model_called=True),
        )

    monkeypatch.setattr(query_module, "execute_streaming", _fake_execute_streaming)

    resp = client.post(
        "/api/ask/stream",
        json={"question": "最新新闻怎么样？", "mode": "light", "web_search": True},
        headers={"accept": "text/event-stream"},
    )
    assert resp.status_code == 200
    assert "event: error" in resp.text
    assert quota.calls == [(1001, "light")]


def test_ask_stream_normal_complete_billed(
    monkeypatch: pytest.MonkeyPatch,
    _client_and_quota,
) -> None:
    client, quota = _client_and_quota

    async def _fake_execute_streaming(**kwargs):
        yield PipelineComplete(_make_success_result())

    monkeypatch.setattr(query_module, "execute_streaming", _fake_execute_streaming)

    resp = client.post(
        "/api/ask/stream",
        json={"question": "最新新闻怎么样？", "mode": "light", "web_search": True},
        headers={"accept": "text/event-stream"},
    )
    assert resp.status_code == 200
    assert "event: complete" in resp.text
    assert quota.calls == [(1001, "light")]
