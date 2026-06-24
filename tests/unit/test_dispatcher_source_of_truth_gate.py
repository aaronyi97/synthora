"""Dispatcher source-of-truth gate tests.

Locks consistency across:
  - SINGLE_MODEL_RECOMMENDATIONS
  - prompt mapping lines
  - fallback semantics for realtime
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import agoracle.services.companion_dispatcher as dispatcher_module
from agoracle.services.companion_dispatcher import (
    CompanionDispatcher,
    DispatcherInput,
    SINGLE_MODEL_RECOMMENDATIONS,
    _single_model_mapping_prompt_lines,
)


class _AdapterStub:
    def supports_model(self, model_id: str) -> bool:
        return model_id in {"perplexity_sonar_pro", "perplexity_sonar", "kimi"}


def _dispatcher_for_prompt() -> CompanionDispatcher:
    dispatcher = CompanionDispatcher.__new__(CompanionDispatcher)
    dispatcher._failure_monitor = None
    dispatcher._config = SimpleNamespace(models={})
    return dispatcher


def test_realtime_recommendation_order_source_of_truth() -> None:
    assert SINGLE_MODEL_RECOMMENDATIONS["realtime"][:3] == [
        "perplexity_sonar_pro",
        "perplexity_sonar",
        "kimi",
    ]


def test_mapping_prompt_lines_include_realtime_and_match_mapping_order() -> None:
    lines = _single_model_mapping_prompt_lines()
    expected = (
        f"realtime → {', '.join(SINGLE_MODEL_RECOMMENDATIONS['realtime'])}"
        "（新闻/价格/实时数据必须优先搜索模型）"
    )
    assert expected in lines


def test_realtime_fallback_semantics_single_model_light_reason() -> None:
    dispatcher = CompanionDispatcher(config=None, model_adapter=_AdapterStub(), failure_monitor=None)
    output = dispatcher._fallback_route(
        DispatcherInput(question="最新新闻", question_type="realtime")
    )

    assert output.strategy == "single_model"
    assert output.mode == "light"
    assert output.route_reason
    assert "single_model" in output.route_reason
    assert "fallback" in output.route_reason


def test_route_prompt_uses_mapping_output_not_hardcoded() -> None:
    mutated = dict(SINGLE_MODEL_RECOMMENDATIONS)
    mutated["realtime"] = ["kimi", "perplexity_sonar", "perplexity_sonar_pro"]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(dispatcher_module, "SINGLE_MODEL_RECOMMENDATIONS", mutated)
        mapping_lines = dispatcher_module._single_model_mapping_prompt_lines()
        expected_line = (
            "realtime → kimi, perplexity_sonar, perplexity_sonar_pro"
            "（新闻/价格/实时数据必须优先搜索模型）"
        )
        assert expected_line in mapping_lines

        dispatcher = _dispatcher_for_prompt()
        prompt = dispatcher._build_route_system_prompt(
            DispatcherInput(question="latest news", question_type="realtime")
        )

        assert expected_line in prompt
        assert "优先级 kimi, perplexity_sonar, perplexity_sonar_pro" in prompt
