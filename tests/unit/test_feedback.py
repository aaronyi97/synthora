"""Unit tests for JSON feedback store."""

from __future__ import annotations

import pytest

from agoracle.adapters.feedback.json_feedback import JsonFeedbackStore


class TestJsonFeedbackStore:
    """Tests for the JSONL feedback store."""

    @pytest.mark.asyncio
    async def test_record_and_retrieve(self, tmp_path):
        """Record feedback and retrieve it."""
        store = JsonFeedbackStore(tmp_path / "feedback.jsonl")
        await store.record("q1", "useful", "Great answer!")
        await store.record("q2", "inaccurate", "Wrong about X")

        entries = await store.get_all()
        assert len(entries) == 2
        assert entries[0]["query_id"] == "q1"
        assert entries[0]["rating"] == "useful"
        assert entries[1]["rating"] == "inaccurate"

    @pytest.mark.asyncio
    async def test_stats(self, tmp_path):
        """Statistics count ratings correctly."""
        store = JsonFeedbackStore(tmp_path / "feedback.jsonl")
        await store.record("q1", "useful")
        await store.record("q2", "useful")
        await store.record("q3", "inaccurate")
        await store.record("q4", "too_shallow")

        stats = await store.get_stats()
        assert stats["useful"] == 2
        assert stats["inaccurate"] == 1
        assert stats["too_shallow"] == 1

    @pytest.mark.asyncio
    async def test_empty_stats(self, tmp_path):
        """Empty store returns empty stats."""
        store = JsonFeedbackStore(tmp_path / "feedback.jsonl")
        stats = await store.get_stats()
        assert stats == {}

    @pytest.mark.asyncio
    async def test_record_without_comment(self, tmp_path):
        """Recording without a comment works."""
        store = JsonFeedbackStore(tmp_path / "feedback.jsonl")
        await store.record("q1", "useful")

        entries = await store.get_all()
        assert entries[0]["comment"] is None
