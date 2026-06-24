from __future__ import annotations

import pytest

from agoracle.domain.types import ModelResponse, Role, RoleCall
from agoracle.services.fan_out import FanOutEngine


class _DummyAdapter:
    def __init__(self, outcomes):
        self._outcomes = outcomes

    async def call(self, rc: RoleCall):
        outcome = self._outcomes[rc.model_id]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _make_role_call(model_id: str) -> RoleCall:
    return RoleCall(
        call_id=f"call-{model_id}",
        model_id=model_id,
        role=Role.CONTRIBUTOR,
        system_prompt="system",
        messages=[{"role": "user", "content": "question"}],
    )


class TestFanOutWaitAll:
    @pytest.mark.asyncio
    async def test_wait_all_keeps_successes_when_one_contributor_raises(self):
        ok = ModelResponse(
            call_id="call-ok",
            model_id="ok",
            role=Role.CONTRIBUTOR,
            content="answer",
            latency_ms=12,
            success=True,
        )
        engine = FanOutEngine(config=None, adapter=_DummyAdapter({
            "ok": ok,
            "bad": RuntimeError("boom"),
        }), prompts=None)

        results = await engine.fan_out_wait_all([
            _make_role_call("ok"),
            _make_role_call("bad"),
        ])

        assert len(results) == 1
        assert results[0].model_id == "ok"
        assert results[0].success is True

    @pytest.mark.asyncio
    async def test_wait_all_returns_failed_model_responses_when_all_raise(self):
        engine = FanOutEngine(config=None, adapter=_DummyAdapter({
            "a": RuntimeError("boom-a"),
            "b": ValueError("boom-b"),
        }), prompts=None)

        results = await engine.fan_out_wait_all([
            _make_role_call("a"),
            _make_role_call("b"),
        ])

        assert len(results) == 2
        assert all(isinstance(result, ModelResponse) for result in results)
        assert all(result.success is False for result in results)
        assert {result.model_id for result in results} == {"a", "b"}
