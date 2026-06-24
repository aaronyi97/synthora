"""
Socratic Orchestrator — two-phase pipeline for Socratic mode.

Phase 1 (Heavy compute, ~60-90s):
  Fan-out to contributors → DivergenceAnalyzer → Judge synthesis → Cache all

Phase 2 (Lightweight dialogue, <5s/turn):
  SocraticGuide generates questions from cached divergence map
  User responds → Guide follows up → repeat until reveal or max rounds

This is a separate orchestrator from the main one because the interaction
model is fundamentally different: multi-turn stateful dialogue vs single-shot.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from datetime import datetime
from typing import Any, AsyncIterator, TYPE_CHECKING

if TYPE_CHECKING:
    from agoracle.adapters.judge.llm_judge import LLMJudge
    from agoracle.adapters.judge.metadata_extractor import LLMMetadataExtractor
    from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter
    from agoracle.services.event_bus import EventBus
    from agoracle.services.prompt_loader import PromptLoader
    from agoracle.services.search_service import SearchService

from agoracle.ports.socratic_session_port import SocraticSessionStorePort
from agoracle.config.schema import AppConfig, ModeConfig
from agoracle.domain.events import SocraticSessionCompleted
from agoracle.domain.types import (
    DivergenceMap,
    Mode,
    ModelResponse,
    OutputDepth,
    QueryContext,
    QueryResult,
    QuestionType,
    Role,
    SocraticSession,
    SocraticTurn,
)
from agoracle.services.cognitive_tracker import CognitiveTracker
from agoracle.services.conversation_memory import ConversationMemoryService
from agoracle.services.divergence_analyzer import DivergenceAnalyzer
from agoracle.services.socratic_guide import SocraticGuide

logger = logging.getLogger(__name__)


def _is_english(language: str) -> bool:
    return (language or "").strip() == "en-US"


def _locale_text(language: str, zh: str, en: str) -> str:
    return en if _is_english(language) else zh


def _is_system_error_message(text: str) -> bool:
    return text.startswith("系统错误") or text.startswith("System error")


class SocraticOrchestrator:
    """
    Orchestrates the Socratic mode two-phase pipeline.

    Usage:
        orch = SocraticOrchestrator(config, adapter, judge, extractor, prompts)

        # Phase 1: heavy compute (user waits with loading indicator)
        session = await orch.start_session(question)

        # Phase 2: lightweight dialogue loop
        guide_turn = session.turns[-1]  # first guide question
        while not session.revealed and session.guide_rounds_used < session.max_guide_rounds:
            user_input = get_user_input()
            if user_input == "/reveal":
                await orch.reveal(session)
                break
            guide_turn = await orch.respond(session, user_input)

        # End: evaluate cognitive patterns
        await orch.finish(session)
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
        profile_store: Any | None = None,
        session_store: SocraticSessionStorePort | None = None,
    ) -> None:
        self._config = config
        self._adapter = model_adapter
        self._judge = judge
        self._extractor = extractor
        self._prompts = prompt_loader
        self._event_bus = event_bus
        self._search = search_service

        # Socratic-specific services
        self._divergence_analyzer = DivergenceAnalyzer(model_adapter, prompt_loader)
        self._conversation_memory = ConversationMemoryService(
            model_adapter, summary_model_id="gemini_3_flash",
        )
        self._guide = SocraticGuide(model_adapter, prompt_loader, self._conversation_memory)
        self._cognitive_tracker = CognitiveTracker(profile_store) if profile_store else None

        # v3.0: Direct fan-out engine for streaming pipeline (bypasses full Orchestrator)
        from agoracle.services.fan_out import FanOutEngine
        self._fan_out = FanOutEngine(config, model_adapter, prompt_loader)

        # Session persistence (SQLite-backed, survives restarts)
        self._session_store = session_store
        # In-memory locks for per-session concurrency (process-local, not persisted)
        self._session_locks: dict[str, asyncio.Lock] = {}
        # v3.0: Background Judge synthesis tasks (for reveal, keyed by session_id)
        self._background_tasks: dict[str, asyncio.Task] = {}
        self._session_ttl = 1800  # 30 minutes
        self._max_sessions = 200

    def _get_lock(self, session_id: str) -> asyncio.Lock:
        """Get or lazily create a per-session lock (survives server restarts)."""
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
        return self._session_locks[session_id]

    def _get_socratic_config(self) -> ModeConfig:
        """Get the Socratic mode config. Raises if not configured."""
        if "socratic" in self._config.modes:
            return self._config.modes["socratic"]
        # v3.0: 强校验 — 静默fallback到deep会导致Socratic行为不可预期 (审计建议)
        raise ValueError(
            "Socratic mode config missing in config.yaml. "
            "Silent fallback to Deep is unsafe — Socratic requires specific "
            "divergence_analyzer/guide_generator/max_guide_rounds settings."
        )

    async def start_session(self, question: str, language: str = "zh-CN") -> SocraticSession:
        """
        Phase 1: Heavy compute — fan-out, divergence analysis, judge synthesis.

        Returns a SocraticSession with cached data, ready for dialogue.
        This is the slow part (~60-90s). Show a loading indicator to the user.
        """
        from agoracle.services.orchestrator import Orchestrator

        session = SocraticSession(question=question, language=language)
        mode_config = self._get_socratic_config()
        session.max_guide_rounds = mode_config.max_guide_rounds

        start = time.monotonic()

        # Step 1: Run the standard orchestrator pipeline to get contributor responses + synthesis
        # Use Deep mode config for the actual fan-out (Socratic shares contributors)
        deep_config = self._config.modes.get("deep", ModeConfig())
        # v3.1fix: 关闭联网 — 苏格拉底需要多样化观点而非实时搜索，Kimi原生搜索会额外增加10-20s
        context = QueryContext(
            query_id=f"socratic-{session.session_id}",
            question=question,
            mode=Mode.DEEP,
            resolved_mode=Mode.DEEP,
            language=session.language,
            web_search_enabled=False,
            critique_enabled=True,
            output_depth=OutputDepth.LEVEL_3,
            question_type=QuestionType.UNKNOWN,
        )

        orchestrator = Orchestrator(
            config=self._config,
            model_adapter=self._adapter,
            judge=self._judge,
            extractor=self._extractor,
            prompt_loader=self._prompts,
            event_bus=self._event_bus,
            search_service=self._search,
        )

        result = await orchestrator.execute(context)

        # Abort if pipeline failed — don't feed error messages into divergence analysis
        if _is_system_error_message(result.final_answer):
            session.phase1_latency_ms = int((time.monotonic() - start) * 1000)
            raise RuntimeError(f"Socratic pipeline failed: {result.final_answer}")

        # Cache the full answer (revealed later)
        session.full_answer = result.final_answer

        # Cache individual contributor responses for reference
        if result.individual_responses:
            session.contributor_responses = result.individual_responses
        else:
            # Reconstruct from orchestrator internals if not available
            session.contributor_responses = []

        # Step 2: Run divergence analysis on contributor responses
        # We need the raw responses — get them by re-running fan-out
        # Actually, we can use the result's divergence info if available
        analyzer_model = mode_config.divergence_analyzer or "gemini_3_flash"

        # Build mock responses from individual_responses if available
        mock_responses = []
        if result.individual_responses:
            for resp_dict in result.individual_responses:
                mock_responses.append(ModelResponse(
                    call_id="cached",
                    model_id=resp_dict.get("model_id", "unknown"),
                    role=Role.CONTRIBUTOR,
                    content=resp_dict.get("content", ""),
                    latency_ms=0,
                    success=True,
                ))

        if len(mock_responses) >= 2:
            try:
                divergence_map = await asyncio.wait_for(
                    self._divergence_analyzer.analyze(
                        question=question,
                        responses=mock_responses,
                        analyzer_model_id=analyzer_model,
                    ),
                    timeout=30,  # v4.32: match streaming path timeout
                )
            except asyncio.TimeoutError:
                logger.warning(f"[{session.session_id}] DivergenceAnalyzer timed out (non-streaming), using fallback")
                divergence_map = self._divergence_analyzer._fallback_map(mock_responses, 30000)
        else:
            divergence_map = DivergenceMap(
                model_count=len(mock_responses),
                overall_consensus_score=1.0,
            )

        # v3.0: 按难度排序分歧点 (easy→medium→hard) — 脚手架原则
        # 低认知用户先处理简单分歧建立信心，再逐步升级难度
        _DIFFICULTY_ORDER = {"easy": 0, "medium": 1, "hard": 2}
        if divergence_map.divergence_points:
            divergence_map.divergence_points.sort(
                key=lambda dp: _DIFFICULTY_ORDER.get(dp.difficulty, 1)
            )

        session.divergence_map = divergence_map
        session.phase1_latency_ms = int((time.monotonic() - start) * 1000)

        logger.info(
            f"[{session.session_id}] Socratic Phase 1 complete: "
            f"{len(divergence_map.divergence_points)} divergence points, "
            f"{len(divergence_map.consensus_points)} consensus points, "
            f"{session.phase1_latency_ms}ms"
        )

        # Step 3: Generate initial guide question
        guide_model = mode_config.guide_generator or "gemini_3_flash"
        try:
            initial_turn = await asyncio.wait_for(
                self._guide.generate_initial_guide(
                    question=question,
                    divergence_map=divergence_map,
                    guide_model_id=guide_model,
                    language=session.language,
                ),
                timeout=15,  # v4.32: match streaming path timeout
            )
        except asyncio.TimeoutError:
            logger.warning(f"[{session.session_id}] Guide generation timed out (non-streaming), using fallback")
            dp = divergence_map.divergence_points[0] if divergence_map.divergence_points else None
            initial_turn = SocraticTurn(
                role="guide",
                content=(
                    _locale_text(
                        session.language,
                        f"关于「{dp.topic}」，专家们有不同看法。你觉得哪一方的论据更有说服力？为什么？",
                        f"Experts disagree about \"{dp.topic}\". Which side sounds more convincing to you, and why?",
                    )
                    if dp else _locale_text(
                        session.language,
                        "专家们分析了这个问题。你对此有什么看法？",
                        "The experts have analyzed this question. What is your own view?",
                    )
                ),
                divergence_point_id=dp.point_id if dp else None,
            )
        session.turns.append(initial_turn)

        # Persist session + create lock
        if self._session_store:
            await self._session_store.cleanup_expired(self._session_ttl)
            await self._session_store.save(session)
        self._get_lock(session.session_id)  # pre-create lock

        return session

    # ── 方案C: 流式渐进 Phase 1 ───────────────────────────

    async def start_session_streaming(
        self, question: str, language: str = "zh-CN",
    ) -> AsyncIterator:
        """
        Heart-beat wrapper around _start_session_streaming_inner.

        Uses asyncio.Queue + wait_for(timeout=15) pattern (same as streaming.py)
        to ensure a heartbeat byte is sent every 15s during long silences
        (divergence analysis up to 30s, guide generation up to 15s).
        Without this, Cloudflare Tunnel / nginx idle-timeout kills the connection.

        Also enforces a hard Phase1 total timeout (max_timeout_seconds from config,
        default 120s).  On timeout, yields SocraticError so the frontend always
        receives a terminal event.
        """
        from agoracle.services.streaming import SocraticStage as _SocraticStage, SocraticError as _SocraticError

        mode_config = self._get_socratic_config()
        phase1_hard_timeout = getattr(mode_config, "max_timeout_seconds", 120)

        queue: asyncio.Queue = asyncio.Queue()
        _HEARTBEAT = object()  # sentinel

        async def _producer():
            try:
                async for event in self._start_session_streaming_inner(
                    question,
                    language=language,
                ):
                    await queue.put(event)
            except Exception as e:
                logger.error(f"Socratic producer error: {e}", exc_info=True)
                await queue.put(_SocraticError(error="socratic_pipeline_error"))
            finally:
                await queue.put(None)  # EOF sentinel

        producer_task = asyncio.create_task(_producer())
        deadline = asyncio.get_event_loop().time() + phase1_hard_timeout

        try:
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    producer_task.cancel()
                    logger.warning(f"Socratic Phase1 hard timeout ({phase1_hard_timeout}s), forcing error terminal")
                    yield _SocraticError(error="socratic_timeout")
                    return
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=min(15.0, remaining))
                except asyncio.TimeoutError:
                    yield _SocraticStage("heartbeat", "")  # keep-alive; frontend ignores unknown stages
                    continue
                if item is None:
                    break
                yield item
        finally:
            if not producer_task.done():
                producer_task.cancel()

    async def _start_session_streaming_inner(
        self, question: str, language: str = "zh-CN",
    ) -> AsyncIterator:
        """
        方案C: Progressive streaming Phase 1.

        Instead of running the full Deep orchestrator (~60-120s blocking),
        this method:
          1. Fans out to contributors directly (yields progress per-contributor)
          2. Starts DivergenceAnalyzer as soon as N responses arrive
          3. Generates initial guide question
          4. Kicks off Judge synthesis as a background task (for reveal)
          5. User can start interacting in ~25-35s instead of 60-120s

        Yields StreamEvent objects for SSE consumption.
        End result quality is identical — Judge synthesis still runs, just in background.
        """
        from agoracle.services.streaming import (
            SocraticStage, SocraticContributorDone,
            SocraticDivergenceReady, SocraticReady, SocraticError,
        )

        session = SocraticSession(question=question, language=language)
        mode_config = self._get_socratic_config()
        session.max_guide_rounds = mode_config.max_guide_rounds
        start = time.monotonic()

        try:
            # ── Step 0: Signal start ──
            yield SocraticStage(
                "recruiting",
                _locale_text(session.language, "正在召集专家...", "Gathering experts..."),
            )

            # ── Step 1: Build contributor calls using Socratic config directly ──
            socratic_mode_config = copy.copy(mode_config)
            socratic_mode_config.contributors = list(mode_config.contributors)

            context = QueryContext(
                query_id=f"socratic-{session.session_id}",
                question=question,
                mode=Mode.DEEP,
                resolved_mode=Mode.DEEP,
                language=session.language,
                web_search_enabled=False,  # v3.1fix: 关闭联网，避免Kimi原生搜索增加10-20s延迟
                critique_enabled=True,
                output_depth=OutputDepth.LEVEL_3,
                question_type=QuestionType.UNKNOWN,
            )

            # Optional: web search first (~2-3s)
            rag_section = ""
            search_attempted = False
            if self._search and context.web_search_enabled:
                try:
                    from agoracle.services.search_service import SearchService
                    yield SocraticStage(
                        "searching",
                        _locale_text(session.language, "搜索相关信息...", "Searching for relevant context..."),
                    )
                    search_response = await self._search.search(question)
                    if search_response:
                        rag_section = SearchService.format_for_prompt(
                            search_response, max_chars=self._config.search.max_chars,
                        )
                        search_attempted = True
                except Exception as e:
                    logger.warning(f"[{session.session_id}] Socratic search failed: {e}")

            role_calls = self._fan_out.build_contributor_calls(
                context, socratic_mode_config,
                rag_section=rag_section, search_attempted=search_attempted,
            )

            # ── Step 2: Fan-out with progressive reporting ──
            total = len(role_calls)
            yield SocraticStage(
                "thinking",
                _locale_text(
                    session.language,
                    f"专家思考中 (0/{total})...",
                    f"Experts are thinking (0/{total})...",
                ),
            )

            n_required = socratic_mode_config.n_of_m or total
            all_responses: list[ModelResponse] = []
            pending: set[asyncio.Task] = set()

            # v4.18: per-contributor hard timeout — prevents kimi/slow models from
            # blocking Phase 1 for 7+ minutes (logged: kimi contributor 423167ms)
            # v4.31: 45→20s — Socratic uses fast models (flash/pro/gpt52), p95 <10s.
            # 45s caused 30-45s "stuck" experience when deepseek_reasoner chain-of-thought
            # was slow or gpt52_all API had issues. 20s is 2x normal p95.
            _CONTRIBUTOR_HARD_TIMEOUT = 20  # seconds; 2x normal p95 for fast models

            async def _call_with_timeout(rc) -> ModelResponse:
                try:
                    return await asyncio.wait_for(
                        self._adapter.call(rc),
                        timeout=_CONTRIBUTOR_HARD_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        f"[{session.session_id}] Contributor '{rc.model_id}' "
                        f"timed out after {_CONTRIBUTOR_HARD_TIMEOUT}s (hard limit)"
                    )
                    return ModelResponse(
                        call_id=rc.call_id, model_id=rc.model_id, role=Role.CONTRIBUTOR,
                        content="", latency_ms=_CONTRIBUTOR_HARD_TIMEOUT * 1000,
                        success=False, error="contributor_timeout",
                    )

            for rc in role_calls:
                task = asyncio.create_task(_call_with_timeout(rc))
                task.model_id = rc.model_id  # type: ignore[attr-defined]
                pending.add(task)

            divergence_task: asyncio.Task | None = None

            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    try:
                        result = task.result()
                        all_responses.append(result)
                        yield SocraticContributorDone(
                            model_id=result.model_id,
                            success=result.success,
                            latency_ms=result.latency_ms,
                            done_count=len(all_responses),
                            total_count=total,
                        )
                        # When N successful responses arrive, start DivergenceAnalyzer in parallel
                        successful = [r for r in all_responses if r.success and r.content]
                        if len(successful) >= n_required and divergence_task is None:
                            analyzer_model = mode_config.divergence_analyzer or "gemini_3_flash"
                            divergence_task = asyncio.create_task(
                                self._divergence_analyzer.analyze(
                                    question=question,
                                    responses=successful,
                                    analyzer_model_id=analyzer_model,
                                )
                            )
                            # v3.1fix: cancel remaining slow tasks — n_required met, no need to wait
                            for t in pending:
                                t.cancel()
                            pending.clear()
                    except Exception as e:
                        mid = getattr(task, "model_id", "?")
                        logger.warning(f"[{session.session_id}] Contributor error ({mid}): {e}")
                        all_responses.append(ModelResponse(
                            call_id="", model_id=mid, role=Role.CONTRIBUTOR,
                            content="", latency_ms=0, success=False, error=str(e),
                        ))

            # ── Step 3: Wait for divergence analysis (with heartbeat) ──
            successful = [r for r in all_responses if r.success and r.content]

            async def _run_with_heartbeat(task: asyncio.Task, timeout: float) -> tuple:
                """Poll task every 10s, yield heartbeat events, return (result, timed_out)."""
                deadline = asyncio.get_event_loop().time() + timeout
                heartbeats = []
                while True:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        task.cancel()
                        return (None, True, heartbeats)
                    wait_secs = min(10.0, remaining)
                    done, _ = await asyncio.wait({task}, timeout=wait_secs)
                    if done:
                        return (task.result(), False, heartbeats)
                    heartbeats.append(SocraticStage("heartbeat", ""))

            if divergence_task is None and len(successful) >= 2:
                yield SocraticStage(
                    "analyzing",
                    _locale_text(session.language, "分析专家分歧...", "Analyzing expert disagreements..."),
                )
                analyzer_model = mode_config.divergence_analyzer or "gemini_3_flash"
                _analyze_task = asyncio.create_task(
                    self._divergence_analyzer.analyze(
                        question=question, responses=successful,
                        analyzer_model_id=analyzer_model,
                    )
                )
                _result, _timed_out, _hbs = await _run_with_heartbeat(_analyze_task, timeout=30)
                for _hb in _hbs:
                    yield _hb
                if _timed_out:
                    logger.warning(f"[{session.session_id}] DivergenceAnalyzer timed out, using fallback")
                    divergence_map = self._divergence_analyzer._fallback_map(successful, 30000)
                else:
                    divergence_map = _result
            elif divergence_task is not None:
                yield SocraticStage(
                    "analyzing",
                    _locale_text(session.language, "分析专家分歧...", "Analyzing expert disagreements..."),
                )
                _result, _timed_out, _hbs = await _run_with_heartbeat(divergence_task, timeout=30)
                for _hb in _hbs:
                    yield _hb
                if _timed_out:
                    logger.warning(f"[{session.session_id}] DivergenceAnalyzer task timed out, using fallback")
                    divergence_map = self._divergence_analyzer._fallback_map(successful, 30000)
                else:
                    divergence_map = _result
            else:
                divergence_map = DivergenceMap(
                    model_count=len(successful), overall_consensus_score=1.0,
                )

            # Sort by difficulty (easy first) — scaffolding principle
            _DIFFICULTY_ORDER = {"easy": 0, "medium": 1, "hard": 2}
            if divergence_map.divergence_points:
                divergence_map.divergence_points.sort(
                    key=lambda dp: _DIFFICULTY_ORDER.get(dp.difficulty, 1)
                )

            yield SocraticDivergenceReady(
                consensus_points=divergence_map.consensus_points,
                divergence_count=len(divergence_map.divergence_points),
                overall_consensus=divergence_map.overall_consensus_score,
            )

            session.divergence_map = divergence_map
            session.contributor_responses = [
                {"model_id": r.model_id, "content": r.content}
                for r in successful
            ]

            # ── Step 4: Generate initial guide question (with heartbeat) ──
            yield SocraticStage(
                "guiding",
                _locale_text(session.language, "生成引导问题...", "Generating the guiding question..."),
            )
            guide_model = mode_config.guide_generator or "gemini_3_flash"
            _guide_task = asyncio.create_task(
                self._guide.generate_initial_guide(
                    question=question,
                    divergence_map=divergence_map,
                    guide_model_id=guide_model,
                    language=session.language,
                )
            )
            _guide_result, _guide_timed_out, _guide_hbs = await _run_with_heartbeat(_guide_task, timeout=15)
            for _hb in _guide_hbs:
                yield _hb
            if not _guide_timed_out:
                initial_turn = _guide_result
            else:
                logger.warning(f"[{session.session_id}] Guide generation timed out, using fallback")
                dp = divergence_map.divergence_points[0] if divergence_map.divergence_points else None
                initial_turn = SocraticTurn(
                    role="guide",
                    content=(
                        _locale_text(
                            session.language,
                            f"关于「{dp.topic}」，专家们有不同看法。你觉得哪一方的论据更有说服力？为什么？",
                            f"Experts disagree about \"{dp.topic}\". Which side sounds more convincing to you, and why?",
                        )
                        if dp else _locale_text(
                            session.language,
                            "专家们分析了这个问题。你对此有什么看法？",
                            "The experts have analyzed this question. What is your own view?",
                        )
                    ),
                    divergence_point_id=dp.point_id if dp else None,
                )
            session.turns.append(initial_turn)
            session.phase1_latency_ms = int((time.monotonic() - start) * 1000)

            logger.info(
                f"[{session.session_id}] Socratic streaming Phase 1 complete: "
                f"{len(divergence_map.divergence_points)} divergence points, "
                f"{session.phase1_latency_ms}ms (user waited this long)"
            )

            # ── Step 5: Background Judge synthesis (for reveal) ──
            self._start_background_synthesis(session, successful)

            # Persist session
            if self._session_store:
                await self._session_store.cleanup_expired(self._session_ttl)
                await self._session_store.save(session)
            self._get_lock(session.session_id)

            # Final event: session ready for interaction
            dm = session.divergence_map
            yield SocraticReady(
                session_id=session.session_id,
                initial_guide=initial_turn.content,
                max_guide_rounds=session.max_guide_rounds,
                divergence_map={
                    "consensus_points": dm.consensus_points if dm else [],
                    "divergence_count": len(dm.divergence_points) if dm else 0,
                    "overall_consensus": dm.overall_consensus_score if dm else 0,
                },
                phase1_latency_ms=session.phase1_latency_ms,
            )

        except Exception as e:
            logger.error(f"[{session.session_id}] Socratic streaming failed: {e}", exc_info=True)
            yield SocraticError(error="socratic_pipeline_error")  # SEC: no internal detail to client

    def _start_background_synthesis(
        self, session: SocraticSession, responses: list[ModelResponse],
    ) -> None:
        """Kick off Judge synthesis as background task. Result cached in session for reveal."""

        async def _synthesize() -> None:
            try:
                judge_model = self._get_socratic_config().judge or "claude_sonnet_thinking"
                synthesis = await self._judge.synthesize(
                    question=session.question,
                    responses=responses,
                    mode="deep",
                    judge_model_id=judge_model,
                    language=session.language,
                )
                session.full_answer = synthesis.final_answer or ""

                if self._session_store:
                    await self._session_store.save(session)

                logger.info(f"[{session.session_id}] Background synthesis complete")
            except Exception as e:
                logger.error(f"[{session.session_id}] Background synthesis failed: {e}")
                # Fallback: use longest contributor response
                best = max(responses, key=lambda r: len(r.content), default=None)
                session.full_answer = best.content if best else _locale_text(
                    session.language,
                    "综合分析生成失败，请查看各专家的单独回答。",
                    "The synthesis could not be completed. Please review the individual expert responses.",
                )
                if self._session_store:
                    try:
                        await self._session_store.save(session)
                    except Exception:
                        pass
            finally:
                self._background_tasks.pop(session.session_id, None)

        task = asyncio.create_task(_synthesize())
        self._background_tasks[session.session_id] = task

    async def respond(
        self, session: SocraticSession, user_message: str
    ) -> SocraticTurn:
        """
        Phase 2: Process user's response and generate next guide question.

        This must complete in <5s. Uses only flash model + cached data.
        Serialized per-session to prevent concurrent turn interleaving.
        """
        async with self._get_lock(session.session_id):
            return await self._respond_inner(session, user_message)

    async def _respond_inner(
        self, session: SocraticSession, user_message: str
    ) -> SocraticTurn:
        # Check if max rounds reached BEFORE recording (prevents off-by-one)
        if session.guide_rounds_used >= session.max_guide_rounds:
            wrap_turn = SocraticTurn(
                role="guide",
                content=_locale_text(
                    session.language,
                    "我们已经讨论了几个关键分歧点。你现在对这个问题有了更清晰的看法吗？"
                    "如果你想看看专家们的完整分析，可以说「揭示答案」。",
                    "We've explored several key disagreements. Do you have a clearer view now? "
                    "If you want to see the experts' full analysis, say \"Reveal answer\".",
                ),
            )
            session.turns.append(wrap_turn)
            return wrap_turn

        # Record user turn (after max-rounds check so we don't overshoot)
        user_turn = SocraticTurn(
            role="user",
            content=user_message,
            divergence_point_id=(
                session.divergence_map.divergence_points[session.current_divergence_index].point_id
                if session.divergence_map and session.divergence_map.divergence_points
                else None
            ),
        )
        session.turns.append(user_turn)
        session.guide_rounds_used += 1

        # v3.0: Adaptive divergence point rotation based on difficulty
        # easy=1 turn, medium=2 turns, hard=3 turns per divergence point
        # (was: fixed 2 turns for all — too fast for hard topics, too slow for easy ones)
        if (
            session.divergence_map
            and session.divergence_map.divergence_points
            and session.current_divergence_index < len(session.divergence_map.divergence_points) - 1
        ):
            current_dp = session.divergence_map.divergence_points[session.current_divergence_index]
            turns_on_current = sum(
                1 for t in session.turns
                if t.role == "user" and t.divergence_point_id == current_dp.point_id
            )
            turns_needed = {"easy": 1, "medium": 2, "hard": 3}.get(current_dp.difficulty, 2)
            if turns_on_current >= turns_needed:
                session.current_divergence_index += 1
                logger.info(
                    f"[{session.session_id}] Moving to divergence point "
                    f"{session.current_divergence_index + 1} "
                    f"(prev was {current_dp.difficulty}, spent {turns_on_current} turns)"
                )

        # Persist user turn BEFORE guide call — prevents data loss if guide fails
        if self._session_store:
            await self._session_store.save(session)

        # Generate follow-up
        socratic_config = self._get_socratic_config()
        guide_model = socratic_config.guide_generator or "gemini_3_flash"

        try:
            guide_turn = await self._guide.generate_followup(
                session=session,
                user_message=user_message,
                guide_model_id=guide_model,
                language=session.language,
            )
        except Exception as e:
            logger.error(f"[{session.session_id}] Guide generation failed: {e}")
            guide_turn = SocraticTurn(
                role="guide",
                content=_locale_text(
                    session.language,
                    "抱歉，我暂时无法生成回应。请再试一次，或者说「揭示答案」查看专家分析。",
                    "Sorry, I can't generate the next response right now. Please try again, or say "
                    "\"Reveal answer\" to see the expert analysis.",
                ),
            )

        session.turns.append(guide_turn)

        # Persist again with guide turn
        if self._session_store:
            await self._session_store.save(session)

        return guide_turn

    async def reveal(self, session: SocraticSession) -> dict:  # noqa: C901
        """
        Reveal the full answer and divergence map to the user.

        Returns a dict with all the information for display.
        """
        async with self._get_lock(session.session_id):
            return await self._reveal_inner(session)

    async def _reveal_inner(self, session: SocraticSession) -> dict:
        # v3.0: Wait for background Judge synthesis if still running (方案C)
        bg_task = self._background_tasks.get(session.session_id)
        if bg_task and not bg_task.done():
            logger.info(f"[{session.session_id}] Reveal: waiting for background synthesis...")
            try:
                await asyncio.wait_for(bg_task, timeout=60)
            except asyncio.TimeoutError:
                logger.warning(f"[{session.session_id}] Background synthesis timed out on reveal")
            except Exception as e:
                logger.warning(f"[{session.session_id}] Background synthesis error on reveal: {e}")

        # If full_answer still empty after waiting, reload from store (task may have saved it)
        if not session.full_answer and self._session_store:
            refreshed = await self._session_store.get(session.session_id)
            if refreshed and refreshed.full_answer:
                session.full_answer = refreshed.full_answer

        session.revealed = True
        session.completed_naturally = False

        # Persist revealed state
        if self._session_store:
            await self._session_store.save(session)

        return {
            "full_answer": session.full_answer,
            "divergence_map": session.divergence_map,
            "contributor_responses": session.contributor_responses,
            "guide_rounds_used": session.guide_rounds_used,
        }

    async def finish(self, session: SocraticSession, user_id: int = 0) -> SocraticSession:
        """
        End the session: evaluate cognitive patterns and clean up.

        Returns the updated session with cognitive snapshot.
        """
        async with self._get_lock(session.session_id):
            return await self._finish_inner(session, user_id)

    async def _finish_inner(self, session: SocraticSession, user_id: int = 0) -> SocraticSession:
        socratic_config = self._get_socratic_config()
        # v3.0: 用 divergence_analyzer (sonnet_thinking) 而非 guide_generator (flash) 做认知评估
        # flash 太浅，无法准确评估推理深度和偏见。评估在会话结束时进行，延迟可接受。
        eval_model = socratic_config.divergence_analyzer or socratic_config.guide_generator or "gemini_3_flash"

        # Evaluate cognitive patterns
        snapshot = await self._guide.evaluate_session(
            session=session,
            evaluator_model_id=eval_model,
            language=session.language,
        )
        session.cognitive_snapshot = snapshot
        session.reasoning_quality_score = snapshot.reasoning_depth

        # Count position changes
        user_stances = [t.user_stance for t in session.turns if t.role == "user" and t.user_stance]
        changes = sum(1 for i in range(1, len(user_stances)) if user_stances[i] != user_stances[i-1])
        if snapshot:
            snapshot.position_change_count = changes

        logger.info(
            f"[{session.session_id}] Socratic session finished: "
            f"{session.guide_rounds_used} rounds, "
            f"quality={session.reasoning_quality_score:.2f}, "
            f"revealed={session.revealed}"
        )

        # Persist cognitive data to UserProfile
        if self._cognitive_tracker:
            try:
                await self._cognitive_tracker.record_session(session, user_id)
            except Exception as e:
                logger.warning(f"[{session.session_id}] Failed to record cognitive data: {e}")

        # Emit SocraticSessionCompleted event
        if self._event_bus:
            try:
                await self._event_bus.emit(SocraticSessionCompleted(
                    query_id=f"socratic-{session.session_id}",
                    question=session.question,
                    session_id=session.session_id,
                    guide_rounds_used=session.guide_rounds_used,
                    user_conclusion=session.user_conclusion,
                    reasoning_quality_score=session.reasoning_quality_score,
                    completed_naturally=session.completed_naturally,
                    divergence_points_count=(
                        len(session.divergence_map.divergence_points)
                        if session.divergence_map else 0
                    ),
                ))
            except Exception as e:
                logger.warning(f"[{session.session_id}] Failed to emit session event: {e}")

        # Persist final state and mark inactive
        if self._session_store:
            await self._session_store.save(session)

        # Clean up process-local locks
        self._session_locks.pop(session.session_id, None)

        return session

    async def get_session(self, session_id: str) -> SocraticSession | None:
        """Retrieve an active session by ID."""
        if self._session_store:
            return await self._session_store.get(session_id)
        return None
