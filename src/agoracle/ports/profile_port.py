"""
Profile Port — interface for user profile management.

Phase 0: interface only + static config file.
Phase 5: learning algorithm populates profile from interaction history.
"""

from __future__ import annotations

from typing import Protocol

from agoracle.domain.types import UserProfile


class ProfilePort(Protocol):
    """Port for user profile (personalization memory)."""

    async def load(self) -> UserProfile:
        """Load user profile."""
        ...

    async def save(self, profile: UserProfile) -> None:
        """Save updated profile."""
        ...

    async def get_summary(self, user_id: int = 0, language: str | None = None) -> str:
        """Generate a concise text summary for prompt injection."""
        ...
