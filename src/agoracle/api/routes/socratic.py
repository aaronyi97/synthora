"""Socratic endpoints — extracted from app.py."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from agoracle.api.app import (
    SocraticRevealRequest,
    SocraticRespondRequest,
    SocraticStartRequest,
)
from agoracle.api.deps import _get_user_id, get_app_state, resolve_language
from agoracle.api.schemas import (
    SocraticRespondResponse,
    SocraticRevealResponse,
    SocraticStartResponse,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _is_english(language: str) -> bool:
    return (language or "").strip() == "en-US"


def _locale_text(language: str, zh: str, en: str) -> str:
    return en if _is_english(language) else zh


@router.post("/socratic/start", response_model=SocraticStartResponse)
async def socratic_start(req: SocraticStartRequest, request: Request):
    """Legacy blocking endpoint — kept for backward compatibility."""
    state = get_app_state(request)
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required for Socratic mode")
    if state.quota_service:
        quota_err = state.quota_service.check_quota(user_id, "socratic")
        if quota_err:
            raise HTTPException(status_code=429, detail=quota_err["message"])

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

    session = await state.socratic_orch.start_session(req.question, language=language)
    if user_id and state.session_store:
        await state.session_store.set_owner(session.session_id, user_id)
    if state.quota_service and user_id:
        state.quota_service.record_usage(user_id, "socratic")
    dm = session.divergence_map
    return {
        "session_id": session.session_id,
        "phase1_latency_ms": session.phase1_latency_ms,
        "max_guide_rounds": session.max_guide_rounds,
        "divergence_map": {
            "consensus_points": dm.consensus_points if dm else [],
            "divergence_count": len(dm.divergence_points) if dm else 0,
            "overall_consensus": dm.overall_consensus_score if dm else 0,
        },
        "initial_guide": session.turns[-1].content if session.turns else "",
    }


@router.post("/socratic/start/stream")
async def socratic_start_stream(req: SocraticStartRequest, request: Request):
    """
    方案C: SSE streaming Socratic start — user sees progress in ~10s.

    Events:
      socratic_stage     — pipeline stage update (recruiting/searching/thinking/analyzing/guiding)
      socratic_contributor — one expert responded
      socratic_divergence — divergence analysis ready
      socratic_ready     — session ready, guide question generated (terminal event)
      socratic_error     — pipeline error (terminal event)
    """
    from agoracle.services.streaming import (
        SocraticStage, SocraticContributorDone,
        SocraticDivergenceReady, SocraticReady, SocraticError,
    )

    state = get_app_state(request)
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required for Socratic mode")
    if state.quota_service:
        quota_err = state.quota_service.check_quota(user_id, "socratic")
        if quota_err:
            raise HTTPException(status_code=429, detail=quota_err["message"])

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

    async def event_generator():
        session_id = None
        try:
            async for event in state.socratic_orch.start_session_streaming(
                req.question,
                language=language,
            ):
                if isinstance(event, SocraticStage):
                    if event.stage == "heartbeat":
                        yield {"event": "heartbeat", "data": ""}  # SSE keep-alive; frontend ignores
                        continue
                    yield {
                        "event": "socratic_stage",
                        "data": json.dumps({"stage": event.stage, "detail": event.detail}, ensure_ascii=False),
                    }
                elif isinstance(event, SocraticContributorDone):
                    yield {
                        "event": "socratic_contributor",
                        "data": json.dumps({
                            "model_id": event.model_id,
                            "success": event.success,
                            "latency_ms": event.latency_ms,
                            "done_count": event.done_count,
                            "total_count": event.total_count,
                        }, ensure_ascii=False),
                    }
                elif isinstance(event, SocraticDivergenceReady):
                    yield {
                        "event": "socratic_divergence",
                        "data": json.dumps({
                            "consensus_points": event.consensus_points,
                            "divergence_count": event.divergence_count,
                            "overall_consensus": event.overall_consensus,
                        }, ensure_ascii=False),
                    }
                elif isinstance(event, SocraticReady):
                    session_id = event.session_id
                    # Track session ownership
                    if user_id and state.session_store:
                        await state.session_store.set_owner(event.session_id, user_id)
                    # Record quota
                    if state.quota_service and user_id:
                        state.quota_service.record_usage(user_id, "socratic")
                    yield {
                        "event": "socratic_ready",
                        "data": json.dumps({
                            "session_id": event.session_id,
                            "initial_guide": event.initial_guide,
                            "max_guide_rounds": event.max_guide_rounds,
                            "divergence_map": event.divergence_map,
                            "phase1_latency_ms": event.phase1_latency_ms,
                        }, ensure_ascii=False),
                    }
                elif isinstance(event, SocraticError):
                    yield {
                        "event": "socratic_error",
                        "data": json.dumps({"error": event.error}, ensure_ascii=False),
                    }
        except Exception as e:
            logger.error(f"Socratic SSE stream error: {e}", exc_info=True)
            yield {
                "event": "socratic_error",
                "data": json.dumps({
                    "error": _locale_text(
                        language,
                        "苏格拉底会话遇到异常，请稍后重试",
                        "The Socratic session hit an unexpected error. Please try again.",
                    ),
                }, ensure_ascii=False),
            }

    return EventSourceResponse(event_generator())


@router.post("/socratic/respond", response_model=SocraticRespondResponse)
async def socratic_respond(req: SocraticRespondRequest, request: Request):
    state = get_app_state(request)
    # Ownership check (persistent, fail-closed)
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    if state.session_store:
        owner = await state.session_store.get_owner(req.session_id)
        # SEC-IDOR: owner=0 means unbound/legacy — deny access (fail-closed, not a free pass)
        if owner is None or owner == 0 or user_id != owner:
            raise HTTPException(status_code=403, detail="Not authorized for this session")
    session = await state.socratic_orch.get_session(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    turn = await state.socratic_orch.respond(session, req.message)
    return {
        "guide_message": turn.content,
        "round": session.guide_rounds_used,
        "max_rounds": session.max_guide_rounds,
        "latency_ms": turn.latency_ms,
    }


@router.post("/socratic/reveal", response_model=SocraticRevealResponse)
async def socratic_reveal(req: SocraticRevealRequest, request: Request):
    state = get_app_state(request)
    # Ownership check (persistent, fail-closed)
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    if state.session_store:
        owner = await state.session_store.get_owner(req.session_id)
        # SEC-IDOR: owner=0 means unbound/legacy — deny access (fail-closed, not a free pass)
        if owner is None or owner == 0 or user_id != owner:
            raise HTTPException(status_code=403, detail="Not authorized for this session")
    session = await state.socratic_orch.get_session(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    reveal_data = await state.socratic_orch.reveal(session)
    finished = await state.socratic_orch.finish(session, user_id=user_id)

    # Save Socratic session to user history (user_id already set above)
    if user_id and state.user_store:
        try:
            dm_saved = reveal_data.get("divergence_map")
            divergence_summary = ""
            if dm_saved:
                if dm_saved.divergence_points:
                    first = dm_saved.divergence_points[0]
                    divergence_summary = (first.description or first.topic or "").strip()
                elif dm_saved.consensus_points:
                    divergence_summary = "；".join(dm_saved.consensus_points[:2])
            await state.user_store.save_query(
                user_id=user_id,
                query_id=req.session_id,
                session_id=req.session_id,
                question=session.question if hasattr(session, 'question') else "Socratic session",
                mode="socratic",
                final_answer=reveal_data.get("full_answer", "")[:500],
                confidence=0.0,
                contributor_count=finished.guide_rounds_used if finished else 0,
                latency_ms=session.phase1_latency_ms if hasattr(session, 'phase1_latency_ms') else 0,
                estimated_cost_usd=0.0,
                # BUG-3 fix: reveal_data has "divergence_map" (DivergenceMap object),
                # not "divergence_points"/"key_insights" at top level.
                has_divergence=bool(dm_saved and dm_saved.divergence_points),
                divergence_summary=divergence_summary,
                key_insights=[dp.description for dp in dm_saved.divergence_points[:5]] if dm_saved else [],
                divergence_points=[{"topic": dp.topic, "description": dp.description, "consensus_ratio": dp.consensus_ratio} for dp in dm_saved.divergence_points] if dm_saved else [],
            )
        except Exception as e:
            logger.warning(f"Failed to save socratic history: {e}")

    dm = reveal_data.get("divergence_map")
    cognitive = finished.cognitive_snapshot

    return {
        "full_answer": reveal_data["full_answer"],
        "divergence_map": {
            "consensus_points": dm.consensus_points if dm else [],
            "divergence_points": [
                {
                    "topic": dp.topic,
                    "description": dp.description,
                    "positions": dp.positions,
                    "consensus_ratio": dp.consensus_ratio,
                    "difficulty": dp.difficulty,
                }
                for dp in (dm.divergence_points if dm else [])
            ],
        },
        "cognitive_snapshot": {
            "reasoning_depth": cognitive.reasoning_depth if cognitive else 0,
            "nuance_recognition": cognitive.nuance_recognition if cognitive else 0,
            "anchoring_detected": cognitive.anchoring_detected if cognitive else False,
            "confirmation_bias": cognitive.confirmation_bias if cognitive else False,
            "blind_spots": cognitive.blind_spots if cognitive else [],
        },
        "guide_rounds_used": finished.guide_rounds_used,
    }
