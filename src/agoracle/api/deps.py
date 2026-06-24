"""
API dependency helpers — shared across routes.

Extracted from app.py as part of Phase 3 deps extraction (DEV-PHASE3-DEPS-R1).
All behaviour is identical to the original inline implementations.

These functions are intentionally pure (no state imports at module level) so they
can be imported by any route module without triggering circular imports.
"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, Request


def _get_user(request: Request) -> Optional[dict]:
    """Extract user from request state (set by UserAuthMiddleware)."""
    return getattr(request.state, "user", None)


def _get_user_id(request: Request) -> int:
    """Extract user_id from request state. Returns 0 if no user."""
    return getattr(request.state, "user_id", 0)


def _get_client_ip(request: Request) -> str:
    """Extract real client IP. SEC-L1-05/06: unified IP extraction.
    CF Tunnel + UFW architecture: CF-Connecting-IP is trustworthy because
    UFW restricts origin to CF IP ranges only. X-Forwarded-For removed (forgeable).
    Fallback to request.client.host for non-CF environments (dev/test)."""
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()
    return request.client.host if request.client else "unknown"


def normalize_locale(raw: str | None) -> str:
    """Normalize arbitrary locale input to the two supported internal values."""
    if not raw:
        return "zh-CN"
    value = raw.strip().replace("_", "-").lower()
    if value.startswith("en"):
        return "en-US"
    if value.startswith("zh"):
        return "zh-CN"
    return "zh-CN"


def parse_accept_language(header: str | None) -> str | None:
    """Extract the primary locale tag from Accept-Language."""
    if not header:
        return None
    primary = header.split(",")[0].split(";")[0].strip()
    return primary or None


def resolve_language(
    request_locale: str | None,
    accept_language: str | None,
    user_profile: object | None,
) -> str:
    """Resolve locale from request > header > user profile > default."""
    preferred = getattr(user_profile, "preferred_language", None) if user_profile else None
    raw = request_locale or parse_accept_language(accept_language) or preferred or "zh-CN"
    return normalize_locale(raw)


def require_auth(request: Request) -> int:
    """Guard: raise 401 if not authenticated. Returns user_id."""
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


def require_admin(request: Request) -> dict:
    """Guard: raise 403 if not admin. Returns user dict."""
    user = _get_user(request)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def get_app_state(request: Request):
    """Get AppState — prefers request.app.state, falls back to module-level state."""
    _app_state = getattr(getattr(request, "app", None), "state", None)
    if _app_state is not None and hasattr(_app_state, "config"):
        return _app_state
    import agoracle.api.app as _m
    return _m.state


def get_stream_limiter():
    """Return per-user SSE stream concurrency primitives.

    Returns (counts_dict, asyncio.Lock, max_streams_int).
    """
    import agoracle.api.app as _m
    return _m._user_stream_counts, _m._stream_count_lock, _m._MAX_STREAMS_PER_USER
