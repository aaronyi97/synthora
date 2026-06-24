"""Tests for the Event Bus."""

import pytest
from agoracle.domain.events import QueryCompleted
from agoracle.services.event_bus import EventBus


@pytest.fixture
def bus():
    return EventBus()


class TestEventBus:
    """Test event bus subscription and emission."""

    @pytest.mark.asyncio
    async def test_subscriber_receives_event(self, bus):
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe(QueryCompleted, handler)
        event = QueryCompleted(query_id="test123", question="hello")
        await bus.emit(event)
        await bus.drain()

        assert len(received) == 1
        assert received[0].query_id == "test123"

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self, bus):
        count = {"a": 0, "b": 0}

        async def handler_a(event):
            count["a"] += 1

        async def handler_b(event):
            count["b"] += 1

        bus.subscribe(QueryCompleted, handler_a)
        bus.subscribe(QueryCompleted, handler_b)
        await bus.emit(QueryCompleted(query_id="x"))
        await bus.drain()

        assert count["a"] == 1
        assert count["b"] == 1

    @pytest.mark.asyncio
    async def test_failed_subscriber_does_not_block_others(self, bus):
        results = []

        async def failing_handler(event):
            raise RuntimeError("boom")

        async def good_handler(event):
            results.append("ok")

        bus.subscribe(QueryCompleted, failing_handler)
        bus.subscribe(QueryCompleted, good_handler)
        await bus.emit(QueryCompleted(query_id="x"))
        await bus.drain()

        assert results == ["ok"]  # good handler still ran

    @pytest.mark.asyncio
    async def test_critical_handler_retries(self, bus):
        call_count = {"n": 0}

        async def flaky_handler(event):
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise RuntimeError("transient error")

        bus.subscribe(QueryCompleted, flaky_handler, critical=True)
        await bus.emit(QueryCompleted(query_id="x"))
        await bus.drain()

        assert call_count["n"] == 3  # succeeded on 3rd attempt

    @pytest.mark.asyncio
    async def test_no_subscribers_is_fine(self, bus):
        await bus.emit(QueryCompleted(query_id="x"))  # should not raise

    def test_subscriber_count(self, bus):
        async def h(e): pass
        bus.subscribe(QueryCompleted, h)
        bus.subscribe(QueryCompleted, h)
        assert bus.subscriber_count == 2
