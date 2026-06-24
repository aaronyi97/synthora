from agoracle.services.roundtable_orchestrator import rule_guard


def test_rule_guard_short_decision_question_goes_to_llm_grey_zone() -> None:
    assert rule_guard("该离职吗？") is None


def test_rule_guard_rejects_writing_task() -> None:
    assert rule_guard("帮我写一封辞职信") == "low"


def test_rule_guard_rejects_empty_input() -> None:
    assert rule_guard("   ") == "low"
