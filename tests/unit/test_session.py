"""Tests for JSON session storage."""

import pytest
import tempfile
from pathlib import Path

from agoracle.adapters.session.json_session import JsonSessionStore
from agoracle.domain.types import Turn


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield JsonSessionStore(tmpdir)


class TestJsonSessionStore:

    @pytest.mark.asyncio
    async def test_create_new_session(self, store):
        session = await store.get_or_create("test-session-1")
        assert session.session_id == "test-session-1"
        assert len(session.turns) == 0

    @pytest.mark.asyncio
    async def test_add_and_retrieve_turn(self, store):
        turn = Turn(
            question="什么是量子计算",
            final_answer_summary="量子计算利用量子力学原理...",
            key_insights=["量子比特可以同时表示0和1"],
            mode="light",
        )
        await store.add_turn("s1", turn)
        turns = await store.get_recent_turns("s1")
        assert len(turns) == 1
        assert turns[0].question == "什么是量子计算"

    @pytest.mark.asyncio
    async def test_persistence_across_reads(self, store):
        turn = Turn(question="test question", mode="deep")
        await store.add_turn("s1", turn)

        # Read again from disk
        session = await store.get_or_create("s1")
        assert len(session.turns) == 1
        assert session.turns[0].question == "test question"

    @pytest.mark.asyncio
    async def test_recent_turns_limit(self, store):
        for i in range(10):
            await store.add_turn("s1", Turn(question=f"q{i}", mode="light"))

        turns = await store.get_recent_turns("s1", limit=3)
        assert len(turns) == 3
        assert turns[0].question == "q7"  # last 3 of 10

    @pytest.mark.asyncio
    async def test_list_sessions(self, store):
        await store.get_or_create("s1")
        await store.get_or_create("s2")
        sessions = await store.list_sessions()
        assert len(sessions) == 2

    @pytest.mark.asyncio
    async def test_path_traversal_safety(self, store):
        """Ensure session_id with path traversal chars is sanitized."""
        session = await store.get_or_create("../../etc/passwd")
        assert "/" not in session.session_id
        assert "\\" not in session.session_id
