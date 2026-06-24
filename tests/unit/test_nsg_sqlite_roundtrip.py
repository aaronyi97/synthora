"""
SQLite conversation store round-trip tests for v5.0 answer_outline.

Verifies:
  1. answer_outline survives write → read round-trip
  2. Auto-migration adds column to existing DBs
  3. Turns without answer_outline default to empty string
  4. Three-tier formatting uses outline from SQLite-loaded turns
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from agoracle.domain.types import Turn
from agoracle.adapters.session.sqlite_conversation_store import SQLiteConversationStore


@pytest.fixture
def tmp_db(tmp_path):
    return tmp_path / "test_conv.db"


@pytest.mark.asyncio
async def test_answer_outline_roundtrip(tmp_db):
    """Write a Turn with answer_outline, read it back, verify it survives."""
    store = SQLiteConversationStore(tmp_db)
    await store.initialize()

    turn = Turn(
        question="量子计算的应用",
        final_answer_summary="量子计算广泛应用于密码学、药物发现...",
        key_insights=["密码学", "药物发现", "优化问题"],
        mode="deep",
        answer_outline="密码学 | 药物发现 | 优化问题",
    )
    await store.append_turn("sess-1", turn, user_id=1)

    turns = await store.get_session_turns("sess-1", user_id=1)
    assert len(turns) == 1
    t = turns[0]
    assert t.question == "量子计算的应用"
    assert t.answer_outline == "密码学 | 药物发现 | 优化问题"
    assert t.final_answer_summary.startswith("量子计算")
    assert t.key_insights == ["密码学", "药物发现", "优化问题"]
    assert t.mode == "deep"

    await store.close()


@pytest.mark.asyncio
async def test_empty_outline_defaults(tmp_db):
    """Turn without answer_outline → empty string after round-trip."""
    store = SQLiteConversationStore(tmp_db)
    await store.initialize()

    turn = Turn(question="简单问题", mode="light")
    await store.append_turn("sess-2", turn, user_id=1)

    turns = await store.get_session_turns("sess-2", user_id=1)
    assert len(turns) == 1
    assert turns[0].answer_outline == ""

    await store.close()


@pytest.mark.asyncio
async def test_migration_on_existing_db(tmp_db):
    """Existing DB without answer_outline column → migration adds it."""
    import aiosqlite

    # Create a DB with the OLD schema (no answer_outline)
    db = await aiosqlite.connect(str(tmp_db))
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS conversation_turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            user_id INTEGER NOT NULL DEFAULT 0,
            turn_id TEXT NOT NULL,
            question TEXT NOT NULL,
            answer_summary TEXT NOT NULL DEFAULT '',
            key_insights TEXT NOT NULL DEFAULT '[]',
            mode TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
    """)
    # Insert a turn WITHOUT answer_outline column
    await db.execute(
        "INSERT INTO conversation_turns (session_id, user_id, turn_id, question, answer_summary, key_insights, mode, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("old-sess", 1, "t1", "旧问题", "旧回答", '["旧要点"]', "light", "2025-01-01T00:00:00"),
    )
    await db.commit()
    await db.close()

    # Now open with the new store — should auto-migrate
    store = SQLiteConversationStore(tmp_db)
    await store.initialize()

    # Old turn should be readable with empty answer_outline
    turns = await store.get_session_turns("old-sess", user_id=1)
    assert len(turns) == 1
    assert turns[0].question == "旧问题"
    assert turns[0].answer_outline == ""

    # New turn with outline should work
    new_turn = Turn(
        question="新问题",
        answer_outline="new-outline",
        mode="deep",
    )
    await store.append_turn("old-sess", new_turn, user_id=1)

    turns = await store.get_session_turns("old-sess", user_id=1)
    assert len(turns) == 2
    assert turns[1].answer_outline == "new-outline"

    await store.close()


@pytest.mark.asyncio
async def test_three_tier_with_sqlite_turns(tmp_db):
    """End-to-end: write turns with outlines → read → three-tier format uses them."""
    from agoracle.services.conversation_memory import ConversationMemoryService

    store = SQLiteConversationStore(tmp_db)
    await store.initialize()

    # Write 6 turns: 2 old, 2 mid, 2 recent
    for i in range(6):
        outline = f"outline-{i}a | outline-{i}b" if i in (2, 3) else ""
        turn = Turn(
            question=f"Q{i}",
            final_answer_summary=f"A{i}-{'x' * 200}",
            key_insights=[f"k{i}a", f"k{i}b"],
            mode="deep",
            answer_outline=outline,
        )
        await store.append_turn("sess-tier", turn, user_id=1)

    turns = await store.get_session_turns("sess-tier", user_id=1)
    assert len(turns) == 6

    formatted = ConversationMemoryService._format_session_turns(turns)

    # Tier 2 (turns 2,3 = age 3,2) should use outline
    assert "outline-2a | outline-2b" in formatted
    assert "outline-3a | outline-3b" in formatted

    # Tier 1 (turns 4,5 = age 1,0) should have full answer
    assert "A4-" in formatted
    assert "A5-" in formatted

    await store.close()
