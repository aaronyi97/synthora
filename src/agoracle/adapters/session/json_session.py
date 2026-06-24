"""
JSON file-based session storage.

Phase 0 implementation: simple, reliable, no external dependencies.
Each session is one JSON file in the sessions directory.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from agoracle.domain.types import Session, Turn

logger = logging.getLogger(__name__)


class JsonSessionStore:
    """
    Session store backed by JSON files.

    Storage layout:
      {session_dir}/{session_id}.json
    """

    def __init__(self, session_dir: str | Path) -> None:
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)

    async def get_or_create(self, session_id: str) -> Session:
        """Get existing session or create new one."""
        safe_id = self._sanitize_id(session_id)
        path = self._path(safe_id)
        if path.exists():
            try:
                text = await asyncio.to_thread(path.read_text, "utf-8")
                data = json.loads(text)
                return self._deserialize(data)
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Corrupt session file {path}: {e}")

        # Create new session
        session = Session(session_id=safe_id)
        await self._save(session)
        return session

    async def add_turn(self, session_id: str, turn: Turn) -> None:
        """Append a turn to the session."""
        session = await self.get_or_create(session_id)
        session.turns.append(turn)
        session.last_active = datetime.now()
        await self._save(session)

    async def get_recent_turns(
        self, session_id: str, limit: int = 5
    ) -> list[Turn]:
        """Get the most recent turns for context injection."""
        session = await self.get_or_create(session_id)
        return session.turns[-limit:]

    async def list_sessions(self, limit: int = 20) -> list[Session]:
        """List recent sessions, sorted by last_active descending."""
        sessions = []
        def _list_sync():
            result = []
            for p in sorted(
                self.session_dir.glob("*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            ):
                try:
                    d = json.loads(p.read_text(encoding="utf-8"))
                    result.append(self._deserialize(d))
                except Exception as e:
                    logger.warning(f"Error reading session {p}: {e}")
                if len(result) >= limit:
                    break
            return result

        sessions = await asyncio.to_thread(_list_sync)
        return sessions

    async def _save(self, session: Session) -> None:
        """Atomic write: write to temp file, then rename."""
        path = self._path(session.session_id)
        tmp_path = path.with_suffix(".tmp")

        def _write_sync():
            tmp_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(path)  # atomic on most filesystems

        try:
            data = self._serialize(session)
            await asyncio.to_thread(_write_sync)
        except Exception as e:
            logger.error(f"Failed to save session {session.session_id}: {e}")
            if tmp_path.exists():
                tmp_path.unlink()

    @staticmethod
    def _sanitize_id(session_id: str) -> str:
        """Sanitize session_id to prevent path traversal and empty collision."""
        safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
        if not safe:
            # Generate a random ID if sanitization emptied the string
            import uuid
            safe = uuid.uuid4().hex[:12]
        return safe

    def _path(self, session_id: str) -> Path:
        """Get file path for a session (expects already-sanitized id)."""
        return self.session_dir / f"{session_id}.json"

    @staticmethod
    def _serialize(session: Session) -> dict:
        """Convert Session to JSON-serializable dict."""
        return {
            "session_id": session.session_id,
            "created_at": session.created_at.isoformat(),
            "last_active": session.last_active.isoformat(),
            "turns": [
                {
                    "turn_id": t.turn_id,
                    "question": t.question,
                    "final_answer_summary": t.final_answer_summary,
                    "key_insights": t.key_insights,
                    "mode": t.mode,
                    "timestamp": t.timestamp.isoformat(),
                }
                for t in session.turns
            ],
        }

    @staticmethod
    def _deserialize(data: dict) -> Session:
        """Convert dict to Session."""
        turns = [
            Turn(
                turn_id=t["turn_id"],
                question=t["question"],
                final_answer_summary=t.get("final_answer_summary", ""),
                key_insights=t.get("key_insights", []),
                mode=t.get("mode", ""),
                timestamp=datetime.fromisoformat(t["timestamp"]),
            )
            for t in data.get("turns", [])
        ]
        return Session(
            session_id=data["session_id"],
            turns=turns,
            created_at=datetime.fromisoformat(data["created_at"]),
            last_active=datetime.fromisoformat(data["last_active"]),
        )
