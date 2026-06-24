from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sse_starlette.sse import AppStatus

import agoracle.api.app as app_module
from agoracle.adapters.user.sqlite_user_store import SQLiteUserStore
from agoracle.api.routes import query as query_module
from agoracle.domain.types import Mode, OutputDepth, QueryContext, QueryResult, QuestionType
from agoracle.services.streaming import PipelineComplete


class _QuotaStub:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []
        self.initial_credits: dict[int, int] = {}

    def check_quota(self, user_id: int, mode: str):
        return None

    def record_usage(self, user_id: int, mode: str) -> None:
        self.calls.append((user_id, mode))

    def set_user_total_credits(self, user_id: int, credits: int) -> None:
        self.initial_credits[user_id] = credits

    def get_lifetime_usage(self, user_id: int) -> dict[str, int]:
        return {}


class _ModelAdapterStub:
    def __init__(self) -> None:
        self.available_models = {"model-a"}

    def supports_model(self, model_id: str) -> bool:
        return True

    def get_cost_tracker(self):
        return {}


class _FakeUserStore:
    def __init__(self) -> None:
        self._next_user_id = 1000
        self._next_session_id = 0
        self._next_api_key_id = 0
        self._users_by_id: dict[int, dict[str, object]] = {}
        self._usernames: dict[str, int] = {}
        self._sessions: dict[str, int] = {}

    def _issue_api_key(self) -> str:
        self._next_api_key_id += 1
        return f"sk-test-{self._next_api_key_id:08d}"

    def _public_user(self, user: dict[str, object]) -> dict[str, object]:
        return {
            "id": user["id"],
            "username": user["username"],
            "display_name": user["display_name"],
            "api_key": user["api_key"],
            "is_admin": user["is_admin"],
        }

    def _request_user(self, user: dict[str, object]) -> dict[str, object]:
        return {
            "id": user["id"],
            "username": user["username"],
            "display_name": user["display_name"],
            "is_admin": user["is_admin"],
        }

    async def register(
        self,
        username: str,
        password: str,
        display_name: str = "",
        is_admin: bool = False,
    ) -> dict[str, object]:
        if username in self._usernames:
            raise ValueError(f"Username '{username}' already exists")
        self._next_user_id += 1
        user = {
            "id": self._next_user_id,
            "username": username,
            "display_name": display_name or username,
            "password": password,
            "api_key": self._issue_api_key(),
            "is_admin": is_admin,
            "query_count": 0,
        }
        self._users_by_id[user["id"]] = user
        self._usernames[username] = user["id"]
        return self._public_user(user)

    async def login(self, username: str, password: str) -> dict[str, object] | None:
        user_id = self._usernames.get(username)
        if not user_id:
            return None
        user = self._users_by_id[user_id]
        if user["password"] != password:
            return None
        return self._public_user(user)

    async def login_by_phone(self, phone: str, password: str) -> dict[str, object] | None:
        return None

    async def check_login_locked(self, username: str, client_ip: str) -> int | None:
        return None

    async def record_login_failure(self, username: str, client_ip: str) -> None:
        return None

    async def clear_login_failures(self, username: str, client_ip: str) -> None:
        return None

    async def create_session(self, user_id: int) -> str:
        self._next_session_id += 1
        session_id = f"sess-test-{self._next_session_id:08d}"
        self._sessions[session_id] = user_id
        return session_id

    async def get_by_session_id(self, session_id: str) -> dict[str, object] | None:
        user_id = self._sessions.get(session_id)
        if not user_id:
            return None
        return self._request_user(self._users_by_id[user_id])

    async def revoke_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    async def revoke_all_sessions(self, user_id: int) -> None:
        stale = [sid for sid, uid in self._sessions.items() if uid == user_id]
        for sid in stale:
            self._sessions.pop(sid, None)

    async def get_by_api_key(self, api_key: str) -> dict[str, object] | None:
        for user in self._users_by_id.values():
            if user["api_key"] == api_key:
                return self._public_user(user)
        return None

    async def has_api_key(self, user_id: int) -> bool:
        user = self._users_by_id.get(user_id)
        return bool(user and user.get("api_key"))

    async def reset_api_key(self, user_id: int) -> str:
        user = self._users_by_id[user_id]
        new_key = self._issue_api_key()
        user["api_key"] = new_key
        return new_key

    async def get_history_count(self, user_id: int) -> int:
        user = self._users_by_id[user_id]
        return int(user["query_count"])

    async def save_query(self, user_id: int, **kwargs) -> None:
        self._users_by_id[user_id]["query_count"] = int(self._users_by_id[user_id]["query_count"]) + 1

    def peek_api_key(self, username: str) -> str:
        user_id = self._usernames[username]
        return str(self._users_by_id[user_id]["api_key"])


ORIGIN_HEADERS = {"origin": "http://127.0.0.1:5173"}


def _make_result(question: str, query_id: str, fast_path: bool = True) -> QueryResult:
    return QueryResult(
        query_id=query_id,
        question=question,
        mode="light",
        resolved_mode="light",
        final_answer="ok",
        quality_gate_result="best_single",
        contributor_count=1,
        total_model_calls=1,
        estimated_cost_usd=0.01,
        fast_path=fast_path,
        key_insights=["a", "b"],
    )


def _build_context(req, user_id: int = 0) -> QueryContext:
    return QueryContext(
        query_id="q-auth-product",
        question=req.question,
        mode=Mode.LIGHT,
        resolved_mode=Mode.LIGHT,
        web_search_enabled=req.web_search,
        output_depth=OutputDepth.LEVEL_1,
        question_type=QuestionType.UNKNOWN,
        user_id=user_id,
    )


@pytest.fixture(autouse=True)
def _stabilize_sse_exit_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _wait_until_cancelled(*_args, **_kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    try:
        import sse_starlette.sse as _sse
    except Exception:
        return

    _resp_cls = getattr(_sse, "EventSourceResponse", None)
    if _resp_cls is not None:
        for _name in ("_listen_for_exit_signal", "listen_for_exit_signal"):
            if hasattr(_resp_cls, _name):
                monkeypatch.setattr(_resp_cls, _name, staticmethod(_wait_until_cancelled), raising=False)

    _app_status = getattr(_sse, "AppStatus", None)
    if _app_status is not None and hasattr(_app_status, "should_exit"):
        monkeypatch.setattr(_app_status, "should_exit", False, raising=False)


@pytest.fixture
def auth_env(monkeypatch: pytest.MonkeyPatch):
    @asynccontextmanager
    async def _noop_lifespan(_app):
        yield

    class _FakeOrchestrator:
        def __init__(self, **kwargs):
            pass

        async def execute(self, context):
            return _make_result(context.question, query_id="q-auth-ask", fast_path=True)

    async def _fake_execute_streaming(**kwargs):
        context = kwargs["context"]
        yield PipelineComplete(_make_result(context.question, query_id="q-auth-stream", fast_path=True))

    monkeypatch.setenv("ENV", "test")
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    monkeypatch.setattr(app_module, "lifespan", _noop_lifespan)
    monkeypatch.setattr(query_module, "_build_context", _build_context)
    monkeypatch.setattr(query_module, "Orchestrator", _FakeOrchestrator)
    monkeypatch.setattr(query_module, "execute_streaming", _fake_execute_streaming)

    quota = _QuotaStub()
    store = _FakeUserStore()
    app_module._user_stream_counts.clear()
    app_module.state.config = SimpleNamespace(
        models={"model-a": SimpleNamespace(name="Model A")},
        modes={"light": SimpleNamespace(preflight_clarity_check=False)},
        features=SimpleNamespace(
            companion_hints=False,
            supplement_restart=False,
            roundtable_enabled=False,
        ),
    )
    app_module.state.model_adapter = _ModelAdapterStub()
    app_module.state.judge = object()
    app_module.state.extractor = object()
    app_module.state.prompt_loader = None
    app_module.state.event_bus = None
    app_module.state.search_service = None
    app_module.state.profile_store = None
    app_module.state.session_store = None
    app_module.state.socratic_orch = None
    app_module.state.user_store = store
    app_module.state.behavior_analytics = None
    app_module.state.quota_service = quota
    app_module.state.proactive_coach = None
    app_module.state.conversation_store = None
    app_module.state.feedback_store = None
    app_module.state.failure_monitor = None
    app_module.state.roundtable_store = None

    AppStatus.should_exit = False
    AppStatus.should_exit_event = None
    app = app_module.create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield app, client, store, quota
    AppStatus.should_exit = False
    AppStatus.should_exit_event = None


def test_browser_mainline_closure_and_non_leak(auth_env) -> None:
    app, client, store, quota = auth_env

    register = client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "password123", "display_name": "Alice"},
        headers=ORIGIN_HEADERS,
    )
    assert register.status_code == 200, register.text
    assert "api_key" not in register.json()
    assert client.cookies.get("session")

    me = client.get("/api/auth/me")
    assert me.status_code == 200, me.text
    assert me.json()["username"] == "alice"

    models = client.get("/api/models")
    assert models.status_code == 200, models.text
    assert models.json()["models"][0]["id"] == "model-a"

    ask = client.post(
        "/api/ask",
        json={"question": "hello", "mode": "light", "skip_preflight": True},
        headers=ORIGIN_HEADERS,
    )
    assert ask.status_code == 200, ask.text
    assert ask.json()["fast_path"] is True

    stream = client.post(
        "/api/ask/stream",
        json={"question": "hello", "mode": "light", "skip_preflight": True},
        headers={**ORIGIN_HEADERS, "accept": "text/event-stream"},
    )
    assert stream.status_code == 200, stream.text
    assert '"fast_path":true' in stream.text.replace(" ", "")

    logout = client.post("/api/auth/logout", headers=ORIGIN_HEADERS)
    assert logout.status_code == 200, logout.text

    me_after_logout = client.get("/api/auth/me")
    assert me_after_logout.status_code == 401

    login = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "password123", "phone": ""},
        headers=ORIGIN_HEADERS,
    )
    assert login.status_code == 200, login.text
    assert "api_key" not in login.json()


def test_cookie_post_without_origin_is_rejected_by_csrf(auth_env) -> None:
    app, client, store, quota = auth_env

    register = client.post(
        "/api/auth/register",
        json={"username": "bob", "password": "password123", "display_name": "Bob"},
        headers=ORIGIN_HEADERS,
    )
    assert register.status_code == 200, register.text

    blocked = client.post(
        "/api/ask",
        json={"question": "hello", "mode": "light", "skip_preflight": True},
    )
    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["error_code"] == "CSRF_REJECTED"


def test_api_key_lifecycle_and_bearer_core_endpoints(auth_env) -> None:
    app, client, store, quota = auth_env

    register = client.post(
        "/api/auth/register",
        json={"username": "carol", "password": "password123", "display_name": "Carol"},
        headers=ORIGIN_HEADERS,
    )
    assert register.status_code == 200, register.text

    status = client.get("/api/auth/api-key")
    assert status.status_code == 200, status.text
    assert status.json() == {
        "status": "ok",
        "has_api_key": True,
        "auth_scheme": "Bearer",
        "session_coupled": False,
    }

    old_key = store.peek_api_key("carol")

    rotate = client.post("/api/auth/api-key/rotate", headers=ORIGIN_HEADERS)
    assert rotate.status_code == 200, rotate.text
    rotate_body = rotate.json()
    assert rotate_body["status"] == "ok"
    assert rotate_body["auth_scheme"] == "Bearer"
    assert rotate_body["session_coupled"] is False
    assert rotate_body["api_key"].startswith("sk-")
    assert rotate_body["api_key"] != old_key
    new_key = rotate_body["api_key"]

    logout = client.post("/api/auth/logout", headers=ORIGIN_HEADERS)
    assert logout.status_code == 200, logout.text

    with TestClient(app, raise_server_exceptions=False) as bearer_client:
        old_models = bearer_client.get(
            "/api/models",
            headers={"Authorization": f"Bearer {old_key}"},
        )
        assert old_models.status_code == 403, old_models.text
        assert old_models.json()["error_code"] == "AUTH_FORBIDDEN"

        models = bearer_client.get(
            "/api/models",
            headers={"Authorization": f"Bearer {new_key}"},
        )
        assert models.status_code == 200, models.text

        ask = bearer_client.post(
            "/api/ask",
            json={"question": "agent hello", "mode": "light", "skip_preflight": True},
            headers={"Authorization": f"Bearer {new_key}"},
        )
        assert ask.status_code == 200, ask.text
        assert ask.json()["fast_path"] is True

        stream = bearer_client.post(
            "/api/ask/stream",
            json={"question": "agent hello", "mode": "light", "skip_preflight": True},
            headers={"Authorization": f"Bearer {new_key}", "accept": "text/event-stream"},
        )
        assert stream.status_code == 200, stream.text
        assert '"fast_path":true' in stream.text.replace(" ", "")


def test_cookie_route_takes_precedence_over_bearer_header(auth_env) -> None:
    app, client, store, quota = auth_env

    register = client.post(
        "/api/auth/register",
        json={"username": "dora", "password": "password123", "display_name": "Dora"},
        headers=ORIGIN_HEADERS,
    )
    assert register.status_code == 200, register.text

    me = client.get(
        "/api/auth/me",
        headers={"Authorization": "Bearer sk-invalid"},
    )
    assert me.status_code == 200, me.text
    assert me.json()["username"] == "dora"


@pytest.mark.asyncio
async def test_sqlite_user_store_api_key_rotation_invalidates_old_key(tmp_path) -> None:
    store = SQLiteUserStore(tmp_path / "users.db")
    await store.initialize()
    try:
        user = await store.register("sqlite_user", "password123", "SQLite User")
        assert await store.has_api_key(user["id"]) is True
        old_key = user["api_key"]
        old_lookup = await store.get_by_api_key(old_key)
        assert old_lookup is not None
        assert old_lookup["id"] == user["id"]
        new_key = await store.reset_api_key(user["id"])
        assert new_key != old_key
        assert await store.get_by_api_key(old_key) is None
        new_lookup = await store.get_by_api_key(new_key)
        assert new_lookup is not None
        assert new_lookup["id"] == user["id"]
    finally:
        await store.close()
