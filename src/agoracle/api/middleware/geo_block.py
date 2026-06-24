"""
GeoBlockMiddleware — FM-06: 受限地区访问控制

策略：
- 通过 CF-Connecting-IP + Cloudflare 提供的 CF-IPCountry 头判断用户所在地区
- 受限地区列表由环境变量 GEO_BLOCK_COUNTRIES 配置（逗号分隔，默认空=不阻断）
- 例: GEO_BLOCK_COUNTRIES=KP,CU,IR,SY
- /api/health 端点不受 Geo-blocking 影响（监控探针需要）
"""

from __future__ import annotations

import logging
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

_BLOCKED_COUNTRIES: set[str] = set()

_raw = os.environ.get("GEO_BLOCK_COUNTRIES", "").strip()
if _raw:
    _BLOCKED_COUNTRIES = {c.strip().upper() for c in _raw.split(",") if c.strip()}
    if _BLOCKED_COUNTRIES:
        logger.info(f"[GeoBlock] Active — blocked countries: {sorted(_BLOCKED_COUNTRIES)}")
else:
    logger.debug("[GeoBlock] Disabled — GEO_BLOCK_COUNTRIES not set")

_EXEMPT_PATHS = {"/api/health"}


class GeoBlockMiddleware(BaseHTTPMiddleware):
    """Block requests from restricted countries based on CF-IPCountry header.

    Only active when GEO_BLOCK_COUNTRIES env var is set.
    Falls back gracefully if header is absent (Cloudflare not in path).
    """

    async def dispatch(self, request: Request, call_next):
        if not _BLOCKED_COUNTRIES:
            return await call_next(request)

        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        country = request.headers.get("CF-IPCountry", "").upper()
        if country and country in _BLOCKED_COUNTRIES:
            logger.warning(
                f"[GeoBlock] Blocked request from country={country} "
                f"path={request.url.path}"
            )
            return JSONResponse(
                status_code=451,
                content={
                    "error_code": "GEO_BLOCKED",
                    "detail": "本服务暂不对您所在地区开放。Service unavailable in your region.",
                },
            )

        return await call_next(request)
