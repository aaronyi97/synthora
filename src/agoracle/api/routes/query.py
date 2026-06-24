"""Query endpoints (/ask, /ask/stream) — extracted from app.py."""
from __future__ import annotations

import asyncio
import json
import logging
import re as _re
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from agoracle.api.app import (
    AskRequest,
    AskResponse,
    GuidanceModel,
    NextStepGuidanceModel,
    GuidanceSuggestionModel,
)
from agoracle.api.deps import (
    _get_user,
    _get_user_id,
    get_app_state,
    get_stream_limiter,
    resolve_language,
)
from agoracle.domain.router import route
from agoracle.domain.types import (
    Attachment,
    Mode,
    OutputDepth,
    QueryContext,
)
from agoracle.services.orchestrator import Orchestrator
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

router = APIRouter()
logger = logging.getLogger(__name__)


def _app():
    import agoracle.api.app as _m
    return _m


# ── Billing / guidance helpers (moved from app._build_router closure) ──


def _resolve_usage_mode(result: Any, fallback_mode: str = "") -> str:
    """Derive the mode string to bill from result, falling back to pipeline mode."""
    if result is not None:
        _rm = getattr(result, "resolved_mode", "")
        _resolved_mode = _rm.value if hasattr(_rm, "value") else str(_rm) if _rm else ""
        if _resolved_mode:
            return _resolved_mode
    return fallback_mode


def _should_record_usage_for_result(result: Any) -> bool:
    """Unified billing gate for /ask and /ask/stream results.

    Rules:
      - Clarify responses are never billable.
      - Normal completed answers are billable.
      - System-error answers are billable only if model calls actually happened.
    """
    if result is None:
        return False
    if getattr(result, "is_clarify", False):
        return False
    final_answer = getattr(result, "final_answer", "") or ""
    is_error_answer = isinstance(final_answer, str) and final_answer.startswith("系统错误")
    if not is_error_answer:
        return True
    try:
        total_model_calls = int(getattr(result, "total_model_calls", 0) or 0)
    except (TypeError, ValueError):
        total_model_calls = 0
    try:
        contributor_count = int(getattr(result, "contributor_count", 0) or 0)
    except (TypeError, ValueError):
        contributor_count = 0
    try:
        estimated_cost = float(getattr(result, "estimated_cost_usd", 0.0) or 0.0)
    except (TypeError, ValueError):
        estimated_cost = 0.0
    return total_model_calls > 0 or contributor_count > 0 or estimated_cost > 0.0


def _tracker_indicates_model_call(model_adapter: Any) -> bool:
    """Best-effort model activity signal for uncaught exception paths."""
    if model_adapter is None:
        return False
    try:
        tracker = model_adapter.get_cost_tracker()
    except Exception:
        return False
    if not tracker:
        return False
    if isinstance(tracker, dict):
        return bool(tracker)
    for row in tracker:
        if isinstance(row, (list, tuple)):
            if len(row) >= 4:
                try:
                    if float(row[3] or 0.0) > 0.0:
                        return True
                except (TypeError, ValueError):
                    pass
            if len(row) > 0:
                return True
        else:
            return True
    return False


def _to_nsg_model(nsg) -> NextStepGuidanceModel | None:
    """Convert legacy `next_steps` compatibility field to its API model."""
    if nsg is None:
        return None
    return NextStepGuidanceModel(
        confidence_statement=nsg.confidence_statement,
        confidence_level=nsg.confidence_level,
        intensity=nsg.intensity,
        suggestions=[
            GuidanceSuggestionModel(
                id=s.id, label=s.label, action_type=s.action_type,
                action_payload=s.action_payload, rationale=s.rationale,
                estimated_seconds=s.estimated_seconds,
                estimated_cost_usd=s.estimated_cost_usd,
                requires_confirm=s.requires_confirm,
            ) for s in (nsg.suggestions or [])
        ],
        show_dismiss=nsg.show_dismiss,
    )


def _to_guidance_model(guidance) -> GuidanceModel | None:
    """v5.2: Convert domain GuidanceOutput dataclass to Pydantic model."""
    if guidance is None:
        return None
    return GuidanceModel(
        source=guidance.source,
        confidence_statement=guidance.confidence_statement,
        confidence_level=guidance.confidence_level,
        message=guidance.message,
        suggestions=[
            GuidanceSuggestionModel(
                id=s.id, label=s.label, action_type=s.action_type,
                action_payload=s.action_payload, rationale=s.rationale,
                estimated_seconds=s.estimated_seconds,
                estimated_cost_usd=s.estimated_cost_usd,
                requires_confirm=s.requires_confirm,
            ) for s in (guidance.suggestions or [])
        ],
        intensity=guidance.intensity,
        is_folded=guidance.is_folded,
        show_dismiss=guidance.show_dismiss,
        route_reason=guidance.route_reason,
        trigger=guidance.trigger,
    )


def _resolve_reason_code(result) -> str:
    """Expose a stable frontend reason code for UI state rendering."""
    if getattr(result, "quality_gate_result", "") == "low_confidence":
        return "low_confidence"
    if bool(getattr(result, "fast_path", False)) and str(getattr(result, "consensus_type", "")).upper() == "SINGLE_FAST":
        return "single_model_fast_path"
    if bool(getattr(result, "fast_path", False)):
        return "fast_path"
    return "standard"


# ── Context builders (moved from app.py module level) ──


def _resolve_attachments(file_ids: list[str], user_id: int = 0) -> list[Attachment]:
    """Resolve file_ids to Attachment objects from disk.

    When user_id > 0, enforces ownership: only returns files owned by that user.
    """
    if not file_ids:
        return []
    import pathlib as _pl
    upload_dir = _app().UPLOAD_DIR
    attachments = []
    for fid in file_ids[:5]:  # max 5 files per request
        # AUDIT-FIX: validate file_id format to prevent glob injection (CWE-155)
        if not _re.match(r'^[a-f0-9]{12}$', fid):
            logger.warning(f"Invalid file_id format rejected: {fid!r}")
            continue
        # T1-3: Check file ownership (IDOR prevention, always-on)
        # SEC-010c: user_id=0 (unauthenticated) is never a valid owner — deny access fail-closed
        owner_path = upload_dir / f"{fid}.owner"
        if not owner_path.exists():
            # SEC-010b: .owner sidecar missing → fail-closed, deny access and audit
            logger.warning(f"File .owner sidecar missing for file={fid}, denying access (fail-closed)")
            continue
        try:
            file_owner = int(owner_path.read_text(encoding="utf-8").strip())
            if file_owner == 0 or file_owner != user_id:
                logger.warning(f"File ownership mismatch: file={fid}, owner={file_owner}, requester={user_id}")
                continue
        except (ValueError, OSError):
            logger.warning(f"Invalid/unreadable .owner for file={fid}, denying access (fail-closed)")
            continue  # SEC-010: fail-closed — deny access if owner unreadable
        # Find file on disk by prefix
        matches = [p for p in upload_dir.glob(f"{fid}.*") if p.suffix != ".owner"]
        if not matches:
            logger.warning(f"Attachment not found: {fid}")
            continue
        fp = matches[0]
        ext = fp.suffix.lower()
        ct_map = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".gif": "image/gif", ".webp": "image/webp",
            ".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
        attachments.append(Attachment(
            file_id=fid,
            filename=fp.name,
            content_type=ct_map.get(ext, "application/octet-stream"),
            file_path=str(fp),
            size_bytes=fp.stat().st_size,
        ))
    return attachments


def _build_context(req: AskRequest, user_id: int = 0) -> QueryContext:
    """Build QueryContext from an API request."""
    state = _app().state
    mode_str = req.mode
    query_id = uuid.uuid4().hex[:12]
    attachments = _resolve_attachments(req.file_ids, user_id=user_id)

    # v5.1: single_model_id override — CompanionBubble query_single action.
    # Bypass Dispatcher routing; Orchestrator skip_judge path handles single-model directly.
    if req.single_model_id:
        from agoracle.domain.router import _classify_question_type
        question_type = _classify_question_type(req.question)
        return QueryContext(
            query_id=query_id,
            question=req.question,
            mode=Mode.AUTO,
            resolved_mode=Mode.LIGHT,
            web_search_enabled=req.web_search,
            output_depth=OutputDepth.LEVEL_1,
            question_type=question_type,
            attachments=attachments,
            dispatcher_routed=True,  # prevent Dispatcher re-routing
            single_model_override=req.single_model_id,
        )

    if mode_str == "auto":
        decision = route(req.question, query_id=query_id)
        return QueryContext(
            query_id=query_id,
            question=req.question,
            mode=Mode.AUTO,
            resolved_mode=Mode.LIGHT,
            intent=decision.intent,
            web_search_enabled=req.web_search,
            critique_enabled=False,
            output_depth=OutputDepth.LEVEL_1,
            question_type=decision.question_type,
            attachments=attachments,
        )

    resolved = Mode(mode_str)
    output_depth = OutputDepth.LEVEL_1
    if req.depth:
        output_depth = [OutputDepth.LEVEL_1, OutputDepth.LEVEL_2, OutputDepth.LEVEL_3][req.depth - 1]
    elif mode_str == "deep":
        output_depth = OutputDepth.LEVEL_2
    elif mode_str == "research":
        output_depth = OutputDepth.LEVEL_3

    # P1-A1: classify question_type even for hand-picked modes,
    # so smart_routing / adaptive aggregation works (not stuck on UNKNOWN)
    from agoracle.domain.router import _classify_question_type
    question_type = _classify_question_type(req.question)

    mode_cfg = state.config.modes.get(mode_str)
    return QueryContext(
        query_id=query_id,
        question=req.question,
        mode=resolved,
        resolved_mode=resolved,
        web_search_enabled=req.web_search,
        critique_enabled=mode_cfg.critique_always_on if mode_cfg else False,
        output_depth=output_depth,
        question_type=question_type,
        attachments=attachments,
    )


# ── Endpoints ──


@router.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest, request: Request):
    if req.mode == "socratic":
        raise HTTPException(status_code=422, detail="Use /api/socratic/start for Socratic mode")
    state = get_app_state(request)
    _uid = _get_user_id(request)
    if not _uid:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user_profile = None
    if state.profile_store:
        try:
            user_profile = await state.profile_store.load(_uid)
        except Exception as e:
            logger.debug(f"Profile load failed for language resolution: {e}")
    context = _build_context(req, user_id=_uid)
    context.user_id = _uid
    context.language = resolve_language(
        req.locale,
        request.headers.get("Accept-Language"),
        user_profile,
    )

    # v2.8.5: Multi-turn session history (persistent SQLite)
    # TODO(WP4-EVENT-WIRE): SessionCreated is intentionally not emitted yet.
    # Current event payload only carries session_id (no user_id), and there is no
    # implemented subscriber that would consume it without becoming a no-op.
    active_session_id = req.session_id or uuid.uuid4().hex[:12]
    if req.session_id and state.conversation_store:
        try:
            context.session_history = await state.conversation_store.get_session_turns(req.session_id, user_id=context.user_id)
        except Exception as e:
            logger.debug(f"Conversation history load failed: {e}")  # #24

    # P0-1: Populate profile summary for contributor prompt injection (数据飞轮环3)
    if state.profile_store and context.user_id:
        try:
            context.user_profile_summary = await state.profile_store.get_summary(
                context.user_id,
                language=context.language,
            )
        except Exception as e:
            logger.debug(f"Profile summary load failed for user {context.user_id}: {e}")  # #24

    # Quota check (v2.7.5)
    if state.quota_service:
        quota_err = state.quota_service.check_quota(context.user_id, context.resolved_mode.value)
        if quota_err:
            raise HTTPException(status_code=429, detail=quota_err["message"])

    # v3.3: Question optimization for /ask endpoint (non-streaming fallback)
    if not req.skip_preflight:
        _mode_cfg = state.config.modes.get(context.resolved_mode.value)
        if _mode_cfg and _mode_cfg.preflight_clarity_check:
            try:
                from agoracle.services.preflight import optimize_question
                _opt = await optimize_question(
                    req.question, state.model_adapter,
                    _mode_cfg.preflight_clarity_check,
                )
                if _opt and _opt.get("needs_confirmation"):
                    return AskResponse(
                        question=req.question,
                        mode=context.resolved_mode.value,
                        preflight=_opt,
                        pipeline_started=False,
                    )
                elif _opt and not _opt.get("needs_confirmation"):
                    context.question = _opt["optimized_question"]
            except Exception as e:
                logger.debug(f"Question optimization failed: {e}")  # #24

    orchestrator = Orchestrator(
        config=state.config,
        model_adapter=state.model_adapter,
        judge=state.judge,
        extractor=state.extractor,
        prompt_loader=state.prompt_loader,
        event_bus=state.event_bus,
        search_service=state.search_service,
        failure_monitor=getattr(state, 'failure_monitor', None),
    )
    try:
        result = await orchestrator.execute(context)
    except Exception:
        # Keep non-stream exception-path billing aligned with /ask/stream:
        # only bill if model activity has actually happened.
        if state.quota_service and _tracker_indicates_model_call(state.model_adapter):
            _bill_mode = context.resolved_mode.value
            if _bill_mode:
                state.quota_service.record_usage(context.user_id, _bill_mode)
        raise

    # Save to user history (fire-and-forget)
    user_id = _get_user_id(request)
    if user_id and state.user_store:
        try:
            _best_single = next(
                (d["content"] for d in result.draft_answers if d.get("stage") == "fan_out_best"), ""
            )
            await state.user_store.save_query(
                user_id=user_id,
                query_id=result.query_id,
                session_id=active_session_id,
                question=result.question,
                mode=result.resolved_mode,
                final_answer=result.final_answer,
                confidence=result.confidence,
                contributor_count=result.contributor_count,
                latency_ms=result.latency_ms,
                estimated_cost_usd=result.estimated_cost_usd,
                quality_gate=result.quality_gate_result,
                best_single_answer=_best_single,
                has_divergence=getattr(result, 'has_divergence', False),
                divergence_summary=getattr(result, 'divergence_summary', '') or '',
                key_insights=getattr(result, 'key_insights', None),
                divergence_points=getattr(result, 'divergence_points', None),
            )
        except Exception as e:
            logger.warning(f"Failed to save query history: {e}")

    # Record usage (v2.7.5) — v5.2: unified billing gate (skip clarify, only bill if model called for errors)
    if state.quota_service and _should_record_usage_for_result(result):
        _bill_mode = _resolve_usage_mode(result, context.resolved_mode.value)
        if _bill_mode:
            state.quota_service.record_usage(context.user_id, _bill_mode)

    # v2.7.9d: Save turn to session history (v5.0: extended summary + outline)
    from agoracle.domain.types import Turn
    _outline = ""
    if result.key_insights and len(result.key_insights) >= 2:
        _outline = " | ".join(result.key_insights[:5])
    turn = Turn(
        question=req.question,
        final_answer_summary=result.final_answer[:2000],
        key_insights=result.key_insights[:5] if result.key_insights else [],
        mode=result.resolved_mode,
        answer_outline=_outline,
    )
    # v2.8.5: Persist turn to SQLite (cap enforced inside store)
    if state.conversation_store:
        try:
            await state.conversation_store.append_turn(active_session_id, turn, user_id=context.user_id)
        except Exception as e:
            logger.debug(f"Conversation turn save failed: {e}")  # #24

    return AskResponse(
        query_id=result.query_id,
        question=result.question,
        mode=result.resolved_mode,
        final_answer=result.final_answer,
        confidence=result.confidence,
        quality_gate=result.quality_gate_result,
        has_divergence=result.has_divergence,
        divergence_summary=result.divergence_summary,
        key_insights=result.key_insights,
        latency_ms=result.latency_ms,
        estimated_cost_usd=result.estimated_cost_usd,
        contributor_count=result.contributor_count,
        individual_responses=result.individual_responses,
        companion_hint=None,  # retired legacy field
        fast_path=bool(getattr(result, "fast_path", False)),
        low_confidence_actions=result.low_confidence_actions,
        session_id=active_session_id,
        context_compressed=len(context.session_history) > 6,
        search_citations=result.search_citations or [],
        next_steps=_to_nsg_model(result.next_steps) if result.next_steps else None,  # legacy compatibility field
        companion_guide=getattr(result, "companion_guide", None),  # legacy compatibility field derived from guidance
        consensus_type=getattr(result, "consensus_type", "unknown"),
        guidance=_to_guidance_model(getattr(result, "guidance", None)),  # canonical Dispatcher guidance
        reason_code=_resolve_reason_code(result),
    )


@router.post("/ask/stream")
async def ask_stream(req: AskRequest, request: Request):
    if req.mode == "socratic":
        raise HTTPException(status_code=422, detail="Use /api/socratic/start for Socratic mode")
    state = get_app_state(request)
    _user_id = _get_user_id(request)
    if not _user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    _user_obj = _get_user(request) or {}
    _is_admin = _user_obj.get("is_admin", False)
    user_profile = None
    if state.profile_store:
        try:
            user_profile = await state.profile_store.load(_user_id)
        except Exception as e:
            logger.debug(f"Profile load failed for language resolution: {e}")
    context = _build_context(req, user_id=_user_id)
    context.user_id = _user_id
    context.language = resolve_language(
        req.locale,
        request.headers.get("Accept-Language"),
        user_profile,
    )

    # v2.8.5: Multi-turn session history (persistent SQLite)
    # TODO(WP4-EVENT-WIRE): SessionCreated is intentionally not emitted yet.
    # Current event payload only carries session_id (no user_id), and there is no
    # implemented subscriber that would consume it without becoming a no-op.
    _active_session_id = req.session_id or uuid.uuid4().hex[:12]
    if req.session_id and state.conversation_store:
        try:
            context.session_history = await state.conversation_store.get_session_turns(req.session_id, user_id=_user_id)
        except Exception as e:
            logger.debug(f"Conversation history load failed: {e}")  # #24

    # P0-1: Populate profile summary for contributor prompt injection (数据飞轮环3)
    if state.profile_store and _user_id:
        try:
            context.user_profile_summary = await state.profile_store.get_summary(
                _user_id,
                language=context.language,
            )
        except Exception:
            pass

    # Quota check (v2.7.5)
    if state.quota_service:
        quota_err = state.quota_service.check_quota(_user_id, context.resolved_mode.value)
        if quota_err:
            raise HTTPException(status_code=429, detail=quota_err["message"])

    # INT-10: per-user concurrent SSE stream hard limit (DoS prevention)
    _user_stream_counts, _stream_count_lock, _MAX_STREAMS_PER_USER = get_stream_limiter()
    if _user_id:
        async with _stream_count_lock:
            if _user_stream_counts.get(_user_id, 0) >= _MAX_STREAMS_PER_USER:
                raise HTTPException(
                    status_code=429,
                    detail=f"已有 {_MAX_STREAMS_PER_USER} 个进行中的请求，请等待完成后重试",
                    headers={"Retry-After": "30"},
                )
            _user_stream_counts[_user_id] = _user_stream_counts.get(_user_id, 0) + 1

    async def event_generator():
        # INT-10: track active streams per user; always decrement on exit
        try:
            # v3.3: Always-on question optimization for Deep/Research
            # Replaces old clarity-only preflight. Now always generates an
            # optimized question and asks user to confirm before expensive pipeline.
            # If model says needs_confirmation=false, auto-proceed (no blocking).
            if not req.skip_preflight:
                _mode_cfg = state.config.modes.get(context.resolved_mode.value)
                if _mode_cfg and _mode_cfg.preflight_clarity_check:
                    try:
                        from agoracle.services.preflight import optimize_question
                        _opt = await optimize_question(
                            req.question, state.model_adapter,
                            _mode_cfg.preflight_clarity_check,
                        )
                        if _opt and _opt.get("needs_confirmation"):
                            yield {
                                "event": "question_confirmation",
                                "data": json.dumps(_opt, ensure_ascii=False),
                            }
                            return  # Wait for user to confirm/edit/skip
                        elif _opt and not _opt.get("needs_confirmation"):
                            # Question was optimized but doesn't need confirmation
                            # Update context with optimized question silently
                            context.question = _opt["optimized_question"]
                    except Exception:
                        pass  # Non-critical — proceed with original question

            _draft_answers: list[dict] = []  # v3.3: collect intermediate versions
            _model_id_map: dict[str, str] = {}   # SEC: real model_id → contributor_N alias
            _model_id_counter: list[int] = [0]   # mutable counter inside closure

            def _alias(model_id: str) -> str:
                """Map real model_id to opaque contributor alias (stable within one stream)."""
                if model_id not in _model_id_map:
                    _model_id_counter[0] += 1
                    _model_id_map[model_id] = f"contributor_{_model_id_counter[0]}"
                return _model_id_map[model_id]

            async for event in execute_streaming(
                context=context,
                config=state.config,
                model_adapter=state.model_adapter,
                judge=state.judge,
                extractor=state.extractor,
                prompt_loader=state.prompt_loader,
                event_bus=state.event_bus,
                search_service=state.search_service,
                failure_monitor=getattr(state, 'failure_monitor', None),
            ):
                if isinstance(event, Heartbeat):
                    yield {"event": "heartbeat", "data": ""}
                    continue
                if isinstance(event, StageStarted):
                    yield {"event": "stage_start", "data": json.dumps({"stage": event.stage, "detail": event.detail, "query_id": context.query_id})}
                elif isinstance(event, ContributorDone):
                    yield {"event": "contributor", "data": json.dumps({"model_id": _alias(event.model_id), "success": event.success, "latency_ms": event.latency_ms})}
                elif isinstance(event, PreviewAnswer):
                    yield {"event": "preview", "data": json.dumps({"model_id": _alias(event.model_id), "content": event.content})}
                elif isinstance(event, JudgeToken):
                    yield {"event": "token", "data": event.token}
                elif isinstance(event, StageCompleted):
                    yield {"event": "stage_complete", "data": json.dumps({"stage": event.stage, "detail": event.detail})}
                elif isinstance(event, CitationsReady):
                    yield {"event": "citations", "data": json.dumps({"citations": event.citations}, ensure_ascii=False)}
                elif isinstance(event, CompanionRoute):
                    yield {"event": "companion_route", "data": json.dumps({
                        "message": event.message,
                        "actions": event.actions,
                        "more_actions": event.more_actions,
                        "route_reason": event.route_reason,
                        "auto_execute_seconds": event.auto_execute_seconds,
                        "is_silent": event.is_silent,
                        "resolved_mode": event.resolved_mode,
                        "contributor_count": event.contributor_count,
                    }, ensure_ascii=False)}
                elif isinstance(event, DraftAnswer):
                    _draft_answers.append({"stage": event.stage, "model_id": event.model_id, "content": event.content})
                    if _is_admin:
                        yield {"event": "draft_answer", "data": json.dumps({"stage": event.stage, "model_id": _alias(event.model_id), "content": event.content}, ensure_ascii=False)}
                elif isinstance(event, PipelineComplete):
                    r = event.result
                    # Record usage (v2.7.5) — v5.2: unified billing gate
                    if state.quota_service and _should_record_usage_for_result(r):
                        _bill_mode = _resolve_usage_mode(r, context.resolved_mode.value)
                        if _bill_mode:
                            state.quota_service.record_usage(_user_id, _bill_mode)

                    # Build complete payload up front so serialization and persistence share one source of truth.
                    from agoracle.domain.types import Turn as _Turn

                    def _json_safe(v):
                        if v is None or isinstance(v, (str, bool, int)):
                            return v
                        if isinstance(v, float):
                            if v != v or v in (float("inf"), float("-inf")):
                                return 0.0
                            return v
                        if isinstance(v, dict):
                            return {str(k): _json_safe(val) for k, val in v.items()}
                        if isinstance(v, (list, tuple, set)):
                            return [_json_safe(i) for i in v]
                        if hasattr(v, "model_dump"):
                            try:
                                return _json_safe(v.model_dump())
                            except Exception:
                                pass
                        if hasattr(v, "__dict__"):
                            try:
                                return _json_safe(vars(v))
                            except Exception:
                                pass
                        return str(v)

                    _resolved_mode = r.resolved_mode.value if hasattr(r.resolved_mode, "value") else r.resolved_mode
                    _complete_payload: dict = {
                        "query_id": r.query_id,
                        "question": r.question,
                        "mode": _resolved_mode,
                        "final_answer": r.final_answer or "",
                        "confidence": _json_safe(r.confidence),
                        "quality_gate": r.quality_gate_result,
                        "has_divergence": bool(r.has_divergence),
                        "divergence_summary": r.divergence_summary,
                        "key_insights": _json_safe(r.key_insights or []),
                        "latency_ms": int(r.latency_ms or 0),
                        "estimated_cost_usd": _json_safe(r.estimated_cost_usd),
                        "contributor_count": int(r.contributor_count or 0),
                        "fast_path": bool(getattr(r, "fast_path", False)),
                        "low_confidence_actions": _json_safe(getattr(r, "low_confidence_actions", [])),
                        "session_id": _active_session_id,
                        "context_compressed": len(context.session_history) > 6,
                        "search_citations": _json_safe(getattr(r, "search_citations", []) or []),  # v4.18
                        "divergence_points": _json_safe(getattr(r, "divergence_points", []) or []),  # v4.20
                        "consensus_points": _json_safe(getattr(r, "consensus_points", []) or []),  # A4: 交叉验证
                        "fact_warnings": _json_safe(getattr(r, "fact_warnings", []) or []),  # v4.22d
                        "next_steps": _json_safe(_to_nsg_model(r.next_steps)) if getattr(r, "next_steps", None) else None,  # legacy compatibility field
                        "companion_guide": _json_safe(getattr(r, "companion_guide", None)),  # legacy compatibility field derived from guidance
                        "consensus_type": getattr(r, "consensus_type", "unknown"),
                        "guidance": _json_safe(_to_guidance_model(getattr(r, "guidance", None))),
                        "reason_code": _resolve_reason_code(r),
                    }
                    if _is_admin:
                        _complete_payload["individual_responses"] = _json_safe(r.individual_responses)
                        _complete_payload["draft_answers"] = _json_safe(_draft_answers)
                    try:
                        _complete_json = json.dumps(_complete_payload, ensure_ascii=False, allow_nan=False)
                    except (ValueError, TypeError) as _je:
                        logger.error(
                            f"complete payload serialization failed: {_je} — fallback to minimal complete payload"
                        )
                        _minimal_payload = {
                            "query_id": r.query_id,
                            "question": r.question,
                            "mode": _resolved_mode,
                            "final_answer": r.final_answer or "",
                            "confidence": 0.0,
                            "quality_gate": "low_confidence",
                            "has_divergence": False,
                            "divergence_summary": "",
                            "key_insights": [],
                            "latency_ms": int(r.latency_ms or 0),
                            "estimated_cost_usd": 0.0,
                            "contributor_count": int(r.contributor_count or 0),
                            "fast_path": bool(getattr(r, "fast_path", False)),
                            "low_confidence_actions": [],
                            "session_id": _active_session_id,
                            "context_compressed": len(context.session_history) > 6,
                            "search_citations": [],
                            "divergence_points": [],
                            "consensus_points": [],
                            "fact_warnings": ["结果已生成，但扩展字段序列化失败，已自动降级返回。"],
                            "guidance": None,
                            "reason_code": "low_confidence",
                        }
                        _complete_json = json.dumps(_minimal_payload, ensure_ascii=False, allow_nan=False)
                    # Persist query history before emitting complete, so /history is consistent
                    # as soon as the frontend receives a terminal success event.
                    _HISTORY_SAVE_TIMEOUT = 5  # seconds
                    if _user_id and state.user_store:
                        try:
                            _best_single_stream = next(
                                (d["content"] for d in _draft_answers if d.get("stage") == "fan_out_best"), ""
                            )
                            await asyncio.wait_for(
                                state.user_store.save_query(
                                    user_id=_user_id,
                                    query_id=r.query_id,
                                    session_id=_active_session_id,
                                    question=r.question,
                                    mode=r.resolved_mode,
                                    final_answer=r.final_answer,
                                    confidence=r.confidence,
                                    contributor_count=r.contributor_count,
                                    latency_ms=r.latency_ms,
                                    estimated_cost_usd=r.estimated_cost_usd,
                                    quality_gate=r.quality_gate_result,
                                    best_single_answer=_best_single_stream,
                                    has_divergence=getattr(r, 'has_divergence', False),
                                    divergence_summary=getattr(r, 'divergence_summary', '') or '',
                                    key_insights=getattr(r, 'key_insights', None),
                                    divergence_points=getattr(r, 'divergence_points', None),
                                ),
                                timeout=_HISTORY_SAVE_TIMEOUT,
                            )
                        except asyncio.TimeoutError:
                            logger.warning(f"[{r.query_id}] History save_query timed out ({_HISTORY_SAVE_TIMEOUT}s)")
                        except Exception as e:
                            logger.warning(f"Failed to save streaming query history: {e}")

                    yield {"event": "complete", "data": _complete_json}

                    _outline_s = ""
                    if r.key_insights and len(r.key_insights) >= 2:
                        _outline_s = " | ".join(r.key_insights[:5])
                    _turn = _Turn(
                        question=req.question,
                        final_answer_summary=r.final_answer[:2000],
                        key_insights=r.key_insights[:5] if r.key_insights else [],
                        mode=r.resolved_mode,
                        answer_outline=_outline_s,
                    )
                    if state.conversation_store:
                        try:
                            await asyncio.wait_for(
                                state.conversation_store.append_turn(
                                    _active_session_id, _turn, user_id=_user_id or 0
                                ),
                                timeout=_HISTORY_SAVE_TIMEOUT,
                            )
                        except asyncio.TimeoutError:
                            logger.warning(f"[{r.query_id}] Conversation turn save timed out ({_HISTORY_SAVE_TIMEOUT}s)")
                        except Exception as _e:
                            logger.debug(f"Conversation turn save failed: {_e}")

                elif isinstance(event, PipelineError):
                    # v5.2: billing on error — unified gate:
                    # if event.result present → gate decides (result model_calls/cost);
                    # if no result but billable flag set → use billed_mode as fallback.
                    if state.quota_service:
                        _err_result = getattr(event, "result", None)
                        _billable = getattr(event, "billable", False)
                        if _err_result is not None:
                            if _should_record_usage_for_result(_err_result):
                                _bill_mode = _resolve_usage_mode(_err_result, context.resolved_mode.value)
                                if _bill_mode:
                                    state.quota_service.record_usage(_user_id, _bill_mode)
                        elif _billable:
                            _bill_mode = getattr(event, "billed_mode", "") or context.resolved_mode.value
                            if _bill_mode:
                                state.quota_service.record_usage(_user_id, _bill_mode)
                    yield {"event": "error", "data": json.dumps({"error": event.error})}
        finally:
            # INT-10: always release stream slot on exit (normal, error, or disconnect)
            if _user_id:
                async with _stream_count_lock:
                    _user_stream_counts[_user_id] = max(0, _user_stream_counts.get(_user_id, 1) - 1)

    return EventSourceResponse(event_generator())
