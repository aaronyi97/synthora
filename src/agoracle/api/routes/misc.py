"""Miscellaneous endpoints — extracted from app.py.

Includes: /upload, /models, /modes, /pricing, /quota, /feedback, /roundtable/*.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from fastapi import APIRouter, HTTPException, Request, UploadFile, File as FastAPIFile
from sse_starlette.sse import EventSourceResponse

from agoracle.api.app import (
    FeedbackRequest,
    RoundtableChoiceRequest,
    RoundtableStartRequest,
)
from agoracle.api.deps import _get_user_id, get_app_state, resolve_language
from agoracle.api.schemas import (
    AvailableModelsResponse,
    FeedbackResponse,
    ModesResponse,
    PricingResponse,
    QuotaResponse,
    RoundtableCheckResponse,
    RoundtableChoiceResponse,
    RoundtableResumeResponse,
    UploadResponse,
)
from agoracle.config.loader import PROJECT_ROOT

router = APIRouter()
logger = logging.getLogger(__name__)


def _is_english(language: str) -> bool:
    return (language or "").strip() == "en-US"


def _locale_text(language: str, zh: str, en: str) -> str:
    return en if _is_english(language) else zh


def _app():
    import agoracle.api.app as _m
    return _m


# ── File Upload (v3.2: multimodal) ─────────────────────────

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}  # AUDIT-FIX: SVG removed (XSS vector)
ALLOWED_DOC_TYPES = {
    "application/pdf",
    "text/plain",
    "text/markdown",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "text/csv",
}
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB
SAFE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "pdf", "txt", "md", "docx", "xlsx", "xls", "csv"}  # AUDIT-FIX: svg removed

_OFFICE_CSV_CANONICAL_MIME = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "csv": "text/csv",
}

_OFFICE_CSV_MIME_ALIASES = {
    "xlsx": {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/zip",
        "application/x-zip",
        "application/x-zip-compressed",
    },
    "xls": {
        "application/vnd.ms-excel",
        "application/x-ole-storage",
        "application/vnd.ms-office",
        "application/cdfv2",
        "application/x-cfb",
        "application/x-ole2",
    },
    "csv": {
        "text/csv",
        "text/plain",
        "application/csv",
        "text/x-csv",
    },
}


def _safe_upload_extension(filename: str | None) -> str:
    raw_ext = ((filename or "file").rsplit(".", 1)[-1] if "." in (filename or "") else "bin").lower()
    return raw_ext if raw_ext in SAFE_EXTENSIONS else "bin"


def _resolve_office_csv_mime_alias(ext: str, declared_mime: str, actual_mime: str) -> str | None:
    """
    Allow only narrow Office/CSV MIME aliases that python-magic commonly returns.

    This is intentionally strict:
      - only xlsx/xls/csv are eligible
      - declared MIME must still look like an allowed document upload (or octet-stream)
      - actual MIME must be in the per-extension alias allowlist
    """
    normalized_ext = (ext or "").strip().lower()
    normalized_declared = (declared_mime or "").strip().lower()
    normalized_actual = (actual_mime or "").strip().lower()

    if normalized_ext not in _OFFICE_CSV_CANONICAL_MIME:
        return None
    if normalized_declared not in ALLOWED_DOC_TYPES and normalized_declared != "application/octet-stream":
        return None
    if normalized_actual not in _OFFICE_CSV_MIME_ALIASES[normalized_ext]:
        return None
    return _OFFICE_CSV_CANONICAL_MIME[normalized_ext]


@router.post("/upload", response_model=UploadResponse)
async def upload_file(request: Request, file: UploadFile = FastAPIFile(...)):
    state = get_app_state(request)
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    content_type = file.content_type or "application/octet-stream"
    ext = _safe_upload_extension(file.filename)
    # Allow octet-stream to pass initial check — drag-drop often sends this.
    # Actual MIME type will be verified via python-magic below.
    if content_type != "application/octet-stream" and content_type not in ALLOWED_IMAGE_TYPES and content_type not in ALLOWED_DOC_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"不支持的文件类型: {content_type}。"
                "支持: 图片(jpg/png/gif/webp), 文档(pdf/txt/md/docx/xlsx/xls/csv)"
            ),
        )

    # Chunked read to prevent memory exhaustion (CWE-770)
    chunks = []
    total_size = 0
    while True:
        chunk = await file.read(64 * 1024)  # 64KB chunks
        if not chunk:
            break
        total_size += len(chunk)
        if total_size > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail=f"文件过大 (最大 {MAX_FILE_SIZE // 1024 // 1024}MB)")
        chunks.append(chunk)
    data = b"".join(chunks)

    # AUDIT-FIX: Verify actual MIME type via file magic bytes (CWE-434)
    try:
        import magic as _magic
        real_mime = _magic.from_buffer(data[:2048], mime=True)
        if real_mime != content_type:
            logger.warning(
                "MIME mismatch: ext=%s, declared=%s, actual=%s, file=%s",
                ext,
                content_type,
                real_mime,
                file.filename,
            )
            alias_content_type = _resolve_office_csv_mime_alias(ext, content_type, real_mime)
            if alias_content_type:
                content_type = alias_content_type
            elif real_mime not in ALLOWED_IMAGE_TYPES and real_mime not in ALLOWED_DOC_TYPES:
                raise HTTPException(status_code=400, detail=f"文件内容与声明类型不符 (声明: {content_type}, 实际: {real_mime})")
            else:
                content_type = real_mime
    except ImportError:
        # BUG-5 fix: degrade gracefully — extension whitelist already applied above,
        # so skip deep MIME verification rather than blocking all uploads with 503.
        logger.warning("python-magic not installed — MIME verification skipped, relying on extension whitelist only")

    UPLOAD_DIR = _app().UPLOAD_DIR
    file_id = uuid.uuid4().hex[:12]
    safe_name = f"{file_id}.{ext}"
    file_path = UPLOAD_DIR / safe_name
    file_path.write_bytes(data)

    # T1-3: Persist file ownership for cross-user IDOR prevention
    owner_path = UPLOAD_DIR / f"{file_id}.owner"
    owner_path.write_text(str(user_id), encoding="utf-8")

    is_image = content_type in ALLOWED_IMAGE_TYPES
    return {
        "file_id": file_id,
        "filename": file.filename,
        "content_type": content_type,
        "size_bytes": len(data),
        "is_image": is_image,
    }


# ── Models / Modes / Pricing ──────────────────────────────


@router.get("/models", response_model=AvailableModelsResponse)
async def list_models(request: Request):
    state = get_app_state(request)
    if not _get_user_id(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    models = []
    for model_id, mc in state.config.models.items():
        models.append({
            "id": model_id,
            "name": mc.name,
            "available": state.model_adapter.supports_model(model_id),
        })
    return {"models": models}


@router.get("/modes", response_model=ModesResponse)
async def list_modes(request: Request):
    state = get_app_state(request)
    if not _get_user_id(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    """Expose mode metadata from config.yaml for frontend rendering.

    This is the SINGLE SOURCE OF TRUTH for mode descriptions.
    Frontend ModeSelector reads from this endpoint instead of hardcoding.
    防线 #1: 运行时对齐 — 前端不描述后端行为，只渲染后端元数据。
    """
    MODE_META = {
        "auto": {
            "label": "Auto",
            "desc": "智能路由",
            "detail": "AI 自动选择最佳模式和模型",
            "icon": "sparkles",
            "order": 0,
        },
        "light": {
            "label": "Light",
            "desc": "5~20 秒",
            "detail": "日常问答、新闻、快速查询",
            "icon": "zap",
            "order": 1,
        },
        "deep": {
            "label": "Deep",
            "desc": "3~5 分钟",
            "detail": "复杂分析、技术问题、需要深度推理",
            "icon": "brain",
            "order": 2,
        },
        "research": {
            "label": "Research",
            "desc": "5~10 分钟",
            "detail": "研究报告、多角度综合、高要求写作",
            "icon": "flask",
            "order": 3,
        },
        "socratic": {
            "label": "苏格拉底",
            "desc": "互动对话",
            "detail": "训练思维、学习新领域、想自己想清楚",
            "icon": "graduation-cap",
            "order": 4,
        },
        "roundtable": {
            "label": "圆桌讨论",
            "desc": "多模型辩论",
            "detail": "5个AI专家公开辩论你的决策，提供多角度分析和结论",
            "icon": "users",
            "order": 5,
        },
    }

    modes = []

    # v5.1: Inject virtual "auto" mode (not in config.yaml, Dispatcher-driven)
    auto_meta = MODE_META["auto"]
    modes.append({
        "id": "auto",
        "label": auto_meta["label"],
        "desc": auto_meta["desc"],
        "detail": auto_meta.get("detail", ""),
        "icon": auto_meta["icon"],
        "order": auto_meta["order"],
        "contributor_count": 0,
        "n_of_m": 0,
        "has_judge": False,
        "has_critique": False,
        "has_preflight": False,
        "max_timeout_seconds": 60,
    })

    for mode_id, mc in state.config.modes.items():
        meta = MODE_META.get(mode_id, {"label": mode_id, "desc": "", "icon": "sparkles", "order": 99})

        contributor_count = len(mc.contributors)
        n_of_m = mc.n_of_m or contributor_count
        has_judge = bool(mc.judge) and not mc.skip_judge
        has_critique = mc.critique_always_on
        has_preflight = bool(mc.preflight_clarity_check)

        modes.append({
            "id": mode_id,
            "label": meta["label"],
            "desc": meta["desc"],
            "detail": meta.get("detail", ""),
            "icon": meta["icon"],
            "order": meta["order"],
            "contributor_count": contributor_count,
            "n_of_m": n_of_m,
            "has_judge": has_judge,
            "has_critique": has_critique,
            "has_preflight": has_preflight,
            "max_timeout_seconds": mc.max_timeout_seconds,
        })

    # Inject roundtable mode if enabled
    rt_enabled = getattr(state.config.features, "roundtable_enabled", False)
    if rt_enabled:
        rt_meta = MODE_META["roundtable"]
        modes.append({
            "id": "roundtable",
            "label": rt_meta["label"],
            "desc": rt_meta["desc"],
            "detail": rt_meta.get("detail", ""),
            "icon": rt_meta["icon"],
            "order": rt_meta["order"],
            "contributor_count": 5,
            "n_of_m": 5,
            "has_judge": False,
            "has_critique": False,
            "has_preflight": True,
            "max_timeout_seconds": 300,
        })

    modes.sort(key=lambda m: m["order"])
    return {"modes": modes}


@router.get("/pricing", response_model=PricingResponse)
async def pricing(request: Request):
    """Per-model pricing information (USD per 1M tokens)."""
    state = get_app_state(request)
    if not _get_user_id(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    pricing_data = []
    for model_id, mc in state.config.models.items():
        pricing_data.append({
            "id": model_id,
            "name": mc.name,
            "cost_per_1m_input": mc.cost_per_1m_input,
            "cost_per_1m_output": mc.cost_per_1m_output,
        })
    return {"pricing": pricing_data, "currency": "USD", "unit": "per 1M tokens"}


# ── Quota / Credits ───────────────────────────────────────


@router.get("/quota", response_model=QuotaResponse, response_model_exclude_none=True)
async def get_quota(request: Request):
    """Return today's usage, limits, and remaining credits for the current user."""
    state = get_app_state(request)
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not state.quota_service:
        return {
            "enabled": False,
            "total_credits": 0,
            "credits_used": 0,
            "credits_remaining": 0,
            "modes": [],
        }

    # v4.10: lifetime credit pool — never resets, per-user total
    CREDIT_COST = {"light": 1, "deep": 60, "research": 100, "socratic": 15, "roundtable": 60}

    total_credits = state.quota_service.get_user_total_credits(user_id)
    credits_used = state.quota_service.get_lifetime_credits_used(user_id)
    credits_remaining = max(0, total_credits - credits_used)

    lifetime_usage = state.quota_service.get_lifetime_usage(user_id)
    today_usage = state.quota_service.get_usage(user_id)

    modes = []
    for mode_id, cost in CREDIT_COST.items():
        lifetime = lifetime_usage.get(mode_id, 0)
        today = today_usage.get(mode_id, 0)
        modes.append({
            "mode": mode_id,
            "used_lifetime": lifetime,
            "used_today": today,
            "credit_cost": cost,
            "credits_spent": lifetime * cost,
        })

    return {
        "enabled": True,
        "total_credits": total_credits,
        "credits_used": credits_used,
        "credits_remaining": credits_remaining,
        "modes": modes,
    }


# ── Feedback ──────────────────────────────────────────────


@router.post("/feedback", response_model=FeedbackResponse)
async def record_feedback(req: FeedbackRequest, request: Request):
    from agoracle.adapters.feedback.json_feedback import JsonFeedbackStore
    store = JsonFeedbackStore(PROJECT_ROOT / "data" / "feedback.jsonl")
    uid = _get_user_id(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # v3.5: normalize vote to rating for unified storage
    rating = req.rating or ("useful" if req.vote == "up" else "not_useful" if req.vote == "down" else "unknown")
    # TODO(WP4-EVENT-WIRE): FeedbackReceived remains intentionally unwired.
    # The current event payload does not include user_id, and there is no
    # implemented profile/analytics subscriber ready to consume it safely.
    await store.record(
        req.query_id, rating, req.comment,
        extra={"vote": req.vote, "mode": req.mode, "quality_gate": req.quality_gate, "user_id": uid} if req.vote else {"user_id": uid},
    )
    return {"status": "ok"}


# ── Roundtable endpoints (v5.2 Phase C) ──────────────────


def _get_protocol_version() -> str:
    """Return current protocol version for request-level diagnostics."""
    from agoracle.services.guidance_compat import PROTOCOL_VERSION
    return PROTOCOL_VERSION


@router.post("/roundtable/check", response_model=RoundtableCheckResponse)
async def roundtable_check(req: RoundtableStartRequest, request: Request):
    """Route guard: check if question is suitable for roundtable."""
    state = get_app_state(request)
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    user_profile = None
    if state.profile_store:
        try:
            user_profile = await state.profile_store.load(user_id)
        except Exception:
            user_profile = None
    language = resolve_language(
        req.locale,
        request.headers.get("Accept-Language"),
        user_profile,
    )

    from agoracle.services.roundtable_orchestrator import check_suitability
    result = await check_suitability(
        req.question, state.model_adapter, state.config,
        prompt_loader=state.prompt_loader,
        language=language,
    )
    return {"suitability": result.suitability, "reason": result.reason}


@router.post(
    "/roundtable/start",
    response_class=EventSourceResponse,
    responses={
        200: {
            "description": "SSE stream",
            "content": {
                "text/event-stream": {
                    "schema": {"type": "string"},
                },
            },
        },
    },
)
async def roundtable_start(req: RoundtableStartRequest, request: Request):
    """Start a roundtable debate session (SSE streaming)."""
    state = get_app_state(request)
    _user_id = _get_user_id(request)
    resume_session_id = (req.session_id or "").strip() or None
    if not _user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not getattr(state.config.features, "roundtable_enabled", False):
        raise HTTPException(status_code=403, detail="Roundtable mode is not enabled")
    if state.quota_service and not resume_session_id:
        quota_err = state.quota_service.check_quota(_user_id, "roundtable")
        if quota_err:
            raise HTTPException(status_code=429, detail=quota_err["message"])

    from agoracle.services.roundtable_orchestrator import (
        RoundtableOrchestrator, RoundtableConfig,
        RoundtableStarted, ExpertDone, DisputesMapped, AwaitingUserChoice,
        DebateStarted, RebuttalDone, DebateComplete, ModeratorStarted,
        RoundtableComplete, RoundtableError, Heartbeat, AutoDraft,
        check_suitability, HEARTBEAT_INTERVAL_S, get_session, _TERMINAL_STATES,
    )

    if resume_session_id:
        existing = get_session(resume_session_id)
        if existing is None:
            logger.info(f"[roundtable] /start reattach 403: session {resume_session_id} not_found")
            raise HTTPException(status_code=403, detail="forbidden")
        if existing.owner_user_id != _user_id:
            logger.info(f"[roundtable] /start reattach 403: session {resume_session_id} owner_mismatch")
            raise HTTPException(status_code=403, detail="forbidden")
        if existing.state in _TERMINAL_STATES:
            raise HTTPException(status_code=410, detail="session_ended")

    rt_orch = RoundtableOrchestrator(
        model_adapter=state.model_adapter,
        config=state.config,
        prompt_loader=state.prompt_loader,
        session_store=getattr(state, 'roundtable_store', None),
    )

    user_profile = None
    if state.profile_store:
        try:
            user_profile = await state.profile_store.load(_user_id)
        except Exception:
            user_profile = None
    language = resolve_language(
        req.locale,
        request.headers.get("Accept-Language"),
        user_profile,
    )

    async def rt_event_generator():
        _has_model_call = False
        _usage_recorded = False
        try:
            async for event in rt_orch.execute_streaming(
                req.question,
                owner_user_id=_user_id,
                session_id=resume_session_id,
                language=language,
            ):
                if isinstance(event, RoundtableStarted):
                    yield {"event": "roundtable_started", "data": json.dumps({
                        "session_id": event.session_id,
                        "expert_count": event.expert_count,
                        "question": event.question,
                    }, ensure_ascii=False)}
                elif isinstance(event, ExpertDone):
                    _has_model_call = True
                    op = event.opinion
                    yield {"event": "expert_done", "data": json.dumps({
                        "model_id": op.model_id, "label": op.label,
                        "stance": op.stance, "confidence": op.confidence,
                        "my_dimensions": op.my_dimensions,
                        "claims": [{"point": c.point, "evidence": c.evidence, "dimension": c.dimension} for c in op.claims],
                        "risk_warning": op.risk_warning,
                        "blind_spot_warning": op.blind_spot_warning,
                        "challenge_to_others": op.challenge_to_others,
                        "raw_response": op.raw_response,
                        "structured": op.structured, "success": op.success,
                        "error": op.error, "latency_ms": op.latency_ms,
                        "done_count": event.done_count, "total_count": event.total_count,
                    }, ensure_ascii=False)}
                elif isinstance(event, DisputesMapped):
                    dm = event.dispute_map
                    yield {"event": "disputes_mapped", "data": json.dumps({
                        "synthesized_dimensions": dm.synthesized_dimensions,
                        "dimension_sources": dm.dimension_sources,
                        "contention_points": [
                            {"topic": cp.topic, "severity": cp.severity,
                             "dispute_type": cp.dispute_type,
                             "factual_aspect": cp.factual_aspect,
                             "value_aspect": cp.value_aspect,
                             "dimension_id": cp.dimension_id,
                             "dimension_label": cp.dimension_label,
                             "dimension_aliases": cp.dimension_aliases,
                             "adjudication_note": cp.adjudication_note,
                             "why_it_matters": cp.why_it_matters,
                             "suggested_focus": cp.suggested_focus,
                             "sides": [{"position": s.position, "supporting_claims": s.supporting_claims,
                                        "lead_expert": s.lead_expert, "main_argument": s.main_argument}
                                       for s in cp.sides]}
                            for cp in dm.contention_points
                        ],
                        "consensus_points": [{"point": cp.point, "strength": cp.strength, "agreed_by": cp.agreed_by}
                                             for cp in dm.consensus_points],
                        "suggested_focus": dm.suggested_focus,
                        "echo_chamber_warning": dm.echo_chamber_warning,
                        "clarifying_questions": dm.clarifying_questions,
                        "degraded": dm.degraded,
                    }, ensure_ascii=False)}
                elif isinstance(event, AwaitingUserChoice):
                    yield {"event": "awaiting_user_choice", "data": json.dumps({
                        "choice_point": event.choice_point,
                        "timeout_s": event.timeout_s,
                        "default_action": event.default_action,
                    }, ensure_ascii=False)}
                elif isinstance(event, DebateStarted):
                    yield {"event": "debate_started", "data": json.dumps({
                        "round": event.round_num, "assignments": event.assignments,
                    }, ensure_ascii=False)}
                elif isinstance(event, RebuttalDone):
                    _has_model_call = True
                    r = event.rebuttal
                    yield {"event": "rebuttal_done", "data": json.dumps({
                        "model_id": r.model_id, "label": r.label, "role": r.role,
                        "target_dispute": r.target_dispute, "response_type": r.response_type,
                        "response": r.response, "new_evidence": r.new_evidence,
                        "revised_stance": r.revised_stance, "stance_changed": r.stance_changed,
                        "confidence": r.confidence, "structured": r.structured,
                        "success": r.success, "latency_ms": r.latency_ms,
                        "done_count": event.done_count, "total_count": event.total_count,
                    }, ensure_ascii=False)}
                elif isinstance(event, DebateComplete):
                    yield {"event": "debate_complete", "data": json.dumps({
                        "round": event.round_num, "stance_changes": event.stance_changes,
                    }, ensure_ascii=False)}
                elif isinstance(event, ModeratorStarted):
                    yield {"event": "moderator_started", "data": "{}"}
                elif isinstance(event, RoundtableComplete):
                    _has_model_call = True
                    r = event.result
                    dp = r.decision_packet
                    if state.quota_service:
                        state.quota_service.record_usage(_user_id, "roundtable")
                        _usage_recorded = True
                    yield {"event": "roundtable_complete", "data": json.dumps({
                        "session_id": r.session_id, "question": r.question,
                        "rounds_completed": r.rounds_completed,
                        "experts": [{"model_id": e.model_id, "label": e.label,
                                     "stance": e.stance, "confidence": e.confidence,
                                     "my_dimensions": e.my_dimensions,
                                     "claims": [{"point": c.point, "evidence": c.evidence, "dimension": c.dimension} for c in e.claims],
                                     "risk_warning": e.risk_warning,
                                     "blind_spot_warning": e.blind_spot_warning,
                                     "challenge_to_others": e.challenge_to_others,
                                     "raw_response": e.raw_response,
                                     "structured": e.structured,
                                     "latency_ms": e.latency_ms,
                                     "success": e.success, "error": e.error,
                                     } for e in r.experts],
                        "dispute_map": {
                            "synthesized_dimensions": r.dispute_map.synthesized_dimensions,
                            "dimension_sources": r.dispute_map.dimension_sources,
                            "contention_points": [
                                {"topic": cp.topic, "severity": cp.severity,
                                 "dispute_type": cp.dispute_type,
                                 "factual_aspect": cp.factual_aspect,
                                 "value_aspect": cp.value_aspect,
                                 "dimension_id": cp.dimension_id,
                                 "dimension_label": cp.dimension_label,
                                 "dimension_aliases": cp.dimension_aliases,
                                 "adjudication_note": cp.adjudication_note,
                                 "why_it_matters": cp.why_it_matters,
                                 "suggested_focus": cp.suggested_focus,
                                 "sides": [{"position": s.position, "supporting_claims": s.supporting_claims,
                                            "lead_expert": s.lead_expert, "main_argument": s.main_argument}
                                           for s in cp.sides]}
                                for cp in r.dispute_map.contention_points
                            ],
                            "consensus_points": [{"point": cp.point, "strength": cp.strength, "agreed_by": cp.agreed_by}
                                                 for cp in r.dispute_map.consensus_points],
                            "suggested_focus": r.dispute_map.suggested_focus,
                            "echo_chamber_warning": r.dispute_map.echo_chamber_warning,
                            "clarifying_questions": r.dispute_map.clarifying_questions,
                            "degraded": r.dispute_map.degraded,
                        },
                        "decision_packet": {
                            "conclusion_type": dp.conclusion_type,
                            "confidence_basis": dp.confidence_basis,
                            "final_summary": dp.final_summary,
                            "stance_evolution": [{"expert": s.expert, "r1_stance": s.r1_stance,
                                                   "final_stance": s.final_stance, "changed": s.changed,
                                                   "changed_reason": s.changed_reason} for s in dp.stance_evolution],
                            "options": [{"choice": o.choice, "pros": o.pros, "cons": o.cons,
                                         "best_when": o.best_when, "risk": o.risk,
                                         "mitigation": o.mitigation} for o in dp.options],
                            "unresolved": [{"point": u.point, "reason": u.reason,
                                            "how_to_resolve": u.how_to_resolve} for u in dp.unresolved],
                            "what_changes_my_mind": dp.what_changes_my_mind,
                            "recommended_action": dp.recommended_action,
                            "value_disputes_to_user": [{"point": vd.point, "dimension_id": vd.dimension_id,
                                                         "ask_user": vd.ask_user} for vd in dp.value_disputes_to_user],
                            "echo_chamber_flag": dp.echo_chamber_flag,
                            "degraded": dp.degraded,
                            "degradation_reason": dp.degradation_reason,
                            "total_latency_ms": dp.total_latency_ms,
                            "estimated_cost_usd": dp.estimated_cost_usd,
                        },
                        "protocol_version": _get_protocol_version(),
                    }, ensure_ascii=False)}
                elif isinstance(event, Heartbeat):
                    yield {"event": "heartbeat", "data": json.dumps({"t": int(time.time())})}
                elif isinstance(event, AutoDraft):
                    _has_model_call = True
                    dp = event.decision_packet
                    if state.quota_service:
                        state.quota_service.record_usage(_user_id, "roundtable")
                        _usage_recorded = True
                    yield {"event": "auto_draft", "data": json.dumps({
                        "decision_packet": {
                            "conclusion_type": dp.conclusion_type,
                            "confidence_basis": dp.confidence_basis,
                            "final_summary": dp.final_summary,
                            "stance_evolution": [{"expert": se.expert, "r1_stance": se.r1_stance,
                                                   "final_stance": se.final_stance, "changed": se.changed,
                                                   "changed_reason": se.changed_reason} for se in dp.stance_evolution],
                            "options": [{"choice": o.choice, "pros": o.pros, "cons": o.cons,
                                         "best_when": o.best_when, "risk": o.risk,
                                         "mitigation": o.mitigation} for o in dp.options],
                            "unresolved": [{"point": u.point, "reason": u.reason,
                                            "how_to_resolve": u.how_to_resolve} for u in dp.unresolved],
                            "what_changes_my_mind": dp.what_changes_my_mind,
                            "recommended_action": dp.recommended_action,
                            "value_disputes_to_user": [{"point": v.point, "dimension_id": v.dimension_id,
                                                         "ask_user": v.ask_user} for v in dp.value_disputes_to_user],
                            "echo_chamber_flag": dp.echo_chamber_flag,
                            "degraded": dp.degraded,
                            "degradation_reason": dp.degradation_reason,
                            "total_latency_ms": dp.total_latency_ms,
                            "estimated_cost_usd": dp.estimated_cost_usd,
                        },
                        "message": _locale_text(
                            language,
                            "5分钟无操作，已自动生成草案。可继续探讨或直接采用。",
                            "No action was taken for 5 minutes, so a draft was generated automatically. You can keep exploring or use it directly.",
                        ),
                    }, ensure_ascii=False)}
                elif isinstance(event, RoundtableError):
                    if state.quota_service and event.billable and not _usage_recorded:
                        state.quota_service.record_usage(_user_id, "roundtable")
                        _usage_recorded = True
                    yield {"event": "roundtable_error", "data": json.dumps({
                        "error": event.error,
                        "code": event.error,
                        "phase": event.phase,
                        "reason": event.reason,
                        "detail": event.detail,
                    }, ensure_ascii=False)}
        except asyncio.CancelledError:
            logger.warning(
                f"[roundtable] SSE client disconnected "
                f"user_id={_user_id} question={req.question[:80]!r}"
            )
            raise
        except Exception as _e:
            logger.error(f"Roundtable stream error: {_e}", exc_info=True)
            if state.quota_service and _has_model_call and not _usage_recorded:
                state.quota_service.record_usage(_user_id, "roundtable")
                _usage_recorded = True
            yield {"event": "roundtable_error", "data": json.dumps({
                "error": "roundtable_stream_error",
                "code": "roundtable_stream_error",
                "phase": "SSE",
                "reason": "server_stream_exception",
                "detail": _locale_text(
                    language,
                    "圆桌处理遇到内部错误，请稍后重试",
                    "Roundtable processing hit an internal error. Please try again later.",
                ),
            }, ensure_ascii=False)}

    return EventSourceResponse(rt_event_generator())


@router.post("/roundtable/{session_id}/choice", response_model=RoundtableChoiceResponse)
async def roundtable_choice(
    session_id: str, req: RoundtableChoiceRequest, request: Request,
):
    """User decision point (A or B). Owner check + state isolation + idempotency."""
    _user_id = _get_user_id(request)
    if not _user_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    from agoracle.services.roundtable_orchestrator import (
        get_session, SessionState, UserChoice, _TERMINAL_STATES,
    )

    session = get_session(session_id)
    if session is None:
        logger.info(f"[roundtable] /choice 403: session {session_id} not_found")
        raise HTTPException(status_code=403, detail="forbidden")
    if session.owner_user_id != _user_id:
        logger.info(f"[roundtable] /choice 403: session {session_id} owner_mismatch")
        raise HTTPException(status_code=403, detail="forbidden")
    if session.state in _TERMINAL_STATES:
        raise HTTPException(status_code=410, detail="session_ended")

    idem_key = request.headers.get("idempotency-key", "")
    if idem_key:
        cached = session.check_idempotency(idem_key)
        if cached is not None:
            return cached

    expected_state = (
        SessionState.AWAITING_A if req.choice_point == "A"
        else SessionState.AWAITING_B
    )
    if session.state != expected_state:
        raise HTTPException(
            status_code=409,
            detail={"error": "choice_point_mismatch", "current_state": session.state.value},
        )

    choice = UserChoice(
        action=req.action,
        choice_point=req.choice_point,
        user_input=req.user_input,
        focus_topic=req.focus_topic,
        idempotency_key=idem_key,
    )
    await session._choice_queue.put(choice)

    resp = {"ok": True}
    if idem_key:
        session.cache_idempotency(idem_key, resp)
    return resp


@router.get("/roundtable/{session_id}/resume", response_model=RoundtableResumeResponse, response_model_exclude_none=True)
async def roundtable_resume(session_id: str, request: Request):
    """Resume a session after auto_draft or SSE disconnect."""
    _user_id = _get_user_id(request)
    if not _user_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    from agoracle.services.roundtable_orchestrator import (
        get_session, SessionState, _TERMINAL_STATES, _dataclass_to_dict,
    )

    session = get_session(session_id)
    if session is None:
        logger.info(f"[roundtable] /resume 403: session {session_id} not_found")
        raise HTTPException(status_code=403, detail="forbidden")
    if session.owner_user_id != _user_id:
        logger.info(f"[roundtable] /resume 403: session {session_id} owner_mismatch")
        raise HTTPException(status_code=403, detail="forbidden")

    if session.state in _TERMINAL_STATES:
        raise HTTPException(status_code=410, detail="session_ended")

    if session.state == SessionState.AUTO_DRAFT_SENT:
        from agoracle.services.roundtable_orchestrator import _schedule_cleanup
        if session.should_expire():
            await session.force_state(SessionState.EXPIRED, "session_expired_on_resume")
            _schedule_cleanup(session_id)
            raise HTTPException(status_code=410, detail="session_ended")

    if session.state == SessionState.AUTO_DRAFT_SENT:
        packet = session.auto_draft_packet
        if packet is None:
            raise HTTPException(status_code=404, detail="auto_draft_packet_missing")
        return {
            "status": "auto_draft_available",
            "session_id": session_id,
            "choice_point": "B" if session._pre_draft_state and
                session._pre_draft_state == SessionState.AWAITING_B else "A",
            "decision_packet": _dataclass_to_dict(packet),
        }

    _choice_point = session.choice_point or (
        "A" if session.state == SessionState.AWAITING_A
        else "B" if session.state == SessionState.AWAITING_B
        else None
    )
    _state_snapshot = {
        "question": getattr(session, "question", ""),
        "expert_count": int(getattr(session, "expert_count", 0) or 0),
        "experts": _dataclass_to_dict(getattr(session, "experts", []) or []),
        "dispute_map": _dataclass_to_dict(getattr(session, "dispute_map", None))
            if getattr(session, "dispute_map", None) is not None else None,
        "rebuttals": _dataclass_to_dict(getattr(session, "rebuttals", []) or []),
        "debate_round": int(getattr(session, "debate_round", 0) or 0),
        "choice_point": _choice_point,
    }
    return {
        "status": "session_active",
        "session_id": session_id,
        "state": session.state.value,
        "choice_point": _choice_point,
        "state_snapshot": _state_snapshot,
    }
