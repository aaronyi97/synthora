"""
SQLite Socratic Session Store — commercial-grade persistent session storage.

Features:
  - WAL mode for concurrent read/write performance
  - Automatic schema migration (version-tracked)
  - TTL-based expiry with lazy + explicit cleanup
  - Session cap enforcement (evict oldest when over limit)
  - Full ACID compliance via SQLite transactions
  - Async-safe via aiosqlite
  - Health check for monitoring

Schema:
  socratic_sessions:
    session_id TEXT PRIMARY KEY
    question TEXT
    data JSON (full serialized SocraticSession)
    created_at TEXT (ISO 8601)
    updated_at TEXT (ISO 8601)
    is_active INTEGER (1=active, 0=finished)
    owner_user_id INTEGER (v2: session creator)

  schema_migrations:
    version INTEGER PRIMARY KEY
    applied_at TEXT
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

import aiosqlite

from agoracle.adapters.session.serializer import session_from_dict, session_to_dict
from agoracle.domain.types import SocraticSession

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 2

_CREATE_TABLES_V1 = """
CREATE TABLE IF NOT EXISTS socratic_sessions (
    session_id TEXT PRIMARY KEY,
    question TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_sessions_created_at
    ON socratic_sessions(created_at);

CREATE INDEX IF NOT EXISTS idx_sessions_is_active
    ON socratic_sessions(is_active);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""


class SQLiteSocraticSessionStore:
    """
    SQLite-backed Socratic session store.

    Implements SocraticSessionStorePort with commercial-grade reliability.

    Usage:
        store = SQLiteSocraticSessionStore("data/socratic_sessions.db")
        await store.initialize()
        # ... use store ...
        await store.close()
    """

    def __init__(self, db_path: str | Path, max_sessions: int = 200) -> None:
        self._db_path = Path(db_path)
        self._max_sessions = max_sessions
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Open DB connection, enable WAL, run migrations."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))

        # WAL mode: allows concurrent readers during writes
        await self._db.execute("PRAGMA journal_mode=WAL")
        # Synchronous NORMAL: good balance of safety vs performance
        await self._db.execute("PRAGMA synchronous=NORMAL")
        # Foreign keys (future-proofing)
        await self._db.execute("PRAGMA foreign_keys=ON")
        # v2.9: busy_timeout — wait up to 5s for write locks before raising SQLITE_BUSY.
        # Prevents spurious write failures when multiple gunicorn workers commit concurrently.
        await self._db.execute("PRAGMA busy_timeout=5000")

        await self._run_migrations()
        logger.info(f"SocraticSessionStore initialized: {self._db_path}")

    def _ensure_db(self) -> aiosqlite.Connection:
        """Return DB connection or raise if not initialized."""
        if self._db is None:
            raise RuntimeError("SocraticSessionStore not initialized — call initialize() first")
        return self._db

    async def _run_migrations(self) -> None:
        """Apply schema migrations up to CURRENT_SCHEMA_VERSION."""
        db = self._ensure_db()

        # Ensure migrations table exists
        await db.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
        """)
        await db.commit()

        # Check current version
        cursor = await db.execute(
            "SELECT MAX(version) FROM schema_migrations"
        )
        row = await cursor.fetchone()
        current_version = row[0] if row and row[0] else 0

        if current_version < 1:
            logger.info("Applying migration v1: create socratic_sessions table")
            await db.executescript(_CREATE_TABLES_V1)
            await db.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (1, datetime.now().isoformat()),
            )
            await db.commit()

        if current_version < 2:
            logger.info("Applying migration v2: add owner_user_id column")
            await db.execute(
                "ALTER TABLE socratic_sessions ADD COLUMN owner_user_id INTEGER NOT NULL DEFAULT 0"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_owner ON socratic_sessions(owner_user_id)"
            )
            await db.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (2, datetime.now().isoformat()),
            )
            await db.commit()

    async def save(self, session: SocraticSession, owner_user_id: int = 0) -> None:
        """Persist a session (upsert). Idempotent."""
        db = self._ensure_db()

        data_dict = session_to_dict(session)
        data_json = json.dumps(data_dict, ensure_ascii=False)
        now = datetime.now().isoformat()
        is_active = 0 if session.revealed or session.cognitive_snapshot else 1

        await db.execute(
            """
            INSERT INTO socratic_sessions (session_id, question, data, created_at, updated_at, is_active, owner_user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                data = excluded.data,
                updated_at = excluded.updated_at,
                is_active = excluded.is_active
            """,
            (session.session_id, session.question, data_json,
             session.created_at.isoformat(), now, is_active, owner_user_id),
        )
        await db.commit()

    async def get_owner(self, session_id: str) -> int | None:
        """Get the owner user_id for a session. Returns None if session not found."""
        db = self._ensure_db()
        cursor = await db.execute(
            "SELECT owner_user_id FROM socratic_sessions WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def set_owner(self, session_id: str, user_id: int) -> None:
        """Set the owner user_id for a session."""
        db = self._ensure_db()
        await db.execute(
            "UPDATE socratic_sessions SET owner_user_id = ? WHERE session_id = ?",
            (user_id, session_id),
        )
        await db.commit()

    async def get(self, session_id: str) -> SocraticSession | None:
        """Retrieve a session by ID. Returns None if not found."""
        db = self._ensure_db()

        cursor = await db.execute(
            "SELECT data FROM socratic_sessions WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None

        try:
            data_dict = json.loads(row[0])
            return session_from_dict(data_dict)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.error(f"Failed to deserialize session {session_id}: {e}")
            return None

    async def delete(self, session_id: str) -> bool:
        """Delete a session. Returns True if it existed."""
        db = self._ensure_db()

        cursor = await db.execute(
            "DELETE FROM socratic_sessions WHERE session_id = ?",
            (session_id,),
        )
        await db.commit()
        return cursor.rowcount > 0

    async def list_active(self, limit: int = 50) -> list[SocraticSession]:
        """List active sessions, newest first."""
        db = self._ensure_db()

        cursor = await db.execute(
            """
            SELECT data FROM socratic_sessions
            WHERE is_active = 1
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()

        sessions = []
        for row in rows:
            try:
                sessions.append(session_from_dict(json.loads(row[0])))
            except Exception as e:
                logger.warning(f"Skipping corrupt session: {e}")
        return sessions

    async def count_active(self) -> int:
        """Count active sessions."""
        db = self._ensure_db()

        cursor = await db.execute(
            "SELECT COUNT(*) FROM socratic_sessions WHERE is_active = 1"
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def cleanup_expired(self, ttl_seconds: int = 1800) -> int:
        """Remove sessions older than TTL. Returns count removed."""
        db = self._ensure_db()

        cutoff = datetime.fromtimestamp(time.time() - ttl_seconds).isoformat()

        # Single transaction: TTL expiry + cap enforcement (atomic)
        cursor = await db.execute(
            "DELETE FROM socratic_sessions WHERE updated_at < ? AND is_active = 1",
            (cutoff,),
        )
        expired_count = cursor.rowcount

        # Enforce max session cap (evict oldest by updated_at)
        cursor2 = await db.execute(
            "SELECT COUNT(*) FROM socratic_sessions WHERE is_active = 1"
        )
        row = await cursor2.fetchone()
        active_count = row[0] if row else 0
        evicted = 0
        if active_count > self._max_sessions:
            excess = active_count - self._max_sessions
            cursor = await db.execute(
                """
                DELETE FROM socratic_sessions WHERE session_id IN (
                    SELECT session_id FROM socratic_sessions
                    WHERE is_active = 1
                    ORDER BY updated_at ASC
                    LIMIT ?
                )
                """,
                (excess,),
            )
            evicted = cursor.rowcount

        await db.commit()

        total = expired_count + evicted
        if total > 0:
            logger.info(
                f"SocraticSessionStore cleanup: {expired_count} expired, "
                f"{evicted} evicted (cap={self._max_sessions})"
            )
        return total

    async def health_check(self) -> bool:
        """Return True if the store is operational."""
        if not self._db:
            return False
        try:
            cursor = await self._db.execute("SELECT 1")
            row = await cursor.fetchone()
            return row is not None and row[0] == 1
        except Exception:
            return False

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None
            logger.info("SocraticSessionStore closed")
