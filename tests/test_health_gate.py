from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Request

from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter
from agoracle.api.routes import health as health_route
from agoracle.api.routes.health import (
    _check_mode_critical_roles,
    _frontend_linked_validation_blockers,
    _overall_status,
)
from agoracle.config.loader import load_config


@pytest.fixture(scope="module")
def app_config():
    return load_config("config.yaml")


def _available_models(config, *missing_model_ids: str) -> list[str]:
    missing = set(missing_model_ids)
    return [model_id for model_id in config.models if model_id not in missing]


def _request_with_remote_ip() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/health",
            "headers": [(b"cf-connecting-ip", b"203.0.113.7")],
            "client": ("127.0.0.1", 12345),
            "query_string": b"",
        }
    )


def test_startup_skip_records_empty_env():
    adapter = OpenAIModelAdapter.__new__(OpenAIModelAdapter)
    adapter._startup_skipped_models = []

    adapter._record_startup_skip(
        model_id="test_model",
        reason="configured_envs_resolved_empty",
        detail="Configured env vars resolved empty during startup.",
        configured_envs=["TEST_API_KEY", "TEST_API_KEY_BACKUP"],
    )

    records = adapter.startup_skipped_models
    assert records == [
        {
            "model_id": "test_model",
            "reason": "configured_envs_resolved_empty",
            "detail": "Configured env vars resolved empty during startup.",
            "configured_envs": ["TEST_API_KEY", "TEST_API_KEY_BACKUP"],
        }
    ]

    records[0]["configured_envs"].append("MUTATED")
    assert adapter.startup_skipped_models[0]["configured_envs"] == [
        "TEST_API_KEY",
        "TEST_API_KEY_BACKUP",
    ]


def test_mode_availability_all_models_present(app_config):
    availability = _check_mode_critical_roles(
        config=app_config,
        available_models=list(app_config.models),
    )

    for mode_name in ("light", "deep", "research", "socratic"):
        assert availability[mode_name]["available"] is True
        assert availability[mode_name]["missing_roles"] == []


def test_mode_availability_judge_missing(app_config):
    judge_model = app_config.modes["deep"].judge
    availability = _check_mode_critical_roles(
        config=app_config,
        available_models=_available_models(app_config, judge_model),
    )

    assert availability["deep"]["available"] is False
    assert availability["research"]["available"] is False
    assert f"judge: {judge_model}" in availability["deep"]["missing_roles"]
    assert f"judge: {judge_model}" in availability["research"]["missing_roles"]


def test_mode_availability_enough_contributors(app_config):
    deep_contributors = app_config.modes["deep"].contributors
    availability = _check_mode_critical_roles(
        config=app_config,
        available_models=_available_models(app_config, deep_contributors[4], deep_contributors[5]),
    )

    assert availability["deep"]["available"] is True
    assert availability["research"]["available"] is True


def test_mode_availability_not_enough_contributors(app_config):
    deep_contributors = app_config.modes["deep"].contributors
    availability = _check_mode_critical_roles(
        config=app_config,
        available_models=_available_models(
            app_config,
            deep_contributors[1],
            deep_contributors[2],
            deep_contributors[4],
            deep_contributors[5],
        ),
    )

    assert availability["deep"]["available"] is False
    assert availability["research"]["available"] is False
    assert "contributors: need >= 3, have 2" in availability["deep"]["missing_roles"]
    assert "contributors: need >= 3, have 2" in availability["research"]["missing_roles"]


def test_frontend_validation_blockers_conversation_store():
    blockers = _frontend_linked_validation_blockers(
        available_count=3,
        session_db_ok=True,
        conversation_store_ok=False,
    )

    assert blockers == ["conversation_store_unavailable"]


def test_frontend_validation_blockers_no_models():
    blockers = _frontend_linked_validation_blockers(
        available_count=0,
        session_db_ok=True,
        conversation_store_ok=True,
    )

    assert blockers == ["no_models_available"]


def test_overall_status_degraded_with_critical_mode_down():
    assert _overall_status(
        available_count=6,
        total_models=6,
        session_db_ok=True,
        conversation_store_ok=True,
        critical_modes_degraded=["deep"],
    ) == "degraded"


def test_overall_status_ok_all_clear():
    assert _overall_status(
        available_count=6,
        total_models=6,
        session_db_ok=True,
        conversation_store_ok=True,
        critical_modes_degraded=[],
    ) == "ok"


def test_overall_status_unhealthy_no_models():
    assert _overall_status(
        available_count=0,
        total_models=6,
        session_db_ok=True,
        conversation_store_ok=True,
        critical_modes_degraded=[],
    ) == "unhealthy"


@pytest.mark.asyncio
async def test_health_route_reports_critical_modes_and_frontend_blockers(app_config, monkeypatch):
    judge_model = app_config.modes["deep"].judge
    state = SimpleNamespace(
        model_adapter=SimpleNamespace(
            available_models=_available_models(app_config, judge_model),
            startup_skipped_models=[],
        ),
        config=app_config,
        session_store=SimpleNamespace(health_check=AsyncMock(return_value=True)),
        conversation_store=None,
    )

    monkeypatch.setattr(health_route, "get_app_state", lambda _request: state)
    monkeypatch.setattr(
        health_route,
        "_app",
        lambda: SimpleNamespace(
            _is_production=lambda: True,
            _get_version=lambda: "test-version",
        ),
    )

    payload = await health_route.health(_request_with_remote_ip())

    assert payload["status"] == "degraded"
    assert payload["critical_modes_degraded"] == ["deep", "research"]
    assert payload["mode_availability"]["deep"]["available"] is False
    assert f"judge: {judge_model}" in payload["mode_availability"]["deep"]["missing_roles"]
    assert payload["ready_for_frontend_linked_validation"] is False
    assert "conversation_store_unavailable" in payload["frontend_linked_validation_blockers"]
