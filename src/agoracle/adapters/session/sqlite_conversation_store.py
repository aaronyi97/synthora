"""
SQLite Conversation Store — persistent multi-turn conversation history.

Replaces the volatile in-memory dict (state.conversation_sessions) that loses
all conversation context on every deploy/restart.

Features:
  - Per-user, per-session turn storage
  - WAL mode for concurrent read/write
  - Automatic cleanup of old sessions (configurable TTL)
  - Returns list[Turn] compatible with ConversationMemoryService

Schema:
  conversation_turns:
    id INTEGER PRIMARY KEY
    session_id TEXT
    user_id INTEGER
    turn_id TEXT
    question TEXT
    answer_summary TEXT
    key_insights TEXT (JSON array)
    mode TEXT
    answer_outline TEXT (v5.0: pipe-separated key insights for mid-tier context)
    created_at TEXT (ISO 8601)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import aiosqlite

from agoracle.domain.types import Turn

logger = logging.getLogger(__name__)

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS conversation_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    user_id INTEGER NOT NULL DEFAULT 0,
    turn_id TEXT NOT NULL,
    question TEXT NOT NULL,
    answer_summary TEXT NOT NULL DEFAULT '',
    key_insights TEXT NOT NULL DEFAULT '[]',
    mode TEXT NOT NULL DEFAULT '',
    answer_outline TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conv_session ON conversation_turns(session_id);
CREATE INDEX IF NOT EXISTS idx_conv_user ON conversation_turns(user_id);
CREATE INDEX IF NOT EXISTS idx_conv_created ON conversation_turns(created_at);
"""

# Default: keep 30 days of conversation history
DEFAULT_TTL_DAYS = 30
# Max turns per session to prevent unbounded growth
MAX_TURNS_PER_SESSION = 50


class SQLiteConversationStore:
    """SQLite-backed persistent conversation history."""

    def __init__(self, db_path: str | Path, ttl_days: int = DEFAULT_TTL_DAYS) -> None:
        self._db_path = Path(db_path)
        self._ttl_days = ttl_days
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        # v2.9: busy_timeout — wait up to 5s for write locks before raising SQLITE_BUSY.
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.executescript(_CREATE_TABLES)
        # v5.0: Migrate existing DBs — add answer_outline column if missing
        try:
            await self._db.execute(
                "ALTER TABLE conversation_turns ADD COLUMN answer_outline TEXT NOT NULL DEFAULT ''"
            )
            await self._db.commit()
            logger.info("ConversationStore: migrated — added answer_outline column")
        except Exception:
            pass  # Column already exists — normal for fresh DBs
        await self._db.commit()
        logger.info(f"ConversationStore initialized: {self._db_path}")

    def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("ConversationStore not initialized")
        return self._db

    async def append_turn(
        self,
        session_id: str,
        turn: Turn,
        user_id: int = 0,
    ) -> None:
        """Append a conversation turn. Enforces MAX_TURNS_PER_SESSION."""
        db = self._ensure_db()
        insights_json = json.dumps(turn.key_insights, ensure_ascii=False)
        await db.execute(
            """INSERT INTO conversation_turns
               (session_id, user_id, turn_id, question, answer_summary, key_insights, mode, answer_outline, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                user_id,
                turn.turn_id,
                turn.question,
                turn.final_answer_summary,
                insights_json,
                turn.mode,
                getattr(turn, 'answer_outline', '') or '',
                turn.timestamp.isoformat() if turn.timestamp else datetime.now().isoformat(),
            ),
        )
        await db.commit()

        # Enforce per-session cap: delete oldest turns beyond limit
        cursor = await db.execute(
            "SELECT COUNT(*) FROM conversation_turns WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        if row and row[0] > MAX_TURNS_PER_SESSION:
            excess = row[0] - MAX_TURNS_PER_SESSION
            await db.execute(
                """DELETE FROM conversation_turns WHERE id IN (
                     SELECT id FROM conversation_turns
                     WHERE session_id = ?
                     ORDER BY created_at ASC LIMIT ?
                   )""",
                (session_id, excess),
            )
            await db.commit()

    async def get_session_turns(self, session_id: str, user_id: int = 0) -> list[Turn]:
        """Retrieve all turns for a session, ordered chronologically.

        When user_id > 0, enforces ownership: only returns turns belonging to that user.
        """
        db = self._ensure_db()
        if user_id:
            cursor = await db.execute(
                """SELECT turn_id, question, answer_summary, key_insights, mode, answer_outline, created_at
                   FROM conversation_turns
                   WHERE session_id = ? AND user_id = ?
                   ORDER BY created_at ASC""",
                (session_id, user_id),
            )
        else:
            cursor = await db.execute(
                """SELECT turn_id, question, answer_summary, key_insights, mode, answer_outline, created_at
                   FROM conversation_turns
                   WHERE session_id = ?
                   ORDER BY created_at ASC""",
                (session_id,),
            )
        rows = await cursor.fetchall()
        turns = []
        for row in rows:
            try:
                insights = json.loads(row[3]) if row[3] else []
            except (json.JSONDecodeError, TypeError):
                insights = []
            turns.append(Turn(
                turn_id=row[0],
                question=row[1],
                final_answer_summary=row[2] or "",
                key_insights=insights,
                mode=row[4] or "",
                answer_outline=row[5] or "",
                timestamp=datetime.fromisoformat(row[6]) if row[6] else datetime.now(),
            ))
        return turns

    async def get_user_recent_sessions(
        self, user_id: int, limit: int = 5
    ) -> list[str]:
        """Get recent session IDs for a user (for cross-session context, Phase 4+)."""
        db = self._ensure_db()
        cursor = await db.execute(
            """SELECT DISTINCT session_id
               FROM conversation_turns
               WHERE user_id = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def cleanup_expired(self) -> int:
        """Delete turns older than TTL. Returns number of deleted rows."""
        db = self._ensure_db()
        cutoff = (datetime.now() - timedelta(days=self._ttl_days)).isoformat()
        cursor = await db.execute(
            "DELETE FROM conversation_turns WHERE created_at < ?",
            (cutoff,),
        )
        await db.commit()
        deleted = cursor.rowcount or 0
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} expired conversation turns (>{self._ttl_days}d)")
        return deleted

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None
