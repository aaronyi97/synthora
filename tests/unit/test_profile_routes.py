"""
Unit/regression tests for profile routes (DEV-PHASE3-ROUTES-PROFILE-R1).

Covers:
- All 16 profile/history endpoints are provided by routes/profile.py
- require_auth is imported from deps.py (not inline)
- No inline profile handlers remain in app.py _build_router
- app.py includes profile router
- Auth guard: unauthenticated → 401, authenticated → pass
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from agoracle.api.app import create_app
from agoracle.api.deps import require_auth


def _make_request(user_id=0, user=None):
    req = MagicMock()
    req.state.user_id = user_id
    req.state.user = user
    req.headers = {}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    return req


EXPECTED_PATHS = {
    "/history",
    "/profile/cognitive-consent",
    "/profile/cognitive-summary",
    "/profile/behavior-summary",
    "/profile/language",
    "/profile/delete-cognitive",
    "/profile/export",
    "/profile/recent-turns",
    "/profile/growth",
    "/profile/usage",
    "/capability-map",
    "/improvement-plans",
    "/improvement-plans/{plan_id}/activate",
    "/improvement-plans/{plan_id}/abandon",
    "/improvement-plans/{plan_id}/engaged",
}


class TestProfileModuleImports:
    """Regression: profile.py must import require_auth from deps.py."""

    def test_profile_imports_require_auth_from_deps(self):
        import agoracle.api.routes.profile as profile_module
        import agoracle.api.deps as deps_module
        assert profile_module.require_auth is deps_module.require_auth

    def test_profile_router_has_all_16_routes(self):
        import agoracle.api.routes.profile as profile_module
        paths = {r.path for r in profile_module.router.routes}
        assert EXPECTED_PATHS == paths, f"Missing: {EXPECTED_PATHS - paths}, Extra: {paths - EXPECTED_PATHS}"

    def test_app_includes_profile_router(self):
        """app.py _build_router must reference routes.profile."""
        import agoracle.api.app as app_module
        source = inspect.getsource(app_module._build_router)
        assert "routes.profile" in source or "_profile_router" in source

    def test_app_no_longer_has_inline_profile_handlers(self):
        """Regression: inline profile endpoint defs must not exist in app.py _build_router."""
        import agoracle.api.app as app_module
        source = inspect.getsource(app_module._build_router)
        assert "def toggle_cognitive_consent" not in source
        assert "def get_cognitive_summary" not in source
        assert "def get_behavior_summary" not in source
        assert "def delete_cognitive_data" not in source
        assert "def export_profile" not in source
        assert "def get_recent_turns" not in source
        assert "def get_growth_dashboard" not in source
        assert "def my_usage" not in source
        assert "def get_capability_map" not in source
        assert "def get_improvement_plans" not in source

    def test_profile_module_uses_require_auth_in_handlers(self):
        """All handlers in profile.py must call require_auth, not inline uid checks."""
        import agoracle.api.routes.profile as profile_module
        source = inspect.getsource(profile_module)
        assert "require_auth(request)" in source
        assert 'if not uid' not in source


class TestRequireAuthGuardViaDeps:
    """Auth guard behaviour for profile routes (uses same require_auth from deps)."""

    def test_unauthenticated_returns_401(self):
        req = _make_request(user_id=0)
        with pytest.raises(HTTPException) as exc_info:
            require_auth(req)
        assert exc_info.value.status_code == 401

    def test_authenticated_returns_user_id(self):
        req = _make_request(user_id=42)
        assert require_auth(req) == 42


class TestProfileRouteCount:
    """Sanity: verify exact path count to catch accidental omissions."""

    def test_exactly_16_routes(self):
        import agoracle.api.routes.profile as profile_module
        assert len(profile_module.router.routes) == 16


class TestHistoryRoute:
    """Regression: GET /api/history must exist and use user_store pagination."""

    _FAKE_USER = {"id": 42, "username": "alice", "display_name": "Alice", "is_admin": False}
    _SESSION_COOKIE = "history-session-token"

    def _make_client(self):
        import agoracle.api.app as app_module

        app = create_app()
        user_store = MagicMock()
        user_store.get_by_session_id = AsyncMock(return_value=self._FAKE_USER)
        user_store.get_history = AsyncMock(return_value=[{
            "query_id": "q1",
            "session_id": None,
            "question": "test",
            "mode": "light",
            "final_answer": "answer",
            "confidence": 0.5,
            "contributor_count": 1,
            "latency_ms": 123,
            "estimated_cost_usd": 0.0,
            "user_marked_usable": False,
            "created_at": "2026-03-09T00:00:00",
            "quality_gate": "",
            "best_single_answer": "",
            "has_divergence": False,
            "divergence_summary": "",
            "key_insights": [],
            "divergence_points": [],
        }])
        user_store.get_history_count = AsyncMock(return_value=7)
        app_module.state.user_store = user_store
        client = TestClient(app, raise_server_exceptions=False)
        return client, user_store

    def test_history_success_caps_limit_and_returns_total(self):
        client, user_store = self._make_client()
        res = client.get(
            "/api/history?limit=999&offset=5",
            cookies={"session": self._SESSION_COOKIE},
            headers={"origin": "http://localhost"},
        )
        assert res.status_code == 200
        assert res.json() == {"history": [{
            "query_id": "q1",
            "session_id": None,
            "question": "test",
            "mode": "light",
            "final_answer": "answer",
            "confidence": 0.5,
            "contributor_count": 1,
            "latency_ms": 123,
            "estimated_cost_usd": 0.0,
            "user_marked_usable": False,
            "created_at": "2026-03-09T00:00:00",
            "quality_gate": "",
            "best_single_answer": "",
            "has_divergence": False,
            "divergence_summary": "",
            "key_insights": [],
            "divergence_points": [],
        }], "total": 7}
        user_store.get_history.assert_awaited_once_with(42, limit=100, offset=5)
        user_store.get_history_count.assert_awaited_once_with(42)

    def test_history_unauthenticated_returns_401(self):
        import agoracle.api.app as app_module

        app = create_app()
        user_store = MagicMock()
        user_store.get_by_session_id = AsyncMock(return_value=None)
        app_module.state.user_store = user_store

        client = TestClient(app, raise_server_exceptions=False)
        res = client.get("/api/history", headers={"origin": "http://localhost"})
        assert res.status_code in (401, 403)


class TestProfileLanguageRoutes:
    """Regression: language preference endpoints must normalize and persist locale."""

    _FAKE_USER = {"id": 42, "username": "alice", "display_name": "Alice", "is_admin": False}
    _SESSION_COOKIE = "language-session-token"

    def _make_client(self, preferred_language: str = "en"):
        import agoracle.api.app as app_module

        app = create_app()
        user_store = MagicMock()
        user_store.get_by_session_id = AsyncMock(return_value=self._FAKE_USER)

        profile = SimpleNamespace(preferred_language=preferred_language)
        profile_store = MagicMock()
        profile_store.load = AsyncMock(return_value=profile)
        profile_store.save = AsyncMock()

        app_module.state.user_store = user_store
        app_module.state.profile_store = profile_store
        client = TestClient(app, raise_server_exceptions=False)
        return client, profile_store, profile

    def test_get_language_returns_normalized_locale(self):
        client, _profile_store, _profile = self._make_client(preferred_language="en-GB")
        res = client.get(
            "/api/profile/language",
            cookies={"session": self._SESSION_COOKIE},
            headers={"origin": "http://localhost"},
        )
        assert res.status_code == 200
        assert res.json() == {"language": "en-US"}

    def test_put_language_normalizes_and_persists(self):
        client, profile_store, profile = self._make_client(preferred_language="zh-CN")
        res = client.put(
            "/api/profile/language",
            json={"language": "en-GB"},
            cookies={"session": self._SESSION_COOKIE},
            headers={"origin": "http://localhost"},
        )
        assert res.status_code == 200
        assert res.json() == {"language": "en-US"}
        assert profile.preferred_language == "en-US"
        profile_store.save.assert_awaited_once_with(profile, 42)
