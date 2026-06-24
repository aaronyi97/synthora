"""
Admin routes: usage dashboard and cost report.

Extracted from app.py as part of Phase 3 route split (DEV-PHASE3-ROUTES-ADMIN-R1).
All behaviour is identical to the original inline implementation.

Dependency pattern: lazy-import `agoracle.api.app` inside each handler to avoid
circular imports (app.py → routes/admin.py → app.py).
Auth: all endpoints guarded by require_admin (replaces inline is_admin check).
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException, Request

from agoracle.api.deps import require_admin
from agoracle.api.schemas import AdminCostReportResponse, AdminUsageResponse, AdminUserUsageResponse

logger = logging.getLogger(__name__)

router = APIRouter()


def _app():
    import agoracle.api.app as _m
    return _m


@router.get("/admin/usage", response_model=AdminUsageResponse)
async def admin_usage(request: Request, date: str = None):
    """Get usage stats for all users. Admin only."""
    require_admin(request)
    m = _app()
    state = m.state
    if not state.quota_service:
        raise HTTPException(status_code=503, detail="Quota service not available")
    if date is not None:
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
            raise HTTPException(status_code=422, detail="date must be in YYYY-MM-DD format")
    return {
        "date": date or state.quota_service._today(),
        "limits": state.quota_service.get_limits(),
        "users": state.quota_service.get_all_usage(date),
    }


@router.get("/admin/usage/{user_id}", response_model=AdminUserUsageResponse)
async def admin_user_usage(user_id: int, request: Request, days: int = 7):
    """Get usage history for a specific user. Admin only."""
    require_admin(request)
    m = _app()
    state = m.state
    if not state.quota_service:
        raise HTTPException(status_code=503, detail="Quota service not available")
    return {
        "user_id": user_id,
        "today": state.quota_service.get_usage(user_id),
        "limits": state.quota_service.get_limits(),
        "history": state.quota_service.get_user_history(user_id, days),
    }


@router.get("/admin/cost-report", response_model=AdminCostReportResponse)
async def admin_cost_report(request: Request, date: str = None):
    """Actual API cost report for today (or given date). Admin only.

    v4.30: Cost figures are now read from per-query estimated_cost_usd stored in
    query_history (actual tracked cost, not P50 estimates). P50 fallback is used
    only when no history records exist for the date (e.g. new deployment).

    Returns per-mode call counts, actual API cost, and top users by consumption.
    """
    require_admin(request)
    m = _app()
    state = m.state
    if not state.quota_service:
        raise HTTPException(status_code=503, detail="Quota service not available")

    if date is not None:
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
            raise HTTPException(status_code=422, detail="date must be in YYYY-MM-DD format")
    target_date = date or state.quota_service._today()

    # v4.30: Primary — actual per-query cost from query_history
    actual_data: dict = {}
    if state.user_store:
        try:
            actual_data = await state.user_store.get_cost_by_date(target_date)
        except Exception as _ce:
            logger.warning(f"cost-report: get_cost_by_date failed: {_ce}")

    mode_totals_actual: dict[str, int] = actual_data.get("mode_counts", {})
    mode_costs_actual: dict[str, float] = actual_data.get("mode_costs_usd", {})
    top_users_by_cost: list = actual_data.get("top_users_by_cost", [])
    has_actual_data = bool(mode_totals_actual)

    # v4.30: Fallback — P50 estimates when no history available for the date
    # (e.g. first day after deployment or date with zero queries)
    _P50_COST_PER_QUERY = {
        "light": 0.006,
        "socratic": 0.073,
        "deep": 0.45,
        "research": 0.53,
    }
    all_usage = state.quota_service.get_all_usage(target_date)
    mode_totals_quota: dict[str, int] = {"light": 0, "socratic": 0, "deep": 0, "research": 0}
    for user_data in all_usage.values():
        for mode in mode_totals_quota:
            mode_totals_quota[mode] += user_data.get(mode, 0)

    # Use actual data if available, otherwise fall back to P50 estimates
    if has_actual_data:
        mode_call_counts = {m: mode_totals_actual.get(m, 0) for m in ["light", "socratic", "deep", "research"]}
        mode_costs = {m: mode_costs_actual.get(m, 0.0) for m in ["light", "socratic", "deep", "research"]}
        cost_basis = "actual per-query tracked cost (v4.30)"
    else:
        mode_call_counts = mode_totals_quota
        mode_costs = {m: round(c * _P50_COST_PER_QUERY.get(m, 0), 4)
                      for m, c in mode_totals_quota.items()}
        cost_basis = "P50 fallback (no query_history for this date): light=$0.006, socratic=$0.073, deep=$0.45, research=$0.53"

    total_cost = round(sum(mode_costs.values()), 6)

    # Top users by query count (from quota, for backward compat display)
    user_totals = [
        {"user_id": uid, "total_queries": sum(d.get(mode, 0) for mode in mode_totals_quota), **{mode: d.get(mode, 0) for mode in mode_totals_quota}}
        for uid, d in all_usage.items()
    ]
    user_totals.sort(key=lambda x: x["total_queries"], reverse=True)

    return {
        "date": target_date,
        "mode_call_counts": mode_call_counts,
        "mode_costs_usd": mode_costs,
        "total_cost_usd": total_cost,
        "top_users_by_queries": user_totals[:10],
        "top_users_by_cost": top_users_by_cost,
        "cost_basis": cost_basis,
    }
