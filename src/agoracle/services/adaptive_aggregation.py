"""
Adaptive Aggregation — question_type-driven synthesis strategy selection.

Maps each QuestionType to a specific aggregation strategy:
  - FACTUAL:        vote-based (majority consensus, accuracy-first)
  - ANALYTICAL:     full debate (MoA + deep refinement, max depth)
  - TECHNICAL:      full debate (same as analytical, code-aware)
  - CONTROVERSIAL:  multi-perspective preservation (no forced consensus)
  - CREATIVE:       best-single selection (aggregation hurts creativity)
  - CULTURAL:       best-single preferred (moa=off; synthesis dilutes cultural nuance)
  - META_COGNITION: best-single preferred (moa=off; preserve original reasoning chain)
  - REASONING:      full debate (same as analytical; deep reasoning benefits from MoA)
  - WRITING:        best-single (prose style cannot be merged; aggregation destroys voice)
  - CODING:         best-single preferred (correctness is binary; synthesis may mix approaches)
  - MATH:           best-single (unique correct answer; synthesis risks introducing errors)
  - UNKNOWN:        default full pipeline

This module provides:
  1. Strategy selection based on question_type
  2. Judge prompt override for each strategy
  3. Quality Gate threshold adjustments
  4. Pipeline parameter tuning (n_of_m, refinement rounds, etc.)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from agoracle.domain.types import QuestionType

logger = logging.getLogger(__name__)


@dataclass
class AggregationStrategy:
    """Configuration for a specific aggregation strategy."""
    name: str
    judge_prompt_key: str          # prompt template name (e.g. "judge_adaptive_factual")
    disable_best_single: bool = False # force synthesis even when one model dominates
    moa_enabled: bool = True          # whether to run MoA Layer 2
    max_refinement_override: Optional[int] = None  # override max_refinement_rounds
    best_single_gap_override: Optional[float] = None  # override gap threshold
    best_single_min_override: Optional[float] = None   # override min score
    exclude_search_contributors: bool = False  # v4.7: exclude search-only models (e.g. Perplexity) from Judge synthesis


# ── Strategy definitions ──────────────────────────────────

STRATEGIES = {
    QuestionType.FACTUAL: AggregationStrategy(
        name="vote",
        judge_prompt_key="judge_adaptive_factual",
        disable_best_single=False,
        moa_enabled=False,               # factual doesn't benefit from MoA
        max_refinement_override=1,        # safety net: 1 critic round to catch all-models-wrong
        best_single_gap_override=0.08,    # v4.0: 0.15→0.08; pairwise Path1c needs gap≤0.25 to fire
        best_single_min_override=0.6,
    ),
    QuestionType.ANALYTICAL: AggregationStrategy(
        name="debate",
        judge_prompt_key="judge_deep",    # full depth, use existing deep prompt
        disable_best_single=False,
        moa_enabled=True,                 # MoA adds value for analytical
        max_refinement_override=None,     # use config default
        # v4.27 S1: 0.5→0.25, 0.85→0.70 — pairwise 赢3/4场(0.75)vs第二(0.50)=gap0.25≥阈値即触发
        # 原0.5几乎不可能触发，导致Gate强行综合已经很好的单模型答案，是稿释核心根因
        best_single_gap_override=0.25,
        best_single_min_override=0.70,
    ),
    QuestionType.TECHNICAL: AggregationStrategy(
        name="debate",
        judge_prompt_key="judge_deep",    # same as analytical
        disable_best_single=False,
        moa_enabled=True,
        max_refinement_override=None,
        best_single_gap_override=0.25,    # v4.27 S1: 0.5→0.25
        best_single_min_override=0.70,    # v4.27 S1: 0.85→0.70
    ),
    QuestionType.CONTROVERSIAL: AggregationStrategy(
        name="multi_perspective",
        judge_prompt_key="judge_adaptive_controversial",
        disable_best_single=False,
        moa_enabled=False,                # MoA converges, bad for controversial
        max_refinement_override=1,        # one round to polish, not converge
        # v4.27 S1: 0.5→0.25 — controversial 仍偏好综合，但当一个模型明显更全面时允许直接采用
        best_single_gap_override=0.25,
        best_single_min_override=0.70,
    ),
    QuestionType.CREATIVE: AggregationStrategy(
        name="best_single",
        judge_prompt_key="judge_adaptive_creative",
        disable_best_single=False,
        moa_enabled=False,                # MoA kills creativity
        max_refinement_override=0,        # no refinement — preserve raw creativity
        best_single_gap_override=0.1,     # very low gap → prefer best single
        best_single_min_override=0.5,
    ),
    QuestionType.CULTURAL: AggregationStrategy(
        name="best_single_preferred",
        judge_prompt_key="judge_adaptive_factual",  # accuracy-first; no cultural-specific prompt yet
        disable_best_single=False,
        moa_enabled=False,                # MoA converges cultural nuance into bland consensus
        max_refinement_override=0,        # no refinement — preserve raw cultural perspective
        best_single_gap_override=0.06,    # v4.0: 0.12→0.06; pairwise Path1c: gap(0.75-0.50)=0.25≥0.06
        best_single_min_override=0.55,
    ),
    QuestionType.META_COGNITION: AggregationStrategy(
        name="best_single_preferred",
        judge_prompt_key="judge_adaptive_factual",  # accuracy-first; meta questions need coherent chain
        disable_best_single=False,
        moa_enabled=False,                # MoA fragments the reasoning chain
        max_refinement_override=1,        # one critic round — meta questions benefit from depth check
        best_single_gap_override=0.15,    # same as FACTUAL
        best_single_min_override=0.6,
    ),
    QuestionType.REASONING: AggregationStrategy(
        name="debate",
        judge_prompt_key="judge_deep",    # same as ANALYTICAL — full depth
        disable_best_single=False,
        moa_enabled=True,                 # MoA adds value: cross-checking logical steps
        max_refinement_override=None,     # use config default
        best_single_gap_override=0.5,     # high bar → synthesis preferred
        best_single_min_override=0.85,
        exclude_search_contributors=True, # v4.7: search answers dilute reasoning quality
    ),
    QuestionType.WRITING: AggregationStrategy(
        name="best_single",
        judge_prompt_key="judge_adaptive_creative",  # reuse creative prompt: style-preserving selection
        disable_best_single=False,
        moa_enabled=False,                # MoA destroys prose voice and style
        max_refinement_override=0,        # no refinement — preserve raw writing quality
        best_single_gap_override=0.08,    # very low gap → adopt best single extremely easily
        best_single_min_override=0.45,    # low bar — any decent response wins
    ),
    QuestionType.CODING: AggregationStrategy(
        name="best_single_preferred",
        judge_prompt_key="judge_adaptive_factual",  # correctness-first evaluation
        disable_best_single=False,
        moa_enabled=False,                # mixing code approaches produces broken hybrids
        max_refinement_override=1,        # one critic round to catch bugs/errors
        best_single_gap_override=0.12,    # low gap → prefer best single
        best_single_min_override=0.55,
        exclude_search_contributors=True, # v4.7: search answers dilute code correctness
    ),
    QuestionType.MATH: AggregationStrategy(
        name="best_single",
        judge_prompt_key="judge_adaptive_factual",  # correctness-first; math has unique answer
        disable_best_single=False,
        moa_enabled=False,                # synthesis risks averaging correct and wrong steps
        max_refinement_override=0,        # no refinement — correct answer is correct
        best_single_gap_override=0.10,    # very low gap → adopt best single
        best_single_min_override=0.50,
        exclude_search_contributors=True, # v4.7: search answers dilute math correctness
    ),
    QuestionType.REALTIME: AggregationStrategy(
        name="best_single",
        judge_prompt_key="judge_adaptive_factual",  # accuracy-first; realtime = factual + recency
        disable_best_single=False,
        moa_enabled=False,                # aggregation adds no value for live data
        max_refinement_override=0,        # no refinement — freshness matters more than polish
        best_single_gap_override=0.15,    # larger gap required before taking best single immediately
        best_single_min_override=0.55,    # raise floor so weak realtime answers do not win too early
        exclude_search_contributors=False,  # MUST NOT exclude search models — they are the point
    ),
    QuestionType.UNKNOWN: AggregationStrategy(
        name="default",
        judge_prompt_key="",              # empty = use mode default (judge_deep/judge_research)
        disable_best_single=False,
        moa_enabled=True,
        max_refinement_override=None,
    ),
}


def get_strategy(question_type: QuestionType) -> AggregationStrategy:
    """Get the aggregation strategy for a given question type."""
    return STRATEGIES.get(question_type, STRATEGIES[QuestionType.UNKNOWN])


def apply_strategy(
    strategy: AggregationStrategy,
    mode_config,     # ModeConfig (avoid circular import)
    judge_config,    # JudgeConfig
    context_query_id: str = "",
) -> dict:
    """
    Apply aggregation strategy overrides to mode/judge config.

    Returns a dict of overrides that the orchestrator should apply.
    Does NOT mutate the original configs — returns override values.
    """
    overrides = {
        "judge_prompt_key": strategy.judge_prompt_key,
        "moa_enabled": strategy.moa_enabled,
        "disable_best_single": strategy.disable_best_single,
        "strategy_name": strategy.name,  # v4.27: include name so Layer1 logs real strategy, not always "default"
    }

    if strategy.exclude_search_contributors:
        overrides["exclude_search_contributors"] = True

    if strategy.max_refinement_override is not None:
        overrides["max_refinement_rounds"] = strategy.max_refinement_override

    if strategy.best_single_gap_override is not None:
        overrides["best_single_gap_threshold"] = strategy.best_single_gap_override

    if strategy.best_single_min_override is not None:
        overrides["best_single_min_score"] = strategy.best_single_min_override

    logger.info(
        f"[{context_query_id}] Adaptive aggregation: "
        f"strategy={strategy.name}, "
        f"judge_prompt={strategy.judge_prompt_key or 'default'}, "
        f"moa={'on' if strategy.moa_enabled else 'off'}, "
        f"refine={strategy.max_refinement_override if strategy.max_refinement_override is not None else 'default'}"
    )

    return overrides
