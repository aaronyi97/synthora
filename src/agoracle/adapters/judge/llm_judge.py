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


def _append_language_instruction(prompt: str, language: str) -> str:
    if language == "en-US":
        return (
            f"{prompt}\n\n"
            "IMPORTANT: You MUST respond entirely in English. "
            "Do not use any Chinese characters in your response."
        )
    return prompt


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

    async def synthesize(
        self,
        question: str,
        responses: list[ModelResponse],
        question_critique: QuestionCritique | None = None,
        rag_context: str = "",
        mode: str = "light",
        judge_model_id: str = "gemini_3_pro",
        judge_prompt_override: str = "",
        best_model_id: str = "",
        augment_insights: list[str] | None = None,
        second_best_model_id: str = "",
        contributor_count: int = 0,
        search_citations: list[dict] | None = None,
        consensus_map: dict[str, int] | None = None,
        session_context: str = "",
        language: str = "zh-CN",
    ) -> JudgeSynthesis:
        """Produce the final synthesized answer.

        v3.1: If best_model_id is set, only the best answer is shown in full.
        Other answers' insights are passed as augment_insights bullet list.
        """
        # Select prompt template: adaptive override > mode default
        if judge_prompt_override:
            system_prompt = self._prompts.load(judge_prompt_override, language=language)
            prompt_name = judge_prompt_override
        else:
            prompt_name = f"judge_{mode}" if mode in ("light", "deep", "research") else "judge_light"
            system_prompt = self._prompts.load(prompt_name, language=language)

        if not system_prompt:
            logger.error(f"Judge prompt '{prompt_name}' not found, using fallback")
            system_prompt = "You are an expert synthesizer. Produce the best possible answer."

        safety_rules = self._prompts.load("safety_rules", language=language)
        if safety_rules:
            system_prompt = f"{safety_rules}\n\n{system_prompt}"

        # R1+R2: inject runtime placeholders into Research prompt
        if mode == "research" and contributor_count:
            import math as _math
            n = contributor_count
            high = max(_math.ceil(n * 0.7), 2)   # ≥70% 同意为高共识（7人→5，5人→4）; 原 max(n-1,n)=n 永远要求全员同意
            mid_lo = max(n // 2, 2)               # ≥50%
            mid_hi = max(_math.ceil(n * 0.7) - 1, mid_lo)  # 50%-69%
            low = max(n // 2 - 1, 1)              # <50%
            system_prompt = (
                system_prompt
                .replace("{contributor_count}", str(n))
                .replace("{high_consensus_threshold}", str(high))
                .replace("{mid_consensus_low}", str(mid_lo))
                .replace("{mid_consensus_high}", str(mid_hi))
                .replace("{low_consensus_threshold}", str(low))
            )
        system_prompt = _append_language_instruction(system_prompt, language)

        # Format model responses into user message
        user_message = self._format_judge_input(
            question, responses, question_critique, rag_context,
            best_model_id=best_model_id, augment_insights=augment_insights,
            second_best_model_id=second_best_model_id,
            contributor_count=contributor_count,
            search_citations=search_citations,
            consensus_map=consensus_map,
            mode=mode,
            session_context=session_context,
        )

        role_call = RoleCall(
            call_id=f"judge-{uuid.uuid4().hex[:8]}",
            model_id=judge_model_id,
            role=Role.JUDGE,
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            timeout_seconds=300 if mode in ("deep", "research") else 30,  # v4.8: 120→300s (claude_opus_thinking needs 200-300s)
        )

        response = await self._adapter.call(role_call)

        return JudgeSynthesis(
            final_answer=response.content,
            latency_ms=response.latency_ms,
            success=response.success,
            error=response.error,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            model_id=judge_model_id,
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
        best_model_id: str = "",
        augment_insights: list[str] | None = None,
        second_best_model_id: str = "",
        contributor_count: int = 0,
        search_citations: list[dict] | None = None,
        consensus_map: dict[str, int] | None = None,
        session_context: str = "",
        language: str = "zh-CN",
    ) -> AsyncIterator[str]:
        """Stream the synthesized answer."""
        if judge_prompt_override:
            system_prompt = self._prompts.load(judge_prompt_override, language=language)
        else:
            prompt_name = f"judge_{mode}" if mode in ("light", "deep", "research") else "judge_light"
            system_prompt = self._prompts.load(prompt_name, language=language)

        if not system_prompt:
            system_prompt = "You are an expert synthesizer. Produce the best possible answer."

        safety_rules = self._prompts.load("safety_rules", language=language)
        if safety_rules:
            system_prompt = f"{safety_rules}\n\n{system_prompt}"

        if mode == "research" and contributor_count:
            import math as _math
            n = contributor_count
            high = max(_math.ceil(n * 0.7), 2)   # ≥70% 同意为高共识（7人→5，5人→4）
            mid_lo = max(n // 2, 2)               # ≥50%
            mid_hi = max(_math.ceil(n * 0.7) - 1, mid_lo)  # 50%-69%
            low = max(n // 2 - 1, 1)              # <50%
            system_prompt = (
                system_prompt
                .replace("{contributor_count}", str(n))
                .replace("{high_consensus_threshold}", str(high))
                .replace("{mid_consensus_low}", str(mid_lo))
                .replace("{mid_consensus_high}", str(mid_hi))
                .replace("{low_consensus_threshold}", str(low))
            )
        system_prompt = _append_language_instruction(system_prompt, language)

        user_message = self._format_judge_input(
            question, responses, question_critique, rag_context,
            best_model_id=best_model_id, augment_insights=augment_insights,
            second_best_model_id=second_best_model_id,
            contributor_count=contributor_count,
            search_citations=search_citations,
            consensus_map=consensus_map,
            mode=mode,
            session_context=session_context,
        )

        role_call = RoleCall(
            call_id=f"judge-stream-{uuid.uuid4().hex[:8]}",
            model_id=judge_model_id,
            role=Role.JUDGE,
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            timeout_seconds=300 if mode in ("deep", "research") else 30,  # v4.8: 120→300s
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
        fact_check_section: str = "",
        search_citations: list[dict] | None = None,
        language: str = "zh-CN",
    ) -> JudgeSynthesis:
        """Refine synthesis based on answer critic feedback (Deep mode only)."""
        if judge_prompt_override:
            system_prompt = self._prompts.load(judge_prompt_override, language=language)
        else:
            system_prompt = self._prompts.load("judge_deep", language=language)
        if not system_prompt:
            system_prompt = "You are an expert synthesizer. Refine the answer based on critique."

        safety_rules = self._prompts.load("safety_rules", language=language)
        if safety_rules:
            system_prompt = f"{safety_rules}\n\n{system_prompt}"
        system_prompt = _append_language_instruction(system_prompt, language)

        # v4.22c: Build grounding context for refinement (prevents blind-fly)
        _grounding_parts: list[str] = []
        if fact_check_section:
            _grounding_parts.append(fact_check_section)
        if search_citations:
            _cite_lines = [f"{i}. [{c.get('title', c.get('url'))}]({c.get('url')})"
                          for i, c in enumerate(search_citations, 1)]
            _grounding_parts.append("## [SEARCH_CITATIONS] 实时搜索来源\n" + "\n".join(_cite_lines))
        _grounding_section = "\n\n".join(_grounding_parts)

        user_message = (
            f"## 用户问题\n{question}\n\n"
            f"## 初步综合答案\n{initial_synthesis}\n\n"
            f"## 答案质疑反馈\n{answer_critique}\n\n"
            + (_grounding_section + "\n\n" if _grounding_section else "")
            + f"## 你的任务\n"
              f"基于质疑反馈，修正和完善初步综合答案。如果质疑不合理，保持原答案。"
              f"精炼过程中禁止新增无搜索来源的具体数字。"
        )

        role_call = RoleCall(
            call_id=f"judge-refine-{uuid.uuid4().hex[:8]}",
            model_id=judge_model_id,
            role=Role.JUDGE_REFINE,
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            timeout_seconds=300,  # v4.8: 120→300s (claude_opus_thinking needs 200-300s)
        )

        response = await self._adapter.call(role_call)

        return JudgeSynthesis(
            final_answer=response.content,
            latency_ms=response.latency_ms,
            success=response.success,
            error=response.error,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            model_id=judge_model_id,
        )

    @staticmethod
    def _format_judge_input(
        question: str,
        responses: list[ModelResponse],
        question_critique: QuestionCritique | None = None,
        rag_context: str = "",
        best_model_id: str = "",
        augment_insights: list[str] | None = None,
        second_best_model_id: str = "",
        contributor_count: int = 0,
        search_citations: list[dict] | None = None,
        consensus_map: dict[str, int] | None = None,
        mode: str = "deep",
        session_context: str = "",
    ) -> str:
        """Format all inputs for the Judge model.

        v3.1 augment mode (best_model_id set):
          - Shows the best answer in full
          - v3.8: Also shows second-best answer in full for cross-reference
          - Key insights from remaining models listed as potential additions
        """
        parts: list[str] = []

        # v5.0: Prepend conversation history for multi-turn understanding
        if session_context:
            parts.append(f"## 对话历史（请结合上下文回答当前问题）\n{session_context}")

        parts.append(f"## 用户问题\n{question}")

        if rag_context:
            parts.append(f"## 相关知识（来自知识库）\n{rag_context}")

        # v4.22: Detect Perplexity (grounded source) among contributors
        _is_perplexity = lambda mid: "perplexity" in (mid or "").lower()

        if best_model_id:
            # ── Augment mode: best + second-best full + other insights ──
            best_content = ""
            second_content = ""
            _perplexity_content = ""  # v4.22: track separately for grounded source
            _perplexity_model_id = ""
            for resp in responses:
                if resp.success and resp.content:
                    if resp.model_id == best_model_id:
                        best_content = resp.content
                    elif second_best_model_id and resp.model_id == second_best_model_id:
                        second_content = resp.content
                    # v4.22: Capture Perplexity content if it's not best/second
                    if _is_perplexity(resp.model_id):
                        _perplexity_content = resp.content
                        _perplexity_model_id = resp.model_id or ""

            # v4.22: Tag [GROUNDED_SOURCE] on section headers when Perplexity is best/second
            _best_grounded = " [GROUNDED_SOURCE]" if _is_perplexity(best_model_id) else ""
            _second_grounded = " [GROUNDED_SOURCE]" if _is_perplexity(second_best_model_id) else ""

            # v4.26: Truncate long responses to reduce Lost-in-the-Middle effect.
            # Strategy: keep head (most content) + tail 500 chars (conclusion usually at end).
            def _trunc(text: str, limit: int) -> str:
                if len(text) <= limit:
                    return text
                tail = text[-(min(500, limit // 4)):]
                head = text[:limit - len(tail) - 5]
                return head + "\n...(截断)...\n" + tail

            if best_content:
                parts.append(f"## 最优回答 [BEST]{_best_grounded}\n{_trunc(best_content, 6000)}")
            else:
                # Fallback: show all if best not found
                parts.append("## 各模型回答")
                for i, resp in enumerate(responses, 1):
                    if resp.success and resp.content:
                        _g = " [GROUNDED_SOURCE]" if _is_perplexity(resp.model_id) else ""
                        parts.append(f"### 回答 {i}{_g}\n{_trunc(resp.content, 4000)}")

            # v3.8: Show second-best full answer for cross-reference & error catching
            if second_content:
                parts.append(f"## 次优回答 [SECOND]{_second_grounded}（供对比参考）\n{_trunc(second_content, 4000)}")

            # v4.22: If Perplexity is neither BEST nor SECOND, show its full response
            # as a dedicated grounded source section so Judge can cross-reference facts
            if (
                _perplexity_content
                and _perplexity_model_id != best_model_id
                and _perplexity_model_id != second_best_model_id
            ):
                parts.append(
                    f"## 实时搜索回答 [GROUNDED_SOURCE]\n{_trunc(_perplexity_content, 3000)}"
                )

            if augment_insights:
                parts.append("## 其他模型的独特观点（供参考，仅补充最优回答中缺失的）")
                for idx, insight in enumerate(augment_insights, 1):
                    parts.append(f"{idx}. {insight}")

            # v4.22: Build grounded source rule for task instructions
            _grounded_rule = ""
            if _perplexity_content:
                _grounded_rule = (
                    "\n**[GROUNDED_SOURCE] 事实优先规则**：标记为 [GROUNDED_SOURCE] 的回答基于实时搜索，"
                    "其事实性声明（数据、日期、事件）优先于纯训练知识。"
                    "当 [GROUNDED_SOURCE] 与其他回答在事实层面矛盾时，以 [GROUNDED_SOURCE] 为准。"
                )

            if second_content:
                parts.append(
                    "## 任务\n"
                    "以上方 [BEST] 最优回答为基础输出最终答案。\n"
                    "参考 [SECOND] 次优回答的论证——如果其中有 [BEST] 明确缺失的重要信息或更准确的事实，"
                    "在合适位置自然插入。\n"
                    '如果“其他模型的独特观点”中有额外值得补充的，也一并考虑。\n'
                    "如果没有值得补充的，直接输出最优回答原文。\n"
                    "**禁止风格重写、禁止改变原文结构。**"
                    "例外：若 [VERIFIED_FACTS] 或 [GROUNDED_SOURCE] 的事实与 [BEST] 中"
                    "某个具体数字矛盾，允许最小编辑替换该数字（只改数字，不改段落）。"
                    + _grounded_rule
                )
            else:
                parts.append(
                    "## 任务\n"
                    "以上方 [BEST] 最优回答为基础输出最终答案。\n"
                    '如果“其他模型的独特观点”中有最优回答明确缺失的重要信息，在合适位置自然插入。\n'
                    "如果没有值得补充的，直接输出最优回答原文。\n"
                    "**禁止风格重写、禁止改变原文结构。**"
                    "例外：若 [VERIFIED_FACTS] 或 [GROUNDED_SOURCE] 的事实与 [BEST] 中"
                    "某个具体数字矛盾，允许最小编辑替换该数字（只改数字，不改段落）。"
                    + _grounded_rule
                )
        else:
            # ── Legacy synthesis mode (Light) ──
            parts.append("## 各模型回答")
            for i, resp in enumerate(responses, 1):
                if resp.success and resp.content:
                    parts.append(f"### 回答 {i}\n{resp.content}")

            parts.append("## 请综合以上所有信息，给出最终答案")

        if question_critique and question_critique.has_issues:
            parts.append(
                f"## 问题质疑分析\n"
                f"问题类型: {question_critique.issue_type}\n"
                f"严重度: {question_critique.severity}\n"
                f"分析: {question_critique.analysis}\n"
                f"建议: {question_critique.suggested_reformulation or '无'}"
            )

        # R1: inject search citations for Research mode
        if search_citations:
            citation_lines = []
            for i, c in enumerate(search_citations, 1):
                url = c.get("url", "")
                title = c.get("title", url)
                date = c.get("date", "")
                date_str = f" · {date}" if date else ""
                citation_lines.append(f"{i}. [{title}]({url}){date_str}")
            parts.append("## [SEARCH_CITATIONS] 实时搜索来源\n" + "\n".join(citation_lines))

        # R2: inject consensus map for Research mode
        # v4.26: Fixed high-consensus threshold: max(n-1,n) was always ==n (requiring unanimous),
        # contradicting the judge_research.md {high_consensus_threshold}=ceil(n*0.7). Now aligned.
        # v2.8.8: When consensus_map is empty (construction disabled since v4.26 — see orchestrator.py),
        # inject an explicit unavailability notice so the Judge does not fabricate consensus numbers.
        # Long-term fix: Extractor should output agreed_models per insight (Phase 4 TODO).
        if not consensus_map and mode == "research":
            parts.append(
                "## [CONSENSUS_MAP] 模型共识度\n"
                "\uff08\u672c\u6b21\u5171\u8bc6\u6570\u636e\u4e0d\u53ef\u7528 \u2014 "
                "\u8bf7\u6839\u636e [BEST]/[OTHERS] \u4e2d\u7684\u5185\u5bb9\u91cd\u53e0\u7a0b\u5ea6\u81ea\u884c\u5224\u65ad\u5171\u8bc6\uff0c"
                "\u7981\u6b62\u7f16\u9020\u5177\u4f53\u6570\u5b57\u5982\"N/M \u6a21\u578b\u540c\u610f\"\uff09"
            )
        elif consensus_map and contributor_count:
            import math as _math_cm
            n = contributor_count
            _high = max(_math_cm.ceil(n * 0.7), 2)  # ≥70% agree → 🟢 (e.g. 7→5, 5→4)
            lines = []
            for point, count in consensus_map.items():
                if count >= _high:
                    badge = "🟢"
                elif count >= n // 2:
                    badge = "🟡"
                else:
                    badge = "🔴"
                lines.append(f"- {point} → {count}/{n} 模型同意 {badge}")
            parts.append("## [CONSENSUS_MAP] 模型共识度\n" + "\n".join(lines))

        return "\n\n".join(parts)
