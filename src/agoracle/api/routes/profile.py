"""
Profile, cognitive, growth, usage, capability-map, and improvement-plan routes.

Extracted from app.py as part of Phase 3 route split (DEV-PHASE3-ROUTES-PROFILE-R1).
All behaviour is identical to the original inline implementation.

Dependency pattern: lazy-import `agoracle.api.app` inside each handler to avoid
circular imports (app.py → routes/profile.py → app.py).
Auth: endpoints that require login use require_auth from deps.py.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request

from agoracle.api.deps import _get_user, normalize_locale, require_auth, resolve_language
from agoracle.api.schemas import (
    BehaviorSummaryResponse,
    CapabilityMapResponse,
    CognitiveConsentResponse,
    CognitiveSummaryResponse,
    DeleteCognitiveResponse,
    GrowthResponse,
    HistoryResponse,
    ImprovementPlanActionResponse,
    ImprovementPlansListResponse,
    ProfileExportResponse,
    RecentTurnsResponse,
    SetLanguageRequest,
    UsageResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_GROWTH_DEPTH_LABELS = {
    "zh-CN": {1: "触达", 2: "框架", 3: "深入", 4: "方案", 5: "验证"},
    "en-US": {1: "Exposure", 2: "Framework", 3: "Deep", 4: "Strategy", 5: "Verified"},
}


def _app():
    import agoracle.api.app as _m
    return _m


# ── Query history ──────────────────────────────────────

@router.get("/history", response_model=HistoryResponse)
async def history(request: Request, limit: int = 20, offset: int = 0):
    """Return paginated query history for the current user."""
    require_auth(request)
    user = _get_user(request)
    uid = user["id"]
    m = _app()
    if not m.state.user_store:
        raise HTTPException(status_code=503, detail="User system not available")
    items = await m.state.user_store.get_history(uid, limit=min(limit, 100), offset=offset)
    total = await m.state.user_store.get_history_count(uid)
    return {"history": items, "total": total}


# ── Cognitive profile management ──────────────────────

@router.post("/profile/cognitive-consent", response_model=CognitiveConsentResponse)
async def toggle_cognitive_consent(request: Request, consent: bool = True):
    """Toggle cognitive tracking consent (opt-in/opt-out)."""
    m = _app()
    state = m.state
    if not state.profile_store:
        raise HTTPException(status_code=503, detail="Profile store not available")
    uid = require_auth(request)
    profile = await state.profile_store.load(uid)
    profile.cognitive_tracking_consent = consent
    await state.profile_store.save(profile, uid)
    return {"status": "ok", "cognitive_tracking_consent": consent}


# DELETE /profile/cognitive-data removed (v2.8.4): lacked ?confirm=true.
# Use POST /profile/delete-cognitive?confirm=true instead (禁令#17: 不可逆操作必须二次确认).

@router.get("/profile/cognitive-summary", response_model=CognitiveSummaryResponse)
async def get_cognitive_summary(request: Request):
    """Get a summary of the user's cognitive profile with structured data for visualization."""
    m = _app()
    state = m.state
    if not state.profile_store:
        raise HTTPException(status_code=503, detail="Profile store not available")
    uid = require_auth(request)
    from agoracle.services.cognitive_tracker import CognitiveTracker
    tracker = CognitiveTracker(state.profile_store)
    summary = await tracker.get_cognitive_summary(uid)
    profile = await state.profile_store.load(uid)
    return {
        "summary": summary,
        "cognitive_tracking_consent": profile.cognitive_tracking_consent,
        "data": {
            "quadrant_dist": profile.cognitive_quadrant_dist,
            "comfort_zone_topics": profile.comfort_zone_topics,
            "growth_zone_topics": profile.growth_zone_topics,
            "mode_usage": profile.mode_usage_history,
            "socratic_sessions": profile.mode_usage_history.get("socratic", 0),
            "completion_rate": profile.socratic_completion_rate,
            "avg_reasoning_quality": profile.average_reasoning_quality,
            "reasoning_trend": profile.reasoning_improvement_trend,
            "last_challenge_date": profile.last_challenge_date,
        },
    }


@router.get(
    "/profile/behavior-summary",
    response_model=BehaviorSummaryResponse,
    response_model_exclude_none=True,
)
async def get_behavior_summary(request: Request):
    """Get CBA narrative behavioral summary (ADR-014).

    Returns narrative-style insights about the user's cognitive
    behavioral patterns: divergent-convergent dynamics, engagement
    patterns, and topic exploration. No labels, only observable patterns.
    """
    m = _app()
    state = m.state
    if not state.behavior_analytics:
        raise HTTPException(status_code=503, detail="Behavior analytics not available")
    uid = require_auth(request)
    user_profile = None
    if state.profile_store:
        try:
            user_profile = await state.profile_store.load(uid)
        except Exception:
            user_profile = None
    language = resolve_language(
        None,
        request.headers.get("Accept-Language"),
        user_profile,
    )
    return await state.behavior_analytics.get_narrative_summary(uid, language=language)


@router.get("/profile/language")
async def get_language(request: Request):
    """Return current user's preferred language."""
    m = _app()
    state = m.state
    if not state.profile_store:
        raise HTTPException(status_code=503, detail="Profile store not available")
    uid = require_auth(request)
    profile = await state.profile_store.load(uid)
    return {"language": normalize_locale(profile.preferred_language)}


@router.put("/profile/language")
async def set_language(body: SetLanguageRequest, request: Request):
    """Persist current user's preferred language."""
    m = _app()
    state = m.state
    if not state.profile_store:
        raise HTTPException(status_code=503, detail="Profile store not available")
    uid = require_auth(request)
    profile = await state.profile_store.load(uid)
    profile.preferred_language = normalize_locale(body.language)
    await state.profile_store.save(profile, uid)
    return {"language": profile.preferred_language}


# ── User: Data Sovereignty (v2.7.8h, 原则#22) ──────

@router.post("/profile/delete-cognitive", response_model=DeleteCognitiveResponse)
async def delete_cognitive_data(request: Request, confirm: bool = Query(False)):
    """Delete all cognitive tracking data for the current user (原则#22 用户主权).

    Preserves non-cognitive fields (preferences, mode history).
    Requires explicit confirmation via query param ?confirm=true.
    """
    m = _app()
    state = m.state
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="此操作不可逆。请添加 ?confirm=true 参数确认删除。",
        )
    uid = require_auth(request)
    from agoracle.services.cognitive_tracker import CognitiveTracker
    tracker = CognitiveTracker(state.profile_store)
    if not state.profile_store:
        raise HTTPException(status_code=503, detail="Profile store not available")
    profile = await tracker.delete_cognitive_data(uid)
    logger.info(f"User {uid} deleted cognitive data (原则#22)")
    return {"status": "ok", "message": "认知数据已删除", "remaining_fields": list(profile.mode_usage_history.keys())}


@router.get("/profile/export", response_model=ProfileExportResponse, response_model_exclude_none=True)
async def export_profile(request: Request):
    """Export all user data as JSON (原则#22 用户主权 — 数据控制权).

    Returns complete profile, cognitive data, behavior analytics,
    and query history in a single downloadable JSON.
    """
    m = _app()
    state = m.state
    uid = require_auth(request)

    export_data: dict = {"exported_at": datetime.now().isoformat(), "user_id": uid}

    # Profile data
    if state.profile_store:
        profile = await state.profile_store.load(uid)
        export_data["profile"] = {
            "topic_frequency": profile.topic_frequency,
            "mode_usage_history": profile.mode_usage_history,
            "cognitive_quadrant_dist": profile.cognitive_quadrant_dist,
            "growth_zone_topics": profile.growth_zone_topics,
            "comfort_zone_topics": profile.comfort_zone_topics,
            "socratic_completion_rate": profile.socratic_completion_rate,
            "average_reasoning_quality": profile.average_reasoning_quality,
            "cognitive_tracking_consent": profile.cognitive_tracking_consent,
        }

    # Query history
    if state.user_store:
        history = await state.user_store.get_history(uid, limit=1000, offset=0)
        export_data["query_history"] = history

    # Usage quota
    if state.quota_service:
        export_data["usage"] = state.quota_service.get_user_history(uid, days=30)

    return export_data


# ── User: Growth Dashboard (P1-1, 原则#19 成长可视化) ──

@router.get("/profile/recent-turns", response_model=RecentTurnsResponse)
async def get_recent_turns(request: Request):
    """Return Memory-lite recent_turns for cross-session continuity card (ML-04)."""
    m = _app()
    state = m.state
    if not state.profile_store:
        return {"recent_turns": []}
    uid = require_auth(request)
    profile = await state.profile_store.load(uid)
    turns = getattr(profile, "recent_turns", [])
    return {"recent_turns": turns[-3:]}


@router.get("/profile/growth", response_model=GrowthResponse)
async def get_growth_dashboard(request: Request):
    """Growth visualization data (原则#19: 成长可视化).

    Returns topic depth progression, expertise scores, and exploration
    patterns so the user can see "我不知不觉就变强了".
    """
    m = _app()
    state = m.state
    if not state.profile_store:
        raise HTTPException(status_code=503, detail="Profile store not available")
    uid = require_auth(request)
    profile = await state.profile_store.load(uid)

    language = resolve_language(
        None,
        request.headers.get("Accept-Language"),
        profile,
    )
    depth_labels = _GROWTH_DEPTH_LABELS.get(language, _GROWTH_DEPTH_LABELS["zh-CN"])
    depth_map = getattr(profile, "topic_depth_map", {})
    topics = []
    for tag, level in sorted(depth_map.items(), key=lambda x: -x[1]):
        topics.append({
            "topic": tag,
            "depth": level,
            "depth_label": depth_labels.get(level, f"L{level}"),
            "frequency": profile.topic_frequency.get(tag, 0),
            "expertise": profile.topic_expertise.get(tag, 0.0),
        })

    total = len(topics)
    deep_count = sum(1 for t in topics if t["depth"] >= 3)
    mastered = sum(1 for t in topics if t["depth"] >= 4)

    return {
        "has_data": total > 0,
        "summary": {
            "total_topics": total,
            "deep_topics": deep_count,
            "mastered_topics": mastered,
            "growth_score": round(deep_count / max(total, 1) * 100),
        },
        "topics": topics[:30],
    }


# ── User: My Usage (v2.7.5) ──────────────────────────

@router.get("/profile/usage", response_model=UsageResponse)
async def my_usage(request: Request):
    """Get current user's daily usage and remaining quota."""
    m = _app()
    state = m.state
    uid = require_auth(request)
    if not state.quota_service:
        raise HTTPException(status_code=503, detail="Quota service not available")
    usage = state.quota_service.get_usage(uid)
    limits = state.quota_service.get_limits()
    return {
        "usage": usage,
        "limits": limits,
        "remaining": {k: limits[k] - usage.get(k, 0) for k in limits},
    }


# ── Proactive Coach: Capability Map & Plans (v2.7.9d) ──

@router.get("/capability-map", response_model=CapabilityMapResponse)
async def get_capability_map(request: Request):
    """Get user's capability map for frontend visualization.

    Returns topic depth radar data, active improvement plans,
    cognitive quadrant summary, and growth trend.
    """
    m = _app()
    state = m.state
    if not state.proactive_coach:
        raise HTTPException(status_code=503, detail="Coach service not available")
    uid = require_auth(request)
    return await state.proactive_coach.get_capability_map(uid)


@router.get("/improvement-plans", response_model=ImprovementPlansListResponse)
async def get_improvement_plans(request: Request):
    """List all improvement plans for the current user."""
    m = _app()
    state = m.state
    if not state.profile_store:
        raise HTTPException(status_code=503, detail="Profile store not available")
    uid = require_auth(request)
    profile = await state.profile_store.load(uid)
    return {"plans": profile.improvement_plans}


@router.post(
    "/improvement-plans/{plan_id}/activate",
    response_model=ImprovementPlanActionResponse,
    response_model_exclude_none=True,
)
async def activate_plan(plan_id: str, request: Request):
    """Accept and activate a proposed improvement plan."""
    m = _app()
    state = m.state
    if not state.proactive_coach:
        raise HTTPException(status_code=503, detail="Coach service not available")
    uid = require_auth(request)
    result = await state.proactive_coach.activate_plan(plan_id, uid)
    if not result:
        raise HTTPException(status_code=404, detail="Plan not found or not in proposed state")
    return {"status": "activated", "plan": result}


@router.post(
    "/improvement-plans/{plan_id}/abandon",
    response_model=ImprovementPlanActionResponse,
    response_model_exclude_none=True,
)
async def abandon_plan(plan_id: str, request: Request):
    """Abandon an improvement plan."""
    m = _app()
    state = m.state
    if not state.proactive_coach:
        raise HTTPException(status_code=503, detail="Coach service not available")
    uid = require_auth(request)
    result = await state.proactive_coach.abandon_plan(plan_id, uid)
    if not result:
        raise HTTPException(status_code=404, detail="Plan not found")
    return {"status": "abandoned", "plan": result}


@router.post(
    "/improvement-plans/{plan_id}/engaged",
    response_model=ImprovementPlanActionResponse,
    response_model_exclude_none=True,
)
async def record_challenge_engagement(plan_id: str, request: Request):
    """Record that user engaged with a micro-challenge (responded to it)."""
    m = _app()
    state = m.state
    if not state.proactive_coach:
        raise HTTPException(status_code=503, detail="Coach service not available")
    uid = require_auth(request)
    await state.proactive_coach.record_challenge_engagement(plan_id, uid)
    return {"status": "recorded"}
