"""Health check endpoint — extracted from app.py."""
from __future__ import annotations

import logging
import math
import os
import subprocess

from fastapi import APIRouter, Request

from agoracle.api.deps import get_app_state
from agoracle.config.loader import PROJECT_ROOT

router = APIRouter()
logger = logging.getLogger(__name__)
MIN_CONTRIBUTOR_RATIO = 0.6


def _app():
    import agoracle.api.app as _m
    return _m


def _conversation_store_status(state) -> str:
    return "ok" if getattr(state, "conversation_store", None) else "degraded"


def _overall_status(
    *,
    available_count: int,
    total_models: int,
    session_db_ok: bool,
    conversation_store_ok: bool,
    critical_modes_degraded: list[str],
) -> str:
    if available_count == 0 or not session_db_ok:
        return "unhealthy"
    if critical_modes_degraded:
        return "degraded"
    if available_count < total_models or not conversation_store_ok:
        return "degraded"
    return "ok"


def _status_meaning(status: str) -> str:
    meanings = {
        "ok": "Service is available and critical dependencies are ready.",
        "degraded": "Service is available, but some models or auxiliary capabilities are unavailable.",
        "unhealthy": "Service cannot provide core external API capability.",
    }
    return meanings.get(status, "Unknown health status.")


def _frontend_linked_validation_blockers(
    *,
    available_count: int,
    session_db_ok: bool,
    conversation_store_ok: bool,
) -> list[str]:
    blockers: list[str] = []
    if available_count == 0:
        blockers.append("no_models_available")
    if not session_db_ok:
        blockers.append("session_store_unavailable")
    if not conversation_store_ok:
        blockers.append("conversation_store_unavailable")
    return blockers


def _minimum_required_contributors(mode_config) -> int:
    contributors = list(getattr(mode_config, "contributors", []) or [])
    if not contributors:
        return 0

    n_of_m = int(getattr(mode_config, "n_of_m", 0) or 0)
    target_count = n_of_m if n_of_m > 0 else len(contributors)
    return max(1, math.ceil(target_count * MIN_CONTRIBUTOR_RATIO))


def _check_mode_critical_roles(
    *,
    config,
    available_models: list[str],
) -> dict[str, dict[str, bool | list[str]]]:
    available_set = set(available_models)
    mode_availability: dict[str, dict[str, bool | list[str]]] = {}

    for mode_name, mode_config in getattr(config, "modes", {}).items():
        missing_roles: list[str] = []

        judge_model = getattr(mode_config, "judge", "")
        if judge_model and not getattr(mode_config, "skip_judge", False) and judge_model not in available_set:
            missing_roles.append(f"judge: {judge_model}")

        extractor_model = getattr(mode_config, "extractor", "")
        if extractor_model and extractor_model not in available_set:
            missing_roles.append(f"extractor: {extractor_model}")

        contributors = list(getattr(mode_config, "contributors", []) or [])
        available_contributors = [model_id for model_id in contributors if model_id in available_set]
        minimum_required = _minimum_required_contributors(mode_config)
        if len(available_contributors) < minimum_required:
            missing_roles.append(
                f"contributors: need >= {minimum_required}, have {len(available_contributors)}"
            )

        mode_availability[mode_name] = {
            "available": not missing_roles,
            "missing_roles": missing_roles,
        }

    return mode_availability


@router.get("/health")
async def health(request: Request):
    state = get_app_state(request)
    available = state.model_adapter.available_models
    total_models = len(state.config.models)
    available_count = len(available)
    startup_skipped_models = getattr(state.model_adapter, "startup_skipped_models", [])
    mode_availability = _check_mode_critical_roles(
        config=state.config,
        available_models=available,
    )
    critical_modes_degraded = [
        mode_name
        for mode_name, details in mode_availability.items()
        if not bool(details["available"])
    ]

    # Session store health
    session_db_ok = False
    if state.session_store:
        try:
            session_db_ok = await state.session_store.health_check()
        except Exception:
            session_db_ok = False
    conversation_store_ok = getattr(state, "conversation_store", None) is not None

    # Overall status: degraded if some models down, unhealthy if critical
    status = _overall_status(
        available_count=available_count,
        total_models=total_models,
        session_db_ok=session_db_ok,
        conversation_store_ok=conversation_store_ok,
        critical_modes_degraded=critical_modes_degraded,
    )
    frontend_linked_validation_blockers = _frontend_linked_validation_blockers(
        available_count=available_count,
        session_db_ok=session_db_ok,
        conversation_store_ok=conversation_store_ok,
    )
    ready_for_frontend_linked_validation = not frontend_linked_validation_blockers
    health_payload = {
        "status": status,
        "status_meaning": _status_meaning(status),
        "mode_availability": mode_availability,
        "critical_modes_degraded": critical_modes_degraded,
        "ready_for_frontend_linked_validation": ready_for_frontend_linked_validation,
        "frontend_linked_validation_blockers": frontend_linked_validation_blockers,
    }

    # SEC-015: minimize info exposure in production.
    # Keep localhost probes verbose so ops can verify deployment fingerprint.
    # SEC-015: Use CF-Connecting-IP to detect truly local probes.
    # Behind reverse proxy, request.client.host is always 127.0.0.1.
    cf_ip = request.headers.get("CF-Connecting-IP", "").strip()
    client_host = cf_ip if cf_ip else (request.client.host if request.client else "").strip()
    is_local_probe = not cf_ip and client_host in {"127.0.0.1", "::1", "localhost"}
    if _app()._is_production() and not is_local_probe:
        return health_payload
    # BUG-1 fix: runtime fingerprint for deployment drift detection
    git_hash = "unknown"
    git_roots = [str(PROJECT_ROOT), os.getenv("SYNTHORA_APP_DIR", ""), "/opt/synthora", os.getcwd()]
    seen_roots: set[str] = set()
    for root in git_roots:
        if not root:
            continue
        if root in seen_roots:
            continue
        seen_roots.add(root)
        git_dir = os.path.join(root, ".git")
        if not os.path.isdir(git_dir):
            continue

        # Avoid git safe.directory ownership issues under systemd users:
        # read HEAD/ref files directly first.
        try:
            head_path = os.path.join(git_dir, "HEAD")
            if os.path.isfile(head_path):
                with open(head_path, "r", encoding="utf-8") as f:
                    head = f.read().strip()
                if head.startswith("ref: "):
                    ref = head.split(" ", 1)[1].strip()
                    ref_path = os.path.join(git_dir, ref)
                    if os.path.isfile(ref_path):
                        with open(ref_path, "r", encoding="utf-8") as f:
                            value = f.read().strip()
                        if value:
                            git_hash = value[:7]
                            break
                    packed_refs = os.path.join(git_dir, "packed-refs")
                    if os.path.isfile(packed_refs):
                        with open(packed_refs, "r", encoding="utf-8", errors="ignore") as f:
                            for line in f:
                                line = line.strip()
                                if not line or line.startswith("#") or line.startswith("^"):
                                    continue
                                parts = line.split(" ", 1)
                                if len(parts) == 2 and parts[1] == ref and parts[0]:
                                    git_hash = parts[0][:7]
                                    break
                        if git_hash != "unknown":
                            break
                elif head:
                    git_hash = head[:7]
                    break
        except Exception:
            pass

        for git_bin in ("/usr/bin/git", "git"):
            try:
                value = subprocess.check_output(
                    [git_bin, "rev-parse", "--short", "HEAD"],
                    stderr=subprocess.DEVNULL,
                    cwd=root,
                ).decode().strip()
                if value:
                    git_hash = value
                    break
            except Exception:
                continue
        if git_hash != "unknown":
            break
    socratic_n_of_m = getattr(getattr(state.config, "socratic", None), "n_of_m", None)
    if socratic_n_of_m is None:
        mode_cfg = state.config.modes.get("socratic") if getattr(state.config, "modes", None) else None
        socratic_n_of_m = getattr(mode_cfg, "n_of_m", None) if mode_cfg else None
        if socratic_n_of_m is None and mode_cfg:
            socratic_n_of_m = len(getattr(mode_cfg, "contributors", []) or [])
    return {
        **health_payload,
        "version": _app()._get_version(),
        "models_available": available_count,
        "models_total": total_models,
        "session_store": "ok" if session_db_ok else "unavailable",
        "conversation_store": _conversation_store_status(state),
        "startup_skipped_models": startup_skipped_models,
        "git_hash": git_hash,
        "socratic_n_of_m": socratic_n_of_m,
    }
