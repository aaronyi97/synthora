"""Unit tests for null memory adapters."""

from __future__ import annotations

import pytest

from agoracle.adapters.memory.null_memory import NullMemoryStore, NullSystemMemory
from agoracle.domain.types import KnowledgeEntry


class TestNullMemoryStore:
    """Tests for the no-op memory store."""

    @pytest.mark.asyncio
    async def test_search_returns_empty(self):
        """Search always returns empty list."""
        store = NullMemoryStore()
        results = await store.search("anything")
        assert results == []

    @pytest.mark.asyncio
    async def test_store_does_not_raise(self):
        """Store accepts entries without error."""
        store = NullMemoryStore()
        entries = [KnowledgeEntry(insight="test")]
        await store.store(entries)  # should not raise

    @pytest.mark.asyncio
    async def test_get_by_tags_returns_empty(self):
        """Tag search always returns empty."""
        store = NullMemoryStore()
        results = await store.get_by_tags(["python", "testing"])
        assert results == []


class TestNullSystemMemory:
    """Tests for the no-op system memory."""

    @pytest.mark.asyncio
    async def test_record_performance_does_not_raise(self):
        """Recording performance is a no-op."""
        mem = NullSystemMemory()
        await mem.record_performance("gpt52", "python", 0.9, True)

    @pytest.mark.asyncio
    async def test_get_model_ranking_returns_empty(self):
        """Rankings always empty."""
        mem = NullSystemMemory()
        assert await mem.get_model_ranking("python") == []

    @pytest.mark.asyncio
    async def test_get_mode_stats_returns_empty(self):
        """Mode stats always empty."""
        mem = NullSystemMemory()
        assert await mem.get_mode_stats() == []
