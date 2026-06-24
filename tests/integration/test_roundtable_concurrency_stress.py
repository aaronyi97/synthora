from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agoracle.api.app as app_module
from agoracle.api.routes import misc as misc_module
from agoracle.services import roundtable_orchestrator as rt


class _QuotaStub:
    def check_quota(self, user_id: int, mode: str):
        return None

    def record_usage(self, user_id: int, mode: str) -> None:
        return None


class _StateFlipQueue:
    """Flip to AWAITING_B after first accepted A-choice put."""

    def __init__(self, session: rt.RoundtableSession) -> None:
        self._session = session
        self._inner: asyncio.Queue = asyncio.Queue()
        self._put_calls = 0
        self.first_put_started = threading.Event()

    async def put(self, item: Any) -> None:
        self._put_calls += 1
        if self._put_calls == 1:
            self._session._state = rt.SessionState.AWAITING_B
            self.first_put_started.set()
            await asyncio.sleep(0.03)
        await self._inner.put(item)

    def put_nowait(self, item: Any) -> None:
        self._inner.put_nowait(item)

    def get_nowait(self) -> Any:
        return self._inner.get_nowait()

    def empty(self) -> bool:
        return self._inner.empty()


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    @asynccontextmanager
    async def _noop_lifespan(_app):
        yield

    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "0")
    monkeypatch.setenv("RATE_LIMIT_RPM", "0")
    monkeypatch.setenv("RATE_LIMIT_MAX_CONCURRENT", "0")
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    monkeypatch.setattr(app_module, "lifespan", _noop_lifespan)
    monkeypatch.setattr(misc_module, "_get_user_id", lambda _request: 1001)

    app_module.state.config = SimpleNamespace(
        features=SimpleNamespace(roundtable_enabled=True),
    )
    app_module.state.quota_service = _QuotaStub()
    app_module.state.model_adapter = object()
    app_module.state.judge = object()
    app_module.state.extractor = object()
    app_module.state.prompt_loader = None
    app_module.state.event_bus = None
    app_module.state.search_service = None
    app_module.state.profile_store = None
    app_module.state.session_store = None
    app_module.state.user_store = None
    app_module.state.behavior_analytics = None
    app_module.state.proactive_coach = None
    app_module.state.conversation_store = None

    return app_module.create_app()


def _post_choice(
    app: FastAPI,
    session_id: str,
    choice_point: str,
    action: str,
    idem: str,
) -> tuple[int, dict[str, Any]]:
    # Use one TestClient per call to avoid cross-thread event-loop issues.
    with TestClient(app, raise_server_exceptions=False) as client:
        res = client.post(
            f"/api/roundtable/{session_id}/choice",
            json={"choice_point": choice_point, "action": action},
            headers={"Idempotency-Key": idem},
        )
    try:
        body = res.json()
    except Exception:
        body = {}
    return res.status_code, body


def test_roundtable_choice_concurrent_double_submit_same_point_one_rejected(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same session+point with different idempotency keys: one 200, one 409."""
    session_id = "sid-concurrent-a"
    session = rt.RoundtableSession(session_id=session_id, owner_user_id=1001)
    session._state = rt.SessionState.AWAITING_A
    flip_queue = _StateFlipQueue(session)
    session._choice_queue = flip_queue
    monkeypatch.setattr(rt, "get_session", lambda _sid: session)

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut1 = pool.submit(_post_choice, app, session_id, "A", "deepen", "race-a-1")
        assert flip_queue.first_put_started.wait(timeout=2.0)
        fut2 = pool.submit(_post_choice, app, session_id, "A", "deepen", "race-a-2")

        r1 = fut1.result(timeout=5.0)
        r2 = fut2.result(timeout=5.0)

    statuses = sorted([r1[0], r2[0]])
    assert statuses == [200, 409], f"got={r1}, {r2}"

    reject_body = r1[1] if r1[0] == 409 else r2[1]
    assert reject_body["detail"]["error"] == "choice_point_mismatch"
    assert reject_body["detail"]["current_state"] == "awaiting_B"


def test_roundtable_choice_rapid_repeat_stress_n50_no_deadlock_no_500(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N=50 rapid submissions: no deadlock, no 500; collect success/rejected/errors."""
    n = 50
    accepted_session = rt.RoundtableSession(session_id="sid-stress", owner_user_id=1001)
    accepted_session._state = rt.SessionState.AWAITING_A
    rejected_session = rt.RoundtableSession(session_id="sid-stress", owner_user_id=1001)
    rejected_session._state = rt.SessionState.DEBATING

    lock = threading.Lock()
    calls = {"count": 0}

    def _get_session(_sid: str):
        with lock:
            calls["count"] += 1
            return accepted_session if calls["count"] == 1 else rejected_session

    monkeypatch.setattr(rt, "get_session", _get_session)

    with TestClient(app, raise_server_exceptions=False) as client:
        responses = []
        for i in range(n):
            res = client.post(
                "/api/roundtable/sid-stress/choice",
                json={"choice_point": "A", "action": "deepen"},
                headers={"Idempotency-Key": f"stress-{i}"},
            )
            try:
                body = res.json()
            except Exception:
                body = {}
            responses.append((res.status_code, body))

    success = sum(1 for code, _ in responses if code == 200)
    rejected = sum(1 for code, _ in responses if code == 409)
    errors = sum(1 for code, _ in responses if code >= 500 or code not in {200, 409})

    assert success == 1, f"success={success} rejected={rejected} errors={errors}"
    assert rejected == n - 1, f"success={success} rejected={rejected} errors={errors}"
    assert errors == 0, f"success={success} rejected={rejected} errors={errors}"


def test_roundtable_choice_ab_isolation_a_request_not_pollute_b(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A-point request at B-state is rejected and does not pollute B queue."""
    session_id = "sid-isolation-b"
    session = rt.RoundtableSession(session_id=session_id, owner_user_id=1001)
    session._state = rt.SessionState.AWAITING_B
    monkeypatch.setattr(rt, "get_session", lambda _sid: session)

    stale_a_code, stale_a_body = _post_choice(app, session_id, "A", "deepen", "stale-a")
    assert stale_a_code == 409
    assert stale_a_body["detail"]["error"] == "choice_point_mismatch"
    assert session._choice_queue.empty()

    valid_b_code, valid_b_body = _post_choice(app, session_id, "B", "conclude", "valid-b")
    assert valid_b_code == 200
    assert valid_b_body == {"ok": True}

    queued = session._choice_queue.get_nowait()
    assert queued.choice_point == "B"
    assert queued.action == "conclude"
    assert queued.idempotency_key == "valid-b"
    assert session._choice_queue.empty()
