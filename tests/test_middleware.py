"""
Tests for API middleware: SecurityHeaders, AuditLog, RateLimit.
"""

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse


# ── SecurityHeadersMiddleware tests ──────────────────────

class TestSecurityHeaders:
    """Verify all OWASP security headers are present."""

    @pytest.fixture
    def app(self):
        from agoracle.api.middleware.security_headers import SecurityHeadersMiddleware
        app = FastAPI()
        app.add_middleware(SecurityHeadersMiddleware)

        @app.get("/api/test")
        async def test_endpoint():
            return {"ok": True}

        @app.get("/public")
        async def public_endpoint():
            return {"public": True}

        return app

    @pytest.fixture
    def client(self, app):
        return TestClient(app)

    def test_x_content_type_options(self, client):
        r = client.get("/api/test")
        assert r.headers["X-Content-Type-Options"] == "nosniff"

    def test_x_frame_options(self, client):
        r = client.get("/api/test")
        assert r.headers["X-Frame-Options"] == "DENY"

    def test_xss_protection(self, client):
        r = client.get("/api/test")
        assert r.headers["X-XSS-Protection"] == "1; mode=block"

    def test_referrer_policy(self, client):
        r = client.get("/api/test")
        assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"

    def test_permissions_policy(self, client):
        r = client.get("/api/test")
        assert "camera=()" in r.headers["Permissions-Policy"]

    def test_cache_control_for_api(self, client):
        r = client.get("/api/test")
        assert "no-store" in r.headers["Cache-Control"]

    def test_no_cache_control_for_public(self, client):
        r = client.get("/public")
        # non-API paths should NOT get cache-control override
        assert r.headers.get("Cache-Control", "") != "no-store, no-cache, must-revalidate"
        assert "font-src 'self' data:" in r.headers["Content-Security-Policy"]

    def test_headers_on_all_status_codes(self, app):
        @app.get("/api/error")
        async def error_endpoint():
            return JSONResponse(status_code=500, content={"error": True})

        client = TestClient(app)
        r = client.get("/api/error")
        assert r.headers["X-Content-Type-Options"] == "nosniff"


# ── CSRFMiddleware tests ──────────────────────────────────

class TestCSRF:
    """Verify CSRF Origin checks handle default ports correctly."""

    @pytest.fixture
    def app(self):
        from agoracle.api.middleware.csrf import CSRFMiddleware

        app = FastAPI()
        app.add_middleware(CSRFMiddleware, allowed_origins=["https://api.example.com"])

        @app.post("/api/write")
        async def write_endpoint():
            return {"ok": True}

        @app.post("/api/health")
        async def health_post():
            return {"ok": True}

        return app

    @pytest.fixture
    def client(self, app):
        return TestClient(app)

    def test_allows_exact_origin(self, client):
        r = client.post("/api/write", headers={"Origin": "https://api.example.com"})
        assert r.status_code == 200

    def test_allows_default_https_port_variant(self, client):
        r = client.post("/api/write", headers={"Origin": "https://api.example.com:443"})
        assert r.status_code == 200

    def test_rejects_unknown_origin(self, client):
        r = client.post("/api/write", headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        assert r.json()["error_code"] == "CSRF_REJECTED"

    def test_health_path_exempt(self, client):
        r = client.post("/api/health", headers={"Origin": "https://evil.example"})
        assert r.status_code == 200


# ── AuditLogMiddleware tests ─────────────────────────────

class TestAuditLog:
    """Verify audit log middleware writes structured JSONL."""

    @pytest.fixture
    def tmp_log(self, tmp_path):
        return tmp_path / "audit.jsonl"

    @pytest.fixture
    def app(self, tmp_log):
        from agoracle.api.middleware.audit_log import AuditLogMiddleware
        app = FastAPI()
        app.add_middleware(AuditLogMiddleware, log_path=str(tmp_log))

        @app.get("/api/test")
        async def test_endpoint():
            return {"ok": True}

        return app

    @pytest.fixture
    def client(self, app):
        return TestClient(app)

    def test_audit_log_created(self, client, tmp_log):
        client.get("/api/test")
        time.sleep(0.2)
        assert tmp_log.exists()

    def test_audit_log_valid_jsonl(self, client, tmp_log):
        client.get("/api/test")
        time.sleep(0.2)
        lines = tmp_log.read_text().strip().split("\n")
        assert len(lines) >= 1
        entry = json.loads(lines[0])
        assert entry["method"] == "GET"
        assert entry["path"] == "/api/test"
        assert entry["status"] == 200
        assert "ts" in entry
        assert "latency_ms" in entry

    def test_audit_log_includes_auth_flag(self, client, tmp_log):
        client.get("/api/test", headers={"Authorization": "Bearer test"})
        time.sleep(0.2)
        lines = tmp_log.read_text().strip().split("\n")
        entry = json.loads(lines[-1])
        assert entry["auth"] is True

    def test_audit_log_disabled(self, tmp_path):
        log_path = tmp_path / "disabled.jsonl"
        from agoracle.api.middleware.audit_log import AuditLogMiddleware
        app = FastAPI()
        app.add_middleware(AuditLogMiddleware, enabled=False, log_path=str(log_path))

        @app.get("/api/test")
        async def test_endpoint():
            return {"ok": True}

        client = TestClient(app)
        client.get("/api/test")
        time.sleep(0.2)
        assert not log_path.exists()

    def test_audit_log_sanitizes_control_chars(self, client, tmp_log):
        """Control characters in User-Agent must be stripped (log injection prevention)."""
        client.get("/api/test", headers={"User-Agent": "evil\nbot\r\x00"})
        time.sleep(0.2)
        lines = tmp_log.read_text().strip().split("\n")
        entry = json.loads(lines[-1])
        assert "\n" not in entry["ua"]
        assert "\r" not in entry["ua"]
        assert "\x00" not in entry["ua"]
        assert "evilbot" in entry["ua"]


# ── RateLimitMiddleware tests ────────────────────────────

class TestRateLimit:
    """Verify rate limiting behavior."""

    @pytest.fixture
    def app(self):
        from agoracle.api.middleware.rate_limit import RateLimitMiddleware
        app = FastAPI()
        # Very low RPM for testing; high concurrent to avoid semaphore issues in sync TestClient
        app.add_middleware(RateLimitMiddleware, rpm=3, max_concurrent=100)

        @app.get("/api/test")
        async def test_endpoint():
            return {"ok": True}

        @app.get("/api/health")
        async def health():
            return {"status": "ok"}

        @app.get("/public")
        async def public():
            return {"public": True}

        return app

    @pytest.fixture
    def client(self, app):
        return TestClient(app)

    def test_allows_requests_within_limit(self, client):
        r = client.get("/api/test")
        assert r.status_code == 200

    def test_blocks_after_limit_exceeded(self, client):
        for _ in range(3):
            client.get("/api/test")
        r = client.get("/api/test")
        assert r.status_code == 429
        assert "Retry-After" in r.headers

    def test_health_exempt_from_rate_limit(self, client):
        # Exhaust the limit
        for _ in range(3):
            client.get("/api/test")
        # Health should still work
        r = client.get("/api/health")
        assert r.status_code == 200

    def test_non_api_exempt_from_rate_limit(self, client):
        for _ in range(3):
            client.get("/api/test")
        r = client.get("/public")
        assert r.status_code == 200

    def test_429_response_has_error_detail(self, client):
        for _ in range(4):
            r = client.get("/api/test")
        assert r.status_code == 429
        body = r.json()
        assert "detail" in body
        assert "retry_after_seconds" in body


class TestUserAuthMiddleware:
    @pytest.fixture
    def app(self):
        from agoracle.api.middleware.user_auth import UserAuthMiddleware

        app = FastAPI()
        state = MagicMock()
        state.user_store = MagicMock()
        app.add_middleware(UserAuthMiddleware, state_getter=lambda: state)

        @app.get("/api/test")
        async def test_endpoint(request: Request):
            return {"user_id": request.state.user_id}

        return app, state

    def test_bearer_session_token_is_checked_before_api_key(self, app):
        app_obj, state = app
        state.user_store.get_by_session_id = AsyncMock(
            return_value={"id": 123, "username": "alice", "is_admin": False}
        )
        state.user_store.get_by_api_key = AsyncMock(return_value=None)

        client = TestClient(app_obj)
        r = client.get("/api/test", headers={"Authorization": "Bearer sid-123"})

        assert r.status_code == 200
        assert r.json()["user_id"] == 123
        state.user_store.get_by_session_id.assert_awaited_once_with("sid-123")
        state.user_store.get_by_api_key.assert_not_awaited()

    def test_bearer_falls_back_to_api_key_lookup_when_session_missing(self, app):
        app_obj, state = app
        state.user_store.get_by_session_id = AsyncMock(return_value=None)
        state.user_store.get_by_api_key = AsyncMock(
            return_value={"id": 456, "username": "bob", "is_admin": False}
        )

        client = TestClient(app_obj)
        r = client.get("/api/test", headers={"Authorization": "Bearer sk-live"})

        assert r.status_code == 200
        assert r.json()["user_id"] == 456
        state.user_store.get_by_session_id.assert_awaited_once_with("sk-live")
        state.user_store.get_by_api_key.assert_awaited_once_with("sk-live")
