"""Tests for /roundtable/check endpoint and check_suitability function."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sse_starlette.sse import AppStatus

import agoracle.api.app as app_module
from agoracle.api.routes import misc as misc_module
from agoracle.domain.types import ModelResponse, Role
from agoracle.services.roundtable_orchestrator import (
    ContentionPoint,
    DisputeMap,
    ExpertOpinion,
    RoundtableOrchestrator,
    check_suitability,
    rule_guard,
    SuitabilityResult,
)


# ── Unit tests for check_suitability ──────────────────────────────────


class _FakeAdapter:
    """Minimal adapter stub returning a preconfigured response."""

    def __init__(self, content: str, success: bool = True):
        self._content = content
        self._success = success

    async def call(self, role_call):
        return ModelResponse(
            call_id=role_call.call_id,
            model_id=role_call.model_id,
            role=Role.CONTRIBUTOR,
            content=self._content,
            latency_ms=50,
            success=self._success,
        )


class _FakePromptLoader:
    """Stub prompt loader that returns safety rules."""

    def __init__(self, safety_text: str = ""):
        self._safety = safety_text

    def load(self, name: str, language: str = "zh-CN") -> str:
        if name == "safety_rules":
            return self._safety
        return ""


@pytest.mark.asyncio
async def test_check_suitability_high():
    adapter = _FakeAdapter(json.dumps({"suitability": "high", "reason": "多角度决策"}))
    result = await check_suitability("该不该辞职？", adapter)
    assert result.suitability == "high"


@pytest.mark.asyncio
async def test_check_suitability_low_from_rules():
    result = await check_suitability("帮我写一封信", _FakeAdapter(""))
    assert result.suitability == "low"
    assert "适合" in result.reason or "直接" in result.reason


@pytest.mark.asyncio
async def test_check_suitability_low_from_rules_in_english():
    result = await check_suitability("Write me a short letter", _FakeAdapter(""), language="en-US")
    assert result.suitability == "low"
    assert "roundtable" in result.reason.lower() or "better handled" in result.reason.lower()


@pytest.mark.asyncio
async def test_check_suitability_empty_input():
    result = await check_suitability("", _FakeAdapter(""))
    assert result.suitability == "low"


@pytest.mark.asyncio
async def test_check_suitability_llm_fallback_on_failure():
    adapter = _FakeAdapter("", success=False)
    result = await check_suitability("要不要买房？", adapter)
    assert result.suitability == "medium"
    assert result.reason == "llm_fallback"


@pytest.mark.asyncio
async def test_check_suitability_passes_prompt_loader():
    """Verify that prompt_loader is accepted and safety rules don't break output."""
    loader = _FakePromptLoader("你是安全助手。")
    adapter = _FakeAdapter(json.dumps({"suitability": "high", "reason": "ok"}))
    result = await check_suitability("创业还是考研？", adapter, prompt_loader=loader)
    assert result.suitability == "high"


@pytest.mark.asyncio
async def test_safety_rules_injected_into_orchestrator():
    """Verify RoundtableOrchestrator receives non-empty safety rules."""
    loader = _FakePromptLoader("你是安全助手。禁止输出违法内容。")
    adapter = _FakeAdapter("")
    orch = RoundtableOrchestrator(model_adapter=adapter, prompt_loader=loader)
    assert orch._load_safety_rules() != ""
    assert "安全" in orch._load_safety_rules()


def test_roundtable_fallback_dispute_map_is_english():
    orch = RoundtableOrchestrator(model_adapter=_FakeAdapter(""), prompt_loader=_FakePromptLoader())
    opinions = [
        ExpertOpinion(model_id="m1", label="Deep Analyst", stance="I support this plan.", confidence=0.8),
        ExpertOpinion(model_id="m2", label="Logic Strategist", stance="I oppose this plan because the risk is too high.", confidence=0.8),
    ]

    dispute_map = orch._build_fallback_dispute_map(opinions, language="en-US")

    assert dispute_map.contention_points
    assert "Competing positions" in dispute_map.contention_points[0].topic
    assert not re.search(r"[一-龥]", dispute_map.contention_points[0].topic)


def test_roundtable_fallback_decision_packet_is_english():
    orch = RoundtableOrchestrator(model_adapter=_FakeAdapter(""), prompt_loader=_FakePromptLoader())
    opinions = [
        ExpertOpinion(model_id="m1", label="Deep Analyst", stance="Support shipping now.", confidence=0.8),
        ExpertOpinion(model_id="m2", label="Logic Strategist", stance="Delay for more validation.", confidence=0.8),
    ]
    dispute_map = DisputeMap(
        contention_points=[
            ContentionPoint(
                topic="Whether to ship immediately",
                severity="high",
                dispute_type=["value"],
                dimension_id="delivery_speed",
                dimension_label="Delivery speed",
            )
        ],
    )

    packet = orch._build_fallback_decision_packet(
        opinions,
        dispute_map,
        reason="s4_moderator_timeout",
        interactive=True,
        language="en-US",
    )

    combined = "\n".join([
        packet.final_summary,
        packet.confidence_basis,
        packet.recommended_action,
        packet.unresolved[0].reason,
        packet.value_disputes_to_user[0].ask_user,
    ])
    assert "moderator" in combined.lower()
    assert not re.search(r"[一-龥]", combined)


# ── API-level tests via TestClient ────────────────────────────────────


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
def _api_client(monkeypatch: pytest.MonkeyPatch):
    @asynccontextmanager
    async def _noop_lifespan(_app):
        yield

    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    monkeypatch.setattr(app_module, "lifespan", _noop_lifespan)
    monkeypatch.setattr(misc_module, "_get_user_id", lambda _request: 1001)
    AppStatus.should_exit = False
    AppStatus.should_exit_event = None

    app_module.state.config = SimpleNamespace(
        features=SimpleNamespace(roundtable_enabled=True)
    )
    app_module.state.quota_service = SimpleNamespace(
        check_quota=lambda *a, **kw: None,
    )
    app_module.state.model_adapter = _FakeAdapter(
        json.dumps({"suitability": "high", "reason": "适合圆桌"})
    )
    app_module.state.judge = object()
    app_module.state.extractor = object()
    app_module.state.event_bus = None
    app_module.state.search_service = None
    app_module.state.prompt_loader = _FakePromptLoader()
    app_module.state.profile_store = None
    app_module.state.behavior_analytics = None
    app_module.state.conversation_store = None
    app_module.state.user_store = None

    app = app_module.create_app()
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()
        AppStatus.should_exit = False
        AppStatus.should_exit_event = None


def test_api_check_suitable_question(_api_client):
    res = _api_client.post("/api/roundtable/check", json={"question": "要不要辞职？"})
    assert res.status_code == 200
    data = res.json()
    assert data["suitability"] == "high"


def test_api_check_unsuitable_question(_api_client):
    res = _api_client.post("/api/roundtable/check", json={"question": "帮我写一封信"})
    assert res.status_code == 200
    data = res.json()
    assert data["suitability"] == "low"


def test_api_check_unsuitable_question_in_english(_api_client):
    res = _api_client.post(
        "/api/roundtable/check",
        json={"question": "Write me a short letter", "locale": "en-US"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["suitability"] == "low"
    assert "roundtable" in data["reason"].lower() or "better handled" in data["reason"].lower()


def test_api_check_empty_question(_api_client):
    res = _api_client.post("/api/roundtable/check", json={"question": ""})
    assert res.status_code == 200
    data = res.json()
    assert data["suitability"] == "low"
