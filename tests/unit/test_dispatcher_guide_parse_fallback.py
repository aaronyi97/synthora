import asyncio
import types

from agoracle.services.companion_dispatcher import CompanionDispatcher
from agoracle.services.companion_dispatcher import DispatcherInput, DispatcherOutput


def _dispatcher() -> CompanionDispatcher:
    return CompanionDispatcher.__new__(CompanionDispatcher)


def test_parse_guide_response_recovers_embedded_json() -> None:
    dispatcher = _dispatcher()
    content = (
        "请参考下面 JSON：\n"
        "```json\n"
        "{\"message\":\"专家在关键点上存在分歧，建议深入比较。\",\"action_type\":\"explore_divergence\"}\n"
        "```\n"
        "以上是建议。"
    )

    output = dispatcher._parse_guide_response(content, {"confidence": 0.4, "divergence_count": 2})

    assert output.strategy == "done"
    assert output.companion_message == "专家在关键点上存在分歧，建议深入比较。"
    assert output.suggested_actions
    assert output.suggested_actions[0]["action_type"] == "explore_divergence"


def test_parse_guide_response_preserves_raw_text_when_json_missing() -> None:
    dispatcher = _dispatcher()
    content = "建议你先用 Deep 模式重新分析，再确认是否继续追问。"

    output = dispatcher._parse_guide_response(content, {"confidence": 0.3, "divergence_count": 0})

    assert output.strategy == "done"
    assert output.post_guide_trigger == "fold"
    assert output.companion_message == content
    assert output.suggested_actions
    assert output.suggested_actions[0] == {
        "label": "🔬 用 Deep 重新分析",
        "action_type": "query_deep",
        "action_payload": {"mode": "deep"},
        "estimated_seconds": 120,
        "requires_confirm": False,
    }
    assert output.is_silent_route is False


def test_dispatch_guide_best_single_uses_fast_path_branch() -> None:
    dispatcher = _dispatcher()
    called: list[dict] = []

    async def fake_call(self, result_meta, dispatcher_input):
        called.append(result_meta)
        return DispatcherOutput(
            strategy="done",
            companion_message="这是一条 fast-path guidance",
            suggested_actions=[{
                "label": "🔬 Deep 重新分析",
                "action_type": "query_deep",
                "action_payload": {"mode": "deep"},
                "estimated_seconds": 120,
                "requires_confirm": False,
            }],
            is_silent_route=False,
        )

    dispatcher._call_guide_sonnet = types.MethodType(fake_call, dispatcher)

    output = asyncio.run(dispatcher.dispatch_guide(
        {
            "confidence": 0.92,
            "divergence_count": 0,
            "quality_gate_result": "best_single",
        },
        DispatcherInput(question="什么是缓存穿透？"),
    ))

    assert called, "best_single must enter fast-path guidance instead of silent fold"
    assert output.post_guide_trigger == "fold"
    assert output.companion_message == "这是一条 fast-path guidance"


def test_dispatch_guide_best_single_failure_still_returns_folded_guidance() -> None:
    dispatcher = _dispatcher()

    async def fake_call(self, result_meta, dispatcher_input):
        raise TimeoutError("sonnet timeout")

    dispatcher._call_guide_sonnet = types.MethodType(fake_call, dispatcher)

    output = asyncio.run(dispatcher.dispatch_guide(
        {
            "confidence": 0.88,
            "divergence_count": 0,
            "quality_gate_result": "best_single",
            "fast_path": True,
        },
        DispatcherInput(question="帮我快速解释 CAP 定理"),
    ))

    assert output.post_guide_trigger == "fold"
    assert output.companion_message != ""
    assert output.is_silent_route is False
    assert output.suggested_actions
    assert output.suggested_actions[0]["action_type"] == "query_deep"


def test_dispatch_guide_low_confidence_branch_still_uses_existing_trigger() -> None:
    dispatcher = _dispatcher()

    async def fake_call(self, result_meta, dispatcher_input):
        return DispatcherOutput(
            strategy="done",
            companion_message="这轮信心偏低，建议继续深挖。",
            suggested_actions=[{
                "label": "🔬 Deep 重新分析",
                "action_type": "query_deep",
                "action_payload": {"mode": "deep"},
                "estimated_seconds": 120,
                "requires_confirm": False,
            }],
            is_silent_route=False,
        )

    dispatcher._call_guide_sonnet = types.MethodType(fake_call, dispatcher)

    output = asyncio.run(dispatcher.dispatch_guide(
        {
            "confidence": 0.32,
            "divergence_count": 0,
            "quality_gate_result": "synthesized",
        },
        DispatcherInput(question="这个方案哪里最容易失败？"),
    ))

    assert output.post_guide_trigger == "low_confidence"
    assert output.companion_message == "这轮信心偏低，建议继续深挖。"
