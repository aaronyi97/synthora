"""
Memory Port — interface for long-term knowledge storage and retrieval (RAG).

Phase 0: interface defined, implementation empty (returns []).
Phase 4: ChromaDB adapter implements this.
"""

from __future__ import annotations

from typing import Protocol

from agoracle.domain.types import KnowledgeEntry


class SearchFilters:
    """Filters for knowledge search."""

    def __init__(
        self,
        categories: list[str] | None = None,
        min_confidence: float = 0.5,
        exclude_outdated: bool = True,
        max_age_days: int | None = None,
    ):
        self.categories = categories
        self.min_confidence = min_confidence
        self.exclude_outdated = exclude_outdated
        self.max_age_days = max_age_days


class MemoryPort(Protocol):
    """Port for RAG knowledge store."""

    async def store(self, entries: list[KnowledgeEntry]) -> None:
        """Store knowledge entries."""
        ...

    async def search(
        self,
        query: str,
        mode: str = "light",
        filters: SearchFilters | None = None,
    ) -> list[KnowledgeEntry]:
        """Semantic search for relevant knowledge."""
        ...

    async def mark_outdated(
        self, entry_id: str, superseded_by: str | None = None
    ) -> None:
        """Mark a knowledge entry as outdated."""
        ...

    async def delete(self, entry_id: str) -> None:
        """Delete a knowledge entry."""
        ...

    async def get_by_tags(
        self, tags: list[str], limit: int = 10
    ) -> list[KnowledgeEntry]:
        """Retrieve entries by topic tags (for associative retrieval)."""
        ...
