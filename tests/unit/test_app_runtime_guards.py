from __future__ import annotations

import pytest

import agoracle.api.app as app_module
from agoracle.api.app import AskResponse


class TestSentryConfig:
    def test_create_app_initializes_sentry_with_environment_and_release(self, monkeypatch: pytest.MonkeyPatch):
        captured: dict[str, object] = {}

        def _fake_init(**kwargs):
            captured.update(kwargs)

        monkeypatch.setenv("SENTRY_DSN", "https://example@sentry.invalid/1")
        monkeypatch.setenv("ENV", "staging")
        monkeypatch.setenv("APP_VERSION", "9.9.9-test")
        monkeypatch.setattr(app_module.sentry_sdk, "init", _fake_init)

        app_module.create_app()

        assert captured["environment"] == "staging"
        assert captured["release"] == "9.9.9-test"
        assert callable(captured["before_send"])

    def test_sentry_before_send_redacts_headers_query_and_question(self):
        event = {
            "request": {
                "headers": {
                    "Cookie": "session=sid-123",
                    "Authorization": "Bearer sk-live",
                    "Content-Type": "application/json",
                },
                "url": "https://example.com/api/ask?question=secret&foo=bar",
                "data": {
                    "question": "secret prompt",
                    "mode": "light",
                },
            }
        }

        redacted = app_module._sentry_before_send(event, {})

        assert redacted["request"]["headers"] == {"Content-Type": "application/json"}
        assert redacted["request"]["url"] == "https://example.com/api/ask"
        assert redacted["request"]["data"]["question"] == "[REDACTED]"
        assert redacted["request"]["data"]["mode"] == "light"

    def test_get_sentry_release_falls_back_to_package_version(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("SENTRY_RELEASE", raising=False)
        monkeypatch.delenv("APP_VERSION", raising=False)
        monkeypatch.setattr(app_module, "_get_version", lambda: "2.8.8-test")

        assert app_module._get_sentry_release() == "2.8.8-test"


class TestWorkerGuard:
    def test_configured_worker_count_reads_web_concurrency(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("WEB_CONCURRENCY", "4")
        monkeypatch.delenv("GUNICORN_WORKERS", raising=False)
        monkeypatch.delenv("GUNICORN_CMD_ARGS", raising=False)

        assert app_module._configured_worker_count(["gunicorn"]) == 4

    def test_configured_worker_count_reads_cli_flag(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
        monkeypatch.delenv("GUNICORN_WORKERS", raising=False)
        monkeypatch.delenv("GUNICORN_CMD_ARGS", raising=False)

        assert app_module._configured_worker_count(["gunicorn", "--workers", "3"]) == 3

    def test_configured_worker_count_reads_gunicorn_cmd_args(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
        monkeypatch.delenv("GUNICORN_WORKERS", raising=False)
        monkeypatch.setenv("GUNICORN_CMD_ARGS", "--bind 127.0.0.1:8000 --workers 5")

        assert app_module._configured_worker_count(["gunicorn"]) == 5

    def test_single_worker_guard_rejects_multi_worker_sqlite_runtime(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("WEB_CONCURRENCY", "2")
        monkeypatch.delenv("GUNICORN_WORKERS", raising=False)
        monkeypatch.delenv("GUNICORN_CMD_ARGS", raising=False)

        with pytest.raises(RuntimeError, match="workers=2"):
            app_module._assert_single_worker_sqlite_runtime()


class TestCompanionGuideModel:
    def test_ask_response_coerces_companion_guide_into_typed_model(self):
        response = AskResponse(
            companion_guide={
                "message": "继续深挖这个方向",
                "actions": [
                    {
                        "label": "深入研究",
                        "action_type": "query_research",
                        "action_payload": {"mode": "research"},
                        "estimated_seconds": 45,
                        "requires_confirm": False,
                    }
                ],
                "trigger": "divergence",
                "is_silent": False,
            }
        )

        assert response.companion_guide is not None
        assert response.companion_guide.message == "继续深挖这个方向"
        assert response.companion_guide.actions[0].action_type == "query_research"
