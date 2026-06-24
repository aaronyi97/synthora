"""
User-aware authentication middleware.

Replaces the simple single-token BearerAuthMiddleware with per-user API key auth.
Attaches user info to request.state for downstream endpoints.

Fallback: if no UserStore is available, falls back to legacy AUTH_PASSWORD check.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Callable, Optional

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

# Paths that never require auth
_PUBLIC_PATHS = {
    "/api/health",
    "/api/auth/register",
    "/api/auth/register-phone",
    "/api/auth/send-code",
    "/api/auth/login",
    "/api/auth/logout",
}


class UserAuthMiddleware(BaseHTTPMiddleware):
    """
    Per-user API key auth middleware.

    - Looks up Bearer token in UserStore → attaches user to request.state
    - Falls back to legacy AUTH_PASSWORD if UserStore not available
    - Public paths bypass auth entirely
    - Uses state_getter (callable) to lazily access user_store after lifespan init
    """

    def __init__(self, app, state_getter=None) -> None:
        super().__init__(app)
        self._state_getter = state_getter  # callable returning obj with .user_store
        self._legacy_token = os.getenv("AUTH_PASSWORD", "")
        logger.info("UserAuthMiddleware initialized (lazy user_store binding)")

    def _get_user_store(self):
        if self._state_getter:
            s = self._state_getter()
            return getattr(s, 'user_store', None)
        return None

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        method = request.method

        # Skip auth for OPTIONS (CORS preflight) and public paths
        if method == "OPTIONS" or path in _PUBLIC_PATHS or not path.startswith("/api"):
            return await call_next(request)

        user_store = self._get_user_store()

        # Extract credential: HttpOnly session cookie first (browser), then Bearer header (API clients)
        session_id = request.cookies.get("session", "")
        bearer_token = ""
        if not session_id:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                bearer_token = auth_header[7:]

        if not session_id and not bearer_token:
            # Allow if no auth configured at all
            if not user_store and not self._legacy_token:
                return await call_next(request)
            return JSONResponse(
                status_code=401,
                content={"error_code": "AUTH_REQUIRED", "detail": "Authentication required"},
            )

        # Try user store first
        if user_store:
            user = None
            try:
                if session_id:
                    # SEC-003: session_id → user lookup (Cookie value ≠ api_key)
                    user = await user_store.get_by_session_id(session_id)
                elif bearer_token:
                    # API client fallback: first treat Bearer as session_id, then as api_key.
                    user = await user_store.get_by_session_id(bearer_token)
                    if not user:
                        user = await user_store.get_by_api_key(bearer_token)
            except Exception as e:
                logger.error(f"UserStore lookup failed: {e}")
                user = None

            if user:
                request.state.user = user
                request.state.user_id = user["id"]
                return await call_next(request)

            # Token not found in user store
            return JSONResponse(
                status_code=403,
                content={"error_code": "AUTH_FORBIDDEN", "detail": "Invalid or expired session"},
            )

        # Legacy single-token fallback
        if self._legacy_token:
            if hmac.compare_digest(bearer_token, self._legacy_token):
                request.state.user = None
                request.state.user_id = 0
                return await call_next(request)
            return JSONResponse(
                status_code=403,
                content={"error_code": "AUTH_FORBIDDEN", "detail": "Invalid token"},
            )

        # No auth configured
        return await call_next(request)
