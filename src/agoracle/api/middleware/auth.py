"""
Authentication middleware — Bearer token protection for API endpoints.

Usage:
  1. Set AUTH_PASSWORD in .env
  2. All /api/* endpoints (except /api/health) require:
     Authorization: Bearer <AUTH_PASSWORD>
  3. If AUTH_PASSWORD is empty/unset, auth is disabled (dev mode)
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

# Paths that never require auth
_PUBLIC_PATHS = {"/api/health", "/docs", "/openapi.json", "/redoc"}


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """
    Simple Bearer token auth middleware.

    If AUTH_PASSWORD env var is set and non-empty, all /api/* requests
    (except health) must include 'Authorization: Bearer <token>'.
    If AUTH_PASSWORD is empty, auth is disabled (development mode).
    """

    def __init__(self, app, auth_password: str = "") -> None:
        super().__init__(app)
        self._token = auth_password or os.getenv("AUTH_PASSWORD", "")
        if self._token:
            logger.info("API auth enabled (Bearer token)")
        else:
            logger.warning("API auth DISABLED (AUTH_PASSWORD not set)")

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Skip auth for public paths and non-API paths
        path = request.url.path
        if not self._token or path in _PUBLIC_PATHS or not path.startswith("/api"):
            return await call_next(request)

        # Check Authorization header
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header. Use: Bearer <token>"},
            )

        token = auth_header[7:]  # strip "Bearer "
        if not hmac.compare_digest(token, self._token):
            return JSONResponse(
                status_code=403,
                content={"detail": "Invalid token"},
            )

        return await call_next(request)
