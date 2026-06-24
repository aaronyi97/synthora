"""
SQLite Roundtable Session Store — persistent metadata for roundtable sessions.

Tracks session lifecycle (creation, state transitions, ownership) in SQLite.
Active runtime state (asyncio queues, locks) remains in-memory in the orchestrator.

Features:
  - WAL mode for concurrent read/write performance
  - busy_timeout for multi-worker safety
  - Session cap enforcement (evict oldest when over limit)
  - TTL-based expiry with explicit cleanup
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS roundtable_sessions (
    session_id TEXT PRIMARY KEY,
    question TEXT NOT NULL DEFAULT '',
    owner_user_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'initializing',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_rt_sessions_created_at
    ON roundtable_sessions(created_at);

CREATE INDEX IF NOT EXISTS idx_rt_sessions_owner
    ON roundtable_sessions(owner_user_id);
"""


class SQLiteRoundtableStore:
    """SQLite-backed roundtable session metadata store.

    Usage:
        store = SQLiteRoundtableStore("data/roundtable_sessions.db")
        await store.initialize()
        await store.register(session_id, question, owner_user_id)
        await store.update_state(session_id, "completed")
        await store.close()
    """

    def __init__(self, db_path: str | Path, max_sessions: int = 100) -> None:
        self._db_path = Path(db_path)
        self._max_sessions = max_sessions
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.executescript(_CREATE_TABLE)
        logger.info(f"RoundtableStore initialized: {self._db_path}")

    def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("RoundtableStore not initialized — call initialize() first")
        return self._db

    async def register(
        self,
        session_id: str,
        question: str,
        owner_user_id: str,
        metadata: dict | None = None,
    ) -> None:
        """Register a new roundtable session."""
        db = self._ensure_db()
        now = datetime.now().isoformat()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        await db.execute(
            """
            INSERT INTO roundtable_sessions
                (session_id, question, owner_user_id, state, created_at, updated_at, metadata)
            VALUES (?, ?, ?, 'initializing', ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (session_id, question, owner_user_id, now, now, meta_json),
        )
        await db.commit()

    async def update_state(self, session_id: str, state: str) -> None:
        """Update the state of a session."""
        db = self._ensure_db()
        now = datetime.now().isoformat()
        await db.execute(
            "UPDATE roundtable_sessions SET state = ?, updated_at = ? WHERE session_id = ?",
            (state, now, session_id),
        )
        await db.commit()

    async def get_owner(self, session_id: str) -> str | None:
        """Get session owner. Returns None if session not found."""
        db = self._ensure_db()
        cursor = await db.execute(
            "SELECT owner_user_id FROM roundtable_sessions WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def get_state(self, session_id: str) -> str | None:
        """Get session state. Returns None if session not found."""
        db = self._ensure_db()
        cursor = await db.execute(
            "SELECT state FROM roundtable_sessions WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def cleanup_expired(self, ttl_seconds: int = 7200) -> int:
        """Remove sessions older than TTL. Returns count removed."""
        db = self._ensure_db()
        cutoff = datetime.fromtimestamp(time.time() - ttl_seconds).isoformat()

        cursor = await db.execute(
            "DELETE FROM roundtable_sessions WHERE updated_at < ?",
            (cutoff,),
        )
        expired = cursor.rowcount

        cursor2 = await db.execute("SELECT COUNT(*) FROM roundtable_sessions")
        row = await cursor2.fetchone()
        total = row[0] if row else 0
        evicted = 0
        if total > self._max_sessions:
            excess = total - self._max_sessions
            cursor3 = await db.execute(
                """
                DELETE FROM roundtable_sessions WHERE session_id IN (
                    SELECT session_id FROM roundtable_sessions
                    ORDER BY updated_at ASC LIMIT ?
                )
                """,
                (excess,),
            )
            evicted = cursor3.rowcount

        await db.commit()
        removed = expired + evicted
        if removed > 0:
            logger.info(f"RoundtableStore cleanup: {expired} expired, {evicted} evicted")
        return removed

    async def health_check(self) -> bool:
        if not self._db:
            return False
        try:
            cursor = await self._db.execute("SELECT 1")
            row = await cursor.fetchone()
            return row is not None and row[0] == 1
        except Exception:
            return False

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None
            logger.info("RoundtableStore closed")
