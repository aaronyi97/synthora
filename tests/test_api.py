"""
Tests for the FastAPI API server.

Tests basic endpoint structure and request/response models.
Does NOT test real API calls (those need integration tests with API keys).
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

from agoracle.api.app import (
    AskRequest,
    AskResponse,
    FeedbackRequest,
    SocraticStartRequest,
    SocraticRespondRequest,
    SocraticRevealRequest,
    create_app,
)


class TestRequestModels:
    def test_ask_request_defaults(self):
        req = AskRequest(question="test?")
        assert req.question == "test?"
        assert req.mode == "auto"
        assert req.web_search is True
        assert req.depth is None

    def test_ask_request_custom(self):
        req = AskRequest(question="AI?", mode="deep", web_search=False, depth=2)
        assert req.mode == "deep"
        assert req.depth == 2

    def test_ask_response_model(self):
        resp = AskResponse(
            query_id="q1", question="test", mode="deep",
            final_answer="answer", confidence=0.8, quality_gate="synthesized",
            has_divergence=False, latency_ms=1000, estimated_cost_usd=0.01,
            contributor_count=4,
        )
        assert resp.confidence == 0.8
        assert resp.divergence_summary is None

    def test_socratic_start_request(self):
        req = SocraticStartRequest(question="AI创造力？")
        assert req.question == "AI创造力？"

    def test_socratic_respond_request(self):
        req = SocraticRespondRequest(session_id="abc", message="我觉得不会")
        assert req.session_id == "abc"

    def test_feedback_request(self):
        req = FeedbackRequest(query_id="q1", rating="useful")
        assert req.comment is None


class TestAppCreation:
    def test_create_app_returns_fastapi(self):
        app = create_app()
        assert app.title == "Synthora API"
        # Check routes exist
        routes = [r.path for r in app.routes]
        assert "/api/health" in routes
        assert "/api/models" in routes
        assert "/api/ask" in routes
        assert "/api/ask/stream" in routes
        assert "/api/socratic/start" in routes
        assert "/api/socratic/respond" in routes
        assert "/api/socratic/reveal" in routes
        assert "/api/feedback" in routes
        assert "/api/history" in routes

    def test_delete_account_openapi_includes_request_body(self):
        app = create_app()
        schema = app.openapi()
        delete_op = schema["paths"]["/api/auth/account"]["delete"]
        assert "requestBody" in delete_op
        content = delete_op["requestBody"]["content"]["application/json"]["schema"]
        assert content["$ref"].endswith("/DeleteAccountBody")


class TestAuthMiddleware:
    def test_middleware_import(self):
        from agoracle.api.middleware.auth import BearerAuthMiddleware
        assert BearerAuthMiddleware is not None

    def test_public_paths(self):
        from agoracle.api.middleware.auth import _PUBLIC_PATHS
        assert "/api/health" in _PUBLIC_PATHS
        assert "/docs" in _PUBLIC_PATHS


class TestRateLimitMiddleware:
    def test_middleware_import(self):
        from agoracle.api.middleware.rate_limit import RateLimitMiddleware
        assert RateLimitMiddleware is not None

    def test_exempt_paths(self):
        from agoracle.api.middleware.rate_limit import _EXEMPT_PATHS
        assert "/api/health" in _EXEMPT_PATHS
        assert "/docs" in _EXEMPT_PATHS


class TestAuthMePreferredLanguage:
    _FAKE_USER = {
        "id": 1001,
        "username": "alice",
        "display_name": "Alice",
        "is_admin": False,
    }
    _SESSION_COOKIE = "me-session-token"

    def test_auth_me_returns_preferred_language(self):
        from fastapi.testclient import TestClient
        import agoracle.api.app as app_module

        app = create_app()
        user_store = MagicMock()
        user_store.get_by_session_id = AsyncMock(return_value=self._FAKE_USER)
        user_store.get_history_count = AsyncMock(return_value=3)

        profile_store = MagicMock()
        profile_store.load = AsyncMock(
            return_value=SimpleNamespace(preferred_language="en-GB")
        )

        app_module.state.user_store = user_store
        app_module.state.profile_store = profile_store

        client = TestClient(app, raise_server_exceptions=False)
        res = client.get(
            "/api/auth/me",
            cookies={"session": self._SESSION_COOKIE},
            headers={"origin": "http://localhost"},
        )

        assert res.status_code == 200
        assert res.json()["preferred_language"] == "en-US"


class TestPricingRoute:
    def test_pricing_route_exists(self):
        app = create_app()
        routes = [r.path for r in app.routes]
        assert "/api/pricing" in routes


class TestCostEstimation:
    def test_estimate_cost_with_config(self):
        from agoracle.services.orchestrator import _estimate_cost
        from agoracle.domain.types import ModelResponse, Role
        from agoracle.config.schema import ModelConfig

        configs = {
            "model_a": ModelConfig(id="model_a", cost_per_1m_input=2.0, cost_per_1m_output=10.0),
        }
        responses = [
            ModelResponse(
                call_id="c1", model_id="model_a", role=Role.CONTRIBUTOR,
                content="test", latency_ms=100, success=True,
                prompt_tokens=1000, completion_tokens=500,
            ),
        ]
        cost = _estimate_cost(responses, model_configs=configs)
        # 1000 * 2.0 / 1M + 500 * 10.0 / 1M = 0.002 + 0.005 = 0.007
        assert abs(cost - 0.007) < 0.0001

    def test_estimate_cost_fallback(self):
        from agoracle.services.orchestrator import _estimate_cost
        from agoracle.domain.types import ModelResponse, Role

        responses = [
            ModelResponse(
                call_id="c1", model_id="unknown_model", role=Role.CONTRIBUTOR,
                content="test", latency_ms=100, success=True,
                prompt_tokens=1000, completion_tokens=500,
            ),
        ]
        cost = _estimate_cost(responses, model_configs={})
        # fallback: (1000 + 500) * 5.0 / 1M = 0.0075
        assert abs(cost - 0.0075) < 0.0001


# ── Test: POST /auth/change-password ─────────────────────

class TestChangePassword:
    """Four-path coverage for POST /api/auth/change-password.

    Auth strategy: mock user_store.get_by_session_id so UserAuthMiddleware
    injects request.state.user_id without hitting real DB.
    """

    _FAKE_USER = {"id": 1001, "username": "alice", "api_key": "tok-test"}
    _SESSION_COOKIE = "test-session-token"

    def _make_client(self, verify_ok: bool = True):
        """Return (TestClient, user_store_mock) with auth pre-wired."""
        from fastapi.testclient import TestClient
        from unittest.mock import AsyncMock, MagicMock
        import agoracle.api.app as app_module

        app = create_app()
        user_store = MagicMock()
        # Middleware path: session lookup → inject user
        user_store.get_by_session_id = AsyncMock(return_value=self._FAKE_USER)
        # Endpoint path: verify old password
        user_store.verify_password_by_id = AsyncMock(return_value=verify_ok)
        user_store.update_password = AsyncMock()
        app_module.state.user_store = user_store

        client = TestClient(app, raise_server_exceptions=False)
        return client, user_store

    def _post(self, client, body: dict) -> object:
        return client.post(
            "/api/auth/change-password",
            json=body,
            cookies={"session": self._SESSION_COOKIE},
            headers={"origin": "http://localhost"},
        )

    def test_change_password_success(self):
        """旧密码正确 → 200 ok."""
        client, user_store = self._make_client(verify_ok=True)
        res = self._post(client, {"old_password": "oldpass", "new_password": "newpass1"})
        assert res.status_code == 200
        assert res.json().get("status") == "ok"
        user_store.update_password.assert_awaited_once()

    def test_change_password_wrong_old_password_returns_403(self):
        """旧密码错误 → 403."""
        client, user_store = self._make_client(verify_ok=False)
        res = self._post(client, {"old_password": "wrongpass", "new_password": "newpass1"})
        assert res.status_code == 403
        user_store.update_password.assert_not_awaited()

    def test_change_password_missing_fields_returns_422(self):
        """缺少 old_password → 422."""
        client, user_store = self._make_client(verify_ok=True)
        res = self._post(client, {"new_password": "newpass1"})
        assert res.status_code == 422

    def test_change_password_unauthenticated_returns_401(self):
        """未登录（无 session cookie）→ 401."""
        from fastapi.testclient import TestClient
        from unittest.mock import AsyncMock, MagicMock
        import agoracle.api.app as app_module

        app = create_app()
        user_store = MagicMock()
        # No valid session — return None
        user_store.get_by_session_id = AsyncMock(return_value=None)
        app_module.state.user_store = user_store

        client = TestClient(app, raise_server_exceptions=False)
        res = client.post(
            "/api/auth/change-password",
            json={"old_password": "x", "new_password": "newpass1"},
            headers={"origin": "http://localhost"},
            # deliberately no session cookie
        )
        assert res.status_code in (401, 403)


class TestChangePasswordSessionRenewal:
    """Regression tests for change_password session renewal (defect lane fix).

    After change_password, update_password revokes all sessions. The endpoint
    should issue a new session cookie so the user isn't kicked out.
    """

    _FAKE_USER = {"id": 1001, "username": "alice", "api_key": "tok-test", "display_name": "Alice", "is_admin": False}
    _SESSION_COOKIE = "test-session-token"

    def _make_client(self, create_session_side_effect=None):
        from fastapi.testclient import TestClient
        from unittest.mock import AsyncMock, MagicMock
        import agoracle.api.app as app_module

        app = create_app()
        user_store = MagicMock()
        user_store.get_by_session_id = AsyncMock(return_value=self._FAKE_USER)
        user_store.verify_password_by_id = AsyncMock(return_value=True)
        user_store.update_password = AsyncMock()
        if create_session_side_effect is not None:
            user_store.create_session = AsyncMock(side_effect=create_session_side_effect)
        else:
            user_store.create_session = AsyncMock(return_value="sess-renewed-abc123")
        app_module.state.user_store = user_store

        client = TestClient(app, raise_server_exceptions=False)
        return client, user_store

    def test_success_sets_new_session_cookie(self):
        """测试组 A：改密成功后响应应包含新的 session cookie。"""
        client, user_store = self._make_client()
        res = client.post(
            "/api/auth/change-password",
            json={"old_password": "oldpass", "new_password": "newpass123"},
            cookies={"session": self._SESSION_COOKIE},
            headers={"origin": "http://localhost"},
        )
        assert res.status_code == 200
        assert res.json()["status"] == "ok"
        # Verify new session cookie was set
        session_cookie = res.cookies.get("session")
        assert session_cookie is not None, "Response should set a new session cookie"
        # Verify create_session was called with the user's id
        user_store.create_session.assert_awaited_once_with(1001)

    def test_create_session_failure_degrades_gracefully(self):
        """测试组 B：create_session 异常时仍返回成功，提示重新登录。"""
        client, user_store = self._make_client(
            create_session_side_effect=RuntimeError("DB locked")
        )
        res = client.post(
            "/api/auth/change-password",
            json={"old_password": "oldpass", "new_password": "newpass123"},
            cookies={"session": self._SESSION_COOKIE},
            headers={"origin": "http://localhost"},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "ok"
        assert "重新登录" in body["message"]
        # Password was still changed
        user_store.update_password.assert_awaited_once()
        # No session cookie should be set (creation failed)
        session_cookie = res.cookies.get("session")
        assert session_cookie is None or session_cookie == "", \
            "No new session cookie when create_session fails"


class TestDeleteAccountFeedbackCleanup:
    def test_delete_account_removes_feedback_entries_for_user_queries(self, tmp_path):
        from agoracle.adapters.feedback.json_feedback import JsonFeedbackStore
        from agoracle.adapters.user.sqlite_user_store import SQLiteUserStore
        from fastapi.testclient import TestClient
        import agoracle.api.app as app_module

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            app = create_app()
            user_store = SQLiteUserStore(tmp_path / "users.db")
            loop.run_until_complete(user_store.initialize())
            user = loop.run_until_complete(
                user_store.register("delete_feedback_user", "correctpass", "Delete Feedback")
            )
            uid = user["id"]
            loop.run_until_complete(user_store.save_query(
                user_id=uid,
                query_id="q-delete-me",
                session_id=None,
                question="question",
                mode="light",
                final_answer="answer",
                confidence=0.8,
                contributor_count=1,
                latency_ms=12,
                estimated_cost_usd=0.01,
            ))
            session_id = loop.run_until_complete(user_store.create_session(uid))

            feedback_store = JsonFeedbackStore(tmp_path / "feedback.jsonl")
            loop.run_until_complete(feedback_store.record("q-delete-me", "useful", extra={"user_id": uid}))
            loop.run_until_complete(feedback_store.record("q-keep", "useful", extra={"user_id": 9999}))

            app_module.state.user_store = user_store
            app_module.state.feedback_store = feedback_store
            app_module.state.quota_service = MagicMock()
            app_module.state.profile_store = None

            uploads = tmp_path / "uploads"
            uploads.mkdir(parents=True, exist_ok=True)

            with patch.object(app_module, "UPLOAD_DIR", uploads):
                client = TestClient(app, raise_server_exceptions=False)
                res = client.request(
                    "DELETE",
                    "/api/auth/account?confirm=DELETE",
                    json={"password": "correctpass"},
                    cookies={"session": session_id},
                    headers={"origin": "http://localhost"},
                )

            assert res.status_code == 200
            entries = loop.run_until_complete(feedback_store.get_all())
            assert [entry["query_id"] for entry in entries] == ["q-keep"]
            loop.run_until_complete(user_store.close())
        finally:
            loop.close()
            asyncio.set_event_loop(asyncio.new_event_loop())


class TestAuthRoutes:
    def test_login_response_uses_cookie_only(self):
        from fastapi.testclient import TestClient
        import agoracle.api.app as app_module

        app = create_app()
        user_store = MagicMock()
        user_store.check_login_locked = AsyncMock(return_value=None)
        user_store.login = AsyncMock(
            return_value={"id": 1001, "username": "alice", "display_name": "Alice", "is_admin": False}
        )
        user_store.clear_login_failures = AsyncMock()
        user_store.create_session = AsyncMock(return_value="sid-login-123")
        app_module.state.user_store = user_store

        client = TestClient(app, raise_server_exceptions=False)
        res = client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "password123", "phone": ""},
            headers={"origin": "http://localhost"},
        )

        assert res.status_code == 200, res.text
        assert "token" not in res.json()
        assert "session=sid-login-123" in res.headers.get("set-cookie", "")
