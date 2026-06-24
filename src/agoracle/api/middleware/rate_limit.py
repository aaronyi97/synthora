"""
Rate limiting middleware — token bucket algorithm.

Protects API endpoints from abuse. Each IP gets a configurable
number of requests per time window.

Configuration via environment variables:
  RATE_LIMIT_RPM=30     (requests per minute, default 30)
  RATE_LIMIT_ENABLED=1  (set to 0 to disable)

Public paths (/api/health, /docs) are exempt.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

_EXEMPT_PATHS = {"/api/health", "/docs", "/openapi.json", "/redoc"}
_AUTH_PATHS = {
    "/api/auth/register",
    "/api/auth/register-phone",
    "/api/auth/send-code",
    "/api/auth/login",
}

# Per-username login lockout (CWE-307: brute force prevention)
_LOGIN_FAIL_LIMIT = 5       # failures before lockout
_LOGIN_LOCKOUT_SECONDS = 900  # 15 minutes


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Simple in-memory token bucket rate limiter.

    Each client IP gets `rpm` tokens per minute. Each request
    consumes one token. When tokens are exhausted, returns 429.

    NOTE: In-memory only — resets on restart. For production,
    use Redis-backed limiter. This is sufficient for single-instance MVP.
    """

    def __init__(self, app, rpm: int = 0, max_concurrent: int = 0) -> None:
        super().__init__(app)
        env_rpm = os.getenv("RATE_LIMIT_RPM", "")
        raw_rpm = rpm or (int(env_rpm) if env_rpm.isdigit() else 30)
        # RPM=0 means "disabled" (not "block everything")
        self._rpm = max(raw_rpm, 1) if raw_rpm > 0 else 0
        self._enabled = os.getenv("RATE_LIMIT_ENABLED", "1") != "0"
        if self._rpm == 0:
            self._enabled = False

        # Token bucket: {ip: [tokens_remaining, last_refill_time]}
        self._buckets: dict[str, list] = defaultdict(lambda: [self._rpm, time.monotonic()])

        # Concurrent request limiter (0 = unlimited)
        env_conc = os.getenv("RATE_LIMIT_MAX_CONCURRENT", "")
        conc = max_concurrent or (int(env_conc) if env_conc.isdigit() else 5)
        self._max_concurrent = conc
        self._semaphore = asyncio.Semaphore(conc) if conc > 0 else None

        # Bucket cleanup: evict IPs not seen in 5 minutes
        self._bucket_ttl = 300  # seconds
        self._last_cleanup = time.monotonic()
        self._max_buckets = 10000  # hard cap

        # Separate stricter bucket for auth endpoints (5 RPM per IP)
        env_auth_rpm = os.getenv("RATE_LIMIT_AUTH_RPM", "")
        self._auth_rpm = int(env_auth_rpm) if env_auth_rpm.isdigit() else 5
        self._auth_buckets: dict[str, list] = defaultdict(lambda: [self._auth_rpm, time.monotonic()])

        # Per-username login failure tracking: {username: [fail_count, first_fail_time]}
        self._login_failures: dict[str, list] = {}

        if self._enabled:
            logger.info(
                f"Rate limiting enabled: {self._rpm} RPM general, "
                f"{self._auth_rpm} RPM auth, {conc} max concurrent (global)"
            )
        else:
            logger.warning("Rate limiting DISABLED")

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path

        # Skip rate limiting for exempt paths and non-API paths
        if not self._enabled or path in _EXEMPT_PATHS or not path.startswith("/api"):
            return await call_next(request)

        # Get client IP — SEC-L1-05: trust CF-Connecting-IP (UFW limits origin to CF IPs)
        cf_ip = request.headers.get("CF-Connecting-IP")
        client_ip = cf_ip.strip() if cf_ip else (request.client.host if request.client else "unknown")

        # Periodic bucket cleanup (every 60s)
        now = time.monotonic()
        if now - self._last_cleanup > 60:
            self._cleanup_buckets(now)

        # Auth endpoints use separate stricter bucket
        is_auth = path in _AUTH_PATHS
        if is_auth:
            bucket = self._auth_buckets[client_ip]
            rpm = self._auth_rpm
        else:
            bucket = self._buckets[client_ip]
            rpm = self._rpm

        elapsed = now - bucket[1]

        # Refill tokens based on elapsed time
        if rpm > 0:
            refill_rate = rpm / 60.0  # tokens per second
            tokens_to_add = elapsed * refill_rate
            bucket[0] = min(rpm, bucket[0] + tokens_to_add)
            bucket[1] = now

        # Check if request is allowed
        if bucket[0] < 1:
            retry_after = max(1, -(-60 // rpm)) if rpm > 0 else 60
            logger.warning(f"Rate limit exceeded for {client_ip} on {path} ({'auth' if is_auth else 'general'})")
            return JSONResponse(
                status_code=429,
                content={
                    "detail": f"Rate limit exceeded. Max {rpm} requests/minute.",
                    "retry_after_seconds": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )

        # Consume one token
        bucket[0] -= 1

        # Auth endpoints: per-IP progressive lockout (CWE-307)
        if is_auth and path == "/api/auth/login":
            lockout_key = f"login:{client_ip}"
            entry = self._login_failures.get(lockout_key)
            if entry:
                fail_count, first_fail_time = entry
                elapsed_since_first = now - first_fail_time
                if fail_count >= _LOGIN_FAIL_LIMIT and elapsed_since_first < _LOGIN_LOCKOUT_SECONDS:
                    remaining = int(_LOGIN_LOCKOUT_SECONDS - elapsed_since_first)
                    logger.warning(f"Login lockout active for {client_ip}: {fail_count} failures, {remaining}s remaining")
                    return JSONResponse(
                        status_code=429,
                        content={
                            "detail": f"Too many failed login attempts. Try again in {remaining // 60 + 1} minutes.",
                            "retry_after_seconds": remaining,
                        },
                        headers={"Retry-After": str(remaining)},
                    )
                elif elapsed_since_first >= _LOGIN_LOCKOUT_SECONDS:
                    del self._login_failures[lockout_key]

            response = await call_next(request)
            if response.status_code in (401, 403):
                if lockout_key not in self._login_failures:
                    self._login_failures[lockout_key] = [1, now]
                else:
                    self._login_failures[lockout_key][0] += 1
            elif response.status_code == 200:
                self._login_failures.pop(lockout_key, None)
            return response

        # Auth endpoints (non-login) skip concurrent limiter (they're fast)
        if is_auth:
            return await call_next(request)

        # Concurrent request limiter — non-blocking try-acquire via zero-timeout wait.
        # Uses only standard asyncio API (no CPython _value dependency).
        if self._semaphore:
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=0.01)
            except asyncio.TimeoutError:
                bucket[0] = min(self._rpm, bucket[0] + 1)
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": f"服务器繁忙，请稍后重试（最大并发 {self._max_concurrent}）。",
                        "retry_after_seconds": 5,
                    },
                    headers={"Retry-After": "5"},
                )
            try:
                return await call_next(request)
            finally:
                self._semaphore.release()

        return await call_next(request)

    def _cleanup_buckets(self, now: float) -> None:
        """Evict stale IP buckets to prevent memory growth."""
        self._last_cleanup = now
        stale = [
            ip for ip, bucket in self._buckets.items()
            if now - bucket[1] > self._bucket_ttl
        ]
        for ip in stale:
            del self._buckets[ip]

        # Also clean auth buckets
        stale_auth = [
            ip for ip, bucket in self._auth_buckets.items()
            if now - bucket[1] > self._bucket_ttl
        ]
        for ip in stale_auth:
            del self._auth_buckets[ip]
        stale.extend(stale_auth)

        # Clean expired login failure entries
        stale_failures = [
            key for key, entry in self._login_failures.items()
            if now - entry[1] > _LOGIN_LOCKOUT_SECONDS
        ]
        for key in stale_failures:
            del self._login_failures[key]

        # Hard cap: if still over limit, evict oldest buckets
        if len(self._buckets) > self._max_buckets:
            sorted_ips = sorted(self._buckets.items(), key=lambda x: x[1][1])
            excess = len(self._buckets) - self._max_buckets
            for ip, _ in sorted_ips[:excess]:
                del self._buckets[ip]
            stale.extend(ip for ip, _ in sorted_ips[:excess])

        evicted = len(stale) + len(stale_failures)
        if evicted:
            logger.debug(f"Rate limiter: evicted {len(stale)} stale IP buckets, {len(stale_failures)} login failure entries")
