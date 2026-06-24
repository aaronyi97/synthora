"""
SocraticSession serializer — convert between dataclass and JSON-safe dict.

Handles the full nested dataclass graph:
  SocraticSession
    ├── DivergenceMap
    │     └── DivergencePoint[]
    ├── SocraticTurn[]
    ├── CognitiveSnapshot
    └── datetime fields

Design:
  - to_dict / from_dict are the public API
  - All datetime fields stored as ISO 8601 strings
  - Schema version field for future migrations
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from agoracle.domain.types import (
    CognitiveSnapshot,
    DivergenceMap,
    DivergencePoint,
    SocraticSession,
    SocraticTurn,
)

SCHEMA_VERSION = 1


def session_to_dict(session: SocraticSession) -> dict[str, Any]:
    """Serialize a SocraticSession to a JSON-safe dictionary."""
    return {
        "_schema_version": SCHEMA_VERSION,
        "session_id": session.session_id,
        "question": session.question,
        "language": session.language,
        # Phase 1 output
        "divergence_map": _divergence_map_to_dict(session.divergence_map),
        "full_answer": session.full_answer,
        "contributor_responses": session.contributor_responses,
        # Phase 2 dialogue
        "turns": [_turn_to_dict(t) for t in session.turns],
        "current_divergence_index": session.current_divergence_index,
        "max_guide_rounds": session.max_guide_rounds,
        # Outcome
        "guide_rounds_used": session.guide_rounds_used,
        "user_conclusion": session.user_conclusion,
        "completed_naturally": session.completed_naturally,
        "revealed": session.revealed,
        # Cognitive analysis
        "cognitive_snapshot": _snapshot_to_dict(session.cognitive_snapshot),
        "reasoning_quality_score": session.reasoning_quality_score,
        # Timing
        "phase1_latency_ms": session.phase1_latency_ms,
        "total_dialogue_ms": session.total_dialogue_ms,
        "created_at": session.created_at.isoformat(),
    }


def session_from_dict(data: dict[str, Any]) -> SocraticSession:
    """Deserialize a SocraticSession from a dictionary."""
    # Future: check data.get("_schema_version") for migrations
    return SocraticSession(
        session_id=data["session_id"],
        question=data.get("question", ""),
        language=data.get("language", "zh-CN"),
        # Phase 1 output
        divergence_map=_divergence_map_from_dict(data.get("divergence_map")),
        full_answer=data.get("full_answer", ""),
        contributor_responses=data.get("contributor_responses", []),
        # Phase 2 dialogue
        turns=[_turn_from_dict(t) for t in data.get("turns", [])],
        current_divergence_index=data.get("current_divergence_index", 0),
        max_guide_rounds=data.get("max_guide_rounds", 5),
        # Outcome
        guide_rounds_used=data.get("guide_rounds_used", 0),
        user_conclusion=data.get("user_conclusion", ""),
        completed_naturally=data.get("completed_naturally", True),
        revealed=data.get("revealed", False),
        # Cognitive analysis
        cognitive_snapshot=_snapshot_from_dict(data.get("cognitive_snapshot")),
        reasoning_quality_score=data.get("reasoning_quality_score", 0.0),
        # Timing
        phase1_latency_ms=data.get("phase1_latency_ms", 0),
        total_dialogue_ms=data.get("total_dialogue_ms", 0),
        created_at=_parse_datetime(data.get("created_at")),
    )


# ── DivergenceMap ────────────────────────────────────────

def _divergence_map_to_dict(dm: DivergenceMap | None) -> dict[str, Any] | None:
    if dm is None:
        return None
    return {
        "consensus_points": dm.consensus_points,
        "divergence_points": [_divergence_point_to_dict(dp) for dp in dm.divergence_points],
        "overall_consensus_score": dm.overall_consensus_score,
        "model_count": dm.model_count,
        "analysis_latency_ms": dm.analysis_latency_ms,
    }


def _divergence_map_from_dict(data: dict[str, Any] | None) -> DivergenceMap | None:
    if data is None:
        return None
    return DivergenceMap(
        consensus_points=data.get("consensus_points", []),
        divergence_points=[_divergence_point_from_dict(dp) for dp in data.get("divergence_points", [])],
        overall_consensus_score=data.get("overall_consensus_score", 0.0),
        model_count=data.get("model_count", 0),
        analysis_latency_ms=data.get("analysis_latency_ms", 0),
    )


# ── DivergencePoint ─────────────────────────────────────

def _divergence_point_to_dict(dp: DivergencePoint) -> dict[str, Any]:
    return {
        "point_id": dp.point_id,
        "topic": dp.topic,
        "description": dp.description,
        "positions": dp.positions,
        "consensus_ratio": dp.consensus_ratio,
        "difficulty": dp.difficulty,
    }


def _divergence_point_from_dict(data: dict[str, Any]) -> DivergencePoint:
    return DivergencePoint(
        point_id=data.get("point_id", ""),
        topic=data.get("topic", ""),
        description=data.get("description", ""),
        positions=data.get("positions", []),
        consensus_ratio=data.get("consensus_ratio", 0.0),
        difficulty=data.get("difficulty", "medium"),
    )


# ── SocraticTurn ─────────────────────────────────────────

def _turn_to_dict(turn: SocraticTurn) -> dict[str, Any]:
    return {
        "turn_id": turn.turn_id,
        "role": turn.role,
        "content": turn.content,
        "divergence_point_id": turn.divergence_point_id,
        "user_stance": turn.user_stance,
        "latency_ms": turn.latency_ms,
        "timestamp": turn.timestamp.isoformat(),
    }


def _turn_from_dict(data: dict[str, Any]) -> SocraticTurn:
    return SocraticTurn(
        turn_id=data.get("turn_id", ""),
        role=data.get("role", ""),
        content=data.get("content", ""),
        divergence_point_id=data.get("divergence_point_id"),
        user_stance=data.get("user_stance"),
        latency_ms=data.get("latency_ms", 0),
        timestamp=_parse_datetime(data.get("timestamp")),
    )


# ── CognitiveSnapshot ───────────────────────────────────

def _snapshot_to_dict(snap: CognitiveSnapshot | None) -> dict[str, Any] | None:
    if snap is None:
        return None
    return {
        "anchoring_detected": snap.anchoring_detected,
        "confirmation_bias": snap.confirmation_bias,
        "nuance_recognition": snap.nuance_recognition,
        "position_change_count": snap.position_change_count,
        "reasoning_depth": snap.reasoning_depth,
        "blind_spots": snap.blind_spots,
    }


def _snapshot_from_dict(data: dict[str, Any] | None) -> CognitiveSnapshot | None:
    if data is None:
        return None
    return CognitiveSnapshot(
        anchoring_detected=data.get("anchoring_detected", False),
        confirmation_bias=data.get("confirmation_bias", False),
        nuance_recognition=data.get("nuance_recognition", 0.0),
        position_change_count=data.get("position_change_count", 0),
        reasoning_depth=data.get("reasoning_depth", 0.0),
        blind_spots=data.get("blind_spots", []),
    )


# ── Helpers ──────────────────────────────────────────────

def _parse_datetime(val: str | None) -> datetime:
    """Parse ISO 8601 datetime string, fallback to now()."""
    if val:
        try:
            return datetime.fromisoformat(val)
        except (ValueError, TypeError):
            pass
    return datetime.now()
