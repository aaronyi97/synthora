"""
System Memory Port — interface for system self-evolution data.

Tracks model performance per topic, mode effectiveness, routing optimization.
Phase 0: interface only.
Phase 2: minimal implementation for quality monitoring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass
class ModelPerformance:
    """Performance record for one model on one topic."""
    model_id: str = ""
    topic: str = ""
    avg_judge_score: float = 0.0
    adoption_rate: float = 0.0
    sample_count: int = 0
    last_updated: datetime = field(default_factory=datetime.now)


@dataclass
class ModeEffectiveness:
    """Effectiveness record for one mode."""
    mode: str = ""
    avg_confidence: float = 0.0
    avg_latency_ms: int = 0
    user_satisfaction: float | None = None
    sample_count: int = 0


class SystemMemoryPort(Protocol):
    """Port for system-level performance memory."""

    async def record_performance(
        self, model_id: str, topic: str, judge_score: float, adopted: bool
    ) -> None:
        """Record a model's performance on a query."""
        ...

    async def get_model_ranking(self, topic: str) -> list[ModelPerformance]:
        """Get model performance rankings for a topic."""
        ...

    async def get_mode_stats(self) -> list[ModeEffectiveness]:
        """Get effectiveness statistics for all modes."""
        ...
