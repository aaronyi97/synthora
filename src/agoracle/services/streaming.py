"""
Streaming output — thin presentation layer over the Orchestrator.

Provides ``execute_streaming()`` which yields ``StreamEvent`` objects
as the Orchestrator pipeline progresses.  ALL pipeline logic lives in
the Orchestrator; this module only translates progress callbacks into
the event protocol consumed by the CLI (and future SSE endpoint).

Phase 1: CLI progressive output.
Phase 3: SSE endpoint streams to web UI.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

logger = logging.getLogger(__name__)

from agoracle.adapters.judge.llm_judge import LLMJudge
from agoracle.adapters.judge.metadata_extractor import LLMMetadataExtractor
from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter
from agoracle.config.schema import AppConfig
from agoracle.domain.types import QueryContext, QueryResult
from agoracle.services.event_bus import EventBus
from agoracle.services.orchestrator import Orchestrator, ProgressReporter
from agoracle.services.prompt_loader import PromptLoader


# ────────────────────────────────────────────────────────
# Stream event types (public API for CLI / SSE consumers)
# ────────────────────────────────────────────────────────

class StreamEvent:
    """Base stream event."""


class StageStarted(StreamEvent):
    """A pipeline stage has started."""
    def __init__(self, stage: str, detail: str = ""):
        self.stage = stage
        self.detail = detail


class ContributorDone(StreamEvent):
    """One contributor has responded."""
    def __init__(self, model_id: str, success: bool, latency_ms: int):
        self.model_id = model_id
        self.success = success
        self.latency_ms = latency_ms


class PreviewAnswer(StreamEvent):
    """Fastest contributor's answer — shown as preview until Judge replaces it."""
    def __init__(self, model_id: str, content: str):
        self.model_id = model_id
        self.content = content


class JudgeToken(StreamEvent):
    """One token from the Judge's streaming response."""
    def __init__(self, token: str):
        self.token = token


class StageCompleted(StreamEvent):
    """A pipeline stage has completed."""
    def __init__(self, stage: str, detail: str = ""):
        self.stage = stage
        self.detail = detail


class PipelineComplete(StreamEvent):
    """Pipeline finished, final result available."""
    def __init__(self, result: QueryResult):
        self.result = result


class PipelineError(StreamEvent):
    """Pipeline encountered an error."""
    def __init__(
        self,
        error: str,
        billable: bool = False,
        billed_mode: str = "",
        result: "QueryResult | None" = None,
    ):
        self.error = error
        self.billable = billable       # v5.2: True = API calls were made, quota should be charged
        self.billed_mode = billed_mode  # v5.2: which mode was billed (e.g. "deep")
        self.result = result            # v5.2: partial result if available


class Heartbeat(StreamEvent):
    """Keepalive signal — prevents SSE connection from appearing dead during long operations."""
    pass


class DraftAnswer(StreamEvent):
    """An intermediate answer version (fan-out best or MoA best) — v3.3."""
    def __init__(self, stage: str, model_id: str, content: str):
        self.stage = stage        # "fan_out_best" | "moa_best"
        self.model_id = model_id
        self.content = content


class CitationsReady(StreamEvent):
    """Search citations available immediately after search — v4.20.

    Emitted before Judge starts streaming so frontend [N] badges are
    clickable from the first token, not only after the complete event.
    """
    def __init__(self, citations: list):
        self.citations = citations


class CompanionRoute(StreamEvent):
    """v5.1: Dispatcher pre-route suggestion — displayed as CompanionBubble with countdown."""
    def __init__(self, message: str, actions: list, route_reason: str,
                 auto_execute_seconds: int = 15, is_silent: bool = False,
                 resolved_mode: str = "", contributor_count: int = 0,
                 more_actions: list | None = None):
        self.message = message
        self.actions = actions
        self.more_actions = more_actions or []
        self.route_reason = route_reason
        self.auto_execute_seconds = auto_execute_seconds
        self.is_silent = is_silent
        self.resolved_mode = resolved_mode
        self.contributor_count = contributor_count


# ── Socratic-specific events (方案C: 流式渐进) ──────────

class SocraticStage(StreamEvent):
    """Socratic pipeline stage update."""
    def __init__(self, stage: str, detail: str = ""):
        self.stage = stage
        self.detail = detail


class SocraticContributorDone(StreamEvent):
    """One Socratic contributor has responded."""
    def __init__(self, model_id: str, success: bool, latency_ms: int, done_count: int, total_count: int):
        self.model_id = model_id
        self.success = success
        self.latency_ms = latency_ms
        self.done_count = done_count
        self.total_count = total_count


class SocraticDivergenceReady(StreamEvent):
    """Divergence analysis complete — user can see what experts disagree about."""
    def __init__(self, consensus_points: list, divergence_count: int, overall_consensus: float):
        self.consensus_points = consensus_points
        self.divergence_count = divergence_count
        self.overall_consensus = overall_consensus


class SocraticReady(StreamEvent):
    """Session ready for interaction — guide question generated."""
    def __init__(self, session_id: str, initial_guide: str, max_guide_rounds: int,
                 divergence_map: dict, phase1_latency_ms: int):
        self.session_id = session_id
        self.initial_guide = initial_guide
        self.max_guide_rounds = max_guide_rounds
        self.divergence_map = divergence_map
        self.phase1_latency_ms = phase1_latency_ms


class SocraticError(StreamEvent):
    """Socratic pipeline error."""
    def __init__(self, error: str):
        self.error = error


# ────────────────────────────────────────────────────────
# Queue-based progress reporter
# ────────────────────────────────────────────────────────

class _QueueReporter(ProgressReporter):
    """Translates Orchestrator progress into StreamEvents on an asyncio.Queue."""

    def __init__(self, queue: asyncio.Queue[StreamEvent]) -> None:
        self._q = queue

    async def on_stage_start(self, stage: str, detail: str = "") -> None:
        await self._q.put(StageStarted(stage, detail))

    async def on_contributor_done(
        self, model_id: str, success: bool, latency_ms: int
    ) -> None:
        await self._q.put(ContributorDone(model_id, success, latency_ms))

    async def on_judge_token(self, token: str) -> None:
        await self._q.put(JudgeToken(token))

    async def on_preview_answer(self, model_id: str, content: str) -> None:
        await self._q.put(PreviewAnswer(model_id, content))

    async def on_stage_complete(self, stage: str, detail: str = "") -> None:
        await self._q.put(StageCompleted(stage, detail))

    async def on_draft_answer(self, stage: str, model_id: str, content: str) -> None:
        await self._q.put(DraftAnswer(stage, model_id, content))

    async def on_citations_ready(self, citations: list) -> None:
        await self._q.put(CitationsReady(citations))

    async def on_companion_route(self, message: str, actions: list, route_reason: str,
                                 auto_execute_seconds: int = 15, is_silent: bool = False,
                                 resolved_mode: str = "", contributor_count: int = 0,
                                 more_actions: list | None = None) -> None:
        await self._q.put(
            CompanionRoute(
                message,
                actions,
                route_reason,
                auto_execute_seconds,
                is_silent,
                resolved_mode,
                contributor_count,
                more_actions,
            )
        )


# ────────────────────────────────────────────────────────
# Public entry point
# ────────────────────────────────────────────────────────

_SENTINEL = object()  # signals end-of-stream


async def execute_streaming(
    context: QueryContext,
    config: AppConfig,
    model_adapter: OpenAIModelAdapter,
    judge: LLMJudge,
    extractor: LLMMetadataExtractor,
    prompt_loader: PromptLoader,
    event_bus: EventBus | None = None,
    search_service: "SearchService | None" = None,
    failure_monitor=None,
) -> AsyncIterator[StreamEvent]:
    """Execute the pipeline with streaming events.

    Delegates to ``Orchestrator.execute(progress=…)`` so that ALL
    pipeline logic (fan-out, Judge, Quality Gate, Deep refinement,
    event emission) is identical between streaming and batch modes.

    Yields ``StreamEvent`` objects as the pipeline progresses.
    """
    orchestrator = Orchestrator(
        config=config,
        model_adapter=model_adapter,
        judge=judge,
        extractor=extractor,
        prompt_loader=prompt_loader,
        event_bus=event_bus,
        search_service=search_service,
        failure_monitor=failure_monitor,
    )

    queue: asyncio.Queue[StreamEvent | object] = asyncio.Queue()
    reporter = _QueueReporter(queue)

    async def _run() -> None:
        """Run the orchestrator and push the final event onto the queue."""
        try:
            result = await orchestrator.execute(context, progress=reporter)
            if result.final_answer.startswith("系统错误"):
                logger.error(f"Pipeline returned error answer: {result.final_answer}")
                await queue.put(PipelineError(
                    "pipeline_answer_error",
                    billable=True,
                    billed_mode=context.mode.value,
                    result=result,
                ))
            else:
                await queue.put(PipelineComplete(result))
        except asyncio.TimeoutError:
            logger.error("Pipeline timeout")
            await queue.put(PipelineError(
                "pipeline_timeout",
                billable=True,
                billed_mode=context.mode.value,
            ))
        except Exception as exc:
            logger.error(f"Pipeline exception: {exc}", exc_info=True)
            # v4.32: classify error for better diagnostics (no internal detail to client)
            _etype = "pipeline_processing_error"
            if "UnboundLocalError" in type(exc).__name__ or "AttributeError" in type(exc).__name__:
                _etype = "pipeline_internal_error"
            elif "timeout" in str(exc).lower():
                _etype = "pipeline_timeout"
            await queue.put(PipelineError(
                _etype,
                billable=True,
                billed_mode=context.mode.value,
            ))
        finally:
            await queue.put(_SENTINEL)

    # Run the pipeline as a background task; yield events as they arrive.
    # v3.3: task is NOT cancelled when SSE disconnects — pipeline runs to
    # completion so the result is saved to history even if user navigates away.
    task = asyncio.create_task(_run())

    _HEARTBEAT_INTERVAL = 15  # seconds between keepalive signals

    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_INTERVAL)
            except asyncio.TimeoutError:
                yield Heartbeat()
                continue
            if event is _SENTINEL:
                break
            yield event  # type: ignore[misc]
    except GeneratorExit:
        pass  # SSE client disconnected — let task finish in background
    # Intentionally NOT cancelling task here — it saves history on completion
