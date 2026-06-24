"""
Refinement Engine — Answer Critic + Judge Refinement pipeline.

Extracted from orchestrator.py (v2.6.3) to reduce God Object complexity.
Handles:
  - Question Critic calls (parallel with fan-out)
  - Answer Critic calls (Deep/Research mode)
  - Multi-round Judge refinement with early stopping
  - Judge fallback chain on primary failure

All methods are stateless relative to the query — they use
injected dependencies (adapter, judge, prompts, config) only.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid

from agoracle.adapters.judge.llm_judge import LLMJudge
from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter
from agoracle.config.schema import AppConfig, ModeConfig
from agoracle.domain.types import (
    JudgeSynthesis,
    ModelResponse,
    QueryContext,
    QuestionCritique,
    Role,
    RoleCall,
)
from agoracle.services.prompt_loader import PromptLoader

logger = logging.getLogger(__name__)

# Patterns indicating Answer Critic found no issues (used to stop refinement early)
_NO_ISSUES_PATTERNS = [
    r"无需修正", r"质量良好", r"无明显.*问题", r"没有.*(?:明显|重大).*(?:问题|错误|遗漏)",
    r"整体质量.*(?:良好|优秀|不错)", r"不需要.*修[改正]", r"无需.*(?:调整|改进|修改)",
    r"回答.*(?:全面|准确|完整).*(?:无需|不需)", r"no\s+(?:significant\s+)?issues",
    r"(?:overall|generally)\s+good\s+quality", r"no\s+(?:corrections?|changes?)\s+needed",
]
_NO_ISSUES_RE = re.compile("|".join(_NO_ISSUES_PATTERNS), re.IGNORECASE)


def critic_says_no_issues(critique_text: str) -> bool:
    """Check if Answer Critic's response indicates no issues found."""
    return bool(_NO_ISSUES_RE.search(critique_text))


def parse_question_critique(raw: str) -> QuestionCritique | None:
    """Parse Question Critic JSON output."""
    try:
        text = raw.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        data = json.loads(text)
        return QuestionCritique(
            has_issues=bool(data.get("has_issues", False)),
            issue_type=data.get("issue_type"),
            analysis=data.get("analysis"),
            suggested_reformulation=data.get("suggested_reformulation"),
            severity=data.get("severity", "low"),
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


class RefinementEngine:
    """
    Handles critic calls, multi-round refinement, and judge fallback.

    Stateless per-query — all state is passed in via method arguments.
    Dependencies are injected once at construction.
    """

    def __init__(
        self,
        config: AppConfig,
        adapter: OpenAIModelAdapter,
        judge: LLMJudge,
        prompts: PromptLoader,
    ) -> None:
        self._config = config
        self._adapter = adapter
        self._judge = judge
        self._prompts = prompts

    async def call_question_critic(
        self, question: str, critic_model_id: str
    ) -> QuestionCritique | None:
        """Call the Question Critic model."""
        system_prompt = self._prompts.load("question_critic")
        if not system_prompt:
            return None

        # Content safety: prepend safety rules (原則 #25)
        safety_rules = self._prompts.load("safety_rules")
        if safety_rules:
            system_prompt = f"{safety_rules}\n\n{system_prompt}"

        mc = self._config.models.get(critic_model_id)
        timeout = mc.timeout_seconds if mc else 30

        role_call = RoleCall(
            call_id=f"qcritic-{uuid.uuid4().hex[:6]}",
            model_id=critic_model_id,
            role=Role.QUESTION_CRITIC,
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": question}],
            timeout_seconds=timeout,
        )

        response = await self._adapter.call(role_call)
        if not response.success:
            return None

        return parse_question_critique(response.content)

    async def call_answer_critic(
        self, question: str, synthesis: str, critic_model_id: str,
        extra_tokens: list[int] | None = None,
    ) -> str | None:
        """Call the Answer Critic model."""
        system_prompt = self._prompts.load("answer_critic")
        if not system_prompt:
            return None

        # Content safety: prepend safety rules (原則 #25)
        safety_rules = self._prompts.load("safety_rules")
        if safety_rules:
            system_prompt = f"{safety_rules}\n\n{system_prompt}"

        user_message = f"## 用户问题\n{question}\n\n## 综合答案\n{synthesis}"

        mc = self._config.models.get(critic_model_id)
        timeout = min(mc.timeout_seconds if mc else 30, 60)  # v4.23: hard cap 60s — prevent thinking models blocking Judge

        role_call = RoleCall(
            call_id=f"acritic-{uuid.uuid4().hex[:6]}",
            model_id=critic_model_id,
            role=Role.ANSWER_CRITIC,
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            timeout_seconds=timeout,
        )

        response = await self._adapter.call(role_call)

        if extra_tokens is not None:
            extra_tokens[0] += response.prompt_tokens + response.completion_tokens
            extra_tokens[1] += 1

        return response.content if response.success else None

    async def _self_refine_call(
        self,
        question: str,
        original_answer: str,
        critique_text: str,
        model_id: str,
        timeout: int = 120,
    ) -> str | None:
        """v3.1 Self-Refine: send critique back to the original model so it can fix its own answer.

        Based on "Self-Refine: Iterative Refinement with Self-Feedback" (Madaan et al., 2023).
        The model sees its own answer + the critic's feedback and produces an improved version.
        This closes the feedback loop: Critic → Model self-corrects → Judge synthesizes improved versions.
        """
        system_prompt = (
            "你是一个严谨的思考者。你将看到你之前对一个问题的回答，以及一位审查员指出的问题。\n"
            "你的任务：基于审查员的反馈，修正和改进你的回答。\n"
            "- 如果审查员的批评合理，认真修正\n"
            "- 如果某条批评不合理，保持原来的判断\n"
            "- 保持你原有的风格和结构，只做必要的修正\n"
            "- 直接输出改进后的完整回答，不要解释你改了什么"
        )
        safety_rules = self._prompts.load("safety_rules")
        if safety_rules:
            system_prompt = f"{safety_rules}\n\n{system_prompt}"

        user_message = (
            f"## 问题\n{question}\n\n"
            f"## 你之前的回答\n{original_answer}\n\n"
            f"## 审查员的反馈\n{critique_text}\n\n"
            f"## 请输出改进后的完整回答"
        )

        role_call = RoleCall(
            call_id=f"selfrefine-{uuid.uuid4().hex[:6]}",
            model_id=model_id,
            role=Role.CONTRIBUTOR,
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            timeout_seconds=timeout,
        )
        response = await self._adapter.call(role_call)
        return response.content if response.success else None

    async def deep_refinement(
        self,
        context: QueryContext,
        synthesis: JudgeSynthesis,
        mode_config: ModeConfig,
        extra_tokens: list[int] | None = None,
        judge_prompt_override: str = "",
        best_model_id: str = "",
        contributor_responses: list | None = None,
        fact_check_section: str = "",
        search_citations: list[dict] | None = None,
        deadline: float | None = None,
    ) -> JudgeSynthesis:
        """Answer Critic + Self-Refine + Judge refinement.

        v3.1: Self-Refine loop added.
        Each round:
          1. Answer Critic identifies issues in current synthesis
          2. [NEW] Best contributor model sees critique and self-corrects its answer
          3. Judge synthesizes the self-corrected responses
        Stops early if critic finds no issues.
        """
        if not mode_config.answer_critic:
            return synthesis

        max_rounds = mode_config.max_refinement_rounds
        if max_rounds <= 0:
            return synthesis
        current = synthesis
        current_responses = list(contributor_responses) if contributor_responses else []

        for round_num in range(1, max_rounds + 1):
            logger.info(
                f"[{context.query_id}] Answer Critic round {round_num}/{max_rounds}"
            )

            # v2.8.8: Per-round deadline guard — each round can cost up to
            # 60s (critic) + 300s (refine) + 300s (fallback) = 660s.
            # Without this check, multi-round refinement silently exceeds
            # the pipeline deadline set in orchestrator.py.
            if deadline is not None and time.monotonic() > deadline:
                logger.warning(
                    f"[{context.query_id}] Refinement round {round_num}: "
                    f"pipeline deadline exceeded, stopping early"
                )
                break

            critique_text = await self.call_answer_critic(
                context.question,
                current.final_answer,
                mode_config.answer_critic,
                extra_tokens=extra_tokens,
            )

            if not critique_text or critic_says_no_issues(critique_text):
                logger.info(
                    f"[{context.query_id}] Answer Critic round {round_num}: "
                    f"no issues found, stopping early"
                )
                break

            # v3.6: Self-Refine removed — self_refined content updated current_responses
            # but judge.refine() only reads current.final_answer + critique_text,
            # never current_responses. The call was wasted cost every round.
            # Future: wire self_refined directly into judge.refine() initial_synthesis.

            logger.info(
                f"[{context.query_id}] Judge refinement round {round_num}"
            )
            refined = await self._judge.refine(
                question=context.question,
                initial_synthesis=current.final_answer,
                answer_critique=critique_text,
                judge_model_id=mode_config.judge,
                judge_prompt_override=judge_prompt_override,
                fact_check_section=fact_check_section,
                search_citations=search_citations,
                language=context.language,
            )
            if extra_tokens is not None:
                extra_tokens[1] += 1

            if refined.success:
                current = refined
            else:
                fallback_id = self._config.judge.refine_fallback
                if fallback_id != mode_config.judge and self._adapter.supports_model(fallback_id):
                    logger.info(
                        f"[{context.query_id}] Judge refine fallback: "
                        f"{mode_config.judge} → {fallback_id} (round {round_num})"
                    )
                    refined = await self._judge.refine(
                        question=context.question,
                        initial_synthesis=current.final_answer,
                        answer_critique=critique_text,
                        judge_model_id=fallback_id,
                        judge_prompt_override=judge_prompt_override,
                        fact_check_section=fact_check_section,
                        search_citations=search_citations,
                        language=context.language,
                    )
                    if extra_tokens is not None:
                        extra_tokens[1] += 1
                    if refined.success:
                        current = refined
                        continue

                logger.warning(
                    f"[{context.query_id}] Judge refine failed at round {round_num}, "
                    f"keeping previous synthesis"
                )
                break

        return current

    async def judge_fallback(
        self, context: QueryContext, responses: list[ModelResponse],
        question_critique: QuestionCritique | None, mode: str,
        primary_judge_id: str = "",
        judge_prompt_override: str = "",
    ) -> JudgeSynthesis:
        """Try alternative Judge model if primary fails.

        v4.8: Total fallback chain timeout = 60s. Prevents sequential
        timeouts from accumulating (e.g., 3 fallbacks × 360s = 18min).
        """
        import asyncio
        import time

        _FALLBACK_CHAIN_TIMEOUT = 60  # seconds total for all fallback attempts
        _chain_start = time.monotonic()

        all_fallbacks = self._config.judge.judge_fallback_chain
        fallbacks = [fb for fb in all_fallbacks if fb != primary_judge_id]
        for fb_id in fallbacks:
            if time.monotonic() - _chain_start > _FALLBACK_CHAIN_TIMEOUT:
                logger.warning(
                    f"[{context.query_id}] Judge fallback chain timeout "
                    f"({_FALLBACK_CHAIN_TIMEOUT}s), skipping remaining fallbacks"
                )
                break
            if self._adapter.supports_model(fb_id):
                logger.info(f"[{context.query_id}] Judge fallback: trying {fb_id}")
                _remaining = _FALLBACK_CHAIN_TIMEOUT - (time.monotonic() - _chain_start)
                # v2.9: skip fallback attempt if remaining budget < 15s.
                # Giving a thinking-capable model (e.g. claude_sonnet_thinking) only 5s
                # guarantees a timeout — wasteful API call with no chance of success.
                _MIN_FALLBACK_BUDGET = 15
                if _remaining < _MIN_FALLBACK_BUDGET:
                    logger.warning(
                        f"[{context.query_id}] Judge fallback {fb_id} skipped: "
                        f"only {_remaining:.0f}s remaining (minimum {_MIN_FALLBACK_BUDGET}s required)"
                    )
                    break
                try:
                    result = await asyncio.wait_for(
                        self._judge.synthesize(
                            question=context.question,
                            responses=responses,
                            question_critique=question_critique,
                            mode=mode,
                            judge_model_id=fb_id,
                            judge_prompt_override=judge_prompt_override,
                            language=context.language,
                        ),
                        timeout=_remaining,  # use full remaining budget, not a 5s floor
                    )
                    if result.success:
                        return result
                except asyncio.TimeoutError:
                    logger.warning(
                        f"[{context.query_id}] Judge fallback {fb_id} timed out"
                    )
                except Exception as e:
                    logger.warning(
                        f"[{context.query_id}] Judge fallback {fb_id} failed: {e}"
                    )

        successful = [r for r in responses if r.success and r.content]
        if successful:
            # v4.1: 取第一个成功回答（确定性，无长度偏差）。
            # 不用 max(len) — 长度≠质量，且此处无 metadata 可用于 get_best_response。
            # 触发条件极端（Judge + 全 fallback chain 均失败），保守选择优于错误启发式。
            best = successful[0]
            logger.warning(
                f"[{context.query_id}] All Judge fallbacks exhausted, "
                f"using contributor {best.model_id} as final answer"
            )
            return JudgeSynthesis(
                final_answer=best.content,
                latency_ms=best.latency_ms,
                success=True,
            )

        return JudgeSynthesis(
            final_answer="抱歉，所有模型均无法生成回答。请稍后重试。",
            latency_ms=0,
            success=False,
            error="All judge fallbacks failed",
        )
