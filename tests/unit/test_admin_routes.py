"""
Unit/integration tests for admin routes (DEV-PHASE3-ROUTES-ADMIN-R1).

Covers:
- Admin endpoints are provided by routes/admin.py (not inline in app.py)
- require_admin guard: unauthenticated → 403, admin → pass
- Admin module imports require_admin from deps.py
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from agoracle.api.deps import require_admin, _get_user


def _make_request(user=None, user_id=None):
    req = MagicMock()
    req.state.user = user
    req.state.user_id = user_id if user_id is not None else (user["id"] if user else 0)
    req.headers = {}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    return req


class TestAdminModuleImports:
    """Regression: admin.py must use require_admin from deps.py."""

    def test_admin_imports_require_admin_from_deps(self):
        import agoracle.api.routes.admin as admin_module
        import agoracle.api.deps as deps_module
        assert admin_module.require_admin is deps_module.require_admin

    def test_admin_router_has_three_routes(self):
        import agoracle.api.routes.admin as admin_module
        paths = {r.path for r in admin_module.router.routes}
        assert "/admin/usage" in paths
        assert "/admin/usage/{user_id}" in paths
        assert "/admin/cost-report" in paths

    def test_app_no_longer_has_inline_admin_usage(self):
        """Regression: inline admin endpoints must not exist in app.py _build_router body."""
        import inspect
        import agoracle.api.app as app_module
        source = inspect.getsource(app_module._build_router)
        assert 'def admin_usage' not in source
        assert 'def admin_user_usage' not in source
        assert 'def admin_cost_report' not in source

    def test_app_includes_admin_router(self):
        """app.py _build_router must include the admin router."""
        import inspect
        import agoracle.api.app as app_module
        source = inspect.getsource(app_module._build_router)
        assert 'routes.admin' in source or 'admin_router' in source


class TestRequireAdminGuard:
    """Guard behaviour used by admin routes."""

    def test_require_admin_raises_403_for_unauthenticated(self):
        req = _make_request(user=None, user_id=0)
        with pytest.raises(HTTPException) as exc_info:
            require_admin(req)
        assert exc_info.value.status_code == 403

    def test_require_admin_raises_403_for_non_admin(self):
        user = {"id": 5, "username": "alice", "is_admin": False}
        req = _make_request(user=user)
        with pytest.raises(HTTPException) as exc_info:
            require_admin(req)
        assert exc_info.value.status_code == 403

    def test_require_admin_returns_user_for_admin(self):
        user = {"id": 1, "username": "admin", "is_admin": True}
        req = _make_request(user=user)
        result = require_admin(req)
        assert result == user
        assert result["is_admin"] is True

    def test_require_admin_raises_403_when_is_admin_false(self):
        user = {"id": 7, "username": "bob", "is_admin": False}
        req = _make_request(user=user)
        with pytest.raises(HTTPException) as exc_info:
            require_admin(req)
        assert exc_info.value.status_code == 403


class TestAdminEndpointsBehaviourUnit:
    """Unit-level behaviour checks for admin handlers."""

    def test_admin_usage_calls_require_admin(self):
        """admin_usage must call require_admin before accessing quota_service."""
        import inspect
        import agoracle.api.routes.admin as admin_module
        source = inspect.getsource(admin_module.admin_usage)
        assert "require_admin(request)" in source

    def test_admin_user_usage_calls_require_admin(self):
        import inspect
        import agoracle.api.routes.admin as admin_module
        source = inspect.getsource(admin_module.admin_user_usage)
        assert "require_admin(request)" in source

    def test_admin_cost_report_calls_require_admin(self):
        import inspect
        import agoracle.api.routes.admin as admin_module
        source = inspect.getsource(admin_module.admin_cost_report)
        assert "require_admin(request)" in source

    def test_no_inline_is_admin_check_in_admin_module(self):
        """admin.py must not contain old-style inline is_admin guard."""
        import inspect
        import agoracle.api.routes.admin as admin_module
        source = inspect.getsource(admin_module)
        assert 'not user or not user.get("is_admin")' not in source
