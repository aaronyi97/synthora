from __future__ import annotations

from types import SimpleNamespace

from agoracle.api.routes.health import _conversation_store_status, _overall_status


class TestConversationStoreStatus:
    def test_returns_ok_when_conversation_store_is_present(self):
        state = SimpleNamespace(conversation_store=object())
        assert _conversation_store_status(state) == "ok"

    def test_returns_degraded_when_conversation_store_is_missing(self):
        state = SimpleNamespace(conversation_store=None)
        assert _conversation_store_status(state) == "degraded"


class TestOverallStatus:
    def test_returns_unhealthy_when_no_models_are_available(self):
        assert _overall_status(
            available_count=0,
            total_models=3,
            session_db_ok=True,
            conversation_store_ok=True,
            critical_modes_degraded=[],
        ) == "unhealthy"

    def test_returns_degraded_when_conversation_store_is_down(self):
        assert _overall_status(
            available_count=3,
            total_models=3,
            session_db_ok=True,
            conversation_store_ok=False,
            critical_modes_degraded=[],
        ) == "degraded"

    def test_returns_ok_when_all_dependencies_are_healthy(self):
        assert _overall_status(
            available_count=3,
            total_models=3,
            session_db_ok=True,
            conversation_store_ok=True,
            critical_modes_degraded=[],
        ) == "ok"
