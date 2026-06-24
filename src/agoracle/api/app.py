"""
FastAPI application — Web API for agoracle.

Endpoints:
  POST /api/ask          — single-shot answer (Light/Deep/Research)
  POST /api/ask/stream   — SSE streaming answer
  POST /api/socratic/start   — start a Socratic session (Phase 1)
  POST /api/socratic/respond — send user message (Phase 2)
  POST /api/socratic/reveal  — reveal full answer
  GET  /api/models       — list available models
  POST /api/feedback     — record user feedback
  GET  /api/health       — health check
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import sys
import uuid
from datetime import datetime
from contextlib import asynccontextmanager
from importlib.metadata import version as pkg_version
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

import sentry_sdk
from fastapi import FastAPI, HTTPException, Query, Request, UploadFile, File as FastAPIFile
from agoracle.api.deps import _get_user, _get_user_id, _get_client_ip, require_auth, require_admin
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.responses import Response as StarletteResponse
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse

from agoracle.adapters.judge.llm_judge import LLMJudge
from agoracle.adapters.judge.metadata_extractor import LLMMetadataExtractor
from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter
from agoracle.adapters.profile.json_profile import JsonProfileStore
from agoracle.api.schemas import (
    AvailableModelsResponse,
    DraftAnswerModel,
    FeedbackResponse,
    PricingResponse,
    SearchCitationModel,
    UploadResponse,
    HealthResponse,
    ModesResponse,
    QuotaResponse,
    RoundtableCheckResponse,
    RoundtableChoiceResponse,
    RoundtableResumeResponse,
    SocraticRevealResponse,
    SocraticRespondResponse,
    SocraticStartResponse,
)
from agoracle.config.loader import PROJECT_ROOT, load_config
from agoracle.config.schema import AppConfig
from agoracle.domain.router import route
from agoracle.domain.types import (
    Intent,
    Mode,
    OutputDepth,
    Attachment,
    QueryContext,
    QuestionType,
)
from agoracle.domain.events import ModelCallFailed, QueryCompleted
from agoracle.services.behavior_analytics import BehaviorAnalytics
from agoracle.services.critique_logger import CritiqueLogger
from agoracle.services.event_bus import EventBus
from agoracle.services.failure_monitor import FailureMonitor
from agoracle.services.orchestrator import Orchestrator
from agoracle.services.prompt_loader import PromptLoader
from agoracle.services.quota import QuotaService
from agoracle.services.search_service import SearchService
from agoracle.adapters.session.sqlite_conversation_store import SQLiteConversationStore
from agoracle.adapters.session.sqlite_socratic_store import SQLiteSocraticSessionStore
from agoracle.adapters.session.sqlite_roundtable_store import SQLiteRoundtableStore
from agoracle.adapters.user.sqlite_user_store import SQLiteUserStore
from agoracle.ports.socratic_session_port import SocraticSessionStorePort
from agoracle.services.socratic_orchestrator import SocraticOrchestrator
from agoracle.services.streaming import (
    CitationsReady,
    CompanionRoute,
    ContributorDone,
    DraftAnswer,
    Heartbeat,
    JudgeToken,
    PipelineComplete,
    PipelineError,
    PreviewAnswer,
    StageCompleted,
    StageStarted,
    execute_streaming,
)

logger = logging.getLogger(__name__)


def _sentry_before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any]:
    del hint  # unused, kept for Sentry callback signature
    request = event.get("request")
    if not isinstance(request, dict):
        return event

    headers = request.get("headers")
    if isinstance(headers, dict):
        request["headers"] = {
            key: value
            for key, value in headers.items()
            if key.lower() not in ("cookie", "authorization")
        }

    url = request.get("url")
    if isinstance(url, str) and url:
        parsed = urlparse(url)
        request["url"] = urlunparse(parsed._replace(query=""))

    data = request.get("data")
    if isinstance(data, dict) and "question" in data:
        data["question"] = "[REDACTED]"

    return event


def _get_version() -> str:
    """Read version from package single source (agoracle.__version__)."""
    try:
        from agoracle import __version__

        return __version__
    except Exception:
        try:
            return pkg_version("agoracle")
        except Exception:
            return "0.0.0-dev"


def _is_production() -> bool:
    """Single source of truth for ENV=production check.

    Normalises with strip().lower() so 'Production', ' production ' etc. all work.
    """
    return os.getenv("ENV", "development").strip().lower() == "production"


def _get_runtime_environment() -> str:
    """Resolve runtime environment for telemetry and deployment guards."""
    return (os.getenv("SENTRY_ENVIRONMENT") or os.getenv("ENV") or "development").strip().lower() or "development"


def _get_sentry_release() -> str:
    """Resolve Sentry release string with explicit override support."""
    release = (os.getenv("SENTRY_RELEASE") or os.getenv("APP_VERSION") or _get_version()).strip()
    return release or "0.0.0-dev"


def _configured_worker_count(argv: list[str] | None = None) -> int:
    """Best-effort detection of configured worker count from env/CLI flags."""
    env_candidates = [
        os.getenv("WEB_CONCURRENCY", "").strip(),
        os.getenv("GUNICORN_WORKERS", "").strip(),
    ]
    gunicorn_cmd_args = os.getenv("GUNICORN_CMD_ARGS", "").strip()
    if gunicorn_cmd_args:
        argv = [*(argv or []), *shlex.split(gunicorn_cmd_args)]
    for value in env_candidates:
        if value:
            try:
                return max(int(value), 1)
            except ValueError:
                continue

    args = argv or sys.argv
    for index, arg in enumerate(args):
        if arg in {"--workers", "-w"} and index + 1 < len(args):
            try:
                return max(int(args[index + 1]), 1)
            except ValueError:
                continue
        if arg.startswith("--workers="):
            try:
                return max(int(arg.split("=", 1)[1]), 1)
            except ValueError:
                continue
        if arg.startswith("-w") and arg != "-w":
            try:
                return max(int(arg[2:]), 1)
            except ValueError:
                continue
    return 1


def _assert_single_worker_sqlite_runtime() -> None:
    """Fail fast when SQLite-backed stores are combined with multi-worker config."""
    workers = _configured_worker_count()
    if workers <= 1:
        return
    raise RuntimeError(
        "FATAL: SQLite-backed session/user stores require a single worker process. "
        f"Detected workers={workers}. Set '--workers 1' or migrate these stores "
        "to a multi-worker-safe database before scaling out."
    )


# ── Shared state (initialized at startup) ──────────────────

class AppState:
    config: AppConfig
    model_adapter: OpenAIModelAdapter
    judge: LLMJudge
    extractor: LLMMetadataExtractor
    prompt_loader: PromptLoader
    event_bus: EventBus
    search_service: Optional[SearchService]
    profile_store: Optional[JsonProfileStore]
    session_store: Optional[SocraticSessionStorePort]
    socratic_orch: Optional[SocraticOrchestrator]
    user_store: Optional[SQLiteUserStore]
    behavior_analytics: Optional[BehaviorAnalytics]
    quota_service: Optional[QuotaService]
    proactive_coach: Optional[Any]  # ProactiveCoachService (v2.7.9d)
    # Persistent conversation store for Light/Deep/Research multi-turn (v2.8.5)
    conversation_store: Optional[SQLiteConversationStore]
    roundtable_store: Optional[SQLiteRoundtableStore]
    feedback_store: Optional[Any] = None


state = AppState()
state.conversation_store = None

# INT-10: per-user active SSE stream counter (in-memory, sufficient for single-instance)
_user_stream_counts: dict[int, int] = {}
_stream_count_lock = asyncio.Lock()
_MAX_STREAMS_PER_USER = int(os.getenv("MAX_STREAMS_PER_USER", "3"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize shared resources on startup."""
    import time as _t
    _t0 = _t.monotonic()
    def _elapsed() -> str:
        return f"{_t.monotonic() - _t0:.2f}s"

    logger.info(f"[STARTUP] begin")
    # P2: Startup self-check — diagnose stale pip install or wrong code path
    import agoracle as _pkg_root
    _running_version = _get_version()
    _pkg_path = getattr(_pkg_root, "__file__", "unknown")
    _env_raw = repr(os.environ.get("ENV", "<not set>"))
    logger.info(
        f"[STARTUP] version={_running_version} | "
        f"pkg_path={_pkg_path} | "
        f"ENV={_env_raw}"
    )
    _assert_single_worker_sqlite_runtime()
    # SEC: Fail-fast if production ENV is not set but HMAC fallback is active
    _startup_env = os.getenv("ENV", "development").strip().lower()
    _startup_is_prod = _startup_env == "production"
    _hmac_secret_set = bool(os.getenv("API_KEY_HMAC_SECRET", ""))
    _allow_fallback = _startup_env in ("development", "test")
    if _startup_is_prod and not _hmac_secret_set:
        raise RuntimeError(
            "FATAL: ENV=production but API_KEY_HMAC_SECRET is not set. "
            "Generate with: python3 -c \"import secrets; print(secrets.token_hex(32))\""
        )
    if not _allow_fallback and not _hmac_secret_set:
        raise RuntimeError(
            f"FATAL: ENV={_startup_env} requires API_KEY_HMAC_SECRET to be set. "
            "Generate with: python3 -c \"import secrets; print(secrets.token_hex(32))\""
        )
    state.config = load_config()
    logger.info(f"[STARTUP] config loaded ({_elapsed()})")
    state.prompt_loader = PromptLoader(PROJECT_ROOT / "prompts")
    state.model_adapter = OpenAIModelAdapter(state.config)
    state.judge = LLMJudge(state.model_adapter, state.prompt_loader)
    state.extractor = LLMMetadataExtractor(state.model_adapter, state.prompt_loader)
    state.event_bus = EventBus()
    state.profile_store = JsonProfileStore(
        PROJECT_ROOT / state.config.memory.profile_path
    )
    logger.info(f"[STARTUP] core services ready ({_elapsed()})")

    sc = state.config.search
    state.search_service = SearchService(
        api_key_env=sc.api_key_env,
        max_results=sc.max_results,
        search_depth=sc.search_depth,
        include_answer=sc.include_answer,
        timeout_seconds=sc.timeout_seconds,
    ) if sc.enabled else None

    # Socratic session persistence (SQLite)
    state.session_store = SQLiteSocraticSessionStore(
        PROJECT_ROOT / "data" / "socratic_sessions.db"
    )
    await state.session_store.initialize()
    logger.info(f"[STARTUP] session_store initialized ({_elapsed()})")

    # Roundtable session persistence (SQLite)
    state.roundtable_store = SQLiteRoundtableStore(
        PROJECT_ROOT / "data" / "roundtable_sessions.db"
    )
    await state.roundtable_store.initialize()
    logger.info(f"[STARTUP] roundtable_store initialized ({_elapsed()})")

    state.socratic_orch = SocraticOrchestrator(
        config=state.config,
        model_adapter=state.model_adapter,
        judge=state.judge,
        extractor=state.extractor,
        prompt_loader=state.prompt_loader,
        event_bus=state.event_bus,
        search_service=state.search_service,
        profile_store=state.profile_store,
        session_store=state.session_store,
    )
    logger.info(f"[STARTUP] socratic_orch ready ({_elapsed()})")

    # User store
    state.user_store = SQLiteUserStore(PROJECT_ROOT / "data" / "users.db")
    await state.user_store.initialize()
    logger.info(f"[STARTUP] user_store initialized ({_elapsed()})")

    # Create or sync admin password if ADMIN_PASSWORD is explicitly set
    admin_pw = os.getenv("ADMIN_PASSWORD", "")
    if admin_pw:
        try:
            await state.user_store.register("admin", admin_pw, "Admin", is_admin=True)
            logger.info(f"[STARTUP] admin user created ({_elapsed()})")
        except ValueError:
            # Admin exists — sync password to match env var
            await state.user_store.update_password("admin", admin_pw)
            logger.info(f"[STARTUP] admin password synced ({_elapsed()})")
        except Exception as e:
            logger.error(f"[STARTUP] admin user setup FAILED ({_elapsed()}): {e}")
    else:
        logger.warning("ADMIN_PASSWORD not set — no default admin created. Set env var to enable.")

    # Persistent conversation store (v2.8.5: replaces volatile in-memory dict)
    try:
        state.conversation_store = SQLiteConversationStore(
            PROJECT_ROOT / "data" / "conversations.db"
        )
        await state.conversation_store.initialize()
        logger.info(f"[STARTUP] conversation_store initialized ({_elapsed()})")
        # v2.9: Run cleanup once at startup to prevent unbounded DB growth.
        # cleanup_expired() removes turns older than ttl_days (default 30d).
        try:
            deleted = await state.conversation_store.cleanup_expired()
            if deleted:
                logger.info(f"[STARTUP] conversation_store cleanup: {deleted} expired turns removed")
        except Exception as _ce:
            logger.warning(f"[STARTUP] conversation_store cleanup failed (non-fatal): {_ce}")
    except Exception as e:
        logger.warning(f"[STARTUP] conversation_store FAILED ({_elapsed()}): {e}")
        state.conversation_store = None

    # Quota service (v2.7.5)
    state.quota_service = QuotaService(
        state.config.quota, data_dir=str(PROJECT_ROOT / "data")
    )

    # Proactive Coach (v2.7.9d) — must init before BehaviorAnalytics
    from agoracle.services.proactive_coach import ProactiveCoachService
    state.proactive_coach = ProactiveCoachService(state.profile_store)
    logger.info(f"[STARTUP] proactive_coach ready ({_elapsed()})")

    # CBA Behavior Analytics subscriber (ADR-014)
    state.behavior_analytics = BehaviorAnalytics(
        state.profile_store,
        proactive_coach=state.proactive_coach,
        model_adapter=state.model_adapter,  # Memory-lite 方案A: LLM摘要
    )
    state.event_bus.subscribe(
        QueryCompleted,
        state.behavior_analytics.on_query_completed,
        critical=False,  # non-blocking, fire-and-forget
    )

    # v4.29: FailureMonitor — 模型熔断与告警
    state.failure_monitor = FailureMonitor()
    state.event_bus.subscribe(
        ModelCallFailed,
        state.failure_monitor.on_model_call_failed,
        critical=False,
    )

    # v4.29: CritiqueLogger — content_critique 错题本落盘
    state.critique_logger = CritiqueLogger()
    await state.critique_logger.start()
    state.event_bus.subscribe(
        QueryCompleted,
        state.critique_logger.on_query_completed,
        critical=False,
    )
    # TODO(WP4-EVENT-WIRE): SocraticSessionCompleted is emitted by SocraticOrchestrator,
    # but app-level subscription stays intentionally unwired for now.
    # CognitiveTracker persistence already happens directly inside the orchestrator,
    # and no second concrete subscriber exists yet.
    logger.info(f"[STARTUP] event_bus subscribers registered ({_elapsed()}) — BehaviorAnalytics + FailureMonitor + CritiqueLogger")

    available = state.model_adapter.available_models
    logger.info(f"[STARTUP] COMPLETE ({_elapsed()}) — {len(available)} models available")
    yield
    # Graceful shutdown
    if state.user_store:
        await state.user_store.close()
    if state.session_store:
        await state.session_store.close()
    if hasattr(state, 'roundtable_store') and state.roundtable_store:
        await state.roundtable_store.close()
    if state.conversation_store:
        await state.conversation_store.close()
    if hasattr(state, 'critique_logger') and state.critique_logger:
        await state.critique_logger.stop()
    logger.info("API server shutting down")


def create_app() -> FastAPI:
    from agoracle.api.middleware.user_auth import UserAuthMiddleware

    sentry_sdk.init(
        dsn=os.getenv("SENTRY_DSN"),
        environment=_get_runtime_environment(),
        release=_get_sentry_release(),
        traces_sample_rate=0.1,
        before_send=_sentry_before_send,
    )

    is_prod = _is_production()

    app = FastAPI(
        title="Synthora API",
        description="Multi-model AI orchestration — answers that surpass any single model",
        version=_get_version(),
        lifespan=lifespan,
        # SEC-004: disable API docs in production to prevent reconnaissance
        docs_url=None if is_prod else "/docs",
        redoc_url=None if is_prod else "/redoc",
        openapi_url=None if is_prod else "/openapi.json",
    )
    cors_env = os.getenv("CORS_ORIGINS", "")
    if is_prod and not cors_env:
        raise RuntimeError(
            "FATAL: CORS_ORIGINS must be set in production. "
            "Example: CORS_ORIGINS=https://your-frontend.pages.dev,https://example.com"
        )
    if is_prod and cors_env.strip() == "*":
        raise RuntimeError(
            "FATAL: CORS_ORIGINS=* is forbidden in production. "
            "Wildcard disables cookie auth (allow_credentials=False) and weakens CSRF. "
            "Set explicit domains: CORS_ORIGINS=https://api.example.com"
        )
    # Development: accept any loopback origin/port (localhost, 127.x.x.x).
    # Production: explicit whitelist only.
    dev_loopback_origin_regex = r"^https?://(localhost|127(?:\.\d{1,3}){3})(:\d+)?$"
    cors_origin_regex = None
    # Preview deploy domain is configurable; defaults to a neutral placeholder.
    _pages_preview_domain = os.getenv("PAGES_PREVIEW_DOMAIN", "your-frontend.pages.dev")
    _pages_preview_domain_re = _pages_preview_domain.replace(".", r"\.")
    pages_preview_regex = rf"^https://[a-z0-9-]+\.{_pages_preview_domain_re}$"
    if cors_env:
        cors_origins = cors_env.split(",")
    else:
        cors_origins = [
            "http://localhost:5173", "http://127.0.0.1:5173",
            "http://localhost:5174", "http://127.0.0.1:5174",
            "http://localhost:5175", "http://127.0.0.1:5175",
            "http://localhost:5176", "http://127.0.0.1:5176",
            "http://localhost:5177", "http://127.0.0.1:5177",
        ]
    cors_origins = [o.strip() for o in cors_origins if o.strip()]

    allow_methods = ["GET", "POST", "DELETE", "OPTIONS"]
    allow_headers = ["Content-Type", "Authorization", "Idempotency-Key"]
    if not is_prod:
        cors_origin_regex = f"({dev_loopback_origin_regex})|({pages_preview_regex})"
        # Local dev should not fail due to newly added headers/methods.
        allow_methods = ["*"]
        allow_headers = ["*"]
    else:
        cors_origin_regex = pages_preview_regex

    # Browsers block credentials with wildcard origin per CORS spec.
    # Only enable credentials for explicit origin whitelists.
    allow_creds = True if not is_prod else "*" not in cors_origins
    if not allow_creds:
        logger.warning(
            "CORS: allow_credentials disabled (origin='*'). "
            "Set CORS_ORIGINS to explicit domains for credentialed requests. "
            "Cookie-based auth (session login) WILL NOT WORK with wildcard origins."
        )
        if is_prod:
            logger.error(
                "CRITICAL: CORS_ORIGINS=* in production disables cookie auth and weakens CSRF. "
                "Set CORS_ORIGINS to explicit domains, e.g.: CORS_ORIGINS=https://api.example.com"
            )
    app.add_middleware(UserAuthMiddleware, state_getter=lambda: state)

    from agoracle.api.middleware.rate_limit import RateLimitMiddleware
    app.add_middleware(RateLimitMiddleware)

    from agoracle.api.middleware.security_headers import SecurityHeadersMiddleware
    app.add_middleware(SecurityHeadersMiddleware)

    from agoracle.api.middleware.csrf import CSRFMiddleware
    app.add_middleware(
        CSRFMiddleware,
        allowed_origins=cors_origins,
        allow_origin_regex=cors_origin_regex,
        allow_opaque_origin=not is_prod,
    )

    from agoracle.api.middleware.audit_log import AuditLogMiddleware
    app.add_middleware(AuditLogMiddleware)

    from agoracle.api.middleware.geo_block import GeoBlockMiddleware
    app.add_middleware(GeoBlockMiddleware)

    # Keep CORS as the outermost middleware so even early 4xx/5xx responses
    # (CSRF/rate-limit/auth) include CORS headers.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_origin_regex=cors_origin_regex,
        allow_credentials=allow_creds,
        allow_methods=allow_methods,
        allow_headers=allow_headers,
        expose_headers=["Retry-After"],
    )

    app.include_router(_build_router())

    # ── Global exception handlers (structured error codes) ──
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.error(f"Unhandled exception on {request.method} {request.url.path}: {exc}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "error_code": "INTERNAL_ERROR",
                "detail": "Internal server error",
            },
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError):
        # CWE-209: never leak internal info — log server-side only
        logger.warning("ValueError on %s: %s", request.url.path, exc)
        return JSONResponse(
            status_code=422,
            content={
                "error_code": "VALIDATION_ERROR",
                "detail": "Invalid input",
            },
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        error_code_map = {
            401: "AUTH_REQUIRED",
            403: "AUTH_FORBIDDEN",
            404: "NOT_FOUND",
            429: "RATE_LIMITED",
        }
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error_code": error_code_map.get(exc.status_code, f"HTTP_{exc.status_code}"),
                "detail": exc.detail,
            },
        )

    from asyncio import TimeoutError as AsyncTimeoutError
    @app.exception_handler(AsyncTimeoutError)
    async def timeout_handler(request: Request, exc: AsyncTimeoutError):
        logger.warning(f"Timeout on {request.method} {request.url.path}")
        return JSONResponse(
            status_code=504,
            content={
                "error_code": "PIPELINE_TIMEOUT",
                "detail": "Request timed out. Try a simpler question or use Light mode.",
            },
        )

    @app.get("/")
    async def redirect_to_frontend():
        return RedirectResponse(os.getenv("FRONTEND_URL", "https://your-frontend.pages.dev"), status_code=302)

    @app.head("/")
    async def redirect_to_frontend_head():
        return RedirectResponse(os.getenv("FRONTEND_URL", "https://your-frontend.pages.dev"), status_code=302)

    return app


# ── Request/Response models ────────────────────────────────
# These Pydantic models are the SINGLE SOURCE OF TRUTH for the API contract.
# Frontend types are auto-generated from these via scripts/generate-types.sh.
# 禁令 #9: 禁止手写双份契约。

class RoundtableStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(..., max_length=10000)
    session_id: Optional[str] = Field(None, min_length=1, max_length=64)
    locale: str | None = None

class RoundtableChoiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choice_point: str = Field(..., pattern=r"^[AB]$")
    action: str = Field(..., pattern=r"^(deepen|conclude|inject)$")
    user_input: Optional[str] = Field(None, max_length=5000)
    focus_topic: Optional[str] = Field(None, max_length=500)

class DeleteAccountRequest(BaseModel):
    password: str = Field(..., min_length=1, max_length=200)


class AskRequest(BaseModel):
    question: str = Field(..., max_length=10000)
    mode: str = "auto"
    web_search: bool = True
    locale: str | None = None
    depth: Optional[int] = Field(None, ge=1, le=3)  # 1-3
    skip_preflight: bool = False  # v2.7.5: skip clarity check (user chose "continue")
    session_id: Optional[str] = Field(None, max_length=50)  # v2.7.9d: multi-turn session
    file_ids: list[str] = Field(default_factory=list)  # v3.2: multimodal attachments
    single_model_id: Optional[str] = Field(None, max_length=100)  # v5.1: CompanionBubble query_single override

class ModelResponseItem(BaseModel):
    model_id: str
    content: str

class CompanionHintSuggestion(BaseModel):
    mode: str
    label: str

class CompanionHintModel(BaseModel):
    type: str
    topic: str = ""
    times_asked: int = 0
    current_depth: int = 0
    message: str = ""
    suggestions: list[CompanionHintSuggestion] = []
    plan_id: str = ""

class ClarificationNeededModel(BaseModel):
    type: str = "clarification_needed"
    clarity: str = "low"
    reason: str = ""
    suggested_questions: list[str] = []
    message: str = ""

class LowConfidenceActionModel(BaseModel):
    action: str
    label: str

class GuidanceSuggestionModel(BaseModel):
    id: str = ""
    label: str = ""
    action_type: str = ""
    action_payload: dict = {}
    rationale: str = ""
    estimated_seconds: int = 0
    estimated_cost_usd: float = 0.0
    requires_confirm: bool = False

class GuidanceModel(BaseModel):
    """v5.2: Canonical guidance protocol — single source of truth."""
    source: str = "none"
    confidence_statement: str = ""
    confidence_level: str = "medium"
    message: str = ""
    suggestions: list[GuidanceSuggestionModel] = []
    intensity: str = "none"
    is_folded: bool = True
    show_dismiss: bool = False
    route_reason: str = ""
    trigger: str = ""


class NextStepGuidanceModel(BaseModel):
    confidence_statement: str = ""
    confidence_level: str = "medium"
    intensity: str = "none"
    suggestions: list[GuidanceSuggestionModel] = []
    show_dismiss: bool = True


class CompanionGuideActionModel(BaseModel):
    id: str = ""
    label: str = ""
    action_type: str = ""
    action_payload: dict = {}
    rationale: str = ""
    estimated_seconds: int = 0
    estimated_cost_usd: float = 0.0
    requires_confirm: bool = False


class CompanionGuideModel(BaseModel):
    message: str = ""
    actions: list[CompanionGuideActionModel] = []
    trigger: str = "fold"
    is_silent: bool = False
    route_reason: str | None = None

class AskResponse(BaseModel):
    query_id: str = ""
    question: str = ""
    mode: str = ""
    final_answer: str = ""
    confidence: float = 0.0
    quality_gate: str = ""
    has_divergence: bool = False
    divergence_summary: Optional[str] = None
    key_insights: list[str] = []
    latency_ms: int = 0
    estimated_cost_usd: float = 0.0
    contributor_count: int = 0
    individual_responses: Optional[list[ModelResponseItem]] = None
    companion_hint: Optional[CompanionHintModel] = None
    preflight: Optional[ClarificationNeededModel] = None
    pipeline_started: bool = True
    fast_path: bool = False
    low_confidence_actions: list[LowConfidenceActionModel] = []
    session_id: Optional[str] = None  # v2.7.9d: session ID for multi-turn
    context_compressed: bool = False  # v2.7.9d: True if earlier turns were summarized
    search_citations: list[SearchCitationModel] = []  # v4.18: Tavily/perplexity sources for inline citation rendering
    draft_answers: list[DraftAnswerModel] = []  # v3.3: intermediate versions for admin/SSE consumers
    divergence_points: list[dict] = []  # v4.20: structured divergence from DivergenceAnalyzer
    consensus_points: list[str] = []   # A4: 交叉验证 cross-validation consensus points (≤3)
    fact_warnings: list[str] = []  # v4.22d: FactChecker warnings for BEST_SINGLE paths
    next_steps: Optional[NextStepGuidanceModel] = None  # v5.0: NSG unified post-answer suggestions
    companion_guide: Optional[CompanionGuideModel] = None  # v5.1: Dispatcher post-guide (Sonnet-generated guidance)
    consensus_type: str = "unknown"  # v5.3: synthesis / consensus route metadata
    guidance: Optional[GuidanceModel] = None  # v5.2: canonical guidance protocol (single source of truth)
    reason_code: str = "standard"  # v5.4: frontend status rendering reason code
    ai_disclosure: str = "本回答由 AI 生成，仅供参考，请自行核实重要信息。"  # FM-01: AI标识（原则#25）

class SocraticStartRequest(BaseModel):
    question: str = Field(..., max_length=10000)
    locale: str | None = None

class SocraticRespondRequest(BaseModel):
    session_id: str = Field(..., max_length=50)
    message: str = Field(..., max_length=5000)

class SocraticRevealRequest(BaseModel):
    session_id: str = Field(..., max_length=50)

class FeedbackRequest(BaseModel):
    query_id: str = Field(..., max_length=50)
    rating: str = Field("", max_length=30)  # useful / inaccurate / too_shallow / too_slow
    vote: str = Field("", max_length=30)     # v3.5: up / down (from frontend thumbs buttons)
    mode: str = Field("", max_length=30)     # v3.5: query mode for analytics
    quality_gate: str = Field("", max_length=30)  # v3.5: gate result for analytics
    comment: Optional[str] = Field(None, max_length=2000)

# ── Router ─────────────────────────────────────────────────

from fastapi import APIRouter

# _get_user, _get_user_id, _get_client_ip, require_auth, require_admin
# imported from agoracle.api.deps (DEV-PHASE3-DEPS-R1)

def _set_auth_cookie(response: StarletteResponse, session_id: str) -> None:
    """Set HttpOnly session cookie with opaque session_id (SEC-003: not api_key).

    Production (HTTPS): Secure + SameSite=none for cross-origin CF Pages→API.
    Local dev (HTTP):   no Secure + SameSite=lax so browser actually saves the cookie.
    """
    is_prod = _is_production()
    response.set_cookie(
        key="session",
        value=session_id,  # opaque session_id, never the api_key itself
        httponly=True,
        secure=is_prod,
        samesite="none" if is_prod else "lax",
        path="/",
        max_age=7 * 24 * 3600,
    )

UPLOAD_DIR = PROJECT_ROOT / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

def _build_router() -> APIRouter:
    router = APIRouter(prefix="/api")

    # ── Auth endpoints (extracted to routes/auth.py) ────────
    from agoracle.api.routes.auth import router as _auth_router
    router.include_router(_auth_router)

    # ── Admin endpoints (extracted to routes/admin.py) ───────
    from agoracle.api.routes.admin import router as _admin_router
    router.include_router(_admin_router)

    # ── Profile endpoints (extracted to routes/profile.py) ───
    from agoracle.api.routes.profile import router as _profile_router
    router.include_router(_profile_router)

    # ── Health endpoints (extracted to routes/health.py) ──────
    from agoracle.api.routes.health import router as _health_router
    router.include_router(_health_router)

    # ── Query endpoints (extracted to routes/query.py) ────────
    from agoracle.api.routes.query import router as _query_router
    router.include_router(_query_router)

    # ── Socratic endpoints (extracted to routes/socratic.py) ──
    from agoracle.api.routes.socratic import router as _socratic_router
    router.include_router(_socratic_router)

    # ── Misc endpoints (extracted to routes/misc.py) ──────────
    from agoracle.api.routes.misc import router as _misc_router
    router.include_router(_misc_router)

    return router
