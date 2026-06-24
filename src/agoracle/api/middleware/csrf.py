"""
CSRF protection middleware — Origin/Referer validation.

Protects state-changing endpoints (POST/PUT/DELETE) from cross-site
request forgery by validating the Origin or Referer header against
the configured CORS_ORIGINS whitelist.

Strategy:
  1. For state-changing methods, require Origin or Referer header
  2. Validate against allowed origins (from CORS_ORIGINS env var)
  3. Reject requests from unknown origins with 403

This works in tandem with SameSite=None cookies by adding a second
layer of defense (defense in depth per OWASP CSRF Prevention Cheat Sheet).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Callable
from urllib.parse import urlparse

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Paths exempt from CSRF check (public endpoints, no state change)
# NOTE: /api/auth/login and /api/auth/register intentionally removed from exempt list
# (audit 2026-02-22, login CSRF prevention). API clients without session cookie are
# still allowed via the "no Origin + no session cookie" branch below (line ~82).
_CSRF_EXEMPT = {"/api/health"}

# NOTE: IP-based trust removed (audit 2026-02-21). In nginx reverse proxy
# setup ALL requests arrive from 127.0.0.1, making IP trust = CSRF disabled.
# Origin/Referer validation is the correct mechanism for browser requests.


def _normalize_origin(value: str) -> str | None:
    """
    Normalize origin to `scheme://host[:port]`.

    Default ports are canonicalized away:
    - https://example.com:443 -> https://example.com
    - http://example.com:80 -> http://example.com
    """
    raw = (value or "").strip()
    if not raw:
        return None
    if raw == "null":
        return "null"

    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.hostname:
        return None

    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    port = parsed.port

    if port is None:
        return f"{scheme}://{host}"
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


class CSRFMiddleware(BaseHTTPMiddleware):
    """Validate Origin/Referer on state-changing requests."""

    def __init__(
        self,
        app,
        allowed_origins: list[str] | None = None,
        allow_origin_regex: str | None = None,
        allow_opaque_origin: bool = False,
    ) -> None:
        super().__init__(app)
        if allowed_origins:
            allowed_raw = {o.strip() for o in allowed_origins if o and o.strip()}
        else:
            cors_env = os.getenv("CORS_ORIGINS", "")
            allowed_raw = {o.strip() for o in cors_env.split(",") if o.strip()} if cors_env else set()
        # In dev mode (no CORS_ORIGINS), allow localhost
        if not allowed_raw:
            allowed_raw = {"http://localhost:5173", "http://127.0.0.1:5173"}

        # Dev mode regex: match any localhost/127.0.0.1 port
        # (local preview servers, random Vite ports, etc.)
        self._origin_regex: re.Pattern | None = re.compile(allow_origin_regex) if allow_origin_regex else None
        self._allow_opaque_origin = allow_opaque_origin

        # Defensive: detect wildcard "*" which _normalize_origin maps to None.
        # Without this, CORS_ORIGINS=* silently creates an empty whitelist that
        # blocks ALL browser requests — the root cause of the 2026-02-25 outage.
        if "*" in allowed_raw:
            logger.warning(
                "CSRF: wildcard '*' detected in allowed_origins. "
                "All origins will be accepted (CSRF origin check effectively disabled). "
                "This is INSECURE for production — set CORS_ORIGINS to explicit domains."
            )
            self._allowed_all = True
            self._allowed: set[str] = set()
        else:
            self._allowed_all = False
            normalized = {_normalize_origin(o) for o in allowed_raw}
            self._allowed = {o for o in normalized if o and o != "null"}
        logger.info(f"CSRF protection enabled for origins: {'*' if self._allowed_all else self._allowed}")
        if self._origin_regex:
            logger.info(f"CSRF dev-mode regex enabled: {allow_origin_regex}")
        if self._allow_opaque_origin:
            logger.warning("CSRF: opaque Origin 'null' accepted (development mode only)")

    def _extract_origin(self, request: Request) -> str | None:
        """Extract origin from Origin header, falling back to Referer."""
        origin = request.headers.get("origin")
        if origin:
            return origin  # includes "null" — caller must reject it explicitly

        referer = request.headers.get("referer")
        if referer:
            parsed = urlparse(referer)
            return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else None

        return None

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Safe methods and exempt paths skip CSRF check
        if request.method in _SAFE_METHODS:
            return await call_next(request)

        path = request.url.path
        if path in _CSRF_EXEMPT or not path.startswith("/api"):
            return await call_next(request)

        origin = self._extract_origin(request)

        # If no origin header at all:
        # - Requests WITH session cookie but NO Origin → likely attack (reject)
        # - Requests WITHOUT cookie and NO Origin → API client like curl (allow)
        if origin is None:
            has_session_cookie = bool(request.cookies.get("session"))
            if has_session_cookie:
                logger.warning(f"CSRF blocked: session cookie present but no Origin/Referer, path={path}")
                return JSONResponse(
                    status_code=403,
                    content={
                        "error_code": "CSRF_REJECTED",
                        "detail": "Origin header required for credentialed requests",
                    },
                )
            return await call_next(request)

        # Opaque origin (browser sandbox/iframe) — treat as untrusted, reject explicitly
        normalized_origin = _normalize_origin(origin)
        if normalized_origin == "null":
            if self._allow_opaque_origin:
                return await call_next(request)
            logger.warning(f"CSRF blocked: opaque Origin 'null' (sandbox/iframe context), path={path}")
            return JSONResponse(
                status_code=403,
                content={"error_code": "CSRF_REJECTED", "detail": "CSRF origin validation failed"},
            )

        if not normalized_origin:
            logger.warning(f"CSRF blocked: malformed Origin={origin!r}, path={path}")
            return JSONResponse(
                status_code=403,
                content={"error_code": "CSRF_REJECTED", "detail": "CSRF origin validation failed"},
            )

        # Wildcard mode: accept all origins (CORS_ORIGINS=*)
        if self._allowed_all:
            return await call_next(request)

        # Validate origin against whitelist
        if normalized_origin not in self._allowed:
            # Dev mode: try regex match (any localhost/127.0.0.1 port)
            if self._origin_regex and self._origin_regex.fullmatch(normalized_origin):
                return await call_next(request)
            logger.warning(
                f"CSRF blocked: origin={origin} (normalized={normalized_origin}) not in {self._allowed}, path={path}"
            )
            return JSONResponse(
                status_code=403,
                content={
                    "error_code": "CSRF_REJECTED",
                    "detail": "CSRF origin validation failed",
                },
            )

        return await call_next(request)
