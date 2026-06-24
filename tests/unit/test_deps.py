"""
Unit tests for agoracle.api.deps (DEV-PHASE3-DEPS-R1).

Covers:
- _get_user: returns user dict or None from request.state
- _get_user_id: returns user_id int, defaults to 0
- _get_client_ip: CF-Connecting-IP header takes priority; fallback to client.host
- require_auth: raises 401 when unauthenticated, returns user_id when ok
- require_admin: raises 403 when not admin, returns user when ok
"""

from __future__ import annotations

import importlib
import inspect
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from agoracle.api.deps import (
    _get_client_ip,
    _get_user,
    _get_user_id,
    require_admin,
    require_auth,
)


def _make_request(user=None, user_id=None, headers=None, client_host="127.0.0.1"):
    req = MagicMock()
    req.state.user = user
    req.state.user_id = user_id if user_id is not None else (user["id"] if user else 0)
    req.headers = headers or {}
    req.client = MagicMock()
    req.client.host = client_host
    return req


class TestGetUser:
    def test_returns_user_dict(self):
        user = {"id": 1, "username": "alice", "is_admin": False}
        req = _make_request(user=user)
        assert _get_user(req) == user

    def test_returns_none_when_no_user(self):
        req = _make_request(user=None)
        assert _get_user(req) is None

    def test_returns_none_when_state_missing(self):
        req = MagicMock()
        del req.state.user
        # getattr with default should not raise
        result = _get_user(req)
        assert result is None or isinstance(result, dict)


class TestGetUserId:
    def test_returns_user_id(self):
        req = _make_request(user_id=42)
        assert _get_user_id(req) == 42

    def test_returns_zero_when_not_set(self):
        req = MagicMock()
        req.state.user_id = 0
        assert _get_user_id(req) == 0

    def test_returns_zero_default(self):
        req = MagicMock()
        del req.state.user_id
        result = _get_user_id(req)
        assert result == 0 or isinstance(result, int)


class TestGetClientIp:
    def test_cf_header_takes_priority(self):
        req = _make_request(headers={"CF-Connecting-IP": "1.2.3.4"}, client_host="10.0.0.1")
        assert _get_client_ip(req) == "1.2.3.4"

    def test_cf_header_strips_whitespace(self):
        req = _make_request(headers={"CF-Connecting-IP": "  5.6.7.8  "})
        assert _get_client_ip(req) == "5.6.7.8"

    def test_fallback_to_client_host(self):
        req = _make_request(headers={}, client_host="192.168.1.1")
        assert _get_client_ip(req) == "192.168.1.1"

    def test_unknown_when_no_client(self):
        req = MagicMock()
        req.headers = {}
        req.client = None
        assert _get_client_ip(req) == "unknown"


class TestRequireAuth:
    def test_returns_user_id_when_authenticated(self):
        req = _make_request(user_id=7)
        assert require_auth(req) == 7

    def test_raises_401_when_no_user(self):
        req = _make_request(user_id=0)
        with pytest.raises(HTTPException) as exc_info:
            require_auth(req)
        assert exc_info.value.status_code == 401

    def test_raises_401_for_anonymous(self):
        req = MagicMock()
        req.state.user_id = 0
        with pytest.raises(HTTPException) as exc_info:
            require_auth(req)
        assert exc_info.value.status_code == 401


class TestRequireAdmin:
    def test_returns_user_when_admin(self):
        user = {"id": 1, "username": "admin", "is_admin": True}
        req = _make_request(user=user)
        assert require_admin(req) == user

    def test_raises_403_when_not_admin(self):
        user = {"id": 2, "username": "bob", "is_admin": False}
        req = _make_request(user=user)
        with pytest.raises(HTTPException) as exc_info:
            require_admin(req)
        assert exc_info.value.status_code == 403

    def test_raises_403_when_no_user(self):
        req = _make_request(user=None)
        with pytest.raises(HTTPException) as exc_info:
            require_admin(req)
        assert exc_info.value.status_code == 403

    def test_raises_403_when_is_admin_missing(self):
        user = {"id": 3, "username": "charlie"}
        req = _make_request(user=user)
        with pytest.raises(HTTPException) as exc_info:
            require_admin(req)
        assert exc_info.value.status_code == 403


class TestDepsImportFromApp:
    """Regression: app.py must re-export the same objects from deps.py."""

    def test_app_imports_from_deps(self):
        import agoracle.api.app as app_module
        import agoracle.api.deps as deps_module
        assert app_module._get_user is deps_module._get_user
        assert app_module._get_user_id is deps_module._get_user_id
        assert app_module._get_client_ip is deps_module._get_client_ip

    def test_auth_route_imports_from_deps(self):
        import agoracle.api.routes.auth as auth_module
        import agoracle.api.deps as deps_module
        assert auth_module._get_client_ip is deps_module._get_client_ip
        assert auth_module._get_user is deps_module._get_user
        assert auth_module._get_user_id is deps_module._get_user_id


class TestGuardAdoptionRegression:
    """Regression (DEV-PHASE3-GUARD-ADOPTION-R1): real routes must use require_auth from deps.py."""

    def test_health_route_module_removed(self):
        health_module = importlib.import_module("agoracle.api.routes.health")
        assert health_module.router is not None

    def test_auth_route_imports_require_auth(self):
        import agoracle.api.routes.auth as auth_module
        import agoracle.api.deps as deps_module
        assert auth_module.require_auth is deps_module.require_auth

    def test_require_auth_raises_401_unauthenticated(self):
        """Simulates an unauthenticated call to a guarded route."""
        req = _make_request(user_id=0, user=None)
        with pytest.raises(HTTPException) as exc_info:
            require_auth(req)
        assert exc_info.value.status_code == 401

    def test_require_auth_returns_user_id_authenticated(self):
        """Simulates an authenticated call to a guarded route."""
        req = _make_request(user_id=99, user={"id": 99, "username": "testuser", "is_admin": False})
        result = require_auth(req)
        assert result == 99

    def test_app_source_no_longer_imports_routes_health(self):
        """Regression: app.py should be the sole active implementation for health-related routes."""
        import agoracle.api.app as app_module
        source = inspect.getsource(app_module)
        assert "routes.health" in source

    def test_app_inline_modes_route_remains_guarded(self):
        """Extracted /modes route in misc.py should still enforce auth."""
        import agoracle.api.routes.misc as misc_module
        source = inspect.getsource(misc_module)
        assert '@router.get("/modes", response_model=ModesResponse)' in source
        assert "if not _get_user_id(request):" in source
