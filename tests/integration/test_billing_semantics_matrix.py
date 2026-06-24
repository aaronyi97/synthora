from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import agoracle.api.app as app_module
from agoracle.api.routes import query as query_module
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


def _make_result(
    *,
    final_answer: str,
    is_clarify: bool = False,
    model_called: bool = True,
) -> QueryResult:
    return QueryResult(
        query_id="q-billing-matrix",
        question="计费语义矩阵测试",
        mode="light",
        resolved_mode="light",
        final_answer=final_answer,
        quality_gate_result="best_single",
        contributor_count=1 if model_called else 0,
        total_model_calls=1 if model_called else 0,
        estimated_cost_usd=0.01 if model_called else 0.0,
        is_clarify=is_clarify,
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
def _client_and_quota(monkeypatch: pytest.MonkeyPatch):
    @asynccontextmanager
    async def _noop_lifespan(_app):
        yield

    def _fake_build_context(req, user_id: int = 0) -> QueryContext:
        return QueryContext(
            query_id="q-billing-matrix-context",
            question=req.question,
            mode=Mode.LIGHT,
            resolved_mode=Mode.LIGHT,
            web_search_enabled=req.web_search,
            output_depth=OutputDepth.LEVEL_1,
            question_type=QuestionType.UNKNOWN,
            user_id=user_id,
        )

    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    monkeypatch.setattr(app_module, "lifespan", _noop_lifespan)
    monkeypatch.setattr(query_module, "_get_user_id", lambda _request: 1001)
    monkeypatch.setattr(query_module, "_build_context", _fake_build_context)

    quota = _QuotaStub()
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
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield client, quota
    finally:
        client.close()


def _ask_payload() -> dict[str, object]:
    return {
        "question": "最新新闻怎么样？",
        "mode": "light",
        "web_search": True,
        "skip_preflight": True,
    }


def _post_stream(client: TestClient) -> object:
    return client.post(
        "/api/ask/stream",
        json=_ask_payload(),
        headers={"accept": "text/event-stream"},
    )


def test_clarify_non_stream_and_stream_not_billed(
    monkeypatch: pytest.MonkeyPatch,
    _client_and_quota,
) -> None:
    client, quota = _client_and_quota
    clarify_result = _make_result(
        final_answer="请先补充你的城市与预算，我再给出建议。",
        is_clarify=True,
        model_called=True,
    )

    class _FakeOrchestrator:
        def __init__(self, **_kwargs):
            pass

        async def execute(self, _context):
            return clarify_result

    monkeypatch.setattr(query_module, "Orchestrator", _FakeOrchestrator)
    resp = client.post("/api/ask", json=_ask_payload())
    assert resp.status_code == 200
    assert quota.calls == []

    async def _fake_execute_streaming(**_kwargs):
        yield PipelineComplete(clarify_result)

    monkeypatch.setattr(query_module, "execute_streaming", _fake_execute_streaming)
    stream_resp = _post_stream(client)
    assert stream_resp.status_code == 200
    assert "event: complete" in stream_resp.text
    assert quota.calls == []


def test_normal_complete_non_stream_and_stream_billed(
    monkeypatch: pytest.MonkeyPatch,
    _client_and_quota,
) -> None:
    client, quota = _client_and_quota
    ok_result = _make_result(final_answer="这是正常完成的答案。", model_called=True)

    class _FakeOrchestrator:
        def __init__(self, **_kwargs):
            pass

        async def execute(self, _context):
            return ok_result

    monkeypatch.setattr(query_module, "Orchestrator", _FakeOrchestrator)
    resp = client.post("/api/ask", json=_ask_payload())
    assert resp.status_code == 200
    assert quota.calls == [(1001, "light")]

    quota.calls.clear()

    async def _fake_execute_streaming(**_kwargs):
        yield PipelineComplete(ok_result)

    monkeypatch.setattr(query_module, "execute_streaming", _fake_execute_streaming)
    stream_resp = _post_stream(client)
    assert stream_resp.status_code == 200
    assert "event: complete" in stream_resp.text
    assert quota.calls == [(1001, "light")]


def test_system_error_with_model_activity_non_stream_and_stream_billed(
    monkeypatch: pytest.MonkeyPatch,
    _client_and_quota,
) -> None:
    client, quota = _client_and_quota
    error_with_model = _make_result(
        final_answer="系统错误: downstream failed after model call",
        model_called=True,
    )

    class _FakeOrchestrator:
        def __init__(self, **_kwargs):
            pass

        async def execute(self, _context):
            return error_with_model

    monkeypatch.setattr(query_module, "Orchestrator", _FakeOrchestrator)
    resp = client.post("/api/ask", json=_ask_payload())
    assert resp.status_code == 200
    assert quota.calls == [(1001, "light")]

    quota.calls.clear()

    async def _fake_execute_streaming(**_kwargs):
        yield PipelineComplete(error_with_model)

    monkeypatch.setattr(query_module, "execute_streaming", _fake_execute_streaming)
    stream_resp = _post_stream(client)
    assert stream_resp.status_code == 200
    assert "event: complete" in stream_resp.text
    assert quota.calls == [(1001, "light")]


def test_system_error_without_model_activity_non_stream_and_stream_not_billed(
    monkeypatch: pytest.MonkeyPatch,
    _client_and_quota,
) -> None:
    client, quota = _client_and_quota
    error_without_model = _make_result(
        final_answer="系统错误: failed before model call",
        model_called=False,
    )

    class _FakeOrchestrator:
        def __init__(self, **_kwargs):
            pass

        async def execute(self, _context):
            return error_without_model

    monkeypatch.setattr(query_module, "Orchestrator", _FakeOrchestrator)
    resp = client.post("/api/ask", json=_ask_payload())
    assert resp.status_code == 200
    assert quota.calls == []

    async def _fake_execute_streaming(**_kwargs):
        yield PipelineComplete(error_without_model)

    monkeypatch.setattr(query_module, "execute_streaming", _fake_execute_streaming)
    stream_resp = _post_stream(client)
    assert stream_resp.status_code == 200
    assert "event: complete" in stream_resp.text
    assert quota.calls == []


def test_stream_pipeline_error_with_result_uses_unified_gate(
    monkeypatch: pytest.MonkeyPatch,
    _client_and_quota,
) -> None:
    client, quota = _client_and_quota

    async def _fake_without_model(**_kwargs):
        yield PipelineError(
            "pipeline_answer_error",
            billable=True,
            billed_mode="light",
            result=_make_result(
                final_answer="系统错误: no model activity",
                model_called=False,
            ),
        )

    monkeypatch.setattr(query_module, "execute_streaming", _fake_without_model)
    first_resp = _post_stream(client)
    assert first_resp.status_code == 200
    assert "event: error" in first_resp.text
    assert quota.calls == []

    async def _fake_with_model(**_kwargs):
        yield PipelineError(
            "pipeline_answer_error",
            billable=False,
            billed_mode="light",
            result=_make_result(
                final_answer="系统错误: has model activity",
                model_called=True,
            ),
        )

    monkeypatch.setattr(query_module, "execute_streaming", _fake_with_model)
    second_resp = _post_stream(client)
    assert second_resp.status_code == 200
    assert "event: error" in second_resp.text
    assert quota.calls == [(1001, "light")]


def test_stream_pipeline_error_without_result_uses_fallback_gate(
    monkeypatch: pytest.MonkeyPatch,
    _client_and_quota,
) -> None:
    client, quota = _client_and_quota

    async def _fake_billable_fallback_mode(**_kwargs):
        yield PipelineError(
            "pipeline_answer_error",
            billable=True,
            billed_mode="",
            result=None,
        )

    monkeypatch.setattr(query_module, "execute_streaming", _fake_billable_fallback_mode)
    first_resp = _post_stream(client)
    assert first_resp.status_code == 200
    assert "event: error" in first_resp.text
    assert quota.calls == [(1001, "light")]

    quota.calls.clear()

    async def _fake_non_billable(**_kwargs):
        yield PipelineError(
            "pipeline_answer_error",
            billable=False,
            billed_mode="light",
            result=None,
        )

    monkeypatch.setattr(query_module, "execute_streaming", _fake_non_billable)
    second_resp = _post_stream(client)
    assert second_resp.status_code == 200
    assert "event: error" in second_resp.text
    assert quota.calls == []
