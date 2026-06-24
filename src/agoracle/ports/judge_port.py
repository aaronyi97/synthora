"""
Judge Port — interface for the synthesis/evaluation model.

Design decision: Judge produces ONLY the final answer (pure text).
Metadata extraction runs in parallel via a separate MetadataExtractor.
This separates cognitive load: Judge focuses 100% on answer quality.
"""

from __future__ import annotations

from typing import AsyncIterator, Protocol

from agoracle.domain.types import (
    JudgeSynthesis,
    MetadataExtraction,
    ModelResponse,
    QuestionCritique,
)


class JudgePort(Protocol):
    """Synthesize multiple model responses into one optimal answer."""

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
        """Produce the final synthesized answer (pure text)."""
        ...

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
        """Stream the final synthesized answer."""
        ...

    async def refine(
        self,
        question: str,
        initial_synthesis: str,
        answer_critique: str,
        judge_model_id: str = "claude_opus",
        judge_prompt_override: str = "",
        fact_check_section: str = "",
        search_citations: list[dict] | None = None,
    ) -> JudgeSynthesis:
        """Refine synthesis based on answer critic feedback (Deep mode only)."""
        ...


class MetadataExtractorPort(Protocol):
    """
    Extract structured metadata from model responses.

    Runs IN PARALLEL with Judge — uses a fast model (e.g. Gemini Flash).
    Does not need to know the Judge's output.
    """

    async def extract(
        self,
        question: str,
        responses: list[ModelResponse],
        extractor_model_id: str = "gemini_3_flash",
    ) -> MetadataExtraction:
        """Extract key_insights, tags, confidence, model_evaluations."""
        ...
