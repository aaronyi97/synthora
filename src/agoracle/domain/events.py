"""
Domain events — emitted by the pipeline, consumed by subscribers.

Design: Pipeline core emits events; side effects (knowledge extraction,
profile update, sync, etc.) are independent subscribers.
Adding a new feature = adding a new subscriber, not modifying the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class QueryCompleted:
    """
    Core event — emitted after every successful query.

    All subscribers receive the same event. Each subscriber extracts
    what it needs and ignores the rest.
    """
    query_id: str = ""
    question: str = ""
    mode: str = ""
    resolved_mode: str = ""
    session_id: str = ""

    # Answer
    final_answer: str = ""

    # Metadata (from parallel Extractor)
    key_insights: list[str] = field(default_factory=list)
    topic_tags: list[str] = field(default_factory=list)
    confidence: float = 0.0
    consensus_type: str = "unknown"
    has_divergence: bool = False
    divergence_summary: str | None = None

    # Internal
    model_evaluations: dict[str, Any] = field(default_factory=dict)
    quality_gate_result: str = "synthesized"
    question_critique_summary: str | None = None
    contributor_count: int = 0
    total_model_calls: int = 0

    # v2.7: User identity for per-user profile isolation
    user_id: int = 0
    language: str = "zh-CN"

    # Timing
    latency_ms: int = 0
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class FeedbackReceived:
    """Emitted when user provides feedback on a query."""
    query_id: str = ""
    rating: str = ""          # useful / inaccurate / too_shallow / too_slow
    comment: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class SessionCreated:
    """Emitted when a new session starts."""
    session_id: str = ""
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class ModelCallFailed:
    """Emitted when a model API call fails (for monitoring)."""
    model_id: str = ""
    role: str = ""
    error: str = ""
    latency_ms: int = 0
    timestamp: datetime = field(default_factory=datetime.now)


# ============================================================
# v2.0 Events — Phase 3+ implementation
# ============================================================

@dataclass
class SocraticSessionCompleted:
    """
    Emitted after a Socratic session ends (v2.0 — Phase 3).

    Subscribers: CognitiveProfile updater, System Memory, Knowledge extractor.
    """
    query_id: str = ""
    question: str = ""
    session_id: str = ""
    guide_rounds_used: int = 0
    user_conclusion: str = ""
    model_consensus: str = ""
    reasoning_quality_score: float = 0.0  # 0-1
    completed_naturally: bool = True      # True=user completed, False=revealed early
    divergence_points_count: int = 0
    timestamp: datetime = field(default_factory=datetime.now)
