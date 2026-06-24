"""
Socratic Session Store Port — hexagonal interface for session persistence.

Defines the contract that any persistence backend (SQLite, Redis, Postgres)
must implement for Socratic session lifecycle management.

Operations:
  - save: persist a session (create or update)
  - get: retrieve by session_id
  - delete: remove a session
  - list_active: list non-expired sessions
  - cleanup_expired: evict sessions past TTL
  - health_check: verify store is operational
"""

from __future__ import annotations

from typing import Protocol

from agoracle.domain.types import SocraticSession


class SocraticSessionStorePort(Protocol):
    """Port for Socratic session persistence."""

    async def save(self, session: SocraticSession) -> None:
        """Persist a session (insert or update). Idempotent."""
        ...

    async def get(self, session_id: str) -> SocraticSession | None:
        """Retrieve a session by ID. Returns None if not found or expired."""
        ...

    async def delete(self, session_id: str) -> bool:
        """Delete a session. Returns True if it existed."""
        ...

    async def list_active(self, limit: int = 50) -> list[SocraticSession]:
        """List active (non-expired) sessions, newest first."""
        ...

    async def count_active(self) -> int:
        """Count active sessions (for cap enforcement)."""
        ...

    async def cleanup_expired(self, ttl_seconds: int = 1800) -> int:
        """Remove sessions older than TTL. Returns count of removed sessions."""
        ...

    async def health_check(self) -> bool:
        """Return True if the store is operational."""
        ...

    async def close(self) -> None:
        """Release resources (DB connections, etc.)."""
        ...
