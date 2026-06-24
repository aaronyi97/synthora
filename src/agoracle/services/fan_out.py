"""
Fan-Out Engine — parallel contributor dispatch + N-of-M racing + MoA Layer 2.

Extracted from orchestrator.py (v2.6.3) to reduce God Object complexity.
Handles:
  - Building contributor RoleCalls with mode-specific prompts
  - Wait-all and N-of-M fan-out strategies
  - MoA (Mixture of Agents) Layer 2 refinement
  - Progress reporting for streaming output

All methods are stateless relative to the query — dependencies injected at construction.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter
from agoracle.config.schema import AppConfig, ModeConfig
from agoracle.domain.types import (
    ModelResponse,
    QueryContext,
    Role,
    RoleCall,
)
from agoracle.services.multimodal import build_user_message
from agoracle.services.prompt_loader import PromptLoader

if TYPE_CHECKING:
    from agoracle.services.failure_monitor import FailureMonitor

logger = logging.getLogger(__name__)


def _append_language_instruction(prompt: str, language: str) -> str:
    if language == "en-US":
        return (
            f"{prompt}\n\n"
            "IMPORTANT: You MUST respond entirely in English. "
            "Do not use any Chinese characters in your response."
        )
    return prompt


class FanOutEngine:
    """
    Parallel contributor dispatch with N-of-M racing and MoA support.

    Stateless per-query — dependencies injected once at construction.
    """

    def __init__(
        self,
        config: AppConfig,
        adapter: OpenAIModelAdapter,
        prompts: PromptLoader,
    ) -> None:
        self._config = config
        self._adapter = adapter
        self._prompts = prompts

    def build_contributor_calls(
        self,
        context: QueryContext,
        mode_config: ModeConfig,
        rag_section: str = "",
        search_attempted: bool = False,
        profile_section: str = "",
        session_section: str = "",
        planner_sub_questions: list[str] | None = None,
        failure_monitor: "FailureMonitor | None" = None,
    ) -> list[RoleCall]:
        """Build RoleCall list for all contributors based on mode and config."""
        mode = context.resolved_mode.value
        is_deep_or_research = mode in ("deep", "research")

        # Content safety: prepend safety rules to ALL contributor prompts (原则1 #25)
        safety_rules = self._prompts.load("safety_rules", language=context.language)

        contributor_calls = []
        for model_id in mode_config.contributors:
            if not self._adapter.supports_model(model_id):
                continue
            # v4.29: FailureMonitor 熟断——跳过 DEGRADED 模型，山省 120s 超时等待
            if failure_monitor and failure_monitor.is_degraded(model_id):
                logger.warning(
                    f"[{context.query_id}] 跳过 {model_id}（熟断中）"
                )
                continue

            mc = self._config.models.get(model_id)
            timeout = mc.timeout_seconds if mc else mode_config.max_timeout_seconds

            # v4.2: Kimi native search is ONLY enabled in Light mode.
            # In Deep/Research, Kimi's thinking and $web_search are mutually exclusive at API level.
            # When web_search=True, the adapter forcibly disables thinking (temperature→0.6).
            # Perplexity sonar-pro now handles real-time search in Deep/Research.
            # v2.8: Kimi uses native search — don't inject Tavily rag_section
            # Kimi's $web_search does its own Chinese-optimized search independently.
            # Other models still get Tavily rag_section as before.
            is_kimi_native_search = (
                mc is not None
                and mc.search_style == "kimi_builtin"
                and mc.supports_search
                and context.web_search_enabled
            )
            # v4.3: Deep/Research — Kimi thinking-only, Perplexity handles search
            # Kimi K2.5 thinking and $web_search are mutually exclusive at API level.
            # When web_search is on, thinking is disabled — wasting Kimi's best capability.
            # Perplexity sonar-pro now handles all search in Deep/Research.
            if is_deep_or_research and is_kimi_native_search:
                is_kimi_native_search = False
                logger.info(
                    f"[{context.query_id}] v4.3: Kimi '{model_id}' thinking-only in {mode} mode "
                    f"(web_search disabled, Perplexity handles search)"
                )
            model_rag = "" if is_kimi_native_search else rag_section

            # v4.23: Suppress rag_section for always-on search models (e.g., Perplexity)
            # These models always search at the API level — injecting Tavily results is
            # redundant, wastes prompt tokens, and may conflict with their own search.
            is_always_on_search = (
                mc is not None
                and mc.search_style == "always_on"
                and mc.supports_search
            )
            if is_always_on_search:
                model_rag = ""

            # web_search_instruction: tell Kimi whether to search or not
            # Prevents Kimi from generating <function_calls> XML when search is disabled
            if mc is not None and mc.search_style == "kimi_builtin":
                if is_deep_or_research:
                    # v4.3: Deep/Research — Kimi thinking-only
                    web_search_instruction = (
                        "**本次任务中，实时搜索已由 Perplexity 搜索专家完成。**"
                        "请完全基于你的训练知识和深度思考能力回答，"
                        "不要调用任何搜索工具或生成搜索调用代码。"
                        "发挥你强大的推理和分析能力。"
                    )
                elif context.web_search_enabled:
                    web_search_instruction = (
                        "**你拥有实时联网搜索能力**，对涉及时效性的问题务必主动搜索获取最新信息。"
                    )
                else:
                    web_search_instruction = (
                        "**本次请求已禁用联网搜索**，请完全基于你的训练知识回答，"
                        "不要调用任何搜索工具或生成搜索调用代码。"
                    )
            else:
                web_search_instruction = ""

            # Mode-specific contributor prompts
            if mode == "research":
                specific_prompt = self._prompts.render(
                    f"contributor_research_{model_id}",
                    language=context.language,
                    profile_section=profile_section,
                    rag_section=model_rag,
                    session_section=session_section,
                )
                prompt = specific_prompt if specific_prompt else self._prompts.render(
                    "contributor",
                    language=context.language,
                    profile_section=profile_section,
                    rag_section=model_rag,
                    session_section=session_section,
                )
                # v4.5: Inject Planner-lite sub_questions into research contributor prompt
                if planner_sub_questions:
                    _sq_lines = "\n".join(
                        f"{i}. {sq}" for i, sq in enumerate(planner_sub_questions, 1)
                    )
                    prompt += (
                        "\n\n## 请系统化回答以下子问题（来自问题规划器）\n"
                        + _sq_lines
                    )
            elif mode == "deep":
                specific_prompt = self._prompts.render(
                    f"contributor_deep_{model_id}",
                    language=context.language,
                    profile_section=profile_section,
                    rag_section=model_rag,
                    session_section=session_section,
                    web_search_instruction=web_search_instruction,
                )
                if specific_prompt:
                    prompt = specific_prompt
                else:
                    deep_prompt = self._prompts.render(
                        "contributor_deep",
                        language=context.language,
                        profile_section=profile_section,
                        rag_section=model_rag,
                        session_section=session_section,
                    )
                    prompt = deep_prompt if deep_prompt else self._prompts.render(
                        "contributor",
                        language=context.language,
                        profile_section=profile_section,
                        rag_section=model_rag,
                        session_section=session_section,
                    )
            elif mode == "light":
                light_prompt = self._prompts.render(
                    "contributor_light",
                    language=context.language,
                    profile_section=profile_section,
                    rag_section=model_rag,
                    session_section=session_section,
                )
                prompt = light_prompt if light_prompt else self._prompts.render(
                    "contributor",
                    language=context.language,
                    profile_section=profile_section,
                    rag_section=model_rag,
                    session_section=session_section,
                )
            else:
                prompt = self._prompts.render(
                    "contributor",
                    language=context.language,
                    profile_section=profile_section,
                    rag_section=model_rag,
                    session_section=session_section,
                )

            # v4.2: Kimi native search only in Light mode (is_kimi_native_search already enforces this).
            # v2.8: Kimi ALWAYS uses native search when web_search_enabled
            # (even if Tavily unified search succeeded for other models).
            # Other models: only use per-model search as fallback when Tavily fails.
            if is_kimi_native_search:
                use_web_search = True
            elif is_always_on_search:
                # v4.23: always-on search models search regardless of this flag
                use_web_search = True
            elif is_deep_or_research and mc is not None and mc.search_style == "kimi_builtin":
                # v4.3: Kimi thinking-only in Deep/Research — no web search
                use_web_search = False
            else:
                use_web_search = context.web_search_enabled and not search_attempted

            # v2.8.3: Inject current date/time so models know what "today" means
            # Without this, models can't connect search results to the current date.
            now_beijing = datetime.now(timezone(timedelta(hours=8)))
            date_section = (
                f"[当前时间: {now_beijing.strftime('%Y年%m月%d日 %H:%M')} 北京时间, "
                f"{now_beijing.strftime('%A')}]"
            )

            # Prepend safety rules + date to every contributor prompt (原则 #25)
            prefix = f"{safety_rules}\n\n{date_section}" if safety_rules else date_section
            safe_prompt = _append_language_instruction(f"{prefix}\n\n{prompt}", context.language)

            contributor_calls.append(
                RoleCall(
                    call_id=f"contrib-{model_id}-{uuid.uuid4().hex[:6]}",
                    model_id=model_id,
                    role=Role.CONTRIBUTOR,
                    system_prompt=safe_prompt,
                    messages=[build_user_message(context.question, context.attachments, model_id)],
                    timeout_seconds=timeout,
                    web_search=use_web_search,
                )
            )

            if is_kimi_native_search:
                logger.info(
                    f"[{context.query_id}] Kimi '{model_id}' using native $web_search "
                    f"(rag_section suppressed, thinking disabled by adapter)"
                )
            elif is_always_on_search:
                logger.info(
                    f"[{context.query_id}] v4.23: '{model_id}' always-on search "
                    f"(rag_section suppressed, model searches at API level)"
                )

        return contributor_calls

    async def fan_out_wait_all(
        self,
        role_calls: list[RoleCall],
        progress=None,
    ) -> list[ModelResponse]:
        """Wait for all contributors with per-contributor progress."""
        if not progress:
            results = await asyncio.gather(
                *[self._adapter.call(rc) for rc in role_calls],
                return_exceptions=True,
            )
            valid_results: list[ModelResponse] = []
            failed_results: list[ModelResponse] = []
            for i, result in enumerate(results):
                if isinstance(result, BaseException):
                    model_id = role_calls[i].model_id if i < len(role_calls) else f"contributor-{i}"
                    logger.warning(
                        f"fan_out: contributor {i} failed: {type(result).__name__}: {result}"
                    )
                    failed_results.append(ModelResponse(
                        call_id="",
                        model_id=model_id,
                        role=Role.CONTRIBUTOR,
                        content="",
                        latency_ms=0,
                        success=False,
                        error=f"{type(result).__name__}: {result}",
                    ))
                else:
                    valid_results.append(result)
            return valid_results if valid_results else failed_results

        results: list[ModelResponse] = []
        pending: set[asyncio.Task] = set()
        preview_sent = False
        for rc in role_calls:
            task = asyncio.create_task(self._adapter.call(rc))
            task.model_id = rc.model_id  # type: ignore[attr-defined]
            pending.add(task)

        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                try:
                    result = task.result()
                    results.append(result)
                    await progress.on_contributor_done(
                        result.model_id, result.success, result.latency_ms
                    )
                    if not preview_sent and result.success and result.content:
                        await progress.on_preview_answer(
                            result.model_id, result.content
                        )
                        preview_sent = True
                except Exception as e:
                    mid = getattr(task, "model_id", "?")
                    logger.warning(f"Wait-all task exception ({mid}): {e}")
                    results.append(ModelResponse(
                        call_id="", model_id=mid, role=Role.CONTRIBUTOR,
                        content="", latency_ms=0, success=False, error=str(e),
                    ))

        return results

    async def fan_out_n_of_m(
        self,
        role_calls: list[RoleCall],
        n: int,
        progress=None,
    ) -> list[ModelResponse]:
        """
        N-of-M strategy: start all M calls, return as soon as N succeed.
        Remaining calls are cancelled to save cost.
        """
        results: list[ModelResponse] = []
        pending: set[asyncio.Task[ModelResponse]] = set()
        preview_sent = False

        for rc in role_calls:
            task = asyncio.create_task(self._adapter.call(rc))
            task.model_id = rc.model_id  # type: ignore[attr-defined]
            pending.add(task)

        try:
            while pending and len([r for r in results if r.success and r.content]) < n:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    try:
                        result = task.result()
                        results.append(result)
                        if progress:
                            await progress.on_contributor_done(
                                result.model_id, result.success, result.latency_ms
                            )
                            if not preview_sent and result.success and result.content:
                                await progress.on_preview_answer(
                                    result.model_id, result.content
                                )
                                preview_sent = True
                    except Exception as e:
                        mid = getattr(task, "model_id", "?")
                        logger.warning(f"N-of-M task exception ({mid}): {e}")
                        results.append(ModelResponse(
                            call_id="", model_id=mid, role=Role.CONTRIBUTOR,
                            content="", latency_ms=0, success=False, error=str(e),
                        ))
        finally:
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        successful_count = len([r for r in results if r.success and r.content])
        # v2.9: Low-confidence guard — if fewer than ceil(n/2) contributors succeeded,
        # the synthesis evidence base is too thin. Inject a sentinel failed response so
        # that downstream (orchestrator quality-gate) can detect and surface the warning.
        _min_viable = -(-n // 2)  # ceil(n/2) via integer arithmetic
        if successful_count < _min_viable and successful_count > 0:
            logger.warning(
                f"N-of-M low confidence: only {successful_count}/{n} contributors "
                f"succeeded (minimum viable: {_min_viable}). "
                f"Synthesis quality may be degraded."
            )
            # Add a synthetic marker response so orchestrator can detect low_confidence flag
            results.append(ModelResponse(
                call_id="__low_confidence_marker__",
                model_id="__system__",
                role=Role.CONTRIBUTOR,
                content="",
                latency_ms=0,
                success=False,
                error="LOW_CONFIDENCE: insufficient contributor count",
            ))
        elif successful_count < n:
            # v4.30: UNDER_TARGET_N — succeeded but below requested n.
            # Evidence base is thinner than expected; Gate should prefer BEST_SINGLE
            # over synthesis when the winning model's signal is strong enough.
            logger.info(
                f"N-of-M under target: {successful_count}/{n} contributors succeeded "
                f"(above min_viable={_min_viable}). Injecting UNDER_TARGET_N marker."
            )
            results.append(ModelResponse(
                call_id="__under_target_n_marker__",
                model_id="__system__",
                role=Role.CONTRIBUTOR,
                content="",
                latency_ms=0,
                success=False,
                error="UNDER_TARGET_N: contributor count below requested n",
            ))
        logger.info(
            f"N-of-M complete: {successful_count}/{n} needed, "
            f"{len(role_calls)} total started"
        )

        return results

    async def moa_second_layer(
        self,
        context: QueryContext,
        mode_config: ModeConfig,
        layer1_responses: list[ModelResponse],
        rag_section: str = "",
    ) -> list[ModelResponse]:
        """
        MoA Layer 2: each contributor sees other contributors' answers
        and generates an improved version.

        Based on "Mixture-of-Agents Enhances Large Language Model Capabilities"
        (Wang et al., 2024).
        """
        moa_prompt_template = self._prompts.load("moa_refine")
        if not moa_prompt_template:
            logger.warning(f"[{context.query_id}] MoA prompt not found, skipping Layer 2")
            return layer1_responses

        moa_calls = []
        for resp in layer1_responses:
            if not resp.success or not resp.content:
                continue

            other_summaries = []
            for other in layer1_responses:
                if other.model_id == resp.model_id or not other.success or not other.content:
                    continue
                other_summaries.append(f"### 专家 {len(other_summaries) + 1}\n{other.content}")

            if not other_summaries:
                continue

            other_responses_text = "\n\n".join(other_summaries)

            system_prompt = moa_prompt_template.replace(
                "{other_responses}", other_responses_text
            ).replace(
                "{my_original_answer}", resp.content
            ).replace(
                "{rag_section}", rag_section
            )

            # Content safety: prepend safety rules to MoA prompts (原則 #25)
            safety_rules = self._prompts.load("safety_rules")
            if safety_rules:
                system_prompt = f"{safety_rules}\n\n{system_prompt}"

            mc = self._config.models.get(resp.model_id)
            timeout = mc.timeout_seconds if mc else mode_config.max_timeout_seconds

            moa_calls.append(
                RoleCall(
                    call_id=f"moa2-{resp.model_id}-{uuid.uuid4().hex[:6]}",
                    model_id=resp.model_id,
                    role=Role.CONTRIBUTOR,
                    system_prompt=system_prompt,
                    messages=[build_user_message(context.question, context.attachments, resp.model_id)],
                    timeout_seconds=timeout,
                    web_search=False,
                )
            )

        if not moa_calls:
            return layer1_responses

        logger.info(
            f"[{context.query_id}] MoA Layer 2: sending {len(moa_calls)} refinement calls"
        )

        n_of_m = mode_config.n_of_m
        if n_of_m > 0 and n_of_m < len(moa_calls):
            results = await self.fan_out_n_of_m(moa_calls, n_of_m)
        else:
            results = await self.fan_out_wait_all(moa_calls)

        return results
