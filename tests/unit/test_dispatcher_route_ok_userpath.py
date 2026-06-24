from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

import agoracle.api.app as app_module
from agoracle.api.routes import query as query_module
from agoracle.config.schema import AppConfig, ModeConfig, ModelConfig
from agoracle.domain.types import JudgeSynthesis, MetadataExtraction, ModelResponse
from agoracle.services.companion_dispatcher import (
    CompanionDispatcher,
    DispatcherInput,
    DispatcherOutput,
)


class _QuotaStub:
    def check_quota(self, user_id: int, mode: str):
        return None

    def record_usage(self, user_id: int, mode: str) -> None:
        return None

    def get_lifetime_usage(self, user_id: int):
        return {}


class _FakeAdapter:
    available_models = [
        "perplexity_sonar_pro",
        "gemini_3_flash",
        "claude_sonnet",
    ]

    def supports_model(self, model_id: str) -> bool:
        return True

    async def call(self, role_call):
        call_id = str(getattr(role_call, "call_id", ""))
        if call_id.startswith("qc_"):
            content = "0.91"
        elif call_id.startswith("disp_guide_"):
            content = '{"message": "", "action_type": "done"}'
        else:
            content = "这是单模型实时回答。"
        return ModelResponse(
            call_id=call_id,
            model_id=role_call.model_id,
            role=role_call.role,
            content=content,
            latency_ms=5,
            success=True,
            prompt_tokens=16,
            completion_tokens=32,
        )

    def reset_cost_tracker(self) -> None:
        return None

    def get_cost_tracker(self):
        return []


class _FakeJudge:
    async def synthesize(self, **kwargs):
        return JudgeSynthesis(final_answer="synthesized", latency_ms=1, success=True)

    async def refine(self, **kwargs):
        return JudgeSynthesis(final_answer="refined", latency_ms=1, success=True)

    async def synthesize_stream(self, **kwargs):
        yield "synthesized"


class _FakeExtractor:
    async def extract(self, **kwargs):
        return MetadataExtraction(confidence=0.8)


class _FakePromptLoader:
    def load(self, _name: str, language: str = "zh-CN") -> str:
        return ""

    def render(self, _name: str, **kwargs) -> str:
        return ""


def _make_config() -> AppConfig:
    cfg = AppConfig()
    cfg.search.enabled = False

    for model_id in ("model_a", "perplexity_sonar_pro", "gemini_3_flash", "claude_sonnet"):
        cfg.models[model_id] = ModelConfig(
            id=model_id,
            name=model_id,
            provider="openai",
            model_name=model_id,
            api_key_env="TEST_KEY",
            timeout_seconds=10,
        )

    cfg.modes["light"] = ModeConfig(
        name="light",
        contributors=["model_a"],
        judge="claude_sonnet",
        extractor="gemini_3_flash",
        n_of_m=1,
        max_timeout_seconds=30,
        skip_judge=False,
    )
    cfg.modes["deep"] = ModeConfig(
        name="deep",
        contributors=["model_a"],
        judge="claude_sonnet",
        extractor="gemini_3_flash",
        n_of_m=1,
        max_timeout_seconds=30,
    )
    return cfg


def _extract_sse_event_data(sse_text: str, event_name: str) -> dict | None:
    lines = sse_text.splitlines()
    for idx, line in enumerate(lines):
        if line.strip() != f"event: {event_name}":
            continue
        data_lines: list[str] = []
        j = idx + 1
        while j < len(lines):
            cur = lines[j]
            if not cur.strip():
                break
            if cur.startswith("data: "):
                data_lines.append(cur[len("data: "):])
            j += 1
        if data_lines:
            return json.loads("\n".join(data_lines))
    return None


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    @asynccontextmanager
    async def _noop_lifespan(_app):
        yield

    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    monkeypatch.setattr(app_module, "lifespan", _noop_lifespan)
    monkeypatch.setattr(query_module, "_get_user_id", lambda _request: 1001)

    app_module.state.config = _make_config()
    app_module.state.model_adapter = _FakeAdapter()
    app_module.state.judge = _FakeJudge()
    app_module.state.extractor = _FakeExtractor()
    app_module.state.prompt_loader = _FakePromptLoader()
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


def test_dispatcher_route_ok_userpath_logs_api_and_stream(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    client: TestClient,
) -> None:
    async def _fake_route_ok(self, dispatcher_input: DispatcherInput) -> DispatcherOutput:
        return DispatcherOutput(
            strategy="single_model",
            mode="light",
            single_model_id="perplexity_sonar_pro",
            skip_judge=True,
            contributors_override=["perplexity_sonar_pro"],
            companion_message="",
            route_reason="single_model_fallback: realtime query uses search model",
            is_silent_route=False,
            dispatcher_confidence=0.95,
        )

    monkeypatch.setattr(CompanionDispatcher, "_call_route_sonnet", _fake_route_ok)
    caplog.set_level(logging.INFO, logger="agoracle.services.companion_dispatcher")

    req = {
        "question": "最新新闻有哪些？",
        "mode": "auto",
        "web_search": True,
    }

    ask_resp = client.post("/api/ask", json=req)
    assert ask_resp.status_code == 200, ask_resp.text
    ask_data = ask_resp.json()
    assert ask_data["mode"] == "light"
    assert ask_data["quality_gate"] == "best_single"
    assert ask_data["contributor_count"] == 1

    route_ok_logs = [
        rec.getMessage() for rec in caplog.records if "[Dispatcher] Route OK" in rec.getMessage()
    ]
    assert route_ok_logs, "expected Route OK log not found"
    assert any(
        "strategy=single_model" in line
        and "mode=light" in line
        and "model=perplexity_sonar_pro" in line
        for line in route_ok_logs
    )

    stream_resp = client.post(
        "/api/ask/stream",
        json=req,
        headers={"accept": "text/event-stream"},
    )
    assert stream_resp.status_code == 200, stream_resp.text

    companion_route = _extract_sse_event_data(stream_resp.text, "companion_route")
    assert companion_route is not None, stream_resp.text
    assert companion_route["resolved_mode"] == "light"
    assert "single_model_fallback" in companion_route["route_reason"]
