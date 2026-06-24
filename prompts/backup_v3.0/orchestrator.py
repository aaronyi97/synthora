"""
Orchestrator — the core pipeline that executes multi-model queries.

Implements three modes (Light, Deep, Research) with:
  - Parallel fan-out to contributors
  - N-of-M strategy (Light mode: wait for fastest N of M)
  - Judge synthesis + parallel Metadata extraction
  - Quality Gate
  - Answer Critic + Judge refinement (Deep mode only)
  - Event emission for side effects
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from dataclasses import replace as dc_replace
from datetime import datetime

from agoracle.adapters.judge.llm_judge import LLMJudge
from agoracle.adapters.judge.metadata_extractor import LLMMetadataExtractor
from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter
from agoracle.config.schema import AppConfig, JudgeConfig, ModeConfig
from agoracle.domain.events import ModelCallFailed, QueryCompleted
from agoracle.domain.quality_gate import (
    QualityGateThresholds,
    compute_score_gap,
    evaluate_gate,
    get_best_response,
    should_trigger_answer_critic,
)
from agoracle.domain.types import (
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
)
from agoracle.domain.router import enrich_routing_log
from agoracle.services.conversation_memory import ConversationMemoryService
from agoracle.services.event_bus import EventBus
from agoracle.services.adaptive_aggregation import apply_strategy, get_strategy
from agoracle.services.fan_out import FanOutEngine
from agoracle.services.prompt_loader import PromptLoader
from agoracle.services.refinement import RefinementEngine
from agoracle.services.search_service import SearchService

logger = logging.getLogger(__name__)

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
) -> float:
    """Estimate total cost in USD from token usage across all model calls.

    If model_configs is provided, uses per-model input/output pricing.
    Otherwise falls back to a blended default rate.
    """
    total = 0.0
    for r in responses:
        mc = model_configs.get(r.model_id) if model_configs else None
        if mc and (mc.cost_per_1m_input > 0 or mc.cost_per_1m_output > 0):
            total += r.prompt_tokens * mc.cost_per_1m_input / 1_000_000
            total += r.completion_tokens * mc.cost_per_1m_output / 1_000_000
        else:
            tokens = r.prompt_tokens + r.completion_tokens
            total += tokens * _DEFAULT_COST_PER_1M / 1_000_000
    # Extra tokens (critic/refine) — use default rate as we don't track per-call model_id
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
    ) -> None:
        self._config = config
        self._adapter = model_adapter
        self._judge = judge
        self._extractor = extractor
        self._prompts = prompt_loader
        self._event_bus = event_bus
        self._search = search_service
        self._refinement = RefinementEngine(config, model_adapter, judge, prompt_loader)
        self._fan_out_engine = FanOutEngine(config, model_adapter, prompt_loader)
        self._conversation_memory = ConversationMemoryService(
            model_adapter, summary_model_id="gemini_3_flash",
        )

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
        # TODO: Judge.synthesize/refine and Extractor internal tokens not yet tracked.
        extra_tokens = [0, 0]

        if not mode_config:
            return self._error_result(context, f"Mode '{mode}' not found in config")

        # Copy mode_config so pipeline adjustments don't mutate shared state
        mode_config = copy.copy(mode_config)
        mode_config.contributors = list(mode_config.contributors)  # deep copy the list

        # === v2.5.3: Adaptive aggregation — question_type drives synthesis strategy ===
        # Each question type maps to a specific aggregation strategy:
        #   FACTUAL → vote (accuracy-first, no MoA, no refine)
        #   ANALYTICAL/TECHNICAL → debate (full MoA + refine)
        #   CONTROVERSIAL → multi-perspective (preserve divergence, no MoA)
        #   CREATIVE → best-single (no MoA, no refine, low gap threshold)
        qt = context.question_type
        adaptive_overrides = {}
        if mode_config.smart_routing and mode in ("deep", "research"):
            strategy = get_strategy(qt)
            adaptive_overrides = apply_strategy(
                strategy, mode_config, self._config.judge,
                context_query_id=context.query_id,
            )

            # Apply pipeline overrides
            if adaptive_overrides.get("max_refinement_rounds") is not None:
                mode_config.max_refinement_rounds = adaptive_overrides["max_refinement_rounds"]
                if mode_config.max_refinement_rounds == 0:
                    mode_config.answer_critic = ""
            if adaptive_overrides.get("disable_best_single") is not None:
                mode_config.disable_best_single = adaptive_overrides["disable_best_single"]
            # v2.9: contributor_override — replace contributor list for dual/triple model race
            if adaptive_overrides.get("contributor_override"):
                override_list = adaptive_overrides["contributor_override"]
                # Only use models that exist in config
                valid = [m for m in override_list if m in [c for c in mode_config.contributors]]
                if valid:
                    logger.info(
                        f"[{context.query_id}] Contributor override: "
                        f"{mode_config.contributors} → {valid}"
                    )
                    mode_config.contributors = valid
                    mode_config.n_of_m = len(valid)  # wait for all
        else:
            logger.info(
                f"[{context.query_id}] Adaptive aggregation: DISABLED "
                f"(smart_routing={'on' if mode_config.smart_routing else 'off'}, mode={mode})"
            )

        # A3: Hard budget limits — prevent runaway cost and latency
        # v2.4.1: Deep Judge is now sonnet_thinking (fast) → tighten to ×3
        # Research keeps ×5 (opus as judge needs more headroom)
        # Deep: 90s×3=270s≈4.5min, Research: 180s×5=900s=15min
        budget_multiplier = 5 if mode == "research" else 3
        deadline = start + mode_config.max_timeout_seconds * budget_multiplier
        # Max API calls: contributors + judge + extractor + critic/refine rounds × 2
        n_contributors = len(mode_config.contributors)
        max_refinement = getattr(mode_config, 'max_refinement_rounds', 1)
        max_api_calls = n_contributors + 2 + (max_refinement * 3) + 4  # ×3 for fallback refine, +4 safety margin

        logger.info(
            f"[{context.query_id}] Starting pipeline: mode={mode}, "
            f"web_search={context.web_search_enabled}, "
            f"critique={context.critique_enabled}"
        )

        if progress:
            await progress.on_stage_start("pipeline", f"mode={mode}")

        try:
            # === Step 0: Unified search (Phase 2) ===
            # Search ONCE, inject results into ALL contributor prompts.
            # Only for Deep/Research; skipped if web_search disabled or no SearchService.
            rag_section = ""
            search_attempted = False  # True = unified search SUCCEEDED, suppress per-model search
            if (
                self._search
                and self._search.enabled
                and context.web_search_enabled
                and mode in self._config.search.modes
            ):
                if progress:
                    await progress.on_stage_start("search", context.question[:50])
                search_response = await self._search.search(context.question)
                rag_section = SearchService.format_for_prompt(
                    search_response, max_chars=self._config.search.max_chars
                )
                # Only suppress per-model search if unified search actually returned results.
                # If it failed or returned empty, fall back to per-model search.
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

            # === Step 1: Fan-out to contributors ===
            # v2.8.4: Light 模式不发送 fan_out 进度事件，避免 preview→token 双重输出
            # 和无意义的 "fan_out to 1 contributors" 等中间状态噪音。
            fan_out_progress = None if mode_config.skip_judge else progress
            if fan_out_progress:
                await fan_out_progress.on_stage_start(
                    "fan_out", f"{len(mode_config.contributors)} contributors"
                )

            responses, question_critique = await self._fan_out(
                context, mode_config, fan_out_progress,
                rag_section=rag_section,
                search_attempted=search_attempted,
            )

            successful = [r for r in responses if r.success and r.content]
            if not successful:
                return self._error_result(context, "All contributor models failed")

            if fan_out_progress:
                await fan_out_progress.on_stage_complete(
                    "fan_out",
                    f"{len(successful)}/{len(responses)} succeeded",
                )

            logger.info(
                f"[{context.query_id}] Fan-out complete: "
                f"{len(successful)}/{len(responses)} succeeded"
            )

            # === Step 1.5a: Light fast path — Kimi 直通，零开销 ===
            # v2.8.4: 彻底清理多模型遗留组件。
            # Light = Kimi 直通。不需要 Extractor/质量门/divergence/重复流式输出。
            # 之前的问题: Extractor 同步等待 4s、preview→final 双重输出导致"输出变更"、
            # 无意义的 divergence 检查和质量门计算。
            if mode_config.skip_judge:
                best = successful[0]
                final_answer = best.content

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
                    confidence=0.7,  # 单模型固定合理置信度
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
                    ))

                logger.info(
                    f"[{context.query_id}] Light direct complete: "
                    f"{elapsed_ms}ms, model={best.model_id}"
                )
                return result

            # === Step 1.5a: skip_synthesis — model race channel (v2.9) ===
            # For question types where aggregation hurts (factual/controversial/cultural):
            #   N models answer → Extractor scores all (1 API call) → pick highest quality.
            #   Skips: Judge synthesis, MoA, Answer Critic, Refinement (saves 3-5 API calls).
            if adaptive_overrides.get("skip_synthesis") and successful:
                if progress:
                    await progress.on_stage_start("synthesis", "model race — scoring answers")

                # Score all answers with Extractor (1 lightweight API call)
                extractor_model_id = mode_config.extractor
                race_metadata = None
                if extractor_model_id:
                    try:
                        race_metadata = await self._extractor.extract(
                            question=context.question,
                            responses=successful,
                            extractor_model_id=extractor_model_id,
                        )
                    except Exception as e:
                        logger.warning(
                            f"[{context.query_id}] Model race Extractor failed ({e}), "
                            f"falling back to longest-answer"
                        )

                # Pick winner by quality score; fallback to reliable model if Extractor failed
                if race_metadata and race_metadata.model_evaluations:
                    best = get_best_response(successful, race_metadata)
                    selection_method = "extractor_scored"
                else:
                    # v3.0: Prefer historically reliable models over longest answer
                    _RELIABLE_ORDER = ["claude_opus_thinking", "kimi", "deepseek_reasoner"]
                    best = next(
                        (r for mid in _RELIABLE_ORDER
                         for r in successful if r.model_id == mid),
                        None,
                    )
                    if best:
                        selection_method = "reliable_model_fallback"
                    else:
                        best = max(successful, key=lambda r: len(r.content))
                        selection_method = "length_fallback"

                if not best:
                    best = successful[0]
                    selection_method = "first_fallback"

                final_answer = best.content

                logger.info(
                    f"[{context.query_id}] Model race: winner={best.model_id} "
                    f"({selection_method}), {len(successful)} competed"
                )

                if progress:
                    for i in range(0, len(final_answer), 500):
                        await progress.on_judge_token(final_answer[i:i+500])
                    await progress.on_stage_complete(
                        "synthesis",
                        f"winner={best.model_id} ({selection_method})"
                    )

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
                    confidence=0.8,
                    consensus_type="MODEL_RACE",
                    has_divergence=False,
                    divergence_summary=None,
                    model_evaluations={},
                    quality_gate_result=QualityGateResult.BEST_SINGLE.value,
                    fast_path=True,
                    best_single_score_gap=0.0,
                    question_critique=question_critique,
                    contributor_count=len(successful),
                    total_model_calls=len(responses),
                    latency_ms=elapsed_ms,
                    total_tokens=total_tokens,
                    estimated_cost_usd=estimated_cost,
                    output_depth=context.output_depth.value,
                    divergence_report=None,
                    individual_responses=None,
                )

                # v3.0: Fire-and-forget topic tracking (same pattern as Light mode)
                # RACE channel was missing this — affects DepthGate and companion hints
                if not race_metadata:
                    _race_ext_model = mode_config.extractor
                    if _race_ext_model:
                        asyncio.create_task(
                            self._async_light_extract(context, successful, _race_ext_model)
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
                    ))

                logger.info(
                    f"[{context.query_id}] Model race complete: "
                    f"{elapsed_ms}ms, winner={best.model_id}, "
                    f"type={context.question_type.value}, "
                    f"competitors={[r.model_id for r in successful]}"
                )
                return result

            # === Step 1.5: MoA Layer 2 (Research only) ===
            # Each contributor sees others' answers and generates an improved version.
            # This is the key differentiator: models absorb each other's insights.
            # Adaptive aggregation can suppress MoA for question types where convergence hurts.
            moa_layers = getattr(mode_config, 'moa_layers', 1)
            moa_suppressed = adaptive_overrides.get("moa_enabled") is False
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
                else:
                    logger.warning(
                        f"[{context.query_id}] MoA Layer 2 failed, using Layer 1 responses"
                    )

                if progress:
                    await progress.on_stage_complete(
                        "moa_layer2",
                        f"{len(moa_successful) if moa_successful else 0} improved",
                    )

            # === Step 2: Judge + Metadata Extraction (parallel) ===
            judge_model_id = mode_config.judge
            extractor_model_id = mode_config.extractor

            if progress:
                await progress.on_stage_start(
                    "synthesis", f"Judge: {judge_model_id}"
                )

            synthesis, metadata = await self._judge_and_extract(
                context=context,
                successful=successful,
                question_critique=question_critique,
                mode=mode,
                judge_model_id=judge_model_id,
                extractor_model_id=extractor_model_id,
                progress=progress,
                judge_prompt_override=adaptive_overrides.get("judge_prompt_key", ""),
            )

            if progress:
                await progress.on_stage_complete(
                    "synthesis",
                    f"{len(synthesis.final_answer)} chars",
                )

            if not synthesis.success:
                # Judge failed — try fallback (skip the primary that already failed)
                synthesis = await self._refinement.judge_fallback(
                    context, successful, question_critique, mode,
                    primary_judge_id=judge_model_id,
                    judge_prompt_override=adaptive_overrides.get("judge_prompt_key", ""),
                )

            # === Step 3: Quality Gate (BEFORE refinement) ===
            gate_thresholds = _gate_thresholds_from_judge(self._config.judge)
            # Adaptive overrides for Quality Gate thresholds (frozen dataclass → replace)
            gate_overrides = {}
            if "best_single_gap_threshold" in adaptive_overrides:
                gate_overrides["best_single_gap_threshold"] = adaptive_overrides["best_single_gap_threshold"]
            if "best_single_min_score" in adaptive_overrides:
                gate_overrides["best_single_min_score"] = adaptive_overrides["best_single_min_score"]
            if gate_overrides:
                gate_thresholds = dc_replace(gate_thresholds, **gate_overrides)
            if not getattr(self._config.judge, 'quality_gate_enabled', True):
                gate_result = QualityGateResult.SYNTHESIZED
                logger.info(f"[{context.query_id}] QualityGate: DISABLED by config")
            else:
                gate_result = evaluate_gate(successful, metadata, gate_thresholds)

            # Research mode: disable BEST_SINGLE — always synthesize for maximum coverage
            if gate_result == QualityGateResult.BEST_SINGLE and mode_config.disable_best_single:
                logger.info(
                    f"[{context.query_id}] QualityGate: BEST_SINGLE overridden "
                    f"(disable_best_single=True for mode={mode}), using SYNTHESIZED"
                )
                gate_result = QualityGateResult.SYNTHESIZED

            # === Step 3.5: Log quality gate decision for monitoring (v2.7.9d) ===
            try:
                _best_model = ""
                _best_score = 0.0
                _gap = 0.0
                if metadata.model_evaluations:
                    _best_eval = max(metadata.model_evaluations, key=lambda e: e.score)
                    _best_model = _best_eval.model_id
                    _best_score = _best_eval.score
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
                    question_type=qt.value if hasattr(qt, 'value') else str(qt),
                )
            except Exception as e:
                logger.debug(f"Judge decision log write failed: {e}")  # #24: never silent

            # === Step 4: Route based on gate result ===
            if gate_result == QualityGateResult.BEST_SINGLE:
                # One model dominates — adopt it directly, skip refinement (saves cost)
                best = get_best_response(successful, metadata)
                if best:
                    final_answer = best.content
                    logger.info(
                        f"[{context.query_id}] QualityGate: BEST_SINGLE "
                        f"(adopted {best.model_id})"
                    )
                else:
                    # Best model not found (ID mismatch) — correct label to avoid
                    # decoupling between gate_result tag and actual output
                    gate_result = QualityGateResult.SYNTHESIZED
                    final_answer = synthesis.final_answer
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
                        # 🔴-3 fix: carry Light pipeline tokens to Deep via inherited_tokens
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
                            inherited_tokens=context.inherited_tokens + light_tokens,
                        )
                        return await self.execute(context_deep, progress)

                # 🟠-3: Deep/Research LOW_CONFIDENCE — attempt refinement as recovery
                # A3: Check budget before refinement
                budget_ok = (
                    time.monotonic() < deadline
                    and (len(responses) + extra_tokens[1]) < max_api_calls
                )
                if mode in ("deep", "research") and synthesis.success and mode_config.answer_critic and budget_ok:
                    logger.info(
                        f"[{context.query_id}] LOW_CONFIDENCE recovery: "
                        f"attempting refinement for mode={mode}"
                    )
                    synthesis = await self._refinement.deep_refinement(
                        context, synthesis, mode_config, extra_tokens,
                        judge_prompt_override=adaptive_overrides.get("judge_prompt_key", ""),
                    )
                elif not budget_ok:
                    logger.warning(
                        f"[{context.query_id}] Skipping LOW_CONFIDENCE refinement "
                        f"(budget exceeded: deadline or max_api_calls)"
                    )

                final_answer = synthesis.final_answer
                # P0-2: Build actionable suggestions instead of just a text disclaimer
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
                if mode in ("deep", "research") and synthesis.success and budget_ok:
                    if should_trigger_answer_critic(metadata, gate_thresholds):
                        synthesis = await self._refinement.deep_refinement(
                            context, synthesis, mode_config, extra_tokens,
                            judge_prompt_override=adaptive_overrides.get("judge_prompt_key", ""),
                        )
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
                final_answer = synthesis.final_answer
                logger.info(f"[{context.query_id}] QualityGate: SYNTHESIZED")

            # === Step 5: Build result ===
            elapsed_ms = int((time.monotonic() - start) * 1000)

            # Token sum: contributors + critic/refine (extra_tokens) + inherited (escalation)
            total_tokens = sum(
                r.prompt_tokens + r.completion_tokens for r in responses
            ) + extra_tokens[0] + context.inherited_tokens

            # Dynamic call count: contributors + judge + extractor + critic/refine rounds
            base_calls = len(responses) + 2  # +judge +extractor
            refinement_calls = extra_tokens[1] if len(extra_tokens) > 1 else 0

            # Cost estimation from contributor token usage + extra (critic/refine)
            estimated_cost = _estimate_cost(responses, extra_tokens=extra_tokens[0], model_configs=self._config.models)

            # P0-2: Include individual_responses for LOW_CONFIDENCE (user may want to see them)
            include_individual = (
                context.output_depth == OutputDepth.LEVEL_3
                or gate_result == QualityGateResult.LOW_CONFIDENCE
            )

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
                low_confidence_actions=low_conf_actions if gate_result == QualityGateResult.LOW_CONFIDENCE else [],
            )

            # === Step 6: Enrich routing log with outcome ===
            enrich_routing_log(gate_result.value, metadata.confidence, query_id=context.query_id)

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
                ))

            logger.info(
                f"[{context.query_id}] Pipeline complete: "
                f"{elapsed_ms}ms, gate={gate_result.value}, "
                f"confidence={metadata.confidence:.2f}"
            )

            return result

        except Exception as e:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.error(f"[{context.query_id}] Pipeline failed: {e}", exc_info=True)
            return self._error_result(context, str(e), elapsed_ms)

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
    ) -> tuple:
        """Run Judge synthesis + Metadata extraction in parallel.

        When *progress* is set, Judge tokens are streamed through the
        callback.  Otherwise a single non-streaming call is made.
        Both paths produce the same ``(JudgeSynthesis, MetadataExtraction)``
        tuple so downstream logic (quality gate, refinement) is identical.
        """
        # Always start metadata extraction in background
        meta_task = asyncio.create_task(
            self._extractor.extract(
                question=context.question,
                responses=successful,
                extractor_model_id=extractor_model_id,
            )
        )

        if progress:
            # ── Streaming path ──────────────────────────────────
            judge_start = time.monotonic()
            judge_tokens: list[str] = []
            try:
                async for token in self._judge.synthesize_stream(
                    question=context.question,
                    responses=successful,
                    question_critique=question_critique,
                    rag_context="",  # Phase 4
                    mode=mode,
                    judge_model_id=judge_model_id,
                    judge_prompt_override=judge_prompt_override,
                ):
                    judge_tokens.append(token)
                    await progress.on_judge_token(token)

                judge_ms = int((time.monotonic() - judge_start) * 1000)
                synthesis = JudgeSynthesis(
                    final_answer="".join(judge_tokens),
                    latency_ms=judge_ms,
                    success=True,
                )
            except Exception as e:
                logger.warning(f"Judge streaming failed, falling back: {e}")
                # Fallback to non-streaming
                synthesis = await self._judge.synthesize(
                    question=context.question,
                    responses=successful,
                    question_critique=question_critique,
                    rag_context="",
                    mode=mode,
                    judge_model_id=judge_model_id,
                    judge_prompt_override=judge_prompt_override,
                )
        else:
            # ── Batch path (unchanged) ──────────────────────────
            synthesis = await self._judge.synthesize(
                question=context.question,
                responses=successful,
                question_critique=question_critique,
                rag_context="",  # Phase 4
                mode=mode,
                judge_model_id=judge_model_id,
                judge_prompt_override=judge_prompt_override,
            )

        # Collect metadata (may already be done)
        try:
            metadata = await meta_task
        except Exception:
            metadata = MetadataExtraction()

        return synthesis, metadata

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
    # Fan-out
    # ────────────────────────────────────────────────────────

    async def _fan_out(
        self,
        context: QueryContext,
        mode_config: ModeConfig,
        progress: ProgressReporter | None = None,
        rag_section: str = "",
        search_attempted: bool = False,
    ) -> tuple[list[ModelResponse], QuestionCritique | None]:
        """
        Fan-out to contributors + optional Question Critic (parallel).

        Delegates RoleCall building and dispatch to FanOutEngine.
        Orchestrator retains: critic task management, event emission.
        """
        # Build session context from history (fills {session_section} in prompts)
        session_section = ""
        if context.session_history:
            try:
                mem_result = await self._conversation_memory.build_session_context(
                    context.session_history, token_budget=2000,
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

        # Build contributor RoleCalls via FanOutEngine
        contributor_calls = self._fan_out_engine.build_contributor_calls(
            context, mode_config, rag_section, search_attempted,
            profile_section=context.user_profile_summary,
            session_section=session_section,
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
        context: QueryContext, error: str, elapsed_ms: int = 0
    ) -> QueryResult:
        """Create an error QueryResult."""
        return QueryResult(
            query_id=context.query_id,
            question=context.question,
            mode=context.mode.value,
            resolved_mode=context.resolved_mode.value,
            final_answer=f"系统错误: {error}",
            latency_ms=elapsed_ms,
        )
