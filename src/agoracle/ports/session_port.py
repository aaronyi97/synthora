"""
Session Port — interface for conversation session management.

Phase 0: JSON file implementation.
Sessions hold compressed turn history for multi-turn context.
"""

from __future__ import annotations

from typing import Protocol

from agoracle.domain.types import Session, Turn


class SessionPort(Protocol):
    """Port for session (short-term conversation memory)."""

    async def get_or_create(self, session_id: str) -> Session:
        """Get existing session or create new one."""
        ...

    async def add_turn(self, session_id: str, turn: Turn) -> None:
        """Append a turn to the session."""
        ...

    async def get_recent_turns(
        self, session_id: str, limit: int = 5
    ) -> list[Turn]:
        """Get the most recent turns for context injection."""
        ...

    async def list_sessions(self, limit: int = 20) -> list[Session]:
        """List recent sessions."""
        ...
