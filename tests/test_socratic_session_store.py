"""
Tests for Socratic session persistence: serializer + SQLite store.

Covers:
  - Full round-trip serialization (all nested dataclasses)
  - SQLite CRUD operations
  - TTL expiry / cleanup
  - Session cap enforcement
  - Concurrent access safety
  - Health check
  - Schema migration
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from agoracle.adapters.session.serializer import (
    session_from_dict,
    session_to_dict,
)
from agoracle.adapters.session.sqlite_socratic_store import (
    SQLiteSocraticSessionStore,
)
from agoracle.domain.types import (
    CognitiveSnapshot,
    DivergenceMap,
    DivergencePoint,
    SocraticSession,
    SocraticTurn,
)


# ── Fixtures ─────────────────────────────────────────────

def _make_session(
    session_id: str = "test123",
    question: str = "AI能否产生真正的创意？",
    with_divergence: bool = True,
    with_turns: bool = True,
    with_snapshot: bool = False,
    created_at: datetime | None = None,
) -> SocraticSession:
    """Build a realistic SocraticSession for testing."""
    session = SocraticSession(
        session_id=session_id,
        question=question,
        full_answer="这是一个综合性的回答...",
        contributor_responses=[
            {"model_id": "gpt5", "content": "GPT5 says..."},
            {"model_id": "claude", "content": "Claude says..."},
        ],
        max_guide_rounds=5,
        phase1_latency_ms=3200,
    )

    if created_at:
        session.created_at = created_at

    if with_divergence:
        session.divergence_map = DivergenceMap(
            consensus_points=["AI可以模式匹配", "训练数据限制"],
            divergence_points=[
                DivergencePoint(
                    point_id="dp001",
                    topic="创意的本质",
                    description="模型对创意是否需要意识存在分歧",
                    positions=[
                        {"stance": "支持", "summary": "组合创新也是创意", "models": ["gpt5"]},
                        {"stance": "反对", "summary": "真正创意需要意识", "models": ["claude"]},
                    ],
                    consensus_ratio=0.3,
                    difficulty="hard",
                ),
            ],
            overall_consensus_score=0.65,
            model_count=2,
            analysis_latency_ms=1500,
        )

    if with_turns:
        session.turns = [
            SocraticTurn(
                turn_id="t001",
                role="guide",
                content="你觉得AI能否产生真正的创意？",
                latency_ms=800,
            ),
            SocraticTurn(
                turn_id="t002",
                role="user",
                content="我认为可以，组合也是创新",
                divergence_point_id="dp001",
                user_stance="支持",
            ),
        ]
        session.guide_rounds_used = 1

    if with_snapshot:
        session.cognitive_snapshot = CognitiveSnapshot(
            anchoring_detected=False,
            confirmation_bias=True,
            nuance_recognition=0.7,
            position_change_count=1,
            reasoning_depth=0.8,
            blind_spots=["忽略了情感因素"],
        )
        session.reasoning_quality_score = 0.8

    return session


@pytest_asyncio.fixture
async def store(tmp_path):
    """Create a fresh SQLite store for each test."""
    db_path = tmp_path / "test_sessions.db"
    s = SQLiteSocraticSessionStore(db_path, max_sessions=10)
    await s.initialize()
    yield s
    await s.close()


# ============================================================
# Serializer tests
# ============================================================

class TestSerializer:
    """Test session_to_dict / session_from_dict round-trip."""

    def test_basic_roundtrip(self):
        session = _make_session()
        data = session_to_dict(session)
        restored = session_from_dict(data)

        assert restored.session_id == session.session_id
        assert restored.question == session.question
        assert restored.full_answer == session.full_answer
        assert restored.phase1_latency_ms == session.phase1_latency_ms

    def test_divergence_map_roundtrip(self):
        session = _make_session(with_divergence=True)
        data = session_to_dict(session)
        restored = session_from_dict(data)

        assert restored.divergence_map is not None
        dm = restored.divergence_map
        assert len(dm.consensus_points) == 2
        assert len(dm.divergence_points) == 1
        assert dm.divergence_points[0].topic == "创意的本质"
        assert dm.overall_consensus_score == 0.65

    def test_turns_roundtrip(self):
        session = _make_session(with_turns=True)
        data = session_to_dict(session)
        restored = session_from_dict(data)

        assert len(restored.turns) == 2
        assert restored.turns[0].role == "guide"
        assert restored.turns[1].user_stance == "支持"
        assert restored.guide_rounds_used == 1

    def test_cognitive_snapshot_roundtrip(self):
        session = _make_session(with_snapshot=True)
        data = session_to_dict(session)
        restored = session_from_dict(data)

        snap = restored.cognitive_snapshot
        assert snap is not None
        assert snap.confirmation_bias is True
        assert snap.nuance_recognition == 0.7
        assert snap.blind_spots == ["忽略了情感因素"]

    def test_none_divergence_map(self):
        session = _make_session(with_divergence=False)
        data = session_to_dict(session)
        restored = session_from_dict(data)
        assert restored.divergence_map is None

    def test_none_cognitive_snapshot(self):
        session = _make_session(with_snapshot=False)
        data = session_to_dict(session)
        restored = session_from_dict(data)
        assert restored.cognitive_snapshot is None

    def test_json_serializable(self):
        session = _make_session(with_divergence=True, with_turns=True, with_snapshot=True)
        data = session_to_dict(session)
        # Must be valid JSON
        json_str = json.dumps(data, ensure_ascii=False)
        assert len(json_str) > 100
        # Must round-trip through JSON
        parsed = json.loads(json_str)
        restored = session_from_dict(parsed)
        assert restored.session_id == session.session_id

    def test_schema_version_present(self):
        session = _make_session()
        data = session_to_dict(session)
        assert data["_schema_version"] == 1

    def test_datetime_iso_format(self):
        session = _make_session()
        data = session_to_dict(session)
        # Must be valid ISO 8601
        parsed = datetime.fromisoformat(data["created_at"])
        assert isinstance(parsed, datetime)


# ============================================================
# SQLite Store tests
# ============================================================

class TestSQLiteStore:
    """Test SQLiteSocraticSessionStore CRUD + lifecycle."""

    @pytest.mark.asyncio
    async def test_save_and_get(self, store):
        session = _make_session()
        await store.save(session)

        loaded = await store.get("test123")
        assert loaded is not None
        assert loaded.session_id == "test123"
        assert loaded.question == "AI能否产生真正的创意？"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, store):
        result = await store.get("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_upsert_idempotent(self, store):
        session = _make_session()
        await store.save(session)

        # Update and save again
        session.guide_rounds_used = 3
        session.turns.append(SocraticTurn(role="guide", content="Follow up"))
        await store.save(session)

        loaded = await store.get("test123")
        assert loaded.guide_rounds_used == 3
        assert len(loaded.turns) == 3

    @pytest.mark.asyncio
    async def test_delete(self, store):
        session = _make_session()
        await store.save(session)

        deleted = await store.delete("test123")
        assert deleted is True

        loaded = await store.get("test123")
        assert loaded is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, store):
        deleted = await store.delete("nonexistent")
        assert deleted is False

    @pytest.mark.asyncio
    async def test_list_active(self, store):
        for i in range(5):
            s = _make_session(session_id=f"sess{i}", question=f"Q{i}")
            await store.save(s)

        active = await store.list_active()
        assert len(active) == 5

    @pytest.mark.asyncio
    async def test_list_active_excludes_finished(self, store):
        s1 = _make_session(session_id="active1")
        await store.save(s1)

        s2 = _make_session(session_id="finished1", with_snapshot=True)
        s2.revealed = True
        await store.save(s2)

        active = await store.list_active()
        assert len(active) == 1
        assert active[0].session_id == "active1"

    @pytest.mark.asyncio
    async def test_count_active(self, store):
        for i in range(3):
            await store.save(_make_session(session_id=f"s{i}"))
        assert await store.count_active() == 3

    @pytest.mark.asyncio
    async def test_cleanup_expired(self, store):
        # Create sessions — save() sets updated_at=now()
        old = _make_session(session_id="old1")
        await store.save(old)

        recent = _make_session(session_id="recent1")
        await store.save(recent)

        # Manually backdate updated_at to simulate a stale session
        db = store._ensure_db()
        stale_time = (datetime.now() - timedelta(hours=2)).isoformat()
        await db.execute(
            "UPDATE socratic_sessions SET updated_at = ? WHERE session_id = ?",
            (stale_time, "old1"),
        )
        await db.commit()

        removed = await store.cleanup_expired(ttl_seconds=3600)
        assert removed >= 1

        assert await store.get("old1") is None
        assert await store.get("recent1") is not None

    @pytest.mark.asyncio
    async def test_session_cap_enforcement(self, store):
        # Store max is 10, create 15 with staggered updated_at
        db = store._ensure_db()
        for i in range(15):
            s = _make_session(session_id=f"cap{i:02d}")
            await store.save(s)
            # Stagger updated_at so eviction order is deterministic
            t = (datetime.now() - timedelta(minutes=15 - i)).isoformat()
            await db.execute(
                "UPDATE socratic_sessions SET updated_at = ? WHERE session_id = ?",
                (t, f"cap{i:02d}"),
            )
        await db.commit()

        # Cleanup should evict oldest to enforce cap
        removed = await store.cleanup_expired(ttl_seconds=86400)
        assert removed >= 5

        active = await store.count_active()
        assert active <= 10

    @pytest.mark.asyncio
    async def test_health_check(self, store):
        assert await store.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_after_close(self, store):
        await store.close()
        assert await store.health_check() is False

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, store):
        """Simulate a complete Socratic session lifecycle."""
        # Phase 1: create
        session = _make_session(with_turns=True)
        await store.save(session)

        # Phase 2: multiple turns
        for i in range(3):
            loaded = await store.get("test123")
            loaded.turns.append(SocraticTurn(role="user", content=f"Response {i}"))
            loaded.guide_rounds_used += 1
            await store.save(loaded)

        # Verify mid-session state
        mid = await store.get("test123")
        assert mid.guide_rounds_used == 4
        assert len(mid.turns) == 5  # 2 original + 3 new

        # Reveal + finish
        mid.revealed = True
        mid.cognitive_snapshot = CognitiveSnapshot(reasoning_depth=0.9)
        await store.save(mid)

        # Finished session should not appear in active list
        active = await store.list_active()
        assert len(active) == 0

    @pytest.mark.asyncio
    async def test_concurrent_saves(self, store):
        """Multiple concurrent saves should not corrupt data."""
        session = _make_session()
        await store.save(session)

        async def add_turn(turn_num):
            s = await store.get("test123")
            s.turns.append(SocraticTurn(role="user", content=f"Turn {turn_num}"))
            await store.save(s)

        # 5 concurrent saves (last-write-wins, but no corruption)
        await asyncio.gather(*[add_turn(i) for i in range(5)])

        final = await store.get("test123")
        assert final is not None
        # At least the original turns should be preserved
        assert len(final.turns) >= 2

    @pytest.mark.asyncio
    async def test_chinese_content_preserved(self, store):
        """Chinese characters must survive serialization + SQLite storage."""
        session = _make_session(question="量子计算如何改变密码学？")
        session.full_answer = "量子计算通过Shor算法可以破解RSA..."
        await store.save(session)

        loaded = await store.get("test123")
        assert loaded.question == "量子计算如何改变密码学？"
        assert "Shor算法" in loaded.full_answer

    @pytest.mark.asyncio
    async def test_migration_idempotent(self, tmp_path):
        """Running initialize() twice should not break anything."""
        db_path = tmp_path / "migration_test.db"
        s1 = SQLiteSocraticSessionStore(db_path)
        await s1.initialize()

        # Save something
        await s1.save(_make_session())
        await s1.close()

        # Re-open and re-initialize (simulates restart)
        s2 = SQLiteSocraticSessionStore(db_path)
        await s2.initialize()

        loaded = await s2.get("test123")
        assert loaded is not None
        assert loaded.question == "AI能否产生真正的创意？"
        await s2.close()

    @pytest.mark.asyncio
    async def test_corrupt_json_returns_none(self, store):
        """Corrupt JSON data in DB should return None, not crash."""
        db = store._ensure_db()
        from datetime import datetime
        now = datetime.now().isoformat()
        await db.execute(
            "INSERT INTO socratic_sessions (session_id, question, data, created_at, updated_at, is_active) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("corrupt1", "test", "NOT VALID JSON {{{", now, now, 1),
        )
        await db.commit()

        result = await store.get("corrupt1")
        assert result is None

    @pytest.mark.asyncio
    async def test_uninitialized_store_raises(self, tmp_path):
        """Calling methods without initialize() must raise RuntimeError."""
        s = SQLiteSocraticSessionStore(tmp_path / "noinit.db")
        with pytest.raises(RuntimeError, match="not initialized"):
            await s.save(_make_session())

    @pytest.mark.asyncio
    async def test_corrupt_session_skipped_in_list_active(self, store):
        """Corrupt sessions in DB should be silently skipped in list_active()."""
        # Insert a valid session
        await store.save(_make_session(session_id="valid1"))

        # Insert corrupt data directly
        db = store._ensure_db()
        from datetime import datetime
        now = datetime.now().isoformat()
        await db.execute(
            "INSERT INTO socratic_sessions (session_id, question, data, created_at, updated_at, is_active) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("corrupt2", "test", '{"session_id": "corrupt2"}', now, now, 1),
        )
        await db.commit()

        active = await store.list_active()
        # valid1 should be present; corrupt2 may or may not parse depending on defaults
        assert len(active) >= 1
        valid_ids = [s.session_id for s in active]
        assert "valid1" in valid_ids
