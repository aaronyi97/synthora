"""
Event Bus — decoupled side-effect architecture.

Pipeline emits events; subscribers handle side effects independently.
Adding a feature = adding a subscriber, NOT modifying the pipeline.

Critical subscribers (knowledge extraction) use in-process retry (max 3 attempts).
Non-critical subscribers (sync, notes) use fire-and-forget.

Note: Phase 0 uses in-process retry only. A durable outbox (file-based
persistence for failed events) is planned for Phase 4 when knowledge
extraction becomes critical. Current retry is sufficient for session updates.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

# Type alias for async event handlers
EventHandler = Callable[[Any], Coroutine[Any, Any, None]]


class EventBus:
    """
    Simple in-process async event bus.

    Design decisions:
      - Subscribers run in parallel (asyncio.gather)
      - Single subscriber failure does NOT affect other subscribers
      - Critical handlers get retry logic (max 3 attempts)
      - All failures are logged
    """

    def __init__(self) -> None:
        self._subscribers: dict[type, list[tuple[EventHandler, bool]]] = defaultdict(list)
        # tuple: (handler, is_critical)
        self._pending_tasks: set[asyncio.Task] = set()

    def subscribe(
        self,
        event_type: type,
        handler: EventHandler,
        critical: bool = False,
    ) -> None:
        """
        Register a handler for an event type.

        Args:
            event_type: The event class to subscribe to.
            handler: Async function that receives the event.
            critical: If True, handler gets retry logic (outbox pattern).
        """
        self._subscribers[event_type].append((handler, critical))
        logger.debug(
            f"Subscribed {handler.__name__} to {event_type.__name__} "
            f"(critical={critical})"
        )

    async def emit(self, event: Any) -> None:
        """
        Emit an event to all subscribers (fire-and-forget).

        Handlers are launched as background tasks so they do NOT block the
        main pipeline response. Failures are logged but never propagate.
        """
        event_type = type(event)
        handlers = self._subscribers.get(event_type, [])

        if not handlers:
            logger.debug(f"No subscribers for {event_type.__name__}")
            return

        for handler, is_critical in handlers:
            if is_critical:
                task = asyncio.create_task(self._run_with_retry(handler, event))
            else:
                task = asyncio.create_task(self._run_safe(handler, event))
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)

    async def _run_safe(self, handler: EventHandler, event: Any) -> None:
        """Run handler with error catching (fire-and-forget)."""
        try:
            await handler(event)
        except Exception as e:
            logger.error(
                f"Event handler {handler.__name__} failed: {e}",
                exc_info=True,
            )

    async def _run_with_retry(
        self,
        handler: EventHandler,
        event: Any,
        max_retries: int = 3,
    ) -> None:
        """Run critical handler with retry logic."""
        for attempt in range(1, max_retries + 1):
            try:
                await handler(event)
                return
            except Exception as e:
                logger.warning(
                    f"Critical handler {handler.__name__} failed "
                    f"(attempt {attempt}/{max_retries}): {e}"
                )
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)  # exponential backoff: 2s, 4s, 8s

        logger.error(
            f"Critical handler {handler.__name__} exhausted all retries for "
            f"event {type(event).__name__}"
        )

    async def drain(self) -> None:
        """Wait for all pending event handlers to complete (for testing)."""
        if self._pending_tasks:
            await asyncio.gather(*list(self._pending_tasks), return_exceptions=True)

    @property
    def subscriber_count(self) -> int:
        """Total number of subscriptions across all event types."""
        return sum(len(handlers) for handlers in self._subscribers.values())
