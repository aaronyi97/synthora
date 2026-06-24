"""
Conversation Memory Service — manages conversation context within token budgets.

Solves "catastrophic forgetting" by using progressive summarization:
  - Recent turns: kept verbatim (high fidelity)
  - Older turns: compressed into running summary via LLM
  - Key facts: extracted and preserved regardless of window position

This is NOT long-term memory (Phase 4/ChromaDB). This is within-session
context management that prevents silent context truncation.

Integration points:
  - SocraticGuide.generate_followup() — replaces hard-coded [-6:]
  - FanOutEngine.build_contributor_calls() — fills {session_section}
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter

from agoracle.domain.types import Role, RoleCall, SocraticTurn, Turn

logger = logging.getLogger(__name__)

# ── Token estimation ──
# CJK characters ≈ 1-2 tokens each, English words ≈ 1-1.5 tokens.
# We use chars/3 as a conservative estimate for mixed CJK/English text.
_CHARS_PER_TOKEN = 3


def estimate_tokens(text: str) -> int:
    """Rough token count estimation for mixed CJK/English text."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


@dataclass(frozen=True)
class ContextBuildResult:
    """Result of building context from conversation history."""
    context: str                    # formatted context string
    was_compressed: bool = False    # True if older turns were summarized
    total_turns: int = 0            # total turns in original history
    verbatim_turns: int = 0         # turns kept verbatim
    summarized_turns: int = 0       # turns compressed into summary
    estimated_tokens: int = 0       # estimated token count of output


_MEMORY_TEXT = {
    "zh-CN": {
        "summary_system_prompt": """你是一个对话摘要助手。请将以下对话历史压缩为关键要点摘要。要求：
1. 保留所有关键论点、立场变化和重要事实
2. 保留用户表达的核心观点
3. 用简洁的要点格式输出
4. 不超过{max_chars}字
5. 不要添加任何对话中没有的信息""",
        "omitted": "（早期对话已省略）",
        "summary_failed": "（摘要生成失败，仅保留最近内容）",
        "socratic_summary_title": "[对话摘要（前{count}轮）]",
        "session_history_title": "## 对话历史",
        "session_summary_title": "[摘要（前{count}轮）]",
        "recent_dialogue_title": "[最近对话]",
        "guide": "引导",
        "user": "用户",
        "question": "Q",
        "answer": "A",
        "key_points": "要点",
        "outline": "大纲",
    },
    "en-US": {
        "summary_system_prompt": """You are a conversation summarizer. Compress the dialogue below into key takeaways.
1. Preserve all important arguments, stance changes, and facts
2. Preserve the user's core viewpoints
3. Use concise bullet-style phrasing
4. Keep it under {max_chars} characters
5. Do not add information that does not appear in the dialogue""",
        "omitted": "(Earlier conversation omitted)",
        "summary_failed": "(Summary generation failed, keeping only the latest lines)",
        "socratic_summary_title": "[Conversation Summary (first {count} turns)]",
        "session_history_title": "## Conversation History",
        "session_summary_title": "[Summary (first {count} turns)]",
        "recent_dialogue_title": "[Recent Conversation]",
        "guide": "Guide",
        "user": "User",
        "question": "Q",
        "answer": "A",
        "key_points": "Key points",
        "outline": "Outline",
    },
}


def _normalize_locale(raw: str | None) -> str:
    if not raw:
        return "zh-CN"
    value = raw.strip().replace("_", "-").lower()
    if value.startswith("en"):
        return "en-US"
    if value.startswith("zh"):
        return "zh-CN"
    return "zh-CN"


class ConversationMemoryService:
    """
    Manages conversation context within token budgets.

    Strategy: Sliding Window with Progressive Summarization
    - Recent N turns: kept verbatim (high fidelity)
    - Older turns: compressed into a running summary (LLM call)
    """

    def __init__(
        self,
        model_adapter: OpenAIModelAdapter,
        summary_model_id: str = "gemini_3_flash",
        token_budget: int = 4000,
        verbatim_turns: int = 4,
    ) -> None:
        self._adapter = model_adapter
        self._summary_model = summary_model_id
        self._token_budget = token_budget
        self._verbatim_turns = verbatim_turns

    # ── Socratic flow ──

    async def build_socratic_context(
        self,
        turns: list[SocraticTurn],
        token_budget: int | None = None,
        language: str = "zh-CN",
    ) -> ContextBuildResult:
        """
        Build context string from Socratic turns, respecting token budget.

        Replaces the hard-coded session.turns[-6:] in SocraticGuide.
        """
        budget = token_budget or self._token_budget
        locale = _normalize_locale(language)
        text = _MEMORY_TEXT[locale]

        if not turns:
            return ContextBuildResult(context="", total_turns=0)

        # Try verbatim first
        full_text = self._format_socratic_turns(turns, language=locale)
        full_tokens = estimate_tokens(full_text)

        if full_tokens <= budget:
            return ContextBuildResult(
                context=full_text,
                was_compressed=False,
                total_turns=len(turns),
                verbatim_turns=len(turns),
                summarized_turns=0,
                estimated_tokens=full_tokens,
            )

        # Need compression: keep last N turns verbatim, summarize rest
        verbatim_count = min(self._verbatim_turns, len(turns))

        # Adaptive: shrink verbatim window if recent turns are very long
        while verbatim_count > 2:
            recent = turns[-verbatim_count:]
            recent_text = self._format_socratic_turns(recent)
            recent_tokens = estimate_tokens(recent_text)
            summary_budget_tokens = budget - recent_tokens - 100
            if summary_budget_tokens >= 150:
                break
            verbatim_count -= 1

        recent = turns[-verbatim_count:]
        older = turns[:-verbatim_count]

        recent_text = self._format_socratic_turns(recent, language=locale)
        recent_tokens = estimate_tokens(recent_text)
        summary_budget_chars = (budget - recent_tokens - 100) * _CHARS_PER_TOKEN

        if older and summary_budget_chars > 100:
            summary = await self._summarize_turns(
                self._format_socratic_turns(older, language=locale),
                max_chars=max(200, int(summary_budget_chars)),
                language=locale,
            )
        else:
            summary = text["omitted"]

        context = (
            f"{text['socratic_summary_title'].format(count=len(older))}\n{summary}\n\n"
            f"{text['recent_dialogue_title']}\n{recent_text}"
        )
        context_tokens = estimate_tokens(context)

        logger.info(
            f"Context compressed: {len(turns)} turns → "
            f"{len(older)} summarized + {verbatim_count} verbatim, "
            f"~{context_tokens} tokens"
        )

        return ContextBuildResult(
            context=context,
            was_compressed=True,
            total_turns=len(turns),
            verbatim_turns=verbatim_count,
            summarized_turns=len(older),
            estimated_tokens=context_tokens,
        )

    # ── Light/Deep/Research session flow ──

    async def build_session_context(
        self,
        turns: list[Turn],
        token_budget: int | None = None,
        language: str = "zh-CN",
    ) -> ContextBuildResult:
        """
        Build context string from Light/Deep/Research session history.

        Output is injected into contributor prompts via {session_section}.
        """
        budget = token_budget or self._token_budget
        locale = _normalize_locale(language)
        text = _MEMORY_TEXT[locale]

        if not turns:
            return ContextBuildResult(context="", total_turns=0)

        full_text = self._format_session_turns(turns, language=locale)
        full_tokens = estimate_tokens(full_text)

        if full_tokens <= budget:
            return ContextBuildResult(
                context=f"{text['session_history_title']}\n{full_text}",
                was_compressed=False,
                total_turns=len(turns),
                verbatim_turns=len(turns),
                summarized_turns=0,
                estimated_tokens=full_tokens,
            )

        # Compress older turns
        verbatim_count = min(3, len(turns))
        recent = turns[-verbatim_count:]
        older = turns[:-verbatim_count]

        recent_text = self._format_session_turns(recent, language=locale)
        recent_tokens = estimate_tokens(recent_text)
        summary_budget_chars = (budget - recent_tokens - 100) * _CHARS_PER_TOKEN

        if older and summary_budget_chars > 100:
            summary = await self._summarize_turns(
                self._format_session_turns(older, language=locale),
                max_chars=max(200, int(summary_budget_chars)),
                language=locale,
            )
        else:
            summary = text["omitted"]

        context = (
            f"{text['session_history_title']}\n"
            f"{text['session_summary_title'].format(count=len(older))}\n{summary}\n\n"
            f"{text['recent_dialogue_title']}\n{recent_text}"
        )
        context_tokens = estimate_tokens(context)

        logger.info(
            f"Session context compressed: {len(turns)} turns → "
            f"{len(older)} summarized + {verbatim_count} verbatim, "
            f"~{context_tokens} tokens"
        )

        return ContextBuildResult(
            context=context,
            was_compressed=True,
            total_turns=len(turns),
            verbatim_turns=verbatim_count,
            summarized_turns=len(older),
            estimated_tokens=context_tokens,
        )

    # ── Internal ──

    async def _summarize_turns(
        self,
        dialogue_text: str,
        max_chars: int = 500,
        language: str = "zh-CN",
    ) -> str:
        """Compress dialogue into key points using LLM."""
        locale = _normalize_locale(language)
        text = _MEMORY_TEXT[locale]
        system_prompt = text["summary_system_prompt"].format(max_chars=int(max_chars))

        role_call = RoleCall(
            call_id=f"summary-{uuid.uuid4().hex[:6]}",
            model_id=self._summary_model,
            role=Role.SOCRATIC_GUIDE,
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": dialogue_text}],
            timeout_seconds=10,
        )

        try:
            result = await self._adapter.call(role_call)
            if result.success and result.content:
                return result.content.strip()
        except Exception as e:
            logger.warning(f"Conversation summarization failed: {e}")

        # Fallback: keep last few lines raw
        lines = dialogue_text.strip().split("\n")
        truncated = "\n".join(lines[-5:])
        return f"{text['summary_failed']}\n{truncated}"

    @staticmethod
    def _format_socratic_turns(turns: list[SocraticTurn], language: str = "zh-CN") -> str:
        """Format Socratic turns into readable text."""
        text = _MEMORY_TEXT[_normalize_locale(language)]
        return "\n".join(
            f"{text['guide'] if t.role == 'guide' else text['user']}: {t.content}"
            for t in turns
        )

    @staticmethod
    def _format_session_turns(turns: list[Turn], language: str = "zh-CN") -> str:
        """Format session turns with three-tier detail strategy.

        - Recent 2 turns: full answer summary (≤2000 chars)
        - Prior 2 turns: outline + key insights
        - Older turns: key insights only
        """
        text = _MEMORY_TEXT[_normalize_locale(language)]
        parts = []
        n = len(turns)
        for i, t in enumerate(turns):
            age = n - 1 - i  # 0 = newest
            parts.append(f"{text['question']}: {t.question}")
            if age <= 1:
                # Tier 1: recent — full summary
                if t.final_answer_summary:
                    parts.append(f"{text['answer']}: {t.final_answer_summary}")
                if t.key_insights:
                    parts.append(f"{text['key_points']}: {', '.join(t.key_insights[:5])}")
            elif age <= 3:
                # Tier 2: mid — outline or truncated summary
                if getattr(t, "answer_outline", ""):
                    parts.append(f"{text['outline']}: {t.answer_outline}")
                elif t.key_insights:
                    parts.append(f"{text['key_points']}: {', '.join(t.key_insights[:3])}")
                elif t.final_answer_summary:
                    parts.append(f"{text['answer']}: {t.final_answer_summary[:300]}...")
            else:
                # Tier 3: old — insights only
                if t.key_insights:
                    parts.append(f"{text['key_points']}: {', '.join(t.key_insights[:2])}")
        return "\n".join(parts)
