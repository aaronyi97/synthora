"""
LLM Judge adapter — synthesizes multiple model responses into one answer.

Uses the model adapter to call the Judge model with role-specific prompts.
Judge focuses 100% on answer quality; metadata extraction runs in parallel.
"""

from __future__ import annotations

import logging
import uuid
from typing import AsyncIterator

from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter
from agoracle.domain.types import (
    JudgeSynthesis,
    ModelResponse,
    QuestionCritique,
    Role,
    RoleCall,
)
from agoracle.services.prompt_loader import PromptLoader

logger = logging.getLogger(__name__)


class LLMJudge:
    """
    LLM-based Judge implementation.

    Selects the appropriate prompt template based on mode,
    formats model responses, and calls the Judge model.
    """

    def __init__(
        self,
        model_adapter: OpenAIModelAdapter,
        prompt_loader: PromptLoader,
    ) -> None:
        self._adapter = model_adapter
        self._prompts = prompt_loader
        # Content safety: prepend to all Judge prompts (原则 #25)
        self._safety_rules = prompt_loader.load("safety_rules")

    async def synthesize(
        self,
        question: str,
        responses: list[ModelResponse],
        question_critique: QuestionCritique | None = None,
        rag_context: str = "",
        mode: str = "light",
        judge_model_id: str = "gemini_3_pro",
        judge_prompt_override: str = "",
    ) -> JudgeSynthesis:
        """Produce the final synthesized answer."""
        # Select prompt template: adaptive override > mode default
        if judge_prompt_override:
            system_prompt = self._prompts.load(judge_prompt_override)
            prompt_name = judge_prompt_override
        else:
            prompt_name = f"judge_{mode}" if mode in ("light", "deep", "research") else "judge_light"
            system_prompt = self._prompts.load(prompt_name)

        if not system_prompt:
            logger.error(f"Judge prompt '{prompt_name}' not found, using fallback")
            system_prompt = "You are an expert synthesizer. Produce the best possible answer."

        if self._safety_rules:
            system_prompt = f"{self._safety_rules}\n\n{system_prompt}"

        # Format model responses into user message
        user_message = self._format_judge_input(question, responses, question_critique, rag_context)

        role_call = RoleCall(
            call_id=f"judge-{uuid.uuid4().hex[:8]}",
            model_id=judge_model_id,
            role=Role.JUDGE,
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            timeout_seconds=120 if mode in ("deep", "research") else 30,
        )

        response = await self._adapter.call(role_call)

        return JudgeSynthesis(
            final_answer=response.content,
            latency_ms=response.latency_ms,
            success=response.success,
            error=response.error,
        )

    async def synthesize_stream(
        self,
        question: str,
        responses: list[ModelResponse],
        question_critique: QuestionCritique | None = None,
        rag_context: str = "",
        mode: str = "light",
        judge_model_id: str = "gemini_3_pro",
        judge_prompt_override: str = "",
    ) -> AsyncIterator[str]:
        """Stream the synthesized answer."""
        if judge_prompt_override:
            system_prompt = self._prompts.load(judge_prompt_override)
        else:
            prompt_name = f"judge_{mode}" if mode in ("light", "deep", "research") else "judge_light"
            system_prompt = self._prompts.load(prompt_name)

        if not system_prompt:
            system_prompt = "You are an expert synthesizer. Produce the best possible answer."

        if self._safety_rules:
            system_prompt = f"{self._safety_rules}\n\n{system_prompt}"

        user_message = self._format_judge_input(question, responses, question_critique, rag_context)

        role_call = RoleCall(
            call_id=f"judge-stream-{uuid.uuid4().hex[:8]}",
            model_id=judge_model_id,
            role=Role.JUDGE,
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            timeout_seconds=120 if mode in ("deep", "research") else 30,
        )

        async for chunk in self._adapter.call_stream(role_call):
            yield chunk

    async def refine(
        self,
        question: str,
        initial_synthesis: str,
        answer_critique: str,
        judge_model_id: str = "claude_opus",
        judge_prompt_override: str = "",
    ) -> JudgeSynthesis:
        """Refine synthesis based on answer critic feedback (Deep mode only)."""
        if judge_prompt_override:
            system_prompt = self._prompts.load(judge_prompt_override)
        else:
            system_prompt = self._prompts.load("judge_deep")
        if not system_prompt:
            system_prompt = "You are an expert synthesizer. Refine the answer based on critique."

        if self._safety_rules:
            system_prompt = f"{self._safety_rules}\n\n{system_prompt}"

        user_message = (
            f"## 用户问题\n{question}\n\n"
            f"## 初步综合答案\n{initial_synthesis}\n\n"
            f"## 答案质疑反馈\n{answer_critique}\n\n"
            f"## 你的任务\n"
            f"基于质疑反馈，修正和完善初步综合答案。如果质疑不合理，保持原答案。"
        )

        role_call = RoleCall(
            call_id=f"judge-refine-{uuid.uuid4().hex[:8]}",
            model_id=judge_model_id,
            role=Role.JUDGE_REFINE,
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            timeout_seconds=120,
        )

        response = await self._adapter.call(role_call)

        return JudgeSynthesis(
            final_answer=response.content,
            latency_ms=response.latency_ms,
            success=response.success,
            error=response.error,
        )

    @staticmethod
    def _format_judge_input(
        question: str,
        responses: list[ModelResponse],
        question_critique: QuestionCritique | None = None,
        rag_context: str = "",
    ) -> str:
        """Format all inputs for the Judge model."""
        parts: list[str] = []

        parts.append(f"## 用户问题\n{question}")

        if rag_context:
            parts.append(f"## 相关知识（来自知识库）\n{rag_context}")

        parts.append("## 各模型回答")
        for i, resp in enumerate(responses, 1):
            if resp.success and resp.content:
                parts.append(f"### 回答 {i}\n{resp.content}")

        if question_critique and question_critique.has_issues:
            parts.append(
                f"## 问题质疑分析\n"
                f"问题类型: {question_critique.issue_type}\n"
                f"严重度: {question_critique.severity}\n"
                f"分析: {question_critique.analysis}\n"
                f"建议: {question_critique.suggested_reformulation or '无'}"
            )

        parts.append("## 请综合以上所有信息，给出最终答案")

        return "\n\n".join(parts)
