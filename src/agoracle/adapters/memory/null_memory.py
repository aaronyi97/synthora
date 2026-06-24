"""
Null memory adapter — returns empty results for all queries.

Phase 0/1 placeholder: satisfies the MemoryPort interface
so the pipeline can run without ChromaDB dependency.

Phase 4: Replace with ChromaDB adapter.
"""

from __future__ import annotations

import logging

from agoracle.domain.types import KnowledgeEntry

logger = logging.getLogger(__name__)


class NullMemoryStore:
    """
    No-op memory store — always returns empty results.

    Satisfies MemoryPort contract so pipeline doesn't need
    conditional logic for "is memory available?"
    """

    async def store(self, entries: list[KnowledgeEntry]) -> None:
        """Store knowledge entries (no-op)."""
        logger.debug(f"NullMemory: discarding {len(entries)} entries")

    async def search(
        self,
        query: str,
        mode: str = "light",
        filters: object = None,
    ) -> list[KnowledgeEntry]:
        """Semantic search (always returns empty)."""
        return []

    async def mark_outdated(
        self, entry_id: str, superseded_by: str | None = None
    ) -> None:
        """Mark entry as outdated (no-op)."""
        pass

    async def delete(self, entry_id: str) -> None:
        """Delete entry (no-op)."""
        pass

    async def get_by_tags(
        self, tags: list[str], limit: int = 10
    ) -> list[KnowledgeEntry]:
        """Get entries by tags (always returns empty)."""
        return []


class NullSystemMemory:
    """
    No-op system memory — discards all performance data.

    Phase 2: Replace with real implementation for quality monitoring.
    """

    async def record_performance(
        self, model_id: str, topic: str, judge_score: float, adopted: bool
    ) -> None:
        """Record model performance (no-op)."""
        pass

    async def get_model_ranking(self, topic: str) -> list:
        """Get model rankings (always empty)."""
        return []

    async def get_mode_stats(self) -> list:
        """Get mode statistics (always empty)."""
        return []
