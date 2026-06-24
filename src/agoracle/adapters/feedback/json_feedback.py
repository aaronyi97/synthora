"""
JSON file-based feedback storage.

Simple append-only JSONL (one JSON object per line) for user feedback.
Each line records: query_id, rating, comment, timestamp.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from agoracle.domain.types import Feedback

logger = logging.getLogger(__name__)


class JsonFeedbackStore:
    """
    Append-only JSONL feedback storage.

    Storage layout:
      {feedback_path}  (e.g. data/feedback.jsonl)
    """

    def __init__(self, feedback_path: str | Path) -> None:
        self.feedback_path = Path(feedback_path)
        self.feedback_path.parent.mkdir(parents=True, exist_ok=True)

    async def record(
        self,
        query_id: str,
        rating: str,
        comment: str | None = None,
        extra: dict | None = None,
    ) -> None:
        """
        Record user feedback for a query.

        Args:
            query_id: The query this feedback is for.
            rating: One of "useful", "inaccurate", "too_shallow", "too_slow", "not_useful".
            comment: Optional free-text comment.
            extra: v3.5 — additional metadata (vote, mode, quality_gate).
        """
        entry = {
            "query_id": query_id,
            "rating": rating,
            "comment": comment,
            "timestamp": datetime.now().isoformat(),
        }
        if extra:
            entry.update(extra)

        def _write():
            with open(self.feedback_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        try:
            await asyncio.to_thread(_write)
            logger.info(f"Feedback recorded: {query_id} -> {rating}")
        except Exception as e:
            logger.error(f"Failed to record feedback: {e}")

    async def get_all(self) -> list[dict]:
        """Load all feedback entries."""
        if not self.feedback_path.exists():
            return []

        def _read():
            result = []
            with open(self.feedback_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        result.append(json.loads(line))
            return result

        entries = []
        try:
            entries = await asyncio.to_thread(_read)
        except Exception as e:
            logger.error(f"Failed to load feedback: {e}")

        return entries

    async def get_stats(self) -> dict[str, int]:
        """Get feedback statistics by rating type."""
        entries = await self.get_all()
        stats: dict[str, int] = {}
        for entry in entries:
            rating = entry.get("rating", "unknown")
            stats[rating] = stats.get(rating, 0) + 1
        return stats

    async def delete_by_query_ids(self, query_ids: set[str]) -> int:
        """Delete all feedback entries matching the given query_ids. Returns count deleted."""
        if not query_ids or not self.feedback_path.exists():
            return 0

        def _rewrite() -> int:
            kept: list[str] = []
            removed = 0
            with open(self.feedback_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        kept.append(line)
                        continue
                    if entry.get("query_id") in query_ids:
                        removed += 1
                    else:
                        kept.append(json.dumps(entry, ensure_ascii=False))
            with open(self.feedback_path, "w", encoding="utf-8") as f:
                for item in kept:
                    f.write(item + "\n")
            return removed

        try:
            count = await asyncio.to_thread(_rewrite)
            if count:
                logger.info(f"Deleted {count} feedback entries for query_ids batch")
            return count
        except Exception as e:
            logger.error(f"Failed to delete feedback entries: {e}")
            return 0
