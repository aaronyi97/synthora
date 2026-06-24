"""
Targeted regression tests for all quality improvements made on 2026-02-22.

Covers:
  P0-1  adaptive_aggregation: CULTURAL/META_COGNITION/REASONING strategies
  P0-2  query_monitor: Layer2 winner normalization Alpha/Beta + A/B
  P0-2b analyze_monitor: fallback _resolve_winner Alpha/Beta + A/B
  P0-3  benchmark_quality: use_router default = True
  P1-4  (prompt-only, not unit-testable here)
  P1-5  (prompt-only, not unit-testable here)
  #1    router: WRITING/CODING/MATH signal-word classification
  #2    types: WRITING/CODING/MATH QuestionType enum values exist
  #2    adaptive_aggregation: WRITING/CODING/MATH strategies correct
  #3    router: classify_question_type_async exists and is callable
  #4    orchestrator: classify_question_type_async imported correctly
"""

import asyncio
import pytest

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _resolve_winner_from_analyze_monitor(ev: dict) -> str:
    """Inline copy of analyze_monitor._resolve_winner for isolated testing."""
    if ev.get("winner_normalized"):
        return ev["winner_normalized"]
    raw = (ev.get("winner") or "tie").strip().lower()
    swapped = ev.get("swapped", False)
    if raw == "tie":
        return "tie"
    elif raw in ("alpha", "a"):
        return "best_single" if swapped else "aggregated"
    elif raw in ("beta", "b"):
        return "aggregated" if swapped else "best_single"
    return "tie"


# ═══════════════════════════════════════════════════════════════
# P0-1: adaptive_aggregation — new strategies for CULTURAL/META/REASONING
# ═══════════════════════════════════════════════════════════════

class TestP01NewStrategies:
    """P0-1: CULTURAL, META_COGNITION, REASONING must have explicit strategies."""

    def test_cultural_strategy_exists(self):
        from agoracle.services.adaptive_aggregation import get_strategy
        from agoracle.domain.types import QuestionType
        s = get_strategy(QuestionType.CULTURAL)
        assert s.name == "best_single_preferred"

    def test_cultural_moa_disabled(self):
        from agoracle.services.adaptive_aggregation import get_strategy
        from agoracle.domain.types import QuestionType
        s = get_strategy(QuestionType.CULTURAL)
        assert s.moa_enabled is False, "CULTURAL must not use MoA (synthesis dilutes nuance)"

    def test_cultural_low_gap_threshold(self):
        from agoracle.services.adaptive_aggregation import get_strategy
        from agoracle.domain.types import QuestionType
        s = get_strategy(QuestionType.CULTURAL)
        assert s.best_single_gap_override is not None
        assert s.best_single_gap_override <= 0.15, "CULTURAL should trigger BEST_SINGLE easily"

    def test_meta_cognition_strategy_exists(self):
        from agoracle.services.adaptive_aggregation import get_strategy
        from agoracle.domain.types import QuestionType
        s = get_strategy(QuestionType.META_COGNITION)
        assert s.name == "best_single_preferred"

    def test_meta_cognition_moa_disabled(self):
        from agoracle.services.adaptive_aggregation import get_strategy
        from agoracle.domain.types import QuestionType
        s = get_strategy(QuestionType.META_COGNITION)
        assert s.moa_enabled is False, "META_COGNITION must not use MoA (fragments reasoning chain)"

    def test_reasoning_strategy_exists(self):
        from agoracle.services.adaptive_aggregation import get_strategy
        from agoracle.domain.types import QuestionType
        s = get_strategy(QuestionType.REASONING)
        assert s.name == "debate"

    def test_reasoning_moa_enabled(self):
        from agoracle.services.adaptive_aggregation import get_strategy
        from agoracle.domain.types import QuestionType
        s = get_strategy(QuestionType.REASONING)
        assert s.moa_enabled is True, "REASONING benefits from MoA cross-checking"

    def test_no_unknown_fallback_for_cultural_meta_reasoning(self):
        """These types must NOT fall back to UNKNOWN strategy."""
        from agoracle.services.adaptive_aggregation import STRATEGIES, get_strategy
        from agoracle.domain.types import QuestionType
        unknown_strategy = STRATEGIES[QuestionType.UNKNOWN]
        for qt in (QuestionType.CULTURAL, QuestionType.META_COGNITION, QuestionType.REASONING):
            s = get_strategy(qt)
            assert s is not unknown_strategy, f"{qt.value} must not use UNKNOWN fallback"
            assert s.name != "default", f"{qt.value} must not use default strategy"


# ═══════════════════════════════════════════════════════════════
# P0-2: query_monitor — Layer2 winner normalization Alpha/Beta + A/B
# ═══════════════════════════════════════════════════════════════

class TestP02WinnerNormalization:
    """P0-2: winner_normalized must handle Alpha/Beta AND A/B, with swap logic."""

    def _normalize(self, raw_winner: str, swap: bool) -> str:
        """Replicate query_monitor normalization logic for isolated testing."""
        raw_winner_norm = raw_winner.strip().lower()
        if raw_winner_norm == "tie":
            return "tie"
        elif raw_winner_norm in ("alpha", "a"):
            return "best_single" if swap else "aggregated"
        elif raw_winner_norm in ("beta", "b"):
            return "aggregated" if swap else "best_single"
        else:
            return "tie"

    # ── No swap (label_a=Alpha=aggregated, label_b=Beta=best_single) ──
    def test_alpha_no_swap_is_aggregated(self):
        assert self._normalize("Alpha", swap=False) == "aggregated"

    def test_beta_no_swap_is_best_single(self):
        assert self._normalize("Beta", swap=False) == "best_single"

    def test_a_no_swap_is_aggregated(self):
        assert self._normalize("A", swap=False) == "aggregated"

    def test_b_no_swap_is_best_single(self):
        assert self._normalize("B", swap=False) == "best_single"

    # ── With swap (label_a=Beta=best_single, label_b=Alpha=aggregated) ──
    def test_alpha_with_swap_is_best_single(self):
        assert self._normalize("Alpha", swap=True) == "best_single"

    def test_beta_with_swap_is_aggregated(self):
        assert self._normalize("Beta", swap=True) == "aggregated"

    def test_a_with_swap_is_best_single(self):
        assert self._normalize("A", swap=True) == "best_single"

    def test_b_with_swap_is_aggregated(self):
        assert self._normalize("B", swap=True) == "aggregated"

    # ── Tie ──
    def test_tie_no_swap(self):
        assert self._normalize("tie", swap=False) == "tie"

    def test_tie_with_swap(self):
        assert self._normalize("tie", swap=True) == "tie"

    # ── Case insensitivity ──
    def test_alpha_lowercase(self):
        assert self._normalize("alpha", swap=False) == "aggregated"

    def test_ALPHA_uppercase(self):
        assert self._normalize("ALPHA", swap=False) == "aggregated"

    # ── Unknown label falls back to tie ──
    def test_unknown_label_is_tie(self):
        assert self._normalize("C", swap=False) == "tie"
        assert self._normalize("unknown", swap=False) == "tie"


# ═══════════════════════════════════════════════════════════════
# P0-2b: analyze_monitor — _resolve_winner fallback logic
# ═══════════════════════════════════════════════════════════════

class TestP02bAnalyzeMonitorFallback:
    """P0-2b: analyze_monitor fallback must handle Alpha/Beta AND A/B for legacy records."""

    def test_winner_normalized_takes_priority(self):
        ev = {"winner_normalized": "aggregated", "winner": "B", "swapped": False}
        assert _resolve_winner_from_analyze_monitor(ev) == "aggregated"

    def test_alpha_no_swap_fallback(self):
        ev = {"winner": "Alpha", "swapped": False}
        assert _resolve_winner_from_analyze_monitor(ev) == "aggregated"

    def test_beta_no_swap_fallback(self):
        ev = {"winner": "Beta", "swapped": False}
        assert _resolve_winner_from_analyze_monitor(ev) == "best_single"

    def test_a_no_swap_fallback(self):
        ev = {"winner": "A", "swapped": False}
        assert _resolve_winner_from_analyze_monitor(ev) == "aggregated"

    def test_b_no_swap_fallback(self):
        ev = {"winner": "B", "swapped": False}
        assert _resolve_winner_from_analyze_monitor(ev) == "best_single"

    def test_alpha_with_swap_fallback(self):
        ev = {"winner": "Alpha", "swapped": True}
        assert _resolve_winner_from_analyze_monitor(ev) == "best_single"

    def test_beta_with_swap_fallback(self):
        ev = {"winner": "Beta", "swapped": True}
        assert _resolve_winner_from_analyze_monitor(ev) == "aggregated"

    def test_tie_fallback(self):
        ev = {"winner": "tie", "swapped": False}
        assert _resolve_winner_from_analyze_monitor(ev) == "tie"

    def test_missing_winner_defaults_to_tie(self):
        ev = {"swapped": False}
        assert _resolve_winner_from_analyze_monitor(ev) == "tie"


# ═══════════════════════════════════════════════════════════════
# P0-3: benchmark_quality — use_router default = True
# ═══════════════════════════════════════════════════════════════

class TestP03BenchmarkRouterDefault:
    """P0-3: QualityBenchmark must default use_router=True."""

    def test_use_router_default_is_true(self):
        import inspect
        from scripts.benchmark_quality import QualityBenchmark
        sig = inspect.signature(QualityBenchmark.__init__)
        default = sig.parameters["use_router"].default
        assert default is True, f"use_router default should be True, got {default!r}"


# ═══════════════════════════════════════════════════════════════
# #2: types.py — WRITING/CODING/MATH QuestionType enum values
# ═══════════════════════════════════════════════════════════════

class TestNewQuestionTypeEnums:
    """New QuestionType values must exist with correct string values."""

    def test_writing_enum_exists(self):
        from agoracle.domain.types import QuestionType
        assert QuestionType.WRITING.value == "writing"

    def test_coding_enum_exists(self):
        from agoracle.domain.types import QuestionType
        assert QuestionType.CODING.value == "coding"

    def test_math_enum_exists(self):
        from agoracle.domain.types import QuestionType
        assert QuestionType.MATH.value == "math"

    def test_all_types_have_strategies(self):
        """Every QuestionType must have an explicit strategy (no silent UNKNOWN fallback)."""
        from agoracle.domain.types import QuestionType
        from agoracle.services.adaptive_aggregation import STRATEGIES, get_strategy
        unknown_strategy = STRATEGIES[QuestionType.UNKNOWN]
        for qt in QuestionType:
            if qt == QuestionType.UNKNOWN:
                continue
            s = get_strategy(qt)
            assert s is not unknown_strategy, (
                f"{qt.value} falls back to UNKNOWN strategy — add an explicit entry"
            )


# ═══════════════════════════════════════════════════════════════
# #2: adaptive_aggregation — WRITING/CODING/MATH strategies
# ═══════════════════════════════════════════════════════════════

class TestNewTypeStrategies:
    """WRITING/CODING/MATH must have correct strategy configurations."""

    def test_writing_is_best_single(self):
        from agoracle.services.adaptive_aggregation import get_strategy
        from agoracle.domain.types import QuestionType
        s = get_strategy(QuestionType.WRITING)
        assert s.name == "best_single"
        assert s.moa_enabled is False
        assert s.max_refinement_override == 0

    def test_writing_very_low_gap(self):
        from agoracle.services.adaptive_aggregation import get_strategy
        from agoracle.domain.types import QuestionType
        s = get_strategy(QuestionType.WRITING)
        assert s.best_single_gap_override is not None
        assert s.best_single_gap_override <= 0.10, "WRITING should adopt best single very easily"

    def test_coding_moa_disabled(self):
        from agoracle.services.adaptive_aggregation import get_strategy
        from agoracle.domain.types import QuestionType
        s = get_strategy(QuestionType.CODING)
        assert s.moa_enabled is False, "CODING must not use MoA (mixing code approaches breaks output)"

    def test_coding_has_one_critic_round(self):
        from agoracle.services.adaptive_aggregation import get_strategy
        from agoracle.domain.types import QuestionType
        s = get_strategy(QuestionType.CODING)
        assert s.max_refinement_override == 1, "CODING needs 1 critic round to catch bugs"

    def test_math_is_best_single(self):
        from agoracle.services.adaptive_aggregation import get_strategy
        from agoracle.domain.types import QuestionType
        s = get_strategy(QuestionType.MATH)
        assert s.name == "best_single"
        assert s.moa_enabled is False

    def test_math_no_refinement(self):
        from agoracle.services.adaptive_aggregation import get_strategy
        from agoracle.domain.types import QuestionType
        s = get_strategy(QuestionType.MATH)
        assert s.max_refinement_override == 0, "MATH has unique answer, no refinement needed"

    def test_math_very_low_gap(self):
        from agoracle.services.adaptive_aggregation import get_strategy
        from agoracle.domain.types import QuestionType
        s = get_strategy(QuestionType.MATH)
        assert s.best_single_gap_override is not None
        assert s.best_single_gap_override <= 0.12


# ═══════════════════════════════════════════════════════════════
# #1: router — signal-word classification for new types
# ═══════════════════════════════════════════════════════════════

class TestNewTypeSignalWords:
    """Signal-word classifier must route new question types correctly."""

    def _classify(self, question: str):
        from agoracle.domain.router import _classify_question_type_signals
        return _classify_question_type_signals(question)

    # ── WRITING ──
    def test_email_writing_is_writing(self):
        qt = self._classify("帮我写一封邮件给客户")
        assert qt.value == "writing", f"Expected writing, got {qt.value}"

    def test_polish_article_is_writing(self):
        qt = self._classify("帮我润色这篇文章")
        assert qt.value == "writing", f"Expected writing, got {qt.value}"

    def test_translate_text_is_writing(self):
        qt = self._classify("帮我翻译成英文：你好世界")
        assert qt.value == "writing", f"Expected writing, got {qt.value}"

    def test_draft_report_is_writing(self):
        qt = self._classify("帮我起草一份项目报告")
        assert qt.value == "writing", f"Expected writing, got {qt.value}"

    # ── CODING ──
    def test_write_function_is_coding(self):
        qt = self._classify("写一个Python函数实现快速排序")
        assert qt.value == "coding", f"Expected coding, got {qt.value}"

    def test_debug_code_is_coding(self):
        qt = self._classify("这段代码报错了，帮我debug一下")
        assert qt.value == "coding", f"Expected coding, got {qt.value}"

    def test_implement_feature_is_coding(self):
        qt = self._classify("实现一个用户登录功能")
        assert qt.value == "coding", f"Expected coding, got {qt.value}"

    def test_sql_query_is_coding(self):
        qt = self._classify("写一个SQL查询统计每月销售额")
        assert qt.value == "coding", f"Expected coding, got {qt.value}"

    def test_analytical_realtime_like_question_is_not_realtime(self):
        from agoracle.domain.types import QuestionType

        qt = self._classify("请根据2026年中国全国两会的政府工作报告及相关政策文件，分析其核心政策导向、经济目标及重点改革领域。")
        assert qt != QuestionType.REALTIME

    def test_simple_realtime_question_still_routes_to_realtime(self):
        from agoracle.domain.types import QuestionType

        qt = self._classify("今天北京天气怎么样")
        assert qt == QuestionType.REALTIME

    # ── MATH ──
    def test_calculate_probability_is_math(self):
        qt = self._classify("求两个骰子点数之和为7的概率是多少")
        assert qt.value == "math", f"Expected math, got {qt.value}"

    def test_solve_equation_is_math(self):
        qt = self._classify("解方程 x^2 - 5x + 6 = 0")
        assert qt.value == "math", f"Expected math, got {qt.value}"

    def test_prove_theorem_is_math(self):
        qt = self._classify("证明勾股定理")
        assert qt.value == "math", f"Expected math, got {qt.value}"

    def test_arithmetic_is_math(self):
        qt = self._classify("计算 123 * 456 的结果")
        assert qt.value == "math", f"Expected math, got {qt.value}"

    # ── Existing types not broken ──
    def test_cultural_still_works(self):
        qt = self._classify("中国春节有哪些传统习俗")
        assert qt.value == "cultural", f"Expected cultural, got {qt.value}"

    def test_technical_still_works(self):
        qt = self._classify("Redis 缓存穿透的解决方案")
        assert qt.value == "technical", f"Expected technical, got {qt.value}"

    def test_creative_still_works(self):
        qt = self._classify("写一首关于秋天的诗")
        assert qt.value == "creative", f"Expected creative, got {qt.value}"


# ═══════════════════════════════════════════════════════════════
# #3: router — classify_question_type_async exists and is callable
# ═══════════════════════════════════════════════════════════════

class TestLLMRouterInterface:
    """LLM zero-shot router must be importable, confidence-aware, and fall back correctly."""

    def test_classify_question_type_async_importable(self):
        from agoracle.domain.router import classify_question_type_async
        assert callable(classify_question_type_async)

    def test_classify_question_type_async_is_coroutine(self):
        import inspect
        from agoracle.domain.router import classify_question_type_async
        assert inspect.iscoroutinefunction(classify_question_type_async)

    def test_min_confidence_threshold_value(self):
        """Confidence threshold must be 0.70."""
        from agoracle.domain.router import _LLM_CLASSIFY_MIN_CONFIDENCE
        assert _LLM_CLASSIFY_MIN_CONFIDENCE == 0.70

    def test_llm_classify_fallback_when_no_env(self):
        """When env vars missing, LLM call returns None → signal-word fallback runs."""
        import os
        from agoracle.domain.router import classify_question_type_async
        from agoracle.domain.types import QuestionType

        old_key = os.environ.pop("GEMINI_FLASH_API_KEY", None)
        old_url = os.environ.pop("GEMINI_FLASH_BASE_URL", None)
        try:
            result = asyncio.get_event_loop().run_until_complete(
                classify_question_type_async("写一个Python函数实现链表反转")
            )
            assert isinstance(result, QuestionType)
            assert result == QuestionType.CODING
        finally:
            if old_key is not None:
                os.environ["GEMINI_FLASH_API_KEY"] = old_key
            if old_url is not None:
                os.environ["GEMINI_FLASH_BASE_URL"] = old_url

    def test_confidence_aware_high_confidence_overrides_signal(self):
        """High-confidence LLM result must override signal-word result."""
        import asyncio as _asyncio
        from unittest.mock import AsyncMock, patch
        from agoracle.domain.router import classify_question_type_async
        from agoracle.domain.types import QuestionType

        async def _run():
            with patch(
                "agoracle.domain.router._llm_classify_question_type_async",
                new=AsyncMock(return_value=(QuestionType.CULTURAL, 0.95)),
            ):
                # Signal-word would classify this as ANALYTICAL ("为什么")
                # LLM says CULTURAL with 0.95 confidence → LLM wins
                result = await classify_question_type_async(
                    "为什么中国传统文化在现代社会仍有影响"
                )
                return result

        result = _asyncio.get_event_loop().run_until_complete(_run())
        assert result == QuestionType.CULTURAL

    def test_confidence_aware_low_confidence_keeps_signal(self):
        """Low-confidence LLM result must NOT override signal-word result."""
        import asyncio as _asyncio
        from unittest.mock import AsyncMock, patch
        from agoracle.domain.router import classify_question_type_async
        from agoracle.domain.types import QuestionType

        async def _run():
            with patch(
                "agoracle.domain.router._llm_classify_question_type_async",
                new=AsyncMock(return_value=(QuestionType.CULTURAL, 0.55)),
            ):
                # LLM says CULTURAL but only 0.55 confidence → signal-word wins
                result = await classify_question_type_async(
                    "写一个Python函数实现链表反转"
                )
                return result

        result = _asyncio.get_event_loop().run_until_complete(_run())
        assert result == QuestionType.CODING  # signal-word result preserved

    def test_confidence_aware_llm_none_keeps_signal(self):
        """LLM returning None must keep signal-word result."""
        import asyncio as _asyncio
        from unittest.mock import AsyncMock, patch
        from agoracle.domain.router import classify_question_type_async
        from agoracle.domain.types import QuestionType

        async def _run():
            with patch(
                "agoracle.domain.router._llm_classify_question_type_async",
                new=AsyncMock(return_value=None),
            ):
                result = await classify_question_type_async("写一个Python函数实现链表反转")
                return result

        result = _asyncio.get_event_loop().run_until_complete(_run())
        assert result == QuestionType.CODING

    def test_signal_word_fallback_covers_all_new_types(self):
        """Signal-word fallback must classify all new types without returning UNKNOWN."""
        from agoracle.domain.router import _classify_question_type_signals
        from agoracle.domain.types import QuestionType

        cases = [
            ("帮我写一封邮件给客户", QuestionType.WRITING),
            ("写一个Python函数实现链表反转", QuestionType.CODING),
            ("求方程 x^2 - 5x + 6 = 0 的解", QuestionType.MATH),
        ]
        for question, expected in cases:
            result = _classify_question_type_signals(question)
            assert result == expected, f"Q={question!r}: expected {expected.value}, got {result.value}"


# ═══════════════════════════════════════════════════════════════
# #4: orchestrator — imports classify_question_type_async
# ═══════════════════════════════════════════════════════════════

class TestOrchestratorIntegration:
    """Orchestrator must import classify_question_type_async correctly."""

    def test_orchestrator_imports_llm_classifier(self):
        import agoracle.services.orchestrator as orch_module
        assert hasattr(orch_module, "classify_question_type_async") or \
               "classify_question_type_async" in dir(orch_module), \
               "orchestrator must import classify_question_type_async"

    def test_all_question_types_complete_coverage(self):
        """Sanity: every QuestionType has a strategy, no silent fallback."""
        from agoracle.domain.types import QuestionType
        from agoracle.services.adaptive_aggregation import STRATEGIES
        for qt in QuestionType:
            assert qt in STRATEGIES, f"QuestionType.{qt.name} missing from STRATEGIES dict"
