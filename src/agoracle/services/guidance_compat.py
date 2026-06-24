"""
Guidance Compatibility Layer — one-way adapters from canonical guidance.

IRON RULE: `GuidanceOutput` is the SINGLE source of truth.
`next_steps` and `companion_guide` are legacy compatibility fields derived from
it, never independently generated.

There is no NSG branch anymore. `derive_next_steps()` intentionally keeps
returning `None` so older API/frontend contracts remain stable while the
canonical guidance protocol is fully Dispatcher-driven.

Usage in orchestrator.py:
    from agoracle.services.guidance_compat import derive_legacy_fields
    result.guidance = guidance_output
    result.next_steps, result.companion_guide = derive_legacy_fields(guidance_output)
"""
from __future__ import annotations

from typing import Any

from agoracle.domain.types import (
    GuidanceOutput,
    GuidanceIntensity,
    NextStepGuidance,
)

# Protocol version — bumped when GuidanceOutput schema changes.
PROTOCOL_VERSION = "2026-03-04"

CAPABILITIES = [
    "guidance_v1",       # Phase 0: canonical guidance protocol
    "roundtable",        # Phase 2: WP10-B activated
    # "preference_injection",  # Phase 3
    # "supplement_restart",    # Phase 3
]


def derive_next_steps(guidance: GuidanceOutput) -> NextStepGuidance | None:
    """Return retired `next_steps` compatibility field.

    The field is kept in API/domain contracts for backward compatibility with
    older clients, but the NSG producer is gone, so this adapter always returns
    `None`.
    """
    return None


def derive_companion_guide(guidance: GuidanceOutput) -> dict[str, Any] | None:
    """Derive legacy `companion_guide` from canonical `guidance`.

    Returns None when guidance source is "none".
    Only returns dict when source is "dispatcher".
    """
    if guidance.source != "dispatcher":
        return None
    actions = []
    for s in guidance.suggestions:
        actions.append({
            "label": s.label,
            "action_type": s.action_type,
            "action_payload": s.action_payload,
            "estimated_seconds": s.estimated_seconds,
            "requires_confirm": s.requires_confirm,
            "id": s.id,
            "rationale": s.rationale,
            "estimated_cost_usd": s.estimated_cost_usd,
        })
    return {
        "message": guidance.message,
        "actions": actions,
        "trigger": guidance.trigger,
        "is_silent": guidance.intensity == GuidanceIntensity.NONE.value and not guidance.message,
        "route_reason": guidance.route_reason,
    }


def derive_legacy_fields(
    guidance: GuidanceOutput,
) -> tuple[NextStepGuidance | None, dict[str, Any] | None]:
    """Derive both legacy compatibility fields from canonical guidance."""
    return derive_next_steps(guidance), derive_companion_guide(guidance)


def guidance_to_dict(guidance: GuidanceOutput) -> dict[str, Any]:
    """Serialize GuidanceOutput for JSON response / SSE event payload."""
    suggestions_list = []
    for s in guidance.suggestions:
        suggestions_list.append({
            "id": s.id,
            "label": s.label,
            "action_type": s.action_type,
            "action_payload": s.action_payload,
            "rationale": s.rationale,
            "estimated_seconds": s.estimated_seconds,
            "estimated_cost_usd": s.estimated_cost_usd,
            "requires_confirm": s.requires_confirm,
        })
    return {
        "source": guidance.source,
        "confidence_statement": guidance.confidence_statement,
        "confidence_level": guidance.confidence_level,
        "message": guidance.message,
        "suggestions": suggestions_list,
        "intensity": guidance.intensity,
        "is_folded": guidance.is_folded,
        "show_dismiss": guidance.show_dismiss,
        "route_reason": guidance.route_reason,
        "trigger": guidance.trigger,
    }
