"""Tests for the Router rule engine (v2.0: includes Intent + OutputDepth tests)."""

from agoracle.domain.router import route
from agoracle.domain.types import Intent, Mode, OutputDepth


class TestModeSelection:
    """Test mode routing decisions."""

    def test_simple_question_routes_to_light(self):
        result = route("今天天气怎么样")
        assert result.mode == Mode.LIGHT

    def test_translation_routes_to_light(self):
        result = route("翻译这句话：Hello World")
        assert result.mode == Mode.LIGHT

    def test_why_question_routes_to_deep(self):
        result = route("为什么 Python 的 GIL 会影响多线程性能？")
        assert result.mode == Mode.DEEP

    def test_architecture_routes_to_deep(self):
        result = route("帮我设计一个分布式缓存架构")
        assert result.mode == Mode.DEEP

    def test_long_complex_question_routes_to_deep(self):
        long_q = (
            "我想深入了解一下目前主流的分布式数据库在面对 CAP 定理时各自做了什么样的权衡，"
            "比如 Cassandra 选择了 AP 而 HBase 选择了 CP，这些选择在实际生产环境中会带来哪些具体的后果？"
            "如果我们的业务场景是高并发写入但允许短暂的数据不一致，应该优先考虑哪种架构设计方案？"
        )
        result = route(long_q)
        assert result.mode == Mode.DEEP

    def test_long_simple_stays_light(self):
        # Long but simple/repetitive text should NOT trigger Deep
        result = route("x " * 200)  # 400 chars, no complexity
        assert result.mode == Mode.LIGHT

    def test_casual_why_stays_light(self):
        # Casual "为什么" question should not trigger Deep
        result = route("为什么今天心情不好")
        assert result.mode == Mode.LIGHT

    def test_technical_why_routes_to_deep(self):
        # Technical "为什么" question should still trigger Deep
        result = route("为什么分布式系统中的缓存一致性这么难解决")
        assert result.mode == Mode.DEEP

    def test_comparison_routes_to_research(self):
        result = route("对比 React 和 Vue 的优缺点")
        assert result.mode == Mode.RESEARCH

    def test_survey_routes_to_research(self):
        result = route("2026年大模型行业综述")
        assert result.mode == Mode.RESEARCH

    def test_tech_selection_routes_to_research(self):
        result = route("技术选型：PostgreSQL vs MySQL vs MongoDB")
        assert result.mode == Mode.RESEARCH


class TestWebSearchDecision:
    """Test web search enable/disable decisions."""

    def test_realtime_question_enables_search(self):
        result = route("2026年最新的 React 版本是什么")
        assert result.web_search_enabled is True

    def test_code_question_disables_search(self):
        result = route("写一个 Python 快速排序算法")
        assert result.web_search_enabled is False

    def test_math_disables_search(self):
        result = route("证明勾股定理")
        assert result.web_search_enabled is False

    def test_ambiguous_defaults_to_search_on(self):
        result = route("量子计算的前景如何")
        assert result.web_search_enabled is True


class TestCritiqueDecision:
    """Test question critique trigger decisions."""

    def test_deep_always_enables_critique(self):
        result = route("为什么分布式系统需要 CAP 定理")
        assert result.mode == Mode.DEEP
        assert result.critique_enabled is True

    def test_light_rarely_enables_critique(self):
        result = route("什么是 Python")
        assert result.critique_enabled is False

    def test_false_premise_triggers_critique(self):
        result = route("既然 Python 已经被淘汰了，为什么还有人用")
        assert result.critique_enabled is True

    def test_research_with_assumption_triggers_critique(self):
        result = route("假设 AI 取代了所有程序员，研究一下就业市场变化")
        assert result.mode == Mode.RESEARCH
        assert result.critique_enabled is True


class TestIntentDecision:
    """v2.0: Test intent is always ANSWER for auto routing."""

    def test_light_has_answer_intent(self):
        result = route("今天天气怎么样")
        assert result.intent == Intent.ANSWER

    def test_deep_has_answer_intent(self):
        result = route("为什么 Python 的 GIL 会影响多线程性能？")
        assert result.intent == Intent.ANSWER

    def test_research_has_answer_intent(self):
        result = route("对比 React 和 Vue 的优缺点")
        assert result.intent == Intent.ANSWER


class TestOutputDepthBinding:
    """v2.0: Test output depth binds to mode correctly."""

    def test_light_defaults_to_level_1(self):
        result = route("什么是 Python")
        assert result.mode == Mode.LIGHT
        assert result.output_depth == OutputDepth.LEVEL_1

    def test_deep_defaults_to_level_2(self):
        result = route("为什么分布式系统需要 CAP 定理")
        assert result.mode == Mode.DEEP
        assert result.output_depth == OutputDepth.LEVEL_2

    def test_research_defaults_to_level_3(self):
        result = route("对比 React 和 Vue 的优缺点")
        assert result.mode == Mode.RESEARCH
        assert result.output_depth == OutputDepth.LEVEL_3
