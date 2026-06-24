"""
Orchestrator — the core pipeline that executes multi-model queries.

Implements three answer modes (Light, Deep, Research) with:
  - Parallel fan-out to contributors
  - N-of-M strategy (Light mode: wait for fastest N of M)
  - Judge synthesis + parallel Metadata extraction
  - Quality Gate
  - Answer Critic + Judge refinement (Deep mode only)
  - Companion Dispatcher guidance for all modes
  - Event emission for side effects

Post-answer guidance is unified on Dispatcher. Legacy `next_steps` and
`companion_guide` are compatibility projections derived from canonical
`GuidanceOutput`; the old NSG engine is retired.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import random
import time
from dataclasses import replace as dc_replace
from datetime import datetime
from typing import Any

from agoracle.adapters.judge.llm_judge import LLMJudge
from agoracle.adapters.judge.metadata_extractor import LLMMetadataExtractor
from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter
from agoracle.config.schema import AppConfig, JudgeConfig, ModeConfig
from agoracle.domain.events import ModelCallFailed, QueryCompleted
from agoracle.domain.quality_gate import (
    QualityGateThresholds,
    compute_score_gap,
    compute_scores,
    evaluate_gate,
    get_best_response,
    should_trigger_answer_critic,
)
from agoracle.domain.types import (
    GuidanceOutput,
    GuidanceIntensity,
    GuidanceSuggestion,
    JudgeSynthesis,
    MetadataExtraction,
    Mode,
    ModelResponse,
    OutputDepth,
    QualityGateResult,
    QueryContext,
    QueryResult,
    QuestionCritique,
    QuestionType,
    Role,
    RoleCall,
)

# v4.26: Question types that trigger Planner-lite (research mode only)
# Added TECHNICAL: benefits from multi-dimensional search queries.
# FACTUAL excluded by test contract (test_phase45.py:70) — pending benchmark validation.
# CREATIVE/WRITING/MATH/CODING excluded — question decomposition adds no value there.
_PLANNER_ELIGIBLE_TYPES = {
    QuestionType.ANALYTICAL,
    QuestionType.REASONING,
    QuestionType.CONTROVERSIAL,
    QuestionType.TECHNICAL,   # v4.26: benefits from multi-dimensional search queries
}
from agoracle.domain.router import enrich_routing_log, classify_question_type_async
from agoracle.services.conversation_memory import ConversationMemoryService
from agoracle.services.divergence_analyzer import DivergenceAnalyzer
from agoracle.services.fact_checker import FactChecker
from agoracle.services.event_bus import EventBus
from agoracle.services.adaptive_aggregation import apply_strategy, get_strategy
from agoracle.services.fan_out import FanOutEngine
from agoracle.services.prompt_loader import PromptLoader
from agoracle.services.refinement import RefinementEngine
from agoracle.services.search_service import SearchService
from agoracle.services.query_monitor import write_layer1, run_layer2_async
from agoracle.services.companion_dispatcher import (
    CompanionDispatcher,
    DispatcherInput,
    DispatcherOutput,
)

logger = logging.getLogger(__name__)


def _parse_fenced_json(text: str) -> Any:
    """Strip optional ```json...``` fence and parse JSON.

    v2.8.8: Unified helper replacing 3 identical inline patterns in orchestrator.
    Raises json.JSONDecodeError on invalid JSON (caller handles).
    """
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


# ────────────────────────────────────────────────────
# Judge quality monitoring — JSONL log for gate decisions (v2.7.9d)
# Enables post-hoc analysis of Judge accuracy & BEST_SINGLE override rate.
# ────────────────────────────────────────────────────
import os as _os
from pathlib import Path as _Path

_JUDGE_LOG_PATH = _Path(_os.getenv(
    "JUDGE_LOG_PATH",
    "data/logs/judge_decisions.jsonl",
))

_DISPATCHER_LOG_PATH = _Path(_os.getenv(
    "DISPATCHER_LOG_PATH",
    "data/logs/dispatcher_decisions.jsonl",
))


def _responses_convergence(responses: list) -> float:
    """Compute median pairwise char-bigram Jaccard similarity among responses.

    v3.8: Used to conditionally skip MoA Layer 2 when contributors already
    agree — running MoA on convergent answers wastes cost and risks
    consensus trap (anchoring effect, Perez et al. 2023).

    Returns 0.0 (completely different) to 1.0 (identical).
    Threshold ~0.60 indicates high convergence for Chinese/English text.
    """
    contents = [r.content for r in responses if getattr(r, 'success', False) and getattr(r, 'content', '')]
    if len(contents) < 3:
        return 0.0  # too few to judge convergence reliably

    def _bigrams(text: str) -> set:
        return {text[i:i + 2] for i in range(len(text) - 1)} if len(text) >= 2 else set()

    bg_sets = [_bigrams(c) for c in contents]
    sims = []
    for i in range(len(bg_sets)):
        for j in range(i + 1, len(bg_sets)):
            inter = len(bg_sets[i] & bg_sets[j])
            union = len(bg_sets[i] | bg_sets[j])
            sims.append(inter / union if union else 0.0)

    if not sims:
        return 0.0
    sims.sort()
    return sims[len(sims) // 2]


def _log_judge_decision(
    query_id: str,
    mode: str,
    gate_result: str,
    confidence: float,
    best_single_model: str = "",
    best_single_score: float = 0.0,
    score_gap: float = 0.0,
    contributor_count: int = 0,
    question_type: str = "",
    was_refined: bool = False,
) -> None:
    """Append a quality gate decision record to the JSONL log."""
    import json as _json
    try:
        _JUDGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now().isoformat(),
            "query_id": query_id,
            "mode": mode,
            "gate_result": gate_result,
            "confidence": round(confidence, 3),
            "best_single_model": best_single_model,
            "best_single_score": round(best_single_score, 3),
            "score_gap": round(score_gap, 3),
            "contributor_count": contributor_count,
            "question_type": question_type,
            "was_refined": was_refined,
        }
        with open(_JUDGE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(_json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug(f"Judge decision log write failed: {e}")  # #24: never silent


# ────────────────────────────────────────────────────────
# Cost estimation — config-driven per-model pricing (v2.5.7)
# Uses ModelConfig.cost_per_1m_input / cost_per_1m_output from config.yaml.
# Fallback: blended $5/1M tokens for unknown models.
# ────────────────────────────────────────────────────────
_DEFAULT_COST_PER_1M = 5.0  # conservative default for unknown models

# Fallback confidence when Extractor fails or returns no evaluations (fast path)
EXTRACTOR_FALLBACK_CONFIDENCE = 0.65


def _estimate_cost(
    responses: list[ModelResponse],
    extra_tokens: int = 0,
    model_configs: dict | None = None,
    named_calls: list[tuple[str, int, int]] | None = None,
) -> float:
    """Estimate total cost in USD from token usage across all model calls.

    Args:
        responses: Contributor ModelResponse list (fan-out results).
        extra_tokens: Legacy extra token count (critic/refine, unknown model).
        model_configs: Per-model pricing config dict.
        named_calls: Additional calls not in responses, as list of
            (model_id, prompt_tokens, completion_tokens) tuples.
            Use for Judge, Extractor, semantic-check, preflight, etc.
    """
    def _call_cost(model_id: str, prompt_tok: int, completion_tok: int) -> float:
        mc = model_configs.get(model_id) if model_configs else None
        if mc and (mc.cost_per_1m_input > 0 or mc.cost_per_1m_output > 0):
            return (
                prompt_tok * mc.cost_per_1m_input / 1_000_000
                + completion_tok * mc.cost_per_1m_output / 1_000_000
            )
        tokens = prompt_tok + completion_tok
        return tokens * _DEFAULT_COST_PER_1M / 1_000_000

    total = 0.0
    for r in responses:
        total += _call_cost(r.model_id, r.prompt_tokens, r.completion_tokens)
    # Named calls: Judge, Extractor, semantic-check, preflight, critic, refine
    for model_id, prompt_tok, completion_tok in (named_calls or []):
        total += _call_cost(model_id, prompt_tok, completion_tok)
    # Legacy extra_tokens (unknown model) — use default blended rate
    total += extra_tokens * _DEFAULT_COST_PER_1M / 1_000_000
    return round(total, 6)


def _gate_thresholds_from_judge(judge: JudgeConfig) -> QualityGateThresholds:
    """Build QualityGateThresholds from JudgeConfig (config.yaml quality_gate section)."""
    return QualityGateThresholds(
        best_single_gap_threshold=judge.best_single_gap_threshold,
        best_single_min_score=judge.best_single_min_score,
        low_confidence_avg_threshold=judge.low_confidence_avg_threshold,
        low_confidence_meta_threshold=judge.low_confidence_meta_threshold,
        divergence_confidence_threshold=judge.divergence_confidence_threshold,
        answer_critic_confidence_threshold=judge.answer_critic_confidence_threshold,
    )


# ────────────────────────────────────────────────────────
# Progress reporting (for streaming output)
# ────────────────────────────────────────────────────────

class ProgressReporter:
    """Base class for pipeline progress reporting.

    Override methods to receive real-time pipeline events.
    Default implementations are no-ops.  The Orchestrator calls
    these when a reporter is provided to ``execute()``.
    """

    async def on_stage_start(self, stage: str, detail: str = "") -> None:
        """Pipeline stage started (fan_out, synthesis, refinement …)."""

    async def on_contributor_done(
        self, model_id: str, success: bool, latency_ms: int
    ) -> None:
        """One contributor model has responded."""

    async def on_judge_token(self, token: str) -> None:
        """One token from the Judge's streaming synthesis."""

    async def on_preview_answer(self, model_id: str, content: str) -> None:
        """First contributor answered — show as preview until Judge replaces it."""

    async def on_stage_complete(self, stage: str, detail: str = "") -> None:
        """Pipeline stage completed."""

    async def on_draft_answer(self, stage: str, model_id: str, content: str) -> None:
        """Intermediate answer version ready (fan_out_best or moa_best) — v3.3."""

    async def on_citations_ready(self, citations: list) -> None:
        """Search citations available — v4.20. Emitted immediately after search completes."""

    async def on_companion_route(self, message: str, actions: list, route_reason: str,
                                 auto_execute_seconds: int = 15, is_silent: bool = False,
                                 resolved_mode: str = "", contributor_count: int = 0,
                                 more_actions: list | None = None) -> None:
        """v5.1: Dispatcher pre-route suggestion — sent before pipeline starts."""


class Orchestrator:
    """
    Core orchestration engine — executes the full pipeline for a query.

    Modes:
      Light:    Fan-out (N-of-M) → Judge → QualityGate
      Deep:     Fan-out + Critic → Judge → [Answer Critic → Judge Refine] → QualityGate
      Research: Fan-out + Critic → Judge → [Conditional Follow-up] → QualityGate
    """

    def __init__(
        self,
        config: AppConfig,
        model_adapter: OpenAIModelAdapter,
        judge: LLMJudge,
        extractor: LLMMetadataExtractor,
        prompt_loader: PromptLoader,
        event_bus: EventBus | None = None,
        search_service: SearchService | None = None,
        failure_monitor=None,
    ) -> None:
        self._config = config
        self._adapter = model_adapter
        self._judge = judge
        self._extractor = extractor
        self._prompts = prompt_loader
        self._event_bus = event_bus
        self._search = search_service
        self._failure_monitor = failure_monitor
        self._refinement = RefinementEngine(config, model_adapter, judge, prompt_loader)
        self._fan_out_engine = FanOutEngine(config, model_adapter, prompt_loader)
        self._conversation_memory = ConversationMemoryService(
            model_adapter, summary_model_id="gemini_3_flash",
        )
        self._divergence_analyzer = DivergenceAnalyzer(model_adapter, prompt_loader)
        self._fact_checker = FactChecker(model_adapter, prompt_loader)
        self._companion_dispatcher = CompanionDispatcher(config, model_adapter, failure_monitor)

    async def execute(
        self,
        context: QueryContext,
        progress: ProgressReporter | None = None,
    ) -> QueryResult:
        """Execute the full pipeline for a query.

        Args:
            context: The query to execute.
            progress: Optional reporter for real-time progress events.
                      When provided, the Judge synthesis is streamed token-
                      by-token through ``progress.on_judge_token()``.
                      All other pipeline steps (quality gate, Deep refinement,
                      event emission) execute identically regardless.
        """
        start = time.monotonic()
        mode = context.resolved_mode.value
        mode_config = self._config.modes.get(mode)

        # Pipeline-local counters (thread-safe, no instance state).
        # [0] = extra tokens from critic/refine, [1] = extra API call count.
        # Mutable list so nested calls can update it.
        extra_tokens = [0, 0]
        # v4.1 cost tracking: collect (model_id, prompt_tokens, completion_tokens)
        # for all non-contributor calls: Judge, Extractor, semantic-check, preflight, critic, refine
        _named_cost_calls: list[tuple[str, int, int]] = []
        
        # v3.10: Track responses for cost calculation in exception path
        responses: list[ModelResponse] = []

        if not mode_config:
            return self._error_result(context, f"Mode '{mode}' not found in config")

        # Copy mode_config so pipeline adjustments don't mutate shared state
        mode_config = copy.copy(mode_config)
        mode_config.contributors = list(mode_config.contributors)  # deep copy the list

        # === v5.1: single_model_override — CompanionBubble query_single direct call ===
        # When set by API (_build_context), bypass Dispatcher and force a single model.
        if context.single_model_override:
            _override_model = context.single_model_override
            if self._adapter.supports_model(_override_model):
                mode_config.contributors = [_override_model]
                mode_config.n_of_m = 1
                mode_config.skip_judge = True
                logger.info(
                    f"[{context.query_id}] single_model_override: {_override_model}"
                )
            else:
                logger.warning(
                    f"[{context.query_id}] single_model_override={_override_model} "
                    f"not supported, ignoring override"
                )

        # === v5.1: Companion Dispatcher — Smart Path pre-route ===
        # Auto mode + not yet routed → call Dispatcher to decide strategy/mode.
        # Fast Path (user chose Deep/Research) or recursive AUTO_ESCALATE → skip.
        context, mode, mode_config, _dispatcher_output, _pre_route_result = await self._run_dispatcher_pre_route(
            context=context,
            mode=mode,
            mode_config=mode_config,
            progress=progress,
            start=start,
        )
        if _pre_route_result is not None:
            return _pre_route_result

        # === v2.5.3 / v3.3: Adaptive aggregation — deferred until after LLM classifier ===
        # question_type may be updated by LLM zero-shot classifier (parallel with fan-out).
        # adaptive_overrides is populated AFTER fan-out + LLM classifier result is collected.
        # See Step 1 below for the actual apply_strategy() call.
        adaptive_overrides: dict = {}

        # v3.9: MoA tracking — initialized here so write_layer1 can reference them
        # regardless of which code path (Light/Deep/Research) was taken.
        moa_layers: int = getattr(mode_config, 'moa_layers', 1)
        moa_suppressed: bool = False

        # A3: Hard budget limits — prevent runaway cost and latency
        # F4: Use mode_config.max_timeout_seconds as primary source; hardcoded values as fallback only.
        # This eliminates the config.yaml vs orchestrator.py drift (config=180s, hardcoded=420s).
        _PIPELINE_DEADLINE_FALLBACKS = {"research": 900, "deep": 420, "light": 60}
        pipeline_max = getattr(mode_config, 'max_timeout_seconds', None) or _PIPELINE_DEADLINE_FALLBACKS.get(mode, 300)
        deadline = start + pipeline_max
        # v5.2: refinement sub-budget — caps individual refine/critique rounds
        refinement_timeout = getattr(mode_config, "refinement_timeout_seconds", 60)
        # Max API calls: contributors + judge + extractor + critic/refine rounds × 2
        n_contributors = len(mode_config.contributors)
        max_refinement = getattr(mode_config, 'max_refinement_rounds', 1)
        max_api_calls = n_contributors + 2 + (max_refinement * 3) + 4  # ×3 for fallback refine, +4 safety margin

        # v4.2: Reset adapter cost tracker at pipeline start for accurate per-query tracking
        self._adapter.reset_cost_tracker()

        logger.info(
            f"[{context.query_id}] Starting pipeline: mode={mode}, "
            f"web_search={context.web_search_enabled}, "
            f"critique={context.critique_enabled}"
        )

        # v3.3: Collect draft answers for history storage
        _draft_answers: list[dict[str, Any]] = []

        # v5.0: Build session context ONCE for contributors + Judge (avoids duplicate LLM call)
        _session_context = ""
        if context.session_history:
            try:
                _sc_result = await self._conversation_memory.build_session_context(
                    context.session_history, token_budget=2000, language=context.language,
                )
                _session_context = _sc_result.context
            except Exception:
                pass  # Non-critical — pipeline works fine without history

        if progress:
            await progress.on_stage_start("pipeline", f"mode={mode}")

        try:
            # === Step 0.4 + 0.5: Planner-lite ===
            planner_result = await self._stage_planner(
                context, mode, mode_config, progress, _named_cost_calls,
            )

            # === Step 0: Unified search ===
            rag_section, search_attempted, _tavily_search_results = await self._stage_search(
                context, mode, planner_result, progress,
            )

            # === Step 1: Fan-out to contributors ===
            responses, question_critique, adaptive_overrides, successful, _under_target_n = await self._run_fan_out_stage(
                context=context,
                mode=mode,
                mode_config=mode_config,
                planner_result=planner_result,
                rag_section=rag_section,
                search_attempted=search_attempted,
                session_context=_session_context,
                progress=progress,
                draft_answers=_draft_answers,
            )
            if not successful:
                return self._build_all_failed_result(
                    context=context,
                    responses=responses,
                    mode=mode,
                    start=start,
                )

            # === Step 1.5a: Light fast path — Kimi 直通，零开销 ===
            # v2.8.4: 彻底清理多模型遗留组件。
            # Light = Kimi 直通。不需要 Extractor/质量门/divergence/重复流式输出。
            # 之前的问题: Extractor 同步等待 4s、preview→final 双重输出导致"输出变更"、
            # 无意义的 divergence 检查和质量门计算。
            if mode_config.skip_judge:
                return await self._complete_light_fast_path(
                    context=context,
                    start=start,
                    responses=responses,
                    successful=successful,
                    mode=mode,
                    mode_config=mode_config,
                    progress=progress,
                    dispatcher_output=_dispatcher_output,
                )

            # === Step 1.5: MoA Layer 2 (Research only) ===
            # Each contributor sees others' answers and generates an improved version.
            # This is the key differentiator: models absorb each other's insights.
            # Adaptive aggregation can suppress MoA for question types where convergence hurts.
            # v3.8: Also skip MoA when contributors already highly converge (consensus trap risk).
            moa_layers = getattr(mode_config, 'moa_layers', 1)
            moa_suppressed = adaptive_overrides.get("moa_enabled") is False
            if not moa_suppressed and moa_layers >= 2 and len(successful) >= 2:
                convergence = _responses_convergence(successful)
                if convergence > 0.60:
                    moa_suppressed = True
                    logger.info(
                        f"[{context.query_id}] MoA Layer 2 skipped: "
                        f"responses already converged (similarity={convergence:.2f} > 0.60)"
                    )
            # v3.9: MoA weak-contributor filter — run Extractor early to identify pairwise
            # losers before Layer 2. A contributor with composite score < avg - margin is a
            # "clear outlier" whose answer would bias other models' improvement direction.
            # Threshold: avg - 0.25 (v4.0: tightened from 0.20).
            # Basis: benchmark 02-23 showed gpt52_pro nuance avg=55.7 (vs ensemble avg 84.8).
            # In pairwise space avg≈0.50, threshold=0.25 → excludes models winning ≤1/4 pairs.
            # 0.20 was too conservative: gpt52_pro scoring 0.25 (1 win) was not excluded.
            # Safety: never reduce to < 2 contributors (MoA requires at least 2).
            # The Extractor result is stored in _pre_moa_metadata and reused in Step 2
            # via _metadata_override to avoid a second Extractor call.
            # TODO: recalibrate threshold after accumulating ≥50 Layer1 records with
            #       question_type + moa_applied fields (added v3.9 L-2). Target: data-driven
            #       per-question-type thresholds (e.g. ANALYTICAL may tolerate more diversity).
            _MOA_WEAK_EXCLUDE_MARGIN = self._config.judge.moa_weak_exclude_margin  # v4.1: from schema
            _pre_moa_metadata: MetadataExtraction | None = None
            if (
                moa_layers >= 2
                and not moa_suppressed
                and len(successful) >= 3  # need ≥3 to safely exclude 1
                and mode in ("deep", "research")
            ):
                _extractor_model_id_early = mode_config.extractor
                try:
                    _pre_moa_metadata = await self._extractor.extract(
                        question=context.question,
                        responses=successful,
                        extractor_model_id=_extractor_model_id_early,
                        fallback_models=getattr(self._config.judge, 'extractor_fallback_chain', []),
                    )
                    if _pre_moa_metadata and _pre_moa_metadata.extractor_model_id:
                        _named_cost_calls.append((
                            _pre_moa_metadata.extractor_model_id,
                            _pre_moa_metadata.prompt_tokens,
                            _pre_moa_metadata.completion_tokens,
                        ))
                except Exception:
                    _pre_moa_metadata = None

                if (
                    _pre_moa_metadata is not None
                    and _pre_moa_metadata.pairwise_evaluated
                    and _pre_moa_metadata.model_evaluations
                ):
                    from agoracle.domain.quality_gate import compute_scores as _cscores
                    _moa_scores = _cscores(
                        _pre_moa_metadata.model_evaluations, pairwise_mode=True
                    )
                    if _moa_scores:
                        _moa_avg = sum(_moa_scores.values()) / len(_moa_scores)
                        _threshold = _moa_avg - _MOA_WEAK_EXCLUDE_MARGIN
                        _excluded = {
                            mid for mid, sc in _moa_scores.items()
                            if sc < _threshold
                        }
                        if _excluded:
                            _filtered = [r for r in successful if r.model_id not in _excluded]
                            if len(_filtered) >= 2:
                                logger.info(
                                    f"[{context.query_id}] MoA weak-filter: "
                                    f"excluded {_excluded} (scores below {_threshold:.3f}), "
                                    f"{len(_filtered)}/{len(successful)} remain"
                                )
                                successful = _filtered
                            else:
                                logger.debug(
                                    f"[{context.query_id}] MoA weak-filter: "
                                    f"skipped (would reduce to <2 contributors)"
                                )

                # === v4.30: MoA L2 Gate Pre-Check ===
                # If pre-MoA metadata already shows a strong BEST_SINGLE signal,
                # skip MoA Layer 2 entirely. The Gate at Step 2 will confirm and
                # adopt the winner — no need to pay for a second round of model calls.
                # Only short-circuits on strong signals (unanimous or high gap):
                #   - pairwise unanimous winner (score >= 1.0)
                #   - pairwise strong-majority (score >= 0.75, gap >= threshold)
                # Weak BEST_SINGLE (non-pairwise path) is NOT short-circuited here
                # because non-pairwise scores are less reliable pre-MoA.
                # v4.31b fix: gate_thresholds assigned here (before first use) to avoid
                # UnboundLocalError when early-exit runs before Step 2 initialization.
                _early_gate_thresholds = _gate_thresholds_from_judge(self._config.judge)
                if (
                    _pre_moa_metadata is not None
                    and _pre_moa_metadata.pairwise_evaluated
                    and _pre_moa_metadata.model_evaluations
                    and not moa_suppressed
                ):
                    from agoracle.domain.quality_gate import (
                        evaluate_gate as _early_gate,
                    )
                    _early_gate_result = _early_gate(
                        successful, _pre_moa_metadata, _early_gate_thresholds
                    )
                    if _early_gate_result == QualityGateResult.BEST_SINGLE:
                        moa_suppressed = True
                        logger.info(
                            f"[{context.query_id}] v4.30 MoA L2 early-exit: "
                            f"pre-MoA Gate={_early_gate_result.value}, "
                            f"skipping MoA Layer 2 (cost saved)"
                        )

            if moa_layers >= 2 and len(successful) >= 2 and not moa_suppressed:
                # v4.8: Deadline check before MoA Layer 2
                if time.monotonic() > deadline:
                    logger.warning(
                        f"[{context.query_id}] Pipeline deadline exceeded before MoA L2, "
                        f"skipping to synthesis with {len(successful)} L1 responses"
                    )
                    moa_suppressed = True

            if moa_layers >= 2 and len(successful) >= 2 and not moa_suppressed:
                if progress:
                    await progress.on_stage_start(
                        "moa_layer2", f"MoA refine {len(successful)} responses"
                    )

                moa_responses = await self._fan_out_engine.moa_second_layer(
                    context, mode_config, successful, rag_section,
                )
                moa_successful = [r for r in moa_responses if r.success and r.content]

                if moa_successful:
                    logger.info(
                        f"[{context.query_id}] MoA Layer 2: "
                        f"{len(moa_successful)}/{len(successful)} improved"
                    )
                    successful = moa_successful
                    # === v3.3: Emit MoA best draft ===
                    _moa_best = moa_successful[0]
                    _draft_answers.append({"stage": "moa_best", "model_id": _moa_best.model_id, "content": _moa_best.content})
                    if progress:
                        await progress.on_draft_answer("moa_best", _moa_best.model_id, _moa_best.content)
                else:
                    logger.warning(
                        f"[{context.query_id}] MoA Layer 2 failed, using Layer 1 responses"
                    )

                if progress:
                    await progress.on_stage_complete(
                        "moa_layer2",
                        f"{len(moa_successful) if moa_successful else 0} improved",
                    )

            # === Step 1.9: v4.7 Exclude search-only contributors before Judge synthesis ===
            # Keep Perplexity in fan-out + MoA L2 (its data still informs peers), then
            # optionally remove from final Judge synthesis for specific question types.
            # Safety: never reduce to < 2 contributors.
            _exclude_search = bool(adaptive_overrides.get("exclude_search_contributors"))
            if _exclude_search:
                _exclude_model_ids: set[str] = set()

                _exclude_model_ids.update({
                    mid for mid in (r.model_id for r in successful)
                    if mid and "perplexity" in mid.lower()
                })

                if _exclude_model_ids:
                    _before_count = len(successful)
                    _filtered = [r for r in successful if r.model_id not in _exclude_model_ids]
                    if len(_filtered) >= 2:
                        logger.info(
                            f"[{context.query_id}] v4.7: Excluded contributors "
                            f"{sorted(_exclude_model_ids)} from synthesis "
                            f"(question_type={context.question_type.value if hasattr(context.question_type, 'value') else context.question_type}, "
                            f"{len(_filtered)}/{_before_count} remain)"
                        )
                        successful = _filtered
                    else:
                        logger.debug(
                            f"[{context.query_id}] v4.7: Skip contributor exclusion "
                            f"(would reduce to <2 contributors)"
                        )

            # === Step 1.95: v4.20 DivergenceAnalyzer (deep/research only) ===
            # Runs concurrently alongside Extractor/Judge to produce structured divergence points.
            # Uses gemini_3_flash (fast, ~3-5s). Results are awaited before QueryResult is built.
            # Only fires when has_divergence is True or mode is deep/research with 2+ contributors.
            _divergence_task: asyncio.Task | None = None
            if mode in ("deep", "research") and len(successful) >= 2:
                _diverge_model = getattr(mode_config, "divergence_analyzer", None) or "gemini_3_flash"
                _divergence_task = asyncio.create_task(
                    asyncio.wait_for(
                        self._divergence_analyzer.analyze(
                            question=context.question,
                            responses=successful,
                            analyzer_model_id=_diverge_model,
                        ),
                        timeout=30,  # flash <5s normally; hard cap prevents blocking
                    )
                )

            # === Step 1.96: Iterative Search — Gap Detection + Supplementary Search ===
            _tavily_search_results = await self._stage_gap_search(
                context, mode, successful, planner_result,
                _tavily_search_results, progress, _named_cost_calls,
            )

            # === Step 1.97: FactChecker launch (concurrent with DivergenceAnalyzer) ===
            _factcheck_task, _perplexity_citation_urls = self._stage_launch_factcheck(
                context, mode, successful, _tavily_search_results,
            )

            # === Step 2: Judge + Metadata Extraction ===
            # v3.2 Gate-before-Judge (deep/research only):
            #   Extractor → Gate → [BEST_SINGLE: skip Judge | else: call Judge]
            # Light mode: original parallel flow (Judge ‖ Extractor, Gate after Judge)
            judge_model_id = mode_config.judge
            extractor_model_id = mode_config.extractor

            gate_thresholds = _gate_thresholds_from_judge(self._config.judge)
            gate_overrides = {}
            if "best_single_gap_threshold" in adaptive_overrides:
                gate_overrides["best_single_gap_threshold"] = adaptive_overrides["best_single_gap_threshold"]
            if "best_single_min_score" in adaptive_overrides:
                gate_overrides["best_single_min_score"] = adaptive_overrides["best_single_min_score"]
            if gate_overrides:
                gate_thresholds = dc_replace(gate_thresholds, **gate_overrides)
            # v4.30: UNDER_TARGET_N — fewer contributors than requested.
            # Lower best_single_gap_threshold by 30% so the Gate triggers BEST_SINGLE
            # more readily when evidence base is thinner than designed.
            if _under_target_n:
                _utn_gap = gate_thresholds.best_single_gap_threshold * 0.70
                gate_thresholds = dc_replace(gate_thresholds, best_single_gap_threshold=_utn_gap)
                logger.info(
                    f"[{context.query_id}] v4.30 UNDER_TARGET_N: "
                    f"best_single_gap_threshold lowered to {_utn_gap:.3f}"
                )

            synthesis: JudgeSynthesis | None = None
            # v4.19: citations initialized here; set by _judge_and_extract (deep/research)
            # or left as Tavily-only for BEST_SINGLE path where Judge is skipped
            _citations_from_judge: list[dict] = []
            _fact_warnings: list[str] = []  # v4.22c: fact-check warnings for BEST_SINGLE paths

            if mode in ("deep", "research"):
                # ── v3.2 Gate-before-Judge path ──────────────────────────────────
                # v3.9: Reuse _pre_moa_metadata if MoA weak-filter already ran Extractor.
                # This avoids a redundant Extractor call on the same (possibly filtered)
                # contributor set. Note: if weak-filter excluded contributors, _pre_moa_metadata
                # reflects the pre-filter set; we re-run Extractor on the filtered set so
                # Gate and Judge see accurate scores for the actual MoA input.
                _pre_moa_ids = (
                    set(_pre_moa_metadata.model_evaluations.keys())
                    if _pre_moa_metadata and _pre_moa_metadata.model_evaluations
                    else set()
                )
                _current_ids = {r.model_id for r in successful}
                if _pre_moa_metadata is not None and _pre_moa_ids == _current_ids:
                    # Contributor set unchanged (no weak-filter exclusion) — reuse metadata
                    # v4.27: [MoA_METADATA_REUSE] log for Phase 2 data collection.
                    # Pre-MoA metadata reflects L1 quality; MoA L2 may have changed ranking.
                    # After ≥30 MoA queries, analyze pre vs post ranking divergence to decide
                    # whether to force post-MoA Extractor re-run (see DEEP_MODE_OPTIMIZATION_PLAN.md P1-2).
                    _moa_was_applied = moa_layers >= 2 and not moa_suppressed
                    if _moa_was_applied:
                        _pre_scores = compute_scores(_pre_moa_metadata.model_evaluations)
                        _pre_best = max(
                            _pre_scores.items(),
                            key=lambda x: x[1],
                            default=("unknown", 0.0),
                        )
                        logger.info(
                            f"[{context.query_id}] [MoA_METADATA_REUSE] "
                            f"pre_moa_best={_pre_best[0]}, "
                            f"pre_moa_conf={_pre_moa_metadata.confidence:.2f} "
                            f"(reusing pre-MoA metadata — ranking may have shifted after L2)"
                        )
                    metadata = _pre_moa_metadata
                    if progress:
                        await progress.on_stage_start(
                            "extraction", f"Extractor: {extractor_model_id}"
                        )
                        await progress.on_stage_complete(
                            "extraction",
                            f"evals={len(metadata.model_evaluations)}, conf={metadata.confidence:.2f} (reused)",
                        )
                else:
                    if progress:
                        await progress.on_stage_start(
                            "extraction", f"Extractor: {extractor_model_id}"
                        )
                    try:
                        metadata = await self._extractor.extract(
                            question=context.question,
                            responses=successful,
                            extractor_model_id=extractor_model_id,
                            fallback_models=getattr(self._config.judge, 'extractor_fallback_chain', []),
                        )
                    except (asyncio.TimeoutError, ValueError) as _ext_err:
                        logger.warning(
                            f"[{context.query_id}] Extractor failed ({type(_ext_err).__name__}): {_ext_err}, using empty metadata"
                        )
                        metadata = MetadataExtraction()
                    except Exception as _ext_err:
                        logger.error(
                            f"[{context.query_id}] Extractor unexpected error: {_ext_err}",
                            exc_info=True,
                        )
                        metadata = MetadataExtraction()

                    if progress:
                        await progress.on_stage_complete(
                            "extraction",
                            f"evals={len(metadata.model_evaluations)}, conf={metadata.confidence:.2f}",
                        )

                # Gate decision (before Judge)
                if not getattr(self._config.judge, 'quality_gate_enabled', True):
                    gate_result = QualityGateResult.SYNTHESIZED
                    logger.info(f"[{context.query_id}] QualityGate: DISABLED by config")
                else:
                    gate_result = evaluate_gate(successful, metadata, gate_thresholds)

                if gate_result == QualityGateResult.BEST_SINGLE and mode_config.disable_best_single:
                    # v3.5: Allow BEST_SINGLE even in Research if score_gap is extreme
                    _override_gap = getattr(mode_config, "best_single_override_gap", 0)
                    _actual_gap = compute_score_gap(metadata)
                    # v3.9: Distinguish unanimous winner override (stronger signal) from ordinary
                    _is_unanimous = metadata.pairwise_evaluated and max(
                        compute_scores(metadata.model_evaluations, pairwise_mode=True).values(),
                        default=0.0,
                    ) >= 1.0 if metadata.model_evaluations else False
                    if _override_gap > 0 and _actual_gap > _override_gap:
                        logger.info(
                            f"[{context.query_id}] QualityGate: BEST_SINGLE allowed "
                            f"(override_gap={_override_gap}, actual_gap={_actual_gap:.3f})"
                        )
                    else:
                        if _is_unanimous:
                            logger.warning(
                                f"[{context.query_id}] QualityGate: unanimous winner overridden "
                                f"by disable_best_single=True (mode={mode}) — "
                                f"one model won all pairwise but synthesis forced; "
                                f"consider reviewing if synthesis adds value here"
                            )
                        else:
                            logger.info(
                                f"[{context.query_id}] QualityGate: BEST_SINGLE overridden "
                                f"(disable_best_single=True for mode={mode}), using SYNTHESIZED"
                            )
                        gate_result = QualityGateResult.SYNTHESIZED

                if gate_result == QualityGateResult.BEST_SINGLE:
                    # One model dominates — adopt directly, skip Judge entirely
                    best = get_best_response(successful, metadata)
                    if best:
                        final_answer = best.content
                        logger.info(
                            f"[{context.query_id}] QualityGate: BEST_SINGLE "
                            f"(adopted {best.model_id}, Judge skipped)"
                        )
                    else:
                        gate_result = QualityGateResult.SYNTHESIZED
                        logger.warning(
                            f"[{context.query_id}] QualityGate: BEST_SINGLE fallback "
                            f"to SYNTHESIZED (best model not matched)"
                        )
                    # v4.19: build Tavily citations for BEST_SINGLE (Judge was skipped)
                    if _tavily_search_results:
                        _seen_bs_urls: set[str] = set()
                        for _sr in _tavily_search_results:
                            _url = getattr(_sr, "url", "")
                            if _url and _url not in _seen_bs_urls:
                                _seen_bs_urls.add(_url)
                                _citations_from_judge.append({
                                    "url": _url,
                                    "title": getattr(_sr, "title", _url) or _url,
                                })

                    # v4.22c: Collect Perplexity citations for BEST_SINGLE (same as Judge path)
                    if _perplexity_citation_urls:
                        _existing = {c["url"] for c in _citations_from_judge}
                        for _purl in _perplexity_citation_urls:
                            if _purl not in _existing:
                                _citations_from_judge.append({"url": _purl, "title": _purl})
                                _existing.add(_purl)

                    # v4.22d: Three-tier degradation valve for BEST_SINGLE (exit A)
                    # FactChecker was launched at Step 1.96 but would be abandoned.
                    # Tier 1: contradicted == 1  → collect warning only (no degrade)
                    # Tier 2: contradicted 2-3   → warning + degrade to SYNTHESIZED
                    # Tier 3: contradicted >= 4  → degrade to SYNTHESIZED + warning
                    # Gate: creative/controversial/writing → warnings only, never degrade
                    _degrade_eligible_qt = context.question_type not in (
                        QuestionType.CREATIVE, QuestionType.CONTROVERSIAL, QuestionType.WRITING,
                    )
                    if _factcheck_task is not None and gate_result == QualityGateResult.BEST_SINGLE:
                        try:
                            _bs_fc = await asyncio.wait_for(asyncio.shield(_factcheck_task), timeout=5.0)  # v4.26: 3.0→5.0; BEST_SINGLE skips Judge so extra wait is free
                            _n_contra = _bs_fc.contradicted_count
                            _default_evidence = "\u4e0e\u641c\u7d22\u7ed3\u679c\u77db\u76fe"
                            # Always collect warnings for contradicted claims
                            for _fc_claim in _bs_fc.claims:
                                if _fc_claim.verdict == "contradicted":
                                    _ev = _fc_claim.evidence or _default_evidence
                                    _fact_warnings.append(
                                        f"\u26a0\ufe0f {_fc_claim.claim} \u2014 \u641c\u7d22\u6765\u6e90\u663e\u793a: {_ev}"
                                    )
                            if _degrade_eligible_qt and 2 <= _n_contra <= 3:
                                logger.warning(
                                    f"[{context.query_id}] v4.22d degradation valve (tier2): "
                                    f"{_n_contra} contradicted claims, overriding to SYNTHESIZED"
                                )
                                gate_result = QualityGateResult.SYNTHESIZED
                            elif _degrade_eligible_qt and _n_contra >= 4:
                                logger.warning(
                                    f"[{context.query_id}] v4.22d degradation valve (tier3): "
                                    f"{_n_contra} contradicted claims, overriding to SYNTHESIZED"
                                )
                                gate_result = QualityGateResult.SYNTHESIZED
                            else:
                                _reason = "non-factual question type" if not _degrade_eligible_qt else "tier1"
                                logger.info(
                                    f"[{context.query_id}] v4.22d degradation valve ({_reason}): "
                                    f"{_n_contra} contradicted claim(s), warning only, keeping BEST_SINGLE"
                                )
                        except Exception as _bs_fe:
                            logger.warning(f"[{context.query_id}] FactChecker for BEST_SINGLE timed out: {_bs_fe}")

                if gate_result != QualityGateResult.BEST_SINGLE:
                    # v4.8: Deadline check before Judge synthesis
                    if time.monotonic() > deadline:
                        _deadline_best = get_best_response(successful, metadata)
                        if _deadline_best and _deadline_best.content:
                            logger.warning(
                                f"[{context.query_id}] Pipeline deadline exceeded before Judge, "
                                f"falling back to best contributor: {_deadline_best.model_id}"
                            )
                            final_answer = _deadline_best.content
                            gate_result = QualityGateResult.BEST_SINGLE
                    # SYNTHESIZED or LOW_CONFIDENCE — call Judge with pre-computed metadata
                    if gate_result != QualityGateResult.BEST_SINGLE:
                        # v4.21: Collect FactChecker result (was launched at Step 1.96)
                        _fact_check_section = ""
                        if _factcheck_task is not None:
                            try:
                                _fc_report = await asyncio.wait_for(asyncio.shield(_factcheck_task), timeout=3.0)
                                _fact_check_section = _fc_report.to_judge_section()
                                if _fact_check_section:
                                    logger.info(
                                        f"[{context.query_id}] FactChecker: "
                                        f"{_fc_report.verified_count} verified, "
                                        f"{_fc_report.unverified_count} unverified, "
                                        f"{_fc_report.contradicted_count} contradicted "
                                        f"({_fc_report.latency_ms}ms)"
                                    )
                            except Exception as _fe:
                                logger.warning(f"[{context.query_id}] FactChecker timed out/failed: {_fe}")

                        if progress:
                            await progress.on_stage_start(
                                "synthesis", f"Judge: {judge_model_id}"
                            )
                        # G1: Deadline-aware timeout for Judge synthesis — prevents exceeding pipeline budget.
                        # Reserve 90s after Judge for refinement+post-processing.
                        _judge_budget = max(deadline - time.monotonic() - 90, 30)
                        try:
                            synthesis, _, _citations_from_judge = await asyncio.wait_for(
                                self._judge_and_extract(
                                    context=context,
                                    successful=successful,
                                    question_critique=question_critique,
                                    mode=mode,
                                    judge_model_id=judge_model_id,
                                    extractor_model_id=extractor_model_id,
                                    progress=progress,
                                    judge_prompt_override=adaptive_overrides.get("judge_prompt_key", ""),
                                    _metadata_override=metadata,
                                    _named_cost_calls=_named_cost_calls,
                                    tavily_search_results=_tavily_search_results or None,
                                    fact_check_section=_fact_check_section,
                                    session_context=_session_context,
                                ),
                                timeout=_judge_budget,
                            )
                        except asyncio.TimeoutError:
                            logger.warning(
                                f"[{context.query_id}] Judge synthesis timed out "
                                f"(budget={_judge_budget:.0f}s), falling back to best contributor"
                            )
                            _deadline_best_j = get_best_response(successful, metadata)
                            if _deadline_best_j and _deadline_best_j.content:
                                final_answer = _deadline_best_j.content
                                gate_result = QualityGateResult.BEST_SINGLE
                            else:
                                # A1: get_best_response returned None (no successful contributors
                                # or compute_scores empty) — force BEST_SINGLE with empty answer
                                # to prevent synthesis=None AttributeError at line below.
                                gate_result = QualityGateResult.BEST_SINGLE
                                final_answer = "抱歉，所有模型均无法生成回答，请稍后重试。"
                                logger.warning(
                                    f"[{context.query_id}] Judge timeout and no best contributor — "
                                    f"returning fallback empty answer"
                                )
                            if progress:
                                await progress.on_stage_complete("synthesis", "超时降级")
                            # Skip refinement — go straight to result assembly
                        if progress and gate_result != QualityGateResult.BEST_SINGLE:
                            await progress.on_stage_complete(
                                "synthesis", f"{len(synthesis.final_answer)} chars",
                            )
                        if gate_result != QualityGateResult.BEST_SINGLE and not synthesis.success:
                            # A2: Wrap judge_fallback in deadline-aware timeout.
                            # Internal fallback chain already has 60s cap, but adding
                            # deadline awareness prevents exceeding pipeline budget.
                            _fallback_budget = max(deadline - time.monotonic(), 15)
                            try:
                                synthesis = await asyncio.wait_for(
                                    self._refinement.judge_fallback(
                                        context, successful, question_critique, mode,
                                        primary_judge_id=judge_model_id,
                                        judge_prompt_override=adaptive_overrides.get("judge_prompt_key", ""),
                                    ),
                                    timeout=_fallback_budget,
                                )
                            except asyncio.TimeoutError:
                                # A2-fix: timeout → keep original synthesis (success=False);
                                # downstream v3.10 re-check will degrade to BEST_SINGLE.
                                logger.warning(
                                    f"[{context.query_id}] judge_fallback timed out "
                                    f"({_fallback_budget:.0f}s), keeping original synthesis for re-check"
                                )

            else:
                # ── Light mode: original parallel flow (Judge ‖ Extractor) ────────
                # skip_judge=True means no Judge call — don't emit synthesis stage events
                if progress and not mode_config.skip_judge:
                    await progress.on_stage_start(
                        "synthesis", f"Judge: {judge_model_id}"
                    )
                synthesis, metadata, _citations_from_judge = await self._judge_and_extract(
                    context=context,
                    successful=successful,
                    question_critique=question_critique,
                    mode=mode,
                    judge_model_id=judge_model_id,
                    extractor_model_id=extractor_model_id,
                    progress=progress,
                    judge_prompt_override=adaptive_overrides.get("judge_prompt_key", ""),
                    _named_cost_calls=_named_cost_calls,
                    tavily_search_results=_tavily_search_results or None,
                    session_context=_session_context,
                )
                if progress and not mode_config.skip_judge:
                    await progress.on_stage_complete(
                        "synthesis", f"{len(synthesis.final_answer)} chars",
                    )
                if not synthesis.success:
                    synthesis = await self._refinement.judge_fallback(
                        context, successful, question_critique, mode,
                        primary_judge_id=judge_model_id,
                        judge_prompt_override=adaptive_overrides.get("judge_prompt_key", ""),
                    )

                # Gate after Judge (Light mode original flow)
                if not getattr(self._config.judge, 'quality_gate_enabled', True):
                    gate_result = QualityGateResult.SYNTHESIZED
                    logger.info(f"[{context.query_id}] QualityGate: DISABLED by config")
                else:
                    gate_result = evaluate_gate(successful, metadata, gate_thresholds)

                if gate_result == QualityGateResult.BEST_SINGLE and mode_config.disable_best_single:
                    gate_result = QualityGateResult.SYNTHESIZED

            # === Step 3: Log gate decision ===
            try:
                _best_model = ""
                _best_score = 0.0
                _gap = 0.0
                if metadata.model_evaluations:
                    from agoracle.domain.quality_gate import compute_scores
                    _scores = compute_scores(
                        metadata.model_evaluations,
                        pairwise_mode=metadata.pairwise_evaluated,
                    )
                    if _scores:
                        _best_model = max(_scores, key=_scores.get)  # type: ignore
                        _best_score = _scores[_best_model]
                    _gap = compute_score_gap(metadata)
                _log_judge_decision(
                    query_id=context.query_id,
                    mode=mode,
                    gate_result=gate_result.value,
                    confidence=metadata.confidence,
                    best_single_model=_best_model,
                    best_single_score=_best_score,
                    score_gap=_gap,
                    contributor_count=len(successful),
                    question_type=context.question_type.value if hasattr(context.question_type, 'value') else str(context.question_type),
                )
            except Exception as e:
                logger.debug(f"Judge decision log write failed: {e}")

            # === Step 4: Post-Judge routing (refinement / escalation) ===
            if gate_result == QualityGateResult.BEST_SINGLE:
                # Already handled above for deep/research; Light mode path:
                if mode not in ("deep", "research"):
                    best = get_best_response(successful, metadata)
                    if best:
                        final_answer = best.content
                        logger.info(
                            f"[{context.query_id}] QualityGate: BEST_SINGLE "
                            f"(adopted {best.model_id})"
                        )
                    else:
                        gate_result = QualityGateResult.SYNTHESIZED
                        final_answer = synthesis.final_answer if synthesis else ""
                        logger.warning(
                            f"[{context.query_id}] QualityGate: BEST_SINGLE fallback "
                            f"to SYNTHESIZED (best model not matched)"
                        )

            elif gate_result == QualityGateResult.LOW_CONFIDENCE:
                # v2.3: Auto-escalate from Light → Deep on LOW_CONFIDENCE
                has_real_metadata = bool(metadata.model_evaluations)
                if mode_config.auto_escalate and mode == "light" and has_real_metadata:
                    deep_config = self._config.modes.get("deep")
                    if deep_config:
                        light_tokens = sum(
                            r.prompt_tokens + r.completion_tokens for r in responses
                        ) + extra_tokens[0]
                        logger.info(
                            f"[{context.query_id}] AUTO_ESCALATE: Light→Deep "
                            f"(LOW_CONFIDENCE, confidence={metadata.confidence:.2f})"
                        )
                        context_deep = QueryContext(
                            query_id=context.query_id,
                            question=context.question,
                            mode=context.mode,
                            resolved_mode=Mode.DEEP,
                            intent=context.intent,
                            web_search_enabled=context.web_search_enabled,
                            critique_enabled=True,
                            output_depth=OutputDepth.LEVEL_2,
                            rag_results=context.rag_results,
                            user_profile_summary=context.user_profile_summary,
                            session_history=context.session_history,
                            language=context.language,
                            inherited_tokens=context.inherited_tokens + light_tokens,
                            dispatcher_routed=True,  # v5.1: prevent Dispatcher re-route on escalation
                        )
                        return await self.execute(context_deep, progress)

                # Deep/Research LOW_CONFIDENCE — attempt refinement as recovery
                budget_ok = (
                    time.monotonic() < deadline
                    and (len(responses) + extra_tokens[1]) < max_api_calls
                )
                if mode in ("deep", "research") and synthesis and synthesis.success and mode_config.answer_critic and budget_ok:
                    logger.info(
                        f"[{context.query_id}] LOW_CONFIDENCE recovery: "
                        f"attempting refinement for mode={mode}"
                    )
                    # G1: Same hard timeout as SYNTHESIZED branch (F1) — prevents blocking complete
                    _REFINEMENT_HARD_TIMEOUT_LC = getattr(mode_config, "refinement_timeout_seconds", 60)
                    _pre_refine_lc = synthesis
                    if progress:
                        await progress.on_stage_start("refinement", "答案优化中")
                    try:
                        synthesis = await asyncio.wait_for(
                            self._refinement.deep_refinement(
                                context, synthesis, mode_config, extra_tokens,
                                judge_prompt_override=adaptive_overrides.get("judge_prompt_key", ""),
                                best_model_id=_best_model,
                                contributor_responses=successful,
                                fact_check_section=_fact_check_section,
                                search_citations=_citations_from_judge or None,
                                deadline=deadline,
                            ),
                            timeout=_REFINEMENT_HARD_TIMEOUT_LC,
                        )
                        if progress:
                            await progress.on_stage_complete("refinement", "优化完成")
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"[{context.query_id}] LOW_CONFIDENCE refinement hard timeout "
                            f"({_REFINEMENT_HARD_TIMEOUT_LC}s), keeping pre-refinement synthesis"
                        )
                        synthesis = _pre_refine_lc
                        if progress:
                            await progress.on_stage_complete("refinement", "超时跳过")
                    except Exception as _lc_refine_err:
                        logger.error(
                            f"[{context.query_id}] LOW_CONFIDENCE refinement failed: {_lc_refine_err}",
                            exc_info=True,
                        )
                        synthesis = _pre_refine_lc
                        if progress:
                            await progress.on_stage_complete("refinement", "跳过")
                elif not budget_ok:
                    logger.warning(
                        f"[{context.query_id}] Skipping LOW_CONFIDENCE refinement "
                        f"(budget exceeded: deadline or max_api_calls)"
                    )

                final_answer = synthesis.final_answer if synthesis else ""
                _best_resp_low_conf = get_best_response(successful, metadata)
                if not final_answer.strip() and _best_resp_low_conf and _best_resp_low_conf.content:
                    logger.warning(
                        f"[{context.query_id}] LOW_CONFIDENCE synthesis empty, falling back to best_single content"
                    )
                    final_answer = _best_resp_low_conf.content
                low_conf_actions = []
                if mode == "light":
                    low_conf_actions.append({
                        "action": "switch_deep",
                        "label": "切换 Deep 模式获取更深入分析",
                    })
                low_conf_actions.append({
                    "action": "rephrase",
                    "label": "换个更具体的问法试试",
                })
                if context.output_depth != OutputDepth.LEVEL_3:
                    low_conf_actions.append({
                        "action": "show_individual",
                        "label": "查看各模型的原始回答",
                    })
                logger.info(f"[{context.query_id}] QualityGate: LOW_CONFIDENCE")

            else:
                # SYNTHESIZED — refine only when Extractor signals issues (Deep/Research)
                budget_ok = (
                    time.monotonic() < deadline
                    and (len(responses) + extra_tokens[1]) < max_api_calls
                )
                if mode in ("deep", "research") and synthesis and synthesis.success and budget_ok:
                    if should_trigger_answer_critic(metadata, gate_thresholds):
                        # F1: Hard timeout for refinement (60s) — prevents blocking complete event.
                        # If refinement exceeds budget, return current synthesis (already good enough).
                        _REFINEMENT_HARD_TIMEOUT = 60
                        _pre_refine_synthesis = synthesis  # preserve in case of timeout
                        if progress:
                            await progress.on_stage_start("refinement", "答案优化中")
                        try:
                            synthesis = await asyncio.wait_for(
                                self._refinement.deep_refinement(
                                    context, synthesis, mode_config, extra_tokens,
                                    judge_prompt_override=adaptive_overrides.get("judge_prompt_key", ""),
                                    best_model_id=_best_model,
                                    contributor_responses=successful,
                                    fact_check_section=_fact_check_section,
                                    search_citations=_citations_from_judge or None,
                                    deadline=deadline,
                                ),
                                timeout=_REFINEMENT_HARD_TIMEOUT,
                            )
                            if progress:
                                await progress.on_stage_complete("refinement", "优化完成")
                        except asyncio.TimeoutError:
                            logger.warning(
                                f"[{context.query_id}] Refinement hard timeout ({_REFINEMENT_HARD_TIMEOUT}s), "
                                f"returning pre-refinement synthesis"
                            )
                            synthesis = _pre_refine_synthesis
                            if progress:
                                await progress.on_stage_complete("refinement", "超时跳过")
                        except Exception as _refine_err:
                            logger.error(
                                f"[{context.query_id}] Refinement failed: {_refine_err}",
                                exc_info=True,
                            )
                            synthesis = _pre_refine_synthesis
                            if progress:
                                await progress.on_stage_complete("refinement", "跳过")
                    else:
                        logger.info(
                            f"[{context.query_id}] Skipping refinement "
                            f"(confidence={metadata.confidence:.2f}, "
                            f"divergence={metadata.has_divergence})"
                        )
                elif not budget_ok and mode in ("deep", "research"):
                    logger.warning(
                        f"[{context.query_id}] Skipping SYNTHESIZED refinement "
                        f"(budget exceeded)"
                    )
                # v3.10 fix: 二次校验 — Judge 失败或答案过短时 fallback 到 best_single
                final_answer = synthesis.final_answer if synthesis else ""
                _best_resp_for_check = get_best_response(successful, metadata)

                if gate_result == QualityGateResult.SYNTHESIZED and _best_resp_for_check and _best_resp_for_check.content:
                    _best_content = _best_resp_for_check.content

                    # Case A: Judge 失败（超时/连接错误）→ 直接 fallback
                    if not synthesis or not synthesis.success or not final_answer.strip():
                        logger.warning(
                            f"[{context.query_id}] Judge failed/empty, falling back to best_single"
                        )
                        final_answer = _best_content
                        gate_result = QualityGateResult.BEST_SINGLE

                    # Case B: v4.1 语义验证 — gemini_flash pairwise 判断 synthesis vs best_single
                    # 替换 v3.10 的长度启发式（len < 70%）：长度≠质量，synthesis 可能更长但更差。
                    # 用 gemini_flash 快速判断哪个回答更好；synthesis 输了则 fallback 到 best_single。
                    # 成本：~$0.001，延迟：~2s（Deep 总延迟 ~470s，可忽略）。
                    # features.semantic_check=False → 回退到 v3.10 长度启发式（A/B 实验用）
                    elif final_answer != _best_content and not self._config.features.semantic_check:
                        # v3.10 length heuristic (A/B experiment: semantic_check=False)
                        if len(final_answer) < len(_best_content) * 0.7:
                            logger.warning(
                                f"[{context.query_id}] v3.10 length check: synthesis short "
                                f"({len(final_answer)} vs best {len(_best_content)}), "
                                f"falling back to best_single"
                            )
                            final_answer = _best_content
                            gate_result = QualityGateResult.BEST_SINGLE
                    elif final_answer != _best_content:
                        _extractor_id = mode_config.extractor or self._config.judge.extractor_model or "gemini_3_flash"
                        # v4.27: Randomize A/B position to eliminate systematic position bias
                        # (Zheng et al. 2023: 5-10% bias toward position A; Wang et al. 2024: replicated)
                        _swap = random.random() < 0.5
                        _text_a = _best_content[:4000] if _swap else final_answer[:4000]
                        _text_b = final_answer[:4000] if _swap else _best_content[:4000]
                        _label_a = "最优单模型" if _swap else "合成版"
                        _label_b = "合成版" if _swap else "最优单模型"
                        _semantic_prompt = (
                            "你是一个严格的回答质量评判者。以下是同一问题的两个回答，判断哪个更好。\n"
                            "只输出 'A' 或 'B'，不要任何其他文字。\n\n"
                            f"问题：{context.question[:300]}\n\n"
                            f"回答A（{_label_a}）：\n{_text_a}\n\n"
                            f"回答B（{_label_b}）：\n{_text_b}"
                        )
                        try:
                            import uuid as _uuid
                            _check_call = RoleCall(
                                call_id=f"postcheck-{_uuid.uuid4().hex[:8]}",
                                model_id=_extractor_id,
                                role=Role.METADATA_EXTRACTOR,
                                system_prompt="你是回答质量评判者。只输出 'A' 或 'B'。",
                                messages=[{"role": "user", "content": _semantic_prompt}],
                                timeout_seconds=8,
                            )
                            _check_resp = await self._adapter.call(_check_call)
                            # v4.39: track semantic-check token cost (was missing)
                            if _check_resp.prompt_tokens or _check_resp.completion_tokens:
                                _named_cost_calls.append((
                                    _check_resp.model_id or _extractor_id,
                                    _check_resp.prompt_tokens,
                                    _check_resp.completion_tokens,
                                ))
                            if _check_resp.success and _check_resp.content:
                                _verdict = _check_resp.content.strip().upper()[:1]
                                # v4.27: Translate verdict back accounting for swap
                                # If swapped: A=best_single, B=synthesis. "A" means best_single won.
                                # If not swapped: A=synthesis, B=best_single. "B" means best_single won.
                                _synthesis_lost = (_swap and _verdict == "A") or (not _swap and _verdict == "B")
                                if _synthesis_lost:
                                    logger.warning(
                                        f"[{context.query_id}] v4.1 semantic check: synthesis < best_single, "
                                        f"falling back to best_single (swap={_swap}, verdict={_verdict})"
                                    )
                                    final_answer = _best_content
                                    gate_result = QualityGateResult.BEST_SINGLE
                                else:
                                    logger.info(
                                        f"[{context.query_id}] v4.1 semantic check: synthesis >= best_single, keeping"
                                    )
                            else:
                                logger.warning(
                                    f"[{context.query_id}] v4.1 semantic check failed "
                                    f"({_check_resp.error}), keeping synthesis"
                                )
                        except (asyncio.TimeoutError, ValueError) as _e:
                            logger.warning(f"[{context.query_id}] v4.1 semantic check failed ({type(_e).__name__}): {_e}, keeping synthesis")
                        except Exception as _e:
                            logger.error(f"[{context.query_id}] v4.1 semantic check unexpected error: {_e}, keeping synthesis", exc_info=True)

                logger.info(f"[{context.query_id}] QualityGate: {gate_result.value}")
                # v4.27 S4: [GATE_STAT] structured log for Gate decision distribution analysis.
                # Grep: grep '\[GATE_STAT\]' to build BEST_SINGLE vs SYNTHESIZED rate by type.
                _gate_score_gap = compute_score_gap(metadata)
                _gate_scores = compute_scores(metadata.model_evaluations, pairwise_mode=metadata.pairwise_evaluated) if metadata.model_evaluations else {}
                _gate_max_score = max(_gate_scores.values(), default=0.0)
                logger.info(
                    f"[{context.query_id}] [GATE_STAT] "
                    f"gate={gate_result.value} "
                    f"mode={mode} "
                    f"question_type={context.question_type.value if hasattr(context.question_type, 'value') else str(context.question_type)} "
                    f"strategy={adaptive_overrides.get('strategy_name', 'default') if adaptive_overrides else 'default'} "
                    f"contributors={len(successful)} "
                    f"score_gap={_gate_score_gap:.3f} "
                    f"max_score={_gate_max_score:.3f}"
                )

            # v4.22c: Unified BEST_SINGLE post-processing (exits B/C/D/E)
            # For fallback BEST_SINGLE exits (deadline/judge-fail/semantic-check),
            # ensure Perplexity citations are collected and fact_warnings gathered.
            if gate_result == QualityGateResult.BEST_SINGLE and not _fact_warnings:
                # Supplement Perplexity citations if not already added (exits B/C/D/E)
                if _perplexity_citation_urls and not any(
                    c.get("url") in set(_perplexity_citation_urls) for c in _citations_from_judge
                ):
                    _existing_fb = {c["url"] for c in _citations_from_judge}
                    for _purl in _perplexity_citation_urls:
                        if _purl not in _existing_fb:
                            _citations_from_judge.append({"url": _purl, "title": _purl})
                            _existing_fb.add(_purl)

                # Try to collect FactChecker warnings (task may have completed by now)
                if _factcheck_task is not None:
                    try:
                        _fb_fc = await asyncio.wait_for(asyncio.shield(_factcheck_task), timeout=1.0)
                        _default_ev = "\u4e0e\u641c\u7d22\u7ed3\u679c\u77db\u76fe"
                        for _fc_claim in _fb_fc.claims:
                            if _fc_claim.verdict == "contradicted":
                                _ev = _fc_claim.evidence or _default_ev
                                _fact_warnings.append(
                                    f"\u26a0\ufe0f {_fc_claim.claim} \u2014 \u641c\u7d22\u6765\u6e90\u663e\u793a: {_ev}"
                                )
                    except Exception:
                        pass  # Best-effort for fallback exits

            # === Step 5: Build result ===
            elapsed_ms = int((time.monotonic() - start) * 1000)

            # v4.1: Collect Judge tokens into named_calls for accurate cost tracking
            if synthesis and synthesis.model_id and (synthesis.prompt_tokens or synthesis.completion_tokens):
                _named_cost_calls.append((
                    synthesis.model_id,
                    synthesis.prompt_tokens,
                    synthesis.completion_tokens,
                ))

            # Token sum: contributors + all named calls + inherited (escalation)
            _named_tokens = sum(p + c for _, p, c in _named_cost_calls)
            total_tokens = sum(
                r.prompt_tokens + r.completion_tokens for r in responses
            ) + _named_tokens + extra_tokens[0] + context.inherited_tokens

            # Dynamic call count: contributors + judge + extractor + critic/refine rounds
            base_calls = len(responses) + 2  # +judge +extractor
            refinement_calls = extra_tokens[1] if len(extra_tokens) > 1 else 0

            # v4.2: Use adapter's global cost tracker for 100% accurate cost
            # This captures ALL API calls including MoA Layer 2, semantic check, planner, etc.
            _tracker_data = self._adapter.get_cost_tracker()
            estimated_cost = sum(cost for _, _, _, cost in _tracker_data)
            
            # Fallback to old estimation if tracker is empty (shouldn't happen)
            if estimated_cost == 0.0:
                estimated_cost = _estimate_cost(
                    responses,
                    extra_tokens=extra_tokens[0],
                    model_configs=self._config.models,
                    named_calls=_named_cost_calls,
                )

            # P0-2: Include individual_responses for LOW_CONFIDENCE (user may want to see them)
            include_individual = (
                context.output_depth == OutputDepth.LEVEL_3
                or gate_result == QualityGateResult.LOW_CONFIDENCE
            )

            # v3.3: Add judge final as third version
            if _draft_answers and final_answer:
                _judge_model = judge_model_id if gate_result != QualityGateResult.BEST_SINGLE else (_best_model or "best_single")
                _draft_answers.append({"stage": "judge_final", "model_id": _judge_model, "content": final_answer})

            # === Step 1.98: v4.25 Post-synthesis Verification (research only) ===
            # Judge synthesis may introduce new claims via [OTHERS] cross-pollination
            # that were never checked by the pre-synthesis FactChecker.
            # gemini_flash scans the final answer for specific factual claims
            # (numbers, statistics, dates, names) not backed by search snippets.
            # Results appended to _fact_warnings — no modification to final_answer.
            # Only fires: research + post_synthesis_verify=True + Tavily results exist.
            if (
                mode == "research"
                and self._config.features.post_synthesis_verify
                and final_answer
                and _tavily_search_results
                and gate_result != QualityGateResult.BEST_SINGLE
            ):
                try:
                    _psv_system = (
                        "你是一个事实核查专家。你会收到：\n"
                        "1. 一篇 AI 生成的研究报告（最终版本）\n"
                        "2. 支持该报告的搜索来源摘要\n\n"
                        "你的任务：找出报告中**具体的、可验证的声明**（数字、统计、日期、"
                        "专有名词、百分比等），这些声明在搜索来源中**没有明确依据**。\n"
                        "只报告新增的高风险未验证声明（≤5条）。\n\n"
                        "输出严格 JSON，不要其他文字：\n"
                        '{"new_unverified": ["声明1（原文引用）", ...]}'
                        "\n如果没有新增未验证声明，输出：{\"new_unverified\": []}"
                    )
                    _search_ctx_for_psv = "\n".join(
                        f"[来源{i+1}] {getattr(sr, 'title', '')} — "
                        f"{str(getattr(sr, 'content', '') or '')[:300]}"
                        for i, sr in enumerate(_tavily_search_results[:10])
                    )
                    _psv_user_msg = (
                        f"## 最终研究报告\n{final_answer[:3000]}\n\n"
                        f"## 搜索来源摘要\n{_search_ctx_for_psv}"
                    )
                    _psv_resp = await asyncio.wait_for(  # _named_cost_calls: PSV tokens tracked after response validation
                        self._adapter.call(
                            RoleCall(
                                call_id=f"psv-{context.query_id[:8]}",
                                model_id=self._config.judge.extractor_model or "gemini_3_flash",
                                role=Role.METADATA_EXTRACTOR,
                                system_prompt=_psv_system,
                                messages=[{"role": "user", "content": _psv_user_msg}],
                                timeout_seconds=8,
                                web_search=False,
                            )
                        ),
                        timeout=10.0,
                    )
                    if _psv_resp.success and _psv_resp.content:
                        # v4.30: track PSV token cost
                        if _psv_resp.prompt_tokens or _psv_resp.completion_tokens:
                            _named_cost_calls.append((
                                _psv_resp.model_id or self._config.judge.extractor_model or "gemini_3_flash",
                                _psv_resp.prompt_tokens,
                                _psv_resp.completion_tokens,
                            ))
                        _psv_data = _parse_fenced_json(_psv_resp.content)
                        _new_unverified: list[str] = _psv_data.get("new_unverified", [])[:5]
                        if _new_unverified:
                            for _claim in _new_unverified:
                                _fact_warnings.append(
                                    f"⚠️ [综合后新增] {_claim} — 搜索来源未确认"
                                )
                            logger.info(
                                f"[{context.query_id}] Post-synthesis verify: "
                                f"{len(_new_unverified)} new unverified claims flagged"
                            )
                        else:
                            logger.debug(
                                f"[{context.query_id}] Post-synthesis verify: no new unverified claims"
                            )
                except (asyncio.TimeoutError, json.JSONDecodeError, ValueError) as _psv_err:
                    logger.warning(
                        f"[{context.query_id}] Post-synthesis verify failed ({type(_psv_err).__name__}): {_psv_err}"
                    )
                except Exception as _psv_err:
                    logger.error(
                        f"[{context.query_id}] Post-synthesis verify unexpected error: {_psv_err}",
                        exc_info=True,
                    )

            # v4.19: Use citations built in _judge_and_extract (Tavily + Perplexity, deduped).
            _search_citations: list[dict] = _citations_from_judge

            # v4.20: Collect DivergenceAnalyzer result (was launched concurrently at Step 1.95)
            _divergence_points: list[dict] = []
            _consensus_points: list[str] = []
            if _divergence_task is not None:
                try:
                    _dmap = await asyncio.wait_for(asyncio.shield(_divergence_task), timeout=2.0)
                    _divergence_points = [
                        {
                            "topic": dp.topic,
                            "description": dp.description,
                            "positions": dp.positions,
                            "consensus_ratio": dp.consensus_ratio,
                            "difficulty": dp.difficulty,
                        }
                        for dp in (_dmap.divergence_points or [])
                    ]
                    # A4: Collect consensus_points (≤3) for "交叉验证" cross-validation display
                    _consensus_points = (_dmap.consensus_points or [])[:3]
                    logger.info(
                        f"[{context.query_id}] DivergenceAnalyzer: "
                        f"{len(_divergence_points)} divergence points, "
                        f"{len(_consensus_points)} consensus points"
                    )
                except Exception as _de:
                    logger.warning(f"[{context.query_id}] DivergenceAnalyzer timed out/failed: {_de}")

            result = QueryResult(
                query_id=context.query_id,
                question=context.question,
                mode=context.mode.value,
                resolved_mode=mode,
                final_answer=final_answer,
                key_insights=metadata.key_insights,
                topic_tags=metadata.topic_tags,
                confidence=metadata.confidence,
                consensus_type=metadata.consensus_type.value
                    if hasattr(metadata.consensus_type, "value")
                    else str(metadata.consensus_type),
                has_divergence=metadata.has_divergence,
                divergence_summary=metadata.divergence_summary,
                model_evaluations={
                    k: {"accuracy": v.accuracy, "reasoning": v.reasoning, "uniqueness": v.uniqueness}
                    for k, v in metadata.model_evaluations.items()
                },
                quality_gate_result=gate_result.value,
                best_single_score_gap=compute_score_gap(metadata),
                question_critique=question_critique,
                contributor_count=len(successful),
                total_model_calls=base_calls + refinement_calls,
                latency_ms=elapsed_ms,
                total_tokens=total_tokens,
                estimated_cost_usd=estimated_cost,
                output_depth=context.output_depth.value,
                divergence_report=metadata.divergence_summary,
                individual_responses=[
                    {"model_id": r.model_id, "content": r.content}
                    for r in successful
                ] if include_individual else None,
                low_confidence_actions=[],  # DEPRECATED: use guidance.suggestions
                draft_answers=_draft_answers,
                search_citations=_search_citations,
                divergence_points=_divergence_points,  # v4.20: structured divergence from DivergenceAnalyzer
                consensus_points=_consensus_points,    # A4: consensus points for 交叉验证 display
                fact_warnings=_fact_warnings,  # v4.22c: FactChecker warnings for BEST_SINGLE paths
            )

            # === Step 6: Enrich routing log with outcome ===
            enrich_routing_log(gate_result.value, metadata.confidence, query_id=context.query_id)

            # === Step 6b: Query Monitor — Layer 1 zero-cost signal ===
            _best_single_resp = get_best_response(successful, metadata) if successful else None
            _best_single_id = _best_single_resp.model_id if _best_single_resp else ""
            _best_single_content = _best_single_resp.content if _best_single_resp else ""
            write_layer1(
                result=result,
                contributor_responses=successful,
                best_single_model=_best_single_id,
                best_single_content=_best_single_content,
                extractor_metadata=metadata,
                question_type=context.question_type.value if hasattr(context.question_type, "value") else str(context.question_type),
                adaptive_strategy=adaptive_overrides.get("strategy_name", "default") if adaptive_overrides else "default",
                moa_applied=moa_layers >= 2 and not moa_suppressed and bool(successful),
                refinement_api_calls=extra_tokens[1] if len(extra_tokens) > 1 else 0,  # v4.27: API calls (critic+refine), not logical rounds
            )
            # Layer 2: fire-and-forget async eval (sampled, non-blocking)
            asyncio.ensure_future(run_layer2_async(
                query_id=result.query_id,
                question=result.question,
                final_answer=result.final_answer,
                best_single_content=_best_single_content,
                best_single_model=_best_single_id,
                adapter=self._adapter,
            ))

            # === Step 7: Emit event ===
            if self._event_bus:
                await self._event_bus.emit(QueryCompleted(
                    query_id=result.query_id,
                    question=result.question,
                    mode=result.mode,
                    resolved_mode=result.resolved_mode,
                    final_answer=result.final_answer,
                    key_insights=result.key_insights,
                    topic_tags=result.topic_tags,
                    confidence=result.confidence,
                    consensus_type=result.consensus_type,
                    has_divergence=result.has_divergence,
                    divergence_summary=result.divergence_summary,
                    quality_gate_result=result.quality_gate_result,
                    contributor_count=result.contributor_count,
                    total_model_calls=result.total_model_calls,
                    latency_ms=result.latency_ms,
                    user_id=context.user_id,
                    language=context.language,
                ))

            logger.info(
                f"[{context.query_id}] Pipeline complete: "
                f"{elapsed_ms}ms, gate={gate_result.value}, "
                f"confidence={metadata.confidence:.2f}"
            )

            # === 请求费用汇总日志 ===
            _cost_cny = round(result.estimated_cost_usd * 1.35, 4)
            logger.info(
                f"[{context.query_id}] 💰 费用汇总: "
                f"模式={result.resolved_mode}, "
                f"模型调用={result.total_model_calls}次, "
                f"总tokens={result.total_tokens}, "
                f"费用≈¥{_cost_cny:.4f} (${result.estimated_cost_usd:.6f})"
            )

            # === v5.2: GuidanceOutput canonical — Deep/Research path ===
            # Step 1: Companion Dispatcher post-guide (the only active guidance source)
            _guide_output_deep = None
            try:
                _guide_meta = {
                    "confidence": result.confidence,
                    "divergence_count": len(result.divergence_points),
                    "quality_gate_result": result.quality_gate_result,
                    "fast_path": result.fast_path,
                    "key_insights": result.key_insights[:3],
                }
                _disp_input_post = DispatcherInput(
                    question=context.question,
                    question_type=(
                        context.question_type.value
                        if hasattr(context.question_type, "value")
                        else str(context.question_type)
                    ),
                    was_auto_escalated=(context.inherited_tokens > 0),
                )
                _guide_output_deep = await self._companion_dispatcher.dispatch_guide(
                    _guide_meta, _disp_input_post,
                )
                logger.info(
                    f"[{context.query_id}] Dispatcher post-guide: "
                    f"trigger={_guide_output_deep.post_guide_trigger}, "
                    f"has_message={bool(_guide_output_deep.companion_message)}"
                )
            except Exception as _guide_e:
                logger.debug(f"[{context.query_id}] Dispatcher post-guide skipped: {_guide_e}")

            # Step 2: Build canonical GuidanceOutput → derive compatibility fields
            from agoracle.services.guidance_compat import derive_legacy_fields
            result.guidance = Orchestrator._build_guidance(
                guide_output=_guide_output_deep,
                dispatcher_output=_dispatcher_output,
                result=result,
                context=context,
            )
            result.next_steps, result.companion_guide = derive_legacy_fields(result.guidance)

            return result

        except Exception as e:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.error(f"[{context.query_id}] Pipeline failed: {e}", exc_info=True)
            
            # v4.2: Use adapter cost tracker for accurate error-path cost
            _tracker_data = self._adapter.get_cost_tracker()
            consumed_cost = sum(cost for _, _, _, cost in _tracker_data)
            if consumed_cost == 0.0:
                consumed_cost = _estimate_cost(responses, extra_tokens=extra_tokens[0], model_configs=self._config.models)
            
            # User sees generic message; full error stays in server logs only
            return self._error_result(
                context, 
                f"内部处理异常 (trace: {context.query_id})", 
                elapsed_ms,
                consumed_cost
            )

    async def _run_dispatcher_pre_route(
        self,
        context: QueryContext,
        mode: str,
        mode_config: ModeConfig,
        progress: ProgressReporter | None,
        start: float,
    ) -> tuple[QueryContext, str, ModeConfig, DispatcherOutput | None, QueryResult | None]:
        _dispatcher_output: DispatcherOutput | None = None
        if not (
            context.mode == Mode.AUTO
            and not context.dispatcher_routed
        ):
            return context, mode, mode_config, _dispatcher_output, None

        _dispatcher_start = time.monotonic()
        _model_health = {}
        if self._failure_monitor:
            for _mid in self._config.models:
                _model_health[_mid] = (
                    "degraded" if self._failure_monitor.is_degraded(_mid) else "ok"
                )
        # v5.2: Build lightweight session summary from Turn objects (zero LLM cost)
        _disp_session_summary = ""
        _disp_prev_meta: dict = {}
        if context.session_history:
            _recent = context.session_history[-3:]
            _parts = []
            for _t in _recent:
                _q_snip = (_t.question[:60] + "…") if len(_t.question) > 60 else _t.question
                _parts.append(f"Q: {_q_snip} → {_t.mode}")
            _disp_session_summary = " | ".join(_parts)
            _last = context.session_history[-1]
            _disp_prev_meta = {
                "mode": _last.mode,
                "key_insights": _last.key_insights[:3] if _last.key_insights else [],
                "answer_outline": _last.answer_outline[:200] if _last.answer_outline else "",
            }
        _disp_input = DispatcherInput(
            question=context.question,
            question_type=(
                context.question_type.value
                if hasattr(context.question_type, "value")
                else str(context.question_type)
            ),
            model_health=_model_health,
            session_summary=_disp_session_summary,
            previous_result_meta=_disp_prev_meta,
            user_usage_count=getattr(context, "user_usage_count", 0),
            user_verbosity=getattr(context, "user_verbosity", "normal"),
        )
        _dispatcher_output = await self._companion_dispatcher.dispatch_route(_disp_input)
        context.dispatcher_routed = True

        # Apply Dispatcher decision to pipeline config
        if _dispatcher_output.strategy == "single_model" and _dispatcher_output.single_model_id:
            # Single model direct call: override contributors, skip judge
            mode_config.contributors = [_dispatcher_output.single_model_id]
            mode_config.n_of_m = 1
            mode_config.skip_judge = True
            # Keep mode as original resolved_mode (e.g. 'light') — used in
            # downstream checks (auto-escalation, result building). skip_judge
            # already routes to the fast path.
            logger.info(
                f"[{context.query_id}] Dispatcher→single_model: "
                f"{_dispatcher_output.single_model_id}, pipeline_mode={mode}"
            )
        elif _dispatcher_output.strategy == "pipeline":
            # Route to a specific pipeline mode
            _target_mode = _dispatcher_output.mode or "deep"
            _new_mode_config = self._config.modes.get(_target_mode)
            if _new_mode_config and _target_mode != mode:
                mode = _target_mode
                context = QueryContext(
                    query_id=context.query_id,
                    question=context.question,
                    mode=context.mode,
                    resolved_mode=Mode(_target_mode),
                    intent=context.intent,
                    web_search_enabled=context.web_search_enabled,
                    critique_enabled=(_target_mode in ("deep", "research")),
                    output_depth=(
                        OutputDepth.LEVEL_2 if _target_mode == "deep"
                        else OutputDepth.LEVEL_3 if _target_mode == "research"
                        else OutputDepth.LEVEL_1
                    ),
                    question_type=context.question_type,
                    rag_results=context.rag_results,
                    user_profile_summary=context.user_profile_summary,
                    session_history=context.session_history,
                    language=context.language,
                    inherited_tokens=context.inherited_tokens,
                    user_id=context.user_id,
                    attachments=context.attachments,
                    dispatcher_routed=True,
                )
                mode_config = copy.copy(_new_mode_config)
                mode_config.contributors = list(mode_config.contributors)
                logger.info(
                    f"[{context.query_id}] Dispatcher→pipeline:{_target_mode}"
                )
        elif _dispatcher_output.strategy == "clarify":
            # Clarify: return early with companion message, no pipeline
            logger.info(f"[{context.query_id}] Dispatcher→clarify")
            return context, mode, mode_config, _dispatcher_output, QueryResult(
                query_id=context.query_id,
                question=context.question,
                mode=context.mode.value,
                resolved_mode=mode,
                final_answer=_dispatcher_output.companion_message,
                confidence=0.0,
                quality_gate_result=QualityGateResult.LOW_CONFIDENCE.value,
                latency_ms=int((time.monotonic() - start) * 1000),
            )

        _disp_elapsed = int((time.monotonic() - _dispatcher_start) * 1000)
        logger.info(
            f"[{context.query_id}] Dispatcher pre-route: {_disp_elapsed}ms"
        )

        # v5.1: Dispatcher decision log — JSONL for post-hoc route quality analysis
        if _dispatcher_output:
            try:
                _log_entry = self._companion_dispatcher.create_log(
                    query_id=context.query_id,
                    dispatcher_input=_disp_input,
                    output=_dispatcher_output,
                    latency_ms=_disp_elapsed,
                )
                _DISPATCHER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
                import json as _json_log
                from dataclasses import asdict as _asdict
                _log_dict = _asdict(_log_entry)
                _log_dict["timestamp"] = _log_dict["timestamp"].isoformat()
                with open(_DISPATCHER_LOG_PATH, "a", encoding="utf-8") as _lf:
                    _lf.write(_json_log.dumps(_log_dict, ensure_ascii=False) + "\n")
            except Exception as _log_e:
                logger.debug(f"[{context.query_id}] Dispatcher log write failed: {_log_e}")

        # v5.1: Emit companion_route SSE event for frontend skeleton→bubble transition
        if progress and _dispatcher_output and _dispatcher_output.strategy != "clarify":
            _question_type = (
                context.question_type.value
                if hasattr(context.question_type, "value")
                else context.question_type or "UNKNOWN"
            )
            _main_actions, _more_actions = self._companion_dispatcher.build_route_actions(
                _dispatcher_output, _question_type
            )
            _route_actions = _main_actions
            await progress.on_companion_route(
                message=_dispatcher_output.companion_message,
                actions=_route_actions,
                route_reason=_dispatcher_output.route_reason,
                auto_execute_seconds=15 if not _dispatcher_output.is_silent_route else 0,
                is_silent=_dispatcher_output.is_silent_route,
                resolved_mode=mode,
                contributor_count=mode_config.n_of_m if hasattr(mode_config, 'n_of_m') else len(getattr(mode_config, 'contributors', [])),
                more_actions=_more_actions,
            )

        return context, mode, mode_config, _dispatcher_output, None

    async def _run_fan_out_stage(
        self,
        context: QueryContext,
        mode: str,
        mode_config: ModeConfig,
        planner_result: dict | None,
        rag_section: str,
        search_attempted: bool,
        session_context: str,
        progress: ProgressReporter | None,
        draft_answers: list[dict[str, Any]],
    ) -> tuple[list[ModelResponse], QuestionCritique | None, dict, list[ModelResponse], bool]:
        # v2.8.4: Light 模式不发送 fan_out 进度事件，避免 preview→token 双重输出
        # 和无意义的 "fan_out to 1 contributors" 等中间状态噪音。
        fan_out_progress = None if mode_config.skip_judge else progress
        if fan_out_progress:
            await fan_out_progress.on_stage_start(
                "fan_out", f"{len(mode_config.contributors)} contributors"
            )

        # v3.3: LLM zero-shot classifier runs in parallel with fan-out (deep/research only).
        # gemini_3_flash classifies question type with higher accuracy than signal words.
        # Falls back to existing signal-word result on any error or timeout.
        llm_classify_task = None
        if mode_config.smart_routing and mode in ("deep", "research"):
            llm_classify_task = asyncio.create_task(
                classify_question_type_async(context.question)
            )

        # v4.5: Extract sub_questions from planner for contributor prompt injection
        _planner_sub_questions = (
            planner_result.get("sub_questions", [])
            if planner_result else []
        )
        responses, question_critique = await self._fan_out(
            context, mode_config, fan_out_progress,
            rag_section=rag_section,
            search_attempted=search_attempted,
            planner_sub_questions=_planner_sub_questions,
            prebuilt_session_section=session_context,
        )

        # Collect LLM classifier result (fan-out already done, so no extra wait)
        if llm_classify_task is not None:
            try:
                llm_qt = await asyncio.wait_for(llm_classify_task, timeout=3.0)  # v4.26: 1.0→3.0 covers proxy jitter
                if llm_qt != context.question_type:
                    logger.info(
                        f"[{context.query_id}] LLM router updated question_type: "
                        f"{context.question_type.value!r} → {llm_qt.value!r}"
                    )
                context.question_type = llm_qt
            except Exception as _llm_e:
                logger.debug(f"[{context.query_id}] LLM router task failed: {_llm_e}")

        # === Apply adaptive aggregation with final question_type ===
        # Runs here (post-fan-out) so LLM classifier result is incorporated.
        # Pipeline overrides affect: MoA, refinement rounds, best_single thresholds.
        if mode_config.smart_routing and mode in ("deep", "research"):
            strategy = get_strategy(context.question_type)
            adaptive_overrides = apply_strategy(
                strategy, mode_config, self._config.judge,
                context_query_id=context.query_id,
            )
            if adaptive_overrides.get("max_refinement_rounds") is not None:
                mode_config.max_refinement_rounds = adaptive_overrides["max_refinement_rounds"]
                if mode_config.max_refinement_rounds == 0:
                    mode_config.answer_critic = ""
            if "disable_best_single" in adaptive_overrides:
                mode_config.disable_best_single = adaptive_overrides["disable_best_single"]
        else:
            adaptive_overrides = {}
            logger.info(
                f"[{context.query_id}] Adaptive aggregation: DISABLED "
                f"(smart_routing={'on' if mode_config.smart_routing else 'off'}, mode={mode})"
            )

        successful = [r for r in responses if r.success and r.content]
        # v4.30: Detect UNDER_TARGET_N marker injected by fan_out_n_of_m.
        # When contributors < requested n (but >= min_viable), lower Gate's BEST_SINGLE
        # gap threshold so weaker synthesis evidence triggers BEST_SINGLE more readily.
        _under_target_n = any(
            r.call_id == "__under_target_n_marker__" for r in responses
        )
        if not successful:
            return responses, question_critique, adaptive_overrides, successful, _under_target_n

        if fan_out_progress:
            await fan_out_progress.on_stage_complete(
                "fan_out",
                f"{len(successful)}/{len(responses)} succeeded",
            )

        logger.info(
            f"[{context.query_id}] Fan-out complete: "
            f"{len(successful)}/{len(responses)} succeeded"
        )

        # === v3.3: Emit fan-out best draft (deep/research only) ===
        if not mode_config.skip_judge and successful:
            _fan_best = successful[0]  # fastest/first successful
            draft_answers.append({"stage": "fan_out_best", "model_id": _fan_best.model_id, "content": _fan_best.content})
            if progress:
                await progress.on_draft_answer("fan_out_best", _fan_best.model_id, _fan_best.content)

        return responses, question_critique, adaptive_overrides, successful, _under_target_n

    def _build_all_failed_result(
        self,
        context: QueryContext,
        responses: list[ModelResponse],
        mode: str,
        start: float,
    ) -> QueryResult:
        # v4.8: Graceful degradation — return a friendly message with retry
        # suggestions instead of a hard system error. User always sees content.
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _tracker_data = self._adapter.get_cost_tracker()
        consumed_cost = sum(cost for _, _, _, cost in _tracker_data)

        failed_errors = [r.error or "" for r in responses if not r.success]
        if failed_errors and all("QUOTA_EXHAUSTED" in e for e in failed_errors if e):
            _degraded_msg = (
                "⚠️ API 额度暂时不足，无法生成回答。请稍后重试或联系管理员充值。\n\n"
                "你也可以尝试切换到 Light 模式（消耗更少额度）。"
            )
        else:
            _degraded_msg = (
                "⚠️ 当前所有模型暂时不可用，无法生成完整回答。\n\n"
                "**建议：**\n"
                "1. 稍等片刻后重试（模型服务可能正在恢复）\n"
                "2. 尝试切换模式（如 Light 模式通常更稳定）\n"
                "3. 如果问题持续，请联系管理员检查服务状态\n\n"
                f"（技术信息：{len(responses)} 个模型均返回失败，trace: {context.query_id}）"
            )
        logger.error(
            f"[{context.query_id}] All {len(responses)} contributors failed. "
            f"Errors: {failed_errors[:3]}"
        )
        return QueryResult(
            query_id=context.query_id,
            question=context.question,
            mode=context.mode.value,
            resolved_mode=mode,
            final_answer=_degraded_msg,
            confidence=0.0,
            quality_gate_result=QualityGateResult.LOW_CONFIDENCE.value,
            contributor_count=0,
            total_model_calls=len(responses),
            latency_ms=elapsed_ms,
            estimated_cost_usd=consumed_cost,
            output_depth=context.output_depth.value,
            low_confidence_actions=[],  # DEPRECATED: use guidance.suggestions
        )

    async def _complete_light_fast_path(
        self,
        context: QueryContext,
        start: float,
        responses: list[ModelResponse],
        successful: list[ModelResponse],
        mode: str,
        mode_config: ModeConfig,
        progress: ProgressReporter | None,
        dispatcher_output: DispatcherOutput | None,
    ) -> QueryResult:
        best = successful[0]
        final_answer = best.content

        # v5.1: Flash quality check for Dispatcher single-model calls
        # Non-blocking: if Flash fails, assume confidence=0.7 (same as before)
        _single_quality = 0.7
        if dispatcher_output and dispatcher_output.strategy == "single_model":
            try:
                _single_quality = await self._companion_dispatcher.quality_check(
                    context.question, final_answer, best.model_id,
                )
                logger.info(
                    f"[{context.query_id}] Flash quality check: "
                    f"confidence={_single_quality:.2f} for {best.model_id}"
                )
            except Exception as _qc_e:
                logger.debug(f"[{context.query_id}] Flash quality check skipped: {_qc_e}")

        # 流式输出 — 唯一一次输出，不会出现 preview→token 双重显示
        if progress:
            for i in range(0, len(final_answer), 500):
                await progress.on_judge_token(final_answer[i:i+500])
            await progress.on_stage_complete("synthesis", "direct")

        elapsed_ms = int((time.monotonic() - start) * 1000)
        total_tokens = sum(
            r.prompt_tokens + r.completion_tokens for r in responses
        ) + context.inherited_tokens
        estimated_cost = _estimate_cost(responses, model_configs=self._config.models)

        result = QueryResult(
            query_id=context.query_id,
            question=context.question,
            mode=context.mode.value,
            resolved_mode=mode,
            final_answer=final_answer,
            key_insights=[],
            topic_tags=[],
            confidence=_single_quality,  # v5.1: Flash quality score (was fixed 0.7)
            consensus_type="SINGLE_FAST",
            has_divergence=False,
            divergence_summary=None,
            model_evaluations={},
            quality_gate_result=QualityGateResult.BEST_SINGLE.value,
            fast_path=True,
            best_single_score_gap=0.0,
            question_critique=None,
            contributor_count=1,
            total_model_calls=1,
            latency_ms=elapsed_ms,
            total_tokens=total_tokens,
            estimated_cost_usd=estimated_cost,
            output_depth=context.output_depth.value,
            divergence_report=None,
            individual_responses=None,
        )

        # Fire-and-forget: Extractor 异步跑话题追踪，不阻塞用户响应
        extractor_model_id = mode_config.extractor
        if extractor_model_id:
            asyncio.create_task(
                self._async_light_extract(context, successful, extractor_model_id)
            )

        enrich_routing_log(
            result.quality_gate_result, result.confidence,
            query_id=context.query_id,
        )

        if self._event_bus:
            await self._event_bus.emit(QueryCompleted(
                query_id=result.query_id,
                question=result.question,
                mode=result.mode,
                resolved_mode=result.resolved_mode,
                final_answer=result.final_answer,
                key_insights=result.key_insights,
                topic_tags=result.topic_tags,
                confidence=result.confidence,
                consensus_type=result.consensus_type,
                has_divergence=result.has_divergence,
                divergence_summary=result.divergence_summary,
                quality_gate_result=result.quality_gate_result,
                contributor_count=result.contributor_count,
                total_model_calls=result.total_model_calls,
                latency_ms=result.latency_ms,
                user_id=context.user_id,
                language=context.language,
            ))

        logger.info(
            f"[{context.query_id}] Light direct complete: "
            f"{elapsed_ms}ms, model={best.model_id}"
        )

        # === v3.6: Light mode Layer 1 monitoring (zero-cost, fire-and-forget) ===
        try:
            write_layer1(
                result=result,
                contributor_responses=successful,
                best_single_model=best.model_id,
                best_single_content=best.content,
                extractor_metadata=None,
            )
        except Exception as _e:
            logger.debug(f"[{context.query_id}] Light Layer1 write failed: {_e}")

        # === v5.2: GuidanceOutput canonical — Light path ===
        # Light now uses the same Dispatcher post-guide flow as Deep/Research.
        # Step 1: Companion Dispatcher post-guide
        _guide_out_light = None
        try:
            _guide_meta = {
                "confidence": result.confidence,
                "divergence_count": len(result.divergence_points),
                "quality_gate_result": result.quality_gate_result,
                "fast_path": result.fast_path,
                "key_insights": result.key_insights[:3],
            }
            _disp_input_post = DispatcherInput(
                question=context.question,
                question_type=(
                    context.question_type.value
                    if hasattr(context.question_type, "value")
                    else str(context.question_type)
                ),
                was_auto_escalated=(context.inherited_tokens > 0),
            )
            _guide_out_light = await self._companion_dispatcher.dispatch_guide(
                _guide_meta, _disp_input_post,
            )
            logger.info(
                f"[{context.query_id}] Dispatcher post-guide (light): "
                f"trigger={_guide_out_light.post_guide_trigger}, "
                f"has_message={bool(_guide_out_light.companion_message)}"
            )
        except Exception as _guide_e:
            logger.debug(f"[{context.query_id}] Dispatcher post-guide skipped (light): {_guide_e}")

        # Step 2: Build canonical GuidanceOutput → derive compatibility fields
        from agoracle.services.guidance_compat import derive_legacy_fields
        result.guidance = Orchestrator._build_guidance(
            guide_output=_guide_out_light,
            dispatcher_output=dispatcher_output,
            result=result,
            context=context,
        )
        result.next_steps, result.companion_guide = derive_legacy_fields(result.guidance)

        return result

    # ────────────────────────────────────────────────────────
    # Judge + Extraction (streaming-aware)
    # ────────────────────────────────────────────────────────

    async def _judge_and_extract(
        self,
        context: QueryContext,
        successful: list[ModelResponse],
        question_critique: QuestionCritique | None,
        mode: str,
        judge_model_id: str,
        extractor_model_id: str,
        progress: ProgressReporter | None = None,
        judge_prompt_override: str = "",
        _metadata_override: MetadataExtraction | None = None,
        _named_cost_calls: list[tuple[str, int, int]] | None = None,
        tavily_search_results: list | None = None,
        fact_check_section: str = "",
        session_context: str = "",
    ) -> tuple:
        """Run Judge synthesis + Metadata extraction.

        v3.1 PairRank→Augment flow (deep/research):
          1. Extractor runs first → determines best_model via pairwise ranking
          2. Judge runs with best_model_id → augments best answer instead of rewriting

        v3.2 Gate-before-Judge: when called from execute() after Gate evaluation,
          _metadata_override carries the already-computed metadata so Extractor
          is not called a second time.

        Light mode: parallel execution (unchanged, no best_model marking).

        Both paths produce the same ``(JudgeSynthesis, MetadataExtraction)``
        tuple so downstream logic (quality gate, refinement) is identical.
        """
        use_augment_flow = mode in ("deep", "research")

        if use_augment_flow:
            # ── v3.1: Sequential — Extractor first (or reuse override), then Judge ──
            if _metadata_override is not None:
                metadata = _metadata_override
            else:
                try:
                    metadata = await self._extractor.extract(
                        question=context.question,
                        responses=successful,
                        extractor_model_id=extractor_model_id,
                    )
                except Exception:
                    metadata = MetadataExtraction()

            # v4.1 cost tracking: collect Extractor tokens (only if not reusing pre-MoA metadata)
            if _metadata_override is None and metadata.extractor_model_id and _named_cost_calls is not None:
                _named_cost_calls.append((
                    metadata.extractor_model_id,
                    metadata.prompt_tokens,
                    metadata.completion_tokens,
                ))

            best_model_id = metadata.best_model
            # v3.1: key_insights from other models serve as augmentation candidates
            augment_insights = metadata.key_insights if best_model_id else None

            # v3.8: Compute second-best model for Judge cross-reference
            second_best_model_id = ""
            if best_model_id and metadata.model_evaluations:
                from agoracle.domain.quality_gate import compute_scores
                _all_scores = compute_scores(
                    metadata.model_evaluations,
                    pairwise_mode=metadata.pairwise_evaluated,
                )
                _sorted = sorted(_all_scores.items(), key=lambda x: x[1], reverse=True)
                if len(_sorted) >= 2:
                    second_best_model_id = _sorted[1][0]

            if best_model_id:
                logger.info(
                    f"[{context.query_id}] PairRanker: best_model={best_model_id} "
                    f"second_best={second_best_model_id} "
                    f"reason={metadata.best_model_reason!r} "
                    f"insights={len(augment_insights or [])}"
                )
            else:
                logger.warning(
                    f"[{context.query_id}] PairRanker: no best_model found, "
                    f"falling back to full synthesis"
                )

            # R1: build search_citations for deep/research — from Tavily + perplexity
            # v4.18: previously only perplexity citations (research-only) were collected.
            # Now Tavily SearchResult objects (already used for rag_section) are also
            # converted to citation dicts for both deep and research modes.
            _search_citations: list[dict] = []
            _consensus_map: dict[str, int] = {}
            _contributor_count = len([r for r in successful if r.success and r.content])
            if mode in ("deep", "research"):
                # Step R1a: Tavily web search results → citations (passed from execute() Step 0)
                if tavily_search_results:
                    _seen_urls: set[str] = set()
                    for sr in tavily_search_results:
                        url = getattr(sr, "url", "")
                        if url and url not in _seen_urls:
                            _seen_urls.add(url)
                            _search_citations.append({
                                "url": url,
                                "title": getattr(sr, "title", url) or url,
                            })

                # Step R1b: perplexity model citations (deep + research — API-provided)
                # v4.22: Read from resp.metadata["citations"] (populated by openai_adapter)
                # instead of getattr(resp, "citations") which was always None.
                # Expanded from research-only to deep+research — Perplexity citations
                # are valuable grounded sources in both modes.
                for resp in successful:
                    if resp.success and resp.content and "perplexity" in (resp.model_id or "").lower():
                        _citations_raw = resp.metadata.get("citations") or []
                        _existing_urls = {c["url"] for c in _search_citations}
                        for c in _citations_raw:
                            if isinstance(c, dict):
                                if c.get("url") not in _existing_urls:
                                    _search_citations.append(c)
                                    _existing_urls.add(c.get("url", ""))
                            elif isinstance(c, str) and c not in _existing_urls:
                                _search_citations.append({"url": c, "title": c})
                                _existing_urls.add(c)

                # R2: consensus_map (research only)
                # v5.3: Restored via Extractor's insight_agreements (model agreement count per insight).
                # Previous implementation (v4.26-disabled) used a broken .get(k,1)+1 counter on
                # augment_insights which had no per-contributor info, yielding count=2 for all entries.
                # Now the Extractor parses agreed_models from LLM output and provides real counts.
                # Fallback: if insight_agreements is empty, _consensus_map stays {}, equivalent to
                # the v4.26 behavior where Judge infers consensus from [BEST]/[OTHERS].
                if mode == "research" and metadata.insight_agreements:
                    _consensus_map = metadata.insight_agreements

            # v3.9: Self-preference mitigation — when Judge would augment its own answer,
            # inject an extra critical instruction to counteract the 10-25% self-preference
            # premium documented by Panickssery et al. (2024).
            self_preference_note = ""
            if best_model_id and best_model_id == judge_model_id:
                self_preference_note = (
                    "\n\n⚠️ 特别提示：被标注为 [BEST] 的回答恰好来自与你能力相当的模型。"
                    "请保持与其他回答完全相同的批判标准——特别检查是否有独特事实、"
                    "少数派视角或反驳论点被遗漏。不要因为它已被标注为最优就降低审查力度。"
                )
                logger.info(
                    f"[{context.query_id}] Self-preference mitigation: "
                    f"best_model==judge_model ({judge_model_id}), injecting critical note"
                )
            # v3.10 fix: self_preference_note must NOT be string-concatenated with
            # QuestionCritique object (causes TypeError). Append to .analysis instead,
            # or create a minimal QuestionCritique when there is no existing critique.
            if self_preference_note:
                if question_critique is not None:
                    existing = question_critique.analysis or ""
                    question_critique = QuestionCritique(
                        has_issues=question_critique.has_issues,
                        issue_type=question_critique.issue_type,
                        analysis=existing + self_preference_note,
                        suggested_reformulation=question_critique.suggested_reformulation,
                        severity=question_critique.severity,
                    )
                else:
                    question_critique = QuestionCritique(
                        has_issues=True,
                        analysis=self_preference_note,
                    )

            if progress:
                judge_start = time.monotonic()
                judge_tokens: list[str] = []
                try:
                    async for token in self._judge.synthesize_stream(
                        question=context.question,
                        responses=successful,
                        question_critique=question_critique,
                        rag_context=fact_check_section,
                        mode=mode,
                        judge_model_id=judge_model_id,
                        judge_prompt_override=judge_prompt_override,
                        best_model_id=best_model_id,
                        augment_insights=augment_insights,
                        second_best_model_id=second_best_model_id,
                        contributor_count=_contributor_count,
                        search_citations=_search_citations or None,
                        consensus_map=_consensus_map or None,
                        session_context=session_context,
                        language=context.language,
                    ):
                        judge_tokens.append(token)
                        await progress.on_judge_token(token)

                    judge_ms = int((time.monotonic() - judge_start) * 1000)
                    judge_answer = "".join(judge_tokens)
                    if not judge_answer.strip():
                        raise RuntimeError("Judge stream returned empty synthesis")
                    synthesis = JudgeSynthesis(
                        final_answer=judge_answer,
                        latency_ms=judge_ms,
                        success=True,
                    )
                except Exception as e:
                    logger.warning(f"Judge streaming failed, falling back: {e}")
                    synthesis = await self._judge.synthesize(
                        question=context.question,
                        responses=successful,
                        question_critique=question_critique,
                        rag_context=fact_check_section,
                        mode=mode,
                        judge_model_id=judge_model_id,
                        judge_prompt_override=judge_prompt_override,
                        best_model_id=best_model_id,
                        augment_insights=augment_insights,
                        second_best_model_id=second_best_model_id,
                        contributor_count=_contributor_count,
                        search_citations=_search_citations or None,
                        consensus_map=_consensus_map or None,
                        session_context=session_context,
                        language=context.language,
                    )
            else:
                try:
                    synthesis = await self._judge.synthesize(
                        question=context.question,
                        responses=successful,
                        question_critique=question_critique,
                        rag_context=fact_check_section,
                        mode=mode,
                        judge_model_id=judge_model_id,
                        judge_prompt_override=judge_prompt_override,
                        best_model_id=best_model_id,
                        augment_insights=augment_insights,
                        second_best_model_id=second_best_model_id,
                        contributor_count=_contributor_count,
                        search_citations=_search_citations or None,
                        consensus_map=_consensus_map or None,
                        session_context=session_context,
                        language=context.language,
                    )
                except Exception as e:
                    # v3.10 fix: catch TimeoutError / API errors that bypass call()'s
                    # internal handler (e.g. asyncio.wait_for timeout propagation).
                    logger.warning(f"[{context.query_id}] Judge synthesize failed: {e}, falling back")
                    synthesis = JudgeSynthesis(
                        final_answer="", latency_ms=0, success=False,
                        error=f"judge_synthesis_failed: {type(e).__name__}",
                    )

            return synthesis, metadata, _search_citations

        else:
            # ── Light mode: parallel (unchanged) ──────────────────
            meta_task = asyncio.create_task(
                self._extractor.extract(
                    question=context.question,
                    responses=successful,
                    extractor_model_id=extractor_model_id,
                )
            )

            if progress:
                judge_start = time.monotonic()
                judge_tokens: list[str] = []
                try:
                    async for token in self._judge.synthesize_stream(
                        question=context.question,
                        responses=successful,
                        question_critique=question_critique,
                        rag_context="",
                        mode=mode,
                        judge_model_id=judge_model_id,
                        judge_prompt_override=judge_prompt_override,
                        session_context=session_context,
                        language=context.language,
                    ):
                        judge_tokens.append(token)
                        await progress.on_judge_token(token)

                    judge_ms = int((time.monotonic() - judge_start) * 1000)
                    judge_answer = "".join(judge_tokens)
                    if not judge_answer.strip():
                        raise RuntimeError("Judge stream returned empty synthesis")
                    synthesis = JudgeSynthesis(
                        final_answer=judge_answer,
                        latency_ms=judge_ms,
                        success=True,
                    )
                except Exception as e:
                    logger.warning(f"Judge streaming failed, falling back: {e}")
                    synthesis = await self._judge.synthesize(
                        question=context.question,
                        responses=successful,
                        question_critique=question_critique,
                        rag_context="",
                        mode=mode,
                        judge_model_id=judge_model_id,
                        judge_prompt_override=judge_prompt_override,
                        session_context=session_context,
                        language=context.language,
                    )
            else:
                synthesis = await self._judge.synthesize(
                    question=context.question,
                    responses=successful,
                    question_critique=question_critique,
                    rag_context="",
                    mode=mode,
                    judge_model_id=judge_model_id,
                    judge_prompt_override=judge_prompt_override,
                    session_context=session_context,
                    language=context.language,
                )

            try:
                metadata = await meta_task
            except Exception:
                metadata = MetadataExtraction()

            return synthesis, metadata, []  # Light mode has no search citations

    # ────────────────────────────────────────────────────────
    # Light mode async extract (fire-and-forget)
    # ────────────────────────────────────────────────────────

    async def _async_light_extract(
        self,
        context: QueryContext,
        responses: list[ModelResponse],
        extractor_model_id: str,
    ) -> None:
        """Fire-and-forget Extractor for Light mode — topic tracking without blocking."""
        try:
            metadata = await asyncio.wait_for(
                self._extractor.extract(
                    question=context.question,
                    responses=responses,
                    extractor_model_id=extractor_model_id,
                ),
                timeout=8.0,  # generous timeout since it's async
            )
            if metadata.topic_tags and self._event_bus:
                # Emit topic tags for profile tracking (companion hints, depth gate)
                logger.debug(
                    f"[{context.query_id}] Light async extract: "
                    f"tags={metadata.topic_tags}"
                )
        except Exception as e:
            logger.debug(f"[{context.query_id}] Light async extract failed (non-blocking): {e}")

    # ────────────────────────────────────────────────────────
    # Stage: FactChecker launch (extracted from execute() Step 1.97)
    # ────────────────────────────────────────────────────────

    def _stage_launch_factcheck(
        self,
        context: QueryContext,
        mode: str,
        successful: list[ModelResponse],
        tavily_search_results: list,
    ) -> tuple[asyncio.Task | None, list[str]]:
        """Launch FactChecker as a background task and collect Perplexity citations.

        Returns (factcheck_task, perplexity_citation_urls).
        Only fires for deep/research with search evidence available.
        """
        # v4.22: Collect Perplexity citation URLs for structured fact extraction
        _perplexity_citation_urls: list[str] = []
        for _r in successful:
            if _r.success and _r.content and "perplexity" in (_r.model_id or "").lower():
                _raw = _r.metadata.get("citations") or []
                for _c in _raw:
                    if isinstance(_c, str) and _c:
                        _perplexity_citation_urls.append(_c)
                    elif isinstance(_c, dict) and _c.get("url"):
                        _perplexity_citation_urls.append(_c["url"])
                break  # only first Perplexity contributor

        # v4.23: fact_check_shadow gate — False skips FactChecker entirely
        _fact_check_enabled = getattr(self._config.features, "fact_check_shadow", True)
        if not (
            mode in ("deep", "research")
            and len(successful) >= 1
            and _fact_check_enabled
            and (tavily_search_results or _perplexity_citation_urls)
        ):
            return None, _perplexity_citation_urls

        _factcheck_task = asyncio.create_task(
            asyncio.wait_for(
                self._fact_checker.check(
                    question=context.question,
                    responses=successful,
                    search_citations=[
                        {
                            "url": getattr(sr, "url", ""),
                            "title": getattr(sr, "title", ""),
                            "snippet": getattr(sr, "content", "")[:500],
                        }
                        for sr in (tavily_search_results or [])
                        if getattr(sr, "url", "")
                    ] or None,
                    checker_model_id=self._config.judge.extractor_model or "gemini_3_flash",
                    perplexity_citations=_perplexity_citation_urls or None,
                ),
                timeout=25,
            )
        )
        return _factcheck_task, _perplexity_citation_urls

    # ────────────────────────────────────────────────────────
    # Stage: Gap Search (extracted from execute() Step 1.96)
    # ────────────────────────────────────────────────────────

    async def _stage_gap_search(
        self,
        context: QueryContext,
        mode: str,
        successful: list[ModelResponse],
        planner_result: dict | None,
        tavily_search_results: list,
        progress: ProgressReporter | None,
        named_cost_calls: list[tuple[str, int, int]],
    ) -> list:
        """Iterative Search: detect knowledge gaps and run supplementary searches.

        Returns extended tavily_search_results list (original + new deduped results).
        Only fires for research mode with iterative_search=True and existing Tavily results.
        """
        if not (
            mode == "research"
            and self._config.features.iterative_search
            and self._search
            and self._search.enabled
            and tavily_search_results
            and len(successful) >= 2
        ):
            return tavily_search_results

        if progress:
            await progress.on_stage_start("gap_search", "知识缺口检测中...")
        try:
            _gap_system = (
                "你是一个搜索缺口分析师。你会收到：\n"
                "1. 用户问题\n"
                "2. 已完成的搜索词列表（初始搜索已覆盖的范围）\n"
                "3. 多位分析师的回答摘要\n\n"
                "你的任务：找出分析师回答中提到的**重要子话题或具体事实**，"
                "但这些内容在初始搜索词中明显未被覆盖。\n"
                "生成 2-3 个补充搜索词（可中英混合），精准覆盖这些缺口。\n\n"
                "输出严格 JSON，不要其他文字：\n"
                '{"gap_queries": ["补充搜索词1", "supplementary query 2", ...]}'
            )
            # Build compact summary of contributor answers (first 600 chars each)
            _contrib_snippets = "\n".join(
                f"分析师{i+1}: {r.content[:600].replace(chr(10), ' ')}"
                for i, r in enumerate(successful[:5])
                if r.content
            )
            # Include initial search queries for context (from planner_result if available)
            _initial_queries = (
                planner_result.get("search_queries", []) if planner_result else []
            )
            _gap_user_msg = (
                f"## 用户问题\n{context.question[:300]}\n\n"
                f"## 初始搜索词\n{_initial_queries or ['(直接搜索原始问题)']}\n\n"
                f"## 分析师回答摘要\n{_contrib_snippets}"
            )
            _gap_resp = await asyncio.wait_for(
                self._adapter.call(  # cost-exempt: tokens tracked in named_cost_calls below
                    RoleCall(
                        call_id=f"gap-{context.query_id[:8]}",
                        model_id=self._config.judge.extractor_model or "gemini_3_flash",
                        role=Role.METADATA_EXTRACTOR,
                        system_prompt=_gap_system,
                        messages=[{"role": "user", "content": _gap_user_msg}],
                        timeout_seconds=8,
                        web_search=False,
                    )
                ),
                timeout=10.0,
            )
            if _gap_resp.success and _gap_resp.content:
                # v4.30: track gap search LLM token cost
                if _gap_resp.prompt_tokens or _gap_resp.completion_tokens:
                    named_cost_calls.append((
                        _gap_resp.model_id or self._config.judge.extractor_model or "gemini_3_flash",
                        _gap_resp.prompt_tokens,
                        _gap_resp.completion_tokens,
                    ))
                _gap_data = _parse_fenced_json(_gap_resp.content)
                _gap_queries: list[str] = _gap_data.get("gap_queries", [])[:3]
                if _gap_queries:
                    _gap_search_resp = await self._search.search_multi(_gap_queries)
                    if _gap_search_resp.success and _gap_search_resp.results:
                        # Extend tavily_search_results, dedup by URL
                        _existing_urls = {
                            getattr(r, "url", "") for r in tavily_search_results
                        }
                        _new_results = [
                            r for r in _gap_search_resp.results
                            if getattr(r, "url", "") not in _existing_urls
                        ]
                        tavily_search_results = tavily_search_results + _new_results
                        logger.info(
                            f"[{context.query_id}] Iterative search: "
                            f"{len(_gap_queries)} gap_queries → "
                            f"+{len(_new_results)} new results "
                            f"(total: {len(tavily_search_results)})"
                        )
                        if progress:
                            await progress.on_stage_complete(
                                "gap_search",
                                f"+{len(_new_results)} 条补充来源"
                            )
                    else:
                        logger.debug(
                            f"[{context.query_id}] Iterative search: "
                            f"gap search returned no results"
                        )
                        if progress:
                            await progress.on_stage_complete("gap_search", "无新增来源")
                else:
                    logger.debug(
                        f"[{context.query_id}] Iterative search: "
                        f"no gap_queries identified"
                    )
                    if progress:
                        await progress.on_stage_complete("gap_search", "无缺口")
        except (asyncio.TimeoutError, json.JSONDecodeError, ValueError) as _gap_err:
            logger.warning(
                f"[{context.query_id}] Iterative search failed ({type(_gap_err).__name__}): {_gap_err}"
            )
        except Exception as _gap_err:
            logger.error(
                f"[{context.query_id}] Iterative search unexpected error: {_gap_err}",
                exc_info=True,
            )
            if progress:
                await progress.on_stage_complete("gap_search", "跳过")
        return tavily_search_results

    # ────────────────────────────────────────────────────────
    # Stage: Unified Search (extracted from execute() Step 0)
    # ────────────────────────────────────────────────────────

    async def _stage_search(
        self,
        context: QueryContext,
        mode: str,
        planner_result: dict | None,
        progress: ProgressReporter | None,
    ) -> tuple[str, bool, list]:
        """Unified web search: search ONCE, inject into all contributor prompts.

        Returns (rag_section, search_attempted, tavily_search_results).
        Only fires for modes listed in config.search.modes with web_search enabled.
        """
        if not (
            self._search
            and self._search.enabled
            and context.web_search_enabled
            and mode in self._config.search.modes
        ):
            return "", False, []

        if progress:
            await progress.on_stage_start("search", context.question[:50])
        # v4.5: multi-query search — use Planner search_queries for broader coverage
        _multi_queries = (
            planner_result.get("search_queries", [])
            if planner_result and self._config.features.multi_query_search
            else []
        )
        if _multi_queries:
            search_response = await self._search.search_multi(_multi_queries)
            logger.info(
                f"[{context.query_id}] Multi-query search: "
                f"{len(_multi_queries)} queries → {len(search_response.results)} results"
            )
        else:
            search_response = await self._search.search(context.question)
        rag_section = SearchService.format_for_prompt(
            search_response, max_chars=self._config.search.max_chars
        )
        # v4.18: save results for citation injection at Judge stage
        _tavily_search_results: list = []
        if search_response.success:
            _tavily_search_results = search_response.results or []
        # Only suppress per-model search if unified search actually returned results.
        search_attempted = False
        if rag_section:
            search_attempted = True
            logger.info(
                f"[{context.query_id}] Unified search OK: "
                f"{len(search_response.results)} results, "
                f"{search_response.latency_ms}ms, injecting {len(rag_section)} chars"
            )
        else:
            logger.warning(
                f"[{context.query_id}] Unified search failed/empty "
                f"(success={search_response.success}, "
                f"error={search_response.error!r}), "
                f"falling back to per-model search"
            )
        if progress:
            n = len(search_response.results) if search_response.success else 0
            await progress.on_stage_complete(
                "search", f"{n} results, {search_response.latency_ms}ms"
            )
            # v4.20 MISS-2: emit citations immediately after search
            if _tavily_search_results:
                _early_citations = [
                    {"url": getattr(sr, "url", ""), "title": getattr(sr, "title", "") or getattr(sr, "url", "")}
                    for sr in _tavily_search_results
                    if getattr(sr, "url", "")
                ]
                if _early_citations:
                    await progress.on_citations_ready(_early_citations)

        return rag_section, search_attempted, _tavily_search_results

    # ────────────────────────────────────────────────────────
    # Stage: Planner-lite (extracted from execute() Step 0.4 + 0.5)
    # ────────────────────────────────────────────────────────

    async def _stage_planner(
        self,
        context: QueryContext,
        mode: str,
        mode_config: ModeConfig,
        progress: ProgressReporter | None,
        named_cost_calls: list[tuple[str, int, int]],
    ) -> dict | None:
        """Planner-lite: decompose question into sub_questions + search_queries.

        Returns planner_result dict or None.  Only fires for research mode
        with planner_lite=True and eligible question_type.
        """
        # === Step 0.4: Pre-Planner classification (Research + smart_routing only) ===
        if (
            self._config.features.planner_lite
            and mode == "research"
            and mode_config.smart_routing
        ):
            try:
                _pre_qt = await asyncio.wait_for(
                    classify_question_type_async(context.question), timeout=3.0
                )
                if _pre_qt != context.question_type:
                    logger.info(
                        f"[{context.query_id}] Pre-Planner router: "
                        f"{context.question_type.value!r} → {_pre_qt.value!r}"
                    )
                    context.question_type = _pre_qt
            except Exception as _pre_qt_err:
                logger.debug(
                    f"[{context.query_id}] Pre-Planner classify failed (keeping signal-word result): "
                    f"{_pre_qt_err}"
                )

        if not (
            self._config.features.planner_lite
            and mode == "research"
            and context.question_type in _PLANNER_ELIGIBLE_TYPES
        ):
            return None

        if progress:
            await progress.on_stage_start("planner", "问题分解中...")
        try:
            _planner_model_id = self._config.judge.extractor_model or "gemini_3_flash"
            _planner_system = (
                "你是专业研究规划师。将用户问题分解为独立子问题，并生成多样化搜索词。\n"
                "要求：\n"
                "1. sub_questions: 4-6个子问题，覆盖问题的不同维度（背景/现状/原因/影响/解决方案/未来趋势），每个子问题独立可搜索\n"
                "2. search_queries: 4-5个搜索词，必须满足：\n"
                "   - 语义不重叠（覆盖不同方面，避免同义替换）\n"
                "   - 中英混合（至少2个英文搜索词，英文搜索覆盖更广）\n"
                "   - 精准具体（避免泛化词，加入限定词提高精度）\n"
                "只输出 JSON，不要任何解释：\n"
                '{"sub_questions": ["子问题1", ...], "search_queries": ["精准搜索词1", "English query 2", ...]}\n\n'
                "示例（量子计算对密码学的影响）：\n"
                '{"sub_questions": ["量子计算当前技术水平如何？", "哪些密码算法最容易被量子攻破？", '
                '"后量子密码学主要方案有哪些？", "量子计算商业化时间线预测？"], '
                '"search_queries": ["quantum computing impact cryptography 2024", '
                '"post-quantum cryptography NIST standards", "量子计算 RSA 破解 时间预测", '
                '"lattice-based cryptography practical"]}'
            )
            _planner_resp = await asyncio.wait_for(
                self._adapter.call(  # cost-exempt: tokens tracked in named_cost_calls below
                    RoleCall(
                        call_id=f"planner-{context.query_id[:8]}",
                        model_id=_planner_model_id,
                        role=Role.CONTRIBUTOR,
                        system_prompt=_planner_system,
                        messages=[{"role": "user", "content": context.question[:500]}],
                        timeout_seconds=8,
                        web_search=False,
                    )
                ),
                timeout=10.0,
            )
            if _planner_resp.success and _planner_resp.content:
                # v4.30: track planner token cost
                if _planner_resp.prompt_tokens or _planner_resp.completion_tokens:
                    named_cost_calls.append((
                        _planner_resp.model_id or _planner_model_id,
                        _planner_resp.prompt_tokens,
                        _planner_resp.completion_tokens,
                    ))
                planner_result = _parse_fenced_json(_planner_resp.content)
                logger.info(
                    f"[{context.query_id}] Planner-lite OK: "
                    f"{len(planner_result.get('sub_questions', []))} sub_questions, "
                    f"{len(planner_result.get('search_queries', []))} search_queries"
                )
                if progress:
                    _sq = len(planner_result.get('sub_questions', []))
                    _sq2 = len(planner_result.get('search_queries', []))
                    await progress.on_stage_complete(
                        "planner", f"{_sq} 子问题, {_sq2} 搜索词"
                    )
                return planner_result
        except (asyncio.TimeoutError, json.JSONDecodeError, ValueError) as _planner_err:
            logger.warning(
                f"[{context.query_id}] Planner-lite failed ({type(_planner_err).__name__}): {_planner_err}"
            )
            if progress:
                await progress.on_stage_complete("planner", "跳过")
        except Exception as _planner_err:
            logger.error(
                f"[{context.query_id}] Planner-lite unexpected error: {_planner_err}",
                exc_info=True,
            )
            if progress:
                await progress.on_stage_complete("planner", "跳过")
        return None

    # ────────────────────────────────────────────────────────
    # Fan-out
    # ────────────────────────────────────────────────────────

    async def _fan_out(
        self,
        context: QueryContext,
        mode_config: ModeConfig,
        progress: ProgressReporter | None = None,
        rag_section: str = "",
        search_attempted: bool = False,
        planner_sub_questions: list[str] | None = None,
        prebuilt_session_section: str = "",
    ) -> tuple[list[ModelResponse], QuestionCritique | None]:
        """
        Fan-out to contributors + optional Question Critic (parallel).

        Delegates RoleCall building and dispatch to FanOutEngine.
        Orchestrator retains: critic task management, event emission.
        """
        # Build session context from history (fills {session_section} in prompts)
        # v5.0: Accept pre-built context from execute() to avoid duplicate LLM call
        if prebuilt_session_section:
            session_section = prebuilt_session_section
        elif context.session_history:
            session_section = ""
            try:
                mem_result = await self._conversation_memory.build_session_context(
                    context.session_history, token_budget=2000, language=context.language,
                )
                session_section = mem_result.context
                if mem_result.was_compressed:
                    logger.info(
                        f"[{context.query_id}] Session context compressed: "
                        f"{mem_result.summarized_turns} summarized + "
                        f"{mem_result.verbatim_turns} verbatim"
                    )
            except Exception as e:
                logger.warning(f"[{context.query_id}] Session context build failed: {e}")
        else:
            session_section = ""

        # Build contributor RoleCalls via FanOutEngine
        contributor_calls = self._fan_out_engine.build_contributor_calls(
            context, mode_config, rag_section, search_attempted,
            profile_section=context.user_profile_summary,
            session_section=session_section,
            planner_sub_questions=planner_sub_questions or [],
            failure_monitor=self._failure_monitor,
        )

        if not contributor_calls:
            logger.error("No available contributor models!")
            return [], None

        # Question Critic (parallel with contributors)
        critic_task = None
        if context.critique_enabled and mode_config.question_critic:
            critic_task = asyncio.create_task(
                self._refinement.call_question_critic(
                    context.question, mode_config.question_critic
                )
            )

        # Dispatch via FanOutEngine (N-of-M or wait-all)
        n_of_m = mode_config.n_of_m
        if n_of_m > 0 and n_of_m < len(contributor_calls):
            responses = await self._fan_out_engine.fan_out_n_of_m(
                contributor_calls, n_of_m, progress
            )
        else:
            responses = await self._fan_out_engine.fan_out_wait_all(
                contributor_calls, progress
            )

        # Emit failure events
        if self._event_bus:
            for resp in responses:
                if not resp.success:
                    await self._event_bus.emit(ModelCallFailed(
                        model_id=resp.model_id,
                        role=resp.role.value,
                        error=resp.error or "unknown",
                        latency_ms=resp.latency_ms,
                    ))

        # Collect critic result
        question_critique = None
        if critic_task:
            try:
                question_critique = await critic_task
            except Exception as e:
                logger.warning(f"Question critic failed: {e}")

        return responses, question_critique

    # ────────────────────────────────────────────────────────
    # Helpers
    # ────────────────────────────────────────────────────────

    @staticmethod
    def _error_result(
        context: QueryContext, error: str, elapsed_ms: int = 0, estimated_cost_usd: float = 0.0
    ) -> QueryResult:
        """Create an error QueryResult.
        
        v3.10: Now accepts estimated_cost_usd to track already-consumed API costs
        even when pipeline fails (e.g., Judge crashes after contributors succeed).
        """
        return QueryResult(
            query_id=context.query_id,
            question=context.question,
            mode=context.mode.value,
            resolved_mode=context.resolved_mode.value,
            final_answer=f"系统错误: {error}",
            latency_ms=elapsed_ms,
            estimated_cost_usd=estimated_cost_usd,
        )

    @staticmethod
    def _build_guidance(
        guide_output: "Any | None",
        dispatcher_output: "Any | None",
        result: "QueryResult",
        context: "QueryContext",
    ) -> GuidanceOutput:
        """Build canonical GuidanceOutput — SINGLE SOURCE OF TRUTH.

        Priority: Dispatcher guide (if it produced content) > empty.
        Legacy fields (`next_steps`, `companion_guide`) are compatibility
        projections derived from this in the caller.
        """
        def _build_dispatcher_confidence() -> tuple[str, str]:
            _raw_confidence = getattr(result, "confidence", None)
            if _raw_confidence is None:
                return "", "medium"
            try:
                _confidence = float(_raw_confidence)
            except (TypeError, ValueError):
                return "", "medium"

            _level = "high" if _confidence >= 0.8 else ("medium" if _confidence >= 0.5 else "low")
            _parts: list[str] = []
            _quality_gate = getattr(result, "quality_gate_result", "")

            if _quality_gate == QualityGateResult.LOW_CONFIDENCE.value:
                _parts.append("回答专家共识度较低")
            elif _quality_gate == QualityGateResult.BEST_SINGLE.value:
                _parts.append("基于单个专家回答")
            elif _confidence >= 0.8 and not getattr(result, "has_divergence", False):
                _parts.append("多专家高度一致")
            elif _confidence >= 0.5:
                _parts.append("多专家基本一致")
            else:
                _parts.append("专家意见分散")

            if getattr(result, "has_divergence", False):
                _parts.append("部分观点存在分歧")

            _fact_warnings = getattr(result, "fact_warnings", None) or []
            if _fact_warnings:
                _parts.append(f"{len(_fact_warnings)} 处待核实")

            _level_label = {"high": "高", "medium": "中", "low": "低"}.get(_level, "中")
            return f"专家共识度{_level_label} · {'，'.join(_parts)}", _level

        # 1. Dispatcher is the only active guidance producer.
        _disp_has_content = (
            guide_output is not None
            and (getattr(guide_output, 'companion_message', None) or getattr(guide_output, 'suggested_actions', None))
        )
        if _disp_has_content:
            _conf_stmt, _conf_level = _build_dispatcher_confidence()
            _route_reason = (
                getattr(guide_output, 'route_reason', '')
                or (getattr(dispatcher_output, 'route_reason', '') if dispatcher_output else '')
            )
            _suggestions = []
            for act in (getattr(guide_output, 'suggested_actions', None) or []):
                _suggestions.append(GuidanceSuggestion(
                    label=act.get("label", ""),
                    action_type=act.get("action_type", ""),
                    action_payload=act.get("action_payload", {}),
                    estimated_seconds=act.get("estimated_seconds", 0),
                    requires_confirm=act.get("requires_confirm", False),
                ))
            _intensity = GuidanceIntensity.RICH.value if _suggestions else GuidanceIntensity.LIGHT.value
            _post_trigger = getattr(guide_output, 'post_guide_trigger', '')
            return GuidanceOutput(
                source="dispatcher",
                confidence_statement=_conf_stmt,
                confidence_level=_conf_level,
                message=getattr(guide_output, 'companion_message', ''),
                suggestions=_suggestions,
                intensity=_intensity,
                is_folded=_post_trigger == "fold",
                show_dismiss=bool(_suggestions),
                route_reason=_route_reason,
                trigger=_post_trigger,
            )

        # 2. No guidance at all
        return GuidanceOutput(
            source="none",
            confidence_statement="",
            confidence_level="medium",
        )
