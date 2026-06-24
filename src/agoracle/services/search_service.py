"""
Unified Search Service — single search, shared by all contributors.

Architecture:
  1. Orchestrator calls SearchService.search(question) ONCE before fan-out
  2. Results are formatted into a {rag_section} string
  3. All contributor prompts receive the SAME search context
  4. Advantage: consistent facts across models, no per-model search variance

Provider: Tavily (search provider)
Fallback: graceful degradation — if search fails, pipeline continues without it.

v2.4 — Phase 2: Self-built search layer
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """One search result item."""
    title: str = ""
    url: str = ""
    content: str = ""       # snippet / extracted text
    score: float = 0.0      # relevance score (0-1)


@dataclass
class SearchResponse:
    """Complete search response."""
    query: str = ""
    results: list[SearchResult] = field(default_factory=list)
    answer: str = ""         # Tavily's built-in AI answer (optional)
    latency_ms: int = 0
    success: bool = True
    error: str = ""


class SearchService:
    """
    Unified search layer wrapping Tavily API.

    Includes a circuit breaker: after CIRCUIT_BREAKER_THRESHOLD consecutive
    failures, search is temporarily disabled for CIRCUIT_BREAKER_COOLDOWN_S
    seconds. This prevents wasting 15s per request on a broken service
    (e.g. expired API key, quota exhausted).

    Usage:
        service = SearchService()
        response = await service.search("量子纠缠是否允许超光速通信")
        rag_section = service.format_for_prompt(response)
    """

    CIRCUIT_BREAKER_THRESHOLD = 3       # consecutive failures to trip
    CIRCUIT_BREAKER_COOLDOWN_S = 300    # 5 min cooldown before retry

    def __init__(
        self,
        api_key_env: str = "TAVILY_API_KEY",
        max_results: int = 5,
        search_depth: str = "basic",      # "basic" (fast) or "advanced" (slower, better)
        include_answer: bool = True,       # Tavily AI summary
        timeout_seconds: int = 15,
    ) -> None:
        self._api_key = os.getenv(api_key_env, "")
        self._max_results = max_results
        self._search_depth = search_depth
        self._include_answer = include_answer
        self._timeout = timeout_seconds
        self._client = None

        # Circuit breaker state
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0    # monotonic timestamp

        if not self._api_key:
            logger.warning(
                f"Search service disabled: env '{api_key_env}' not set. "
                f"Get a free key at https://tavily.com"
            )
        else:
            try:
                from tavily import AsyncTavilyClient
                self._client = AsyncTavilyClient(api_key=self._api_key)
                logger.info("SearchService initialized (Tavily)")
            except ImportError:
                logger.error("tavily-python not installed: pip install tavily-python")

    @property
    def enabled(self) -> bool:
        """Whether search is available."""
        return self._client is not None

    async def search(self, query: str) -> SearchResponse:
        """
        Search the web for the given query.

        Returns SearchResponse with results, or an error response on failure.
        Always returns (never raises) — pipeline continues without search on error.

        Circuit breaker: after CIRCUIT_BREAKER_THRESHOLD consecutive failures,
        returns immediately for CIRCUIT_BREAKER_COOLDOWN_S seconds.
        """
        if not self._client:
            return SearchResponse(
                query=query, success=False,
                error="Search service not configured (no API key)"
            )

        # Circuit breaker: if open, skip search entirely
        now = time.monotonic()
        if self._consecutive_failures >= self.CIRCUIT_BREAKER_THRESHOLD:
            if now < self._circuit_open_until:
                remaining = int(self._circuit_open_until - now)
                logger.info(f"Search circuit OPEN — skipping ({remaining}s until retry)")
                return SearchResponse(
                    query=query, success=False,
                    error=f"Circuit breaker open ({self._consecutive_failures} consecutive failures, retry in {remaining}s)",
                )
            else:
                # Cooldown expired, allow one probe request
                logger.info("Search circuit half-open — probing...")

        start = time.monotonic()
        try:
            raw = await asyncio.wait_for(
                self._client.search(
                    query=query,
                    max_results=self._max_results,
                    search_depth=self._search_depth,
                    include_answer=self._include_answer,
                ),
                timeout=self._timeout,
            )
            latency = int((time.monotonic() - start) * 1000)

            results = []
            for item in raw.get("results", []):
                results.append(SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    content=item.get("content", ""),
                    score=item.get("score", 0.0),
                ))

            response = SearchResponse(
                query=query,
                results=results,
                answer=raw.get("answer", ""),
                latency_ms=latency,
                success=True,
            )

            # Success: reset circuit breaker
            if self._consecutive_failures > 0:
                logger.info(f"Search recovered after {self._consecutive_failures} failures — circuit CLOSED")
            self._consecutive_failures = 0
            self._circuit_open_until = 0.0

            logger.info(
                f"Search completed: {len(results)} results, "
                f"{latency}ms, query='{query[:50]}...'"
            )
            return response

        except Exception as e:
            latency = int((time.monotonic() - start) * 1000)
            self._consecutive_failures += 1

            if self._consecutive_failures >= self.CIRCUIT_BREAKER_THRESHOLD:
                self._circuit_open_until = time.monotonic() + self.CIRCUIT_BREAKER_COOLDOWN_S
                logger.error(
                    f"Search failed {self._consecutive_failures}x consecutively — "
                    f"circuit OPEN for {self.CIRCUIT_BREAKER_COOLDOWN_S}s. Error: {e}"
                )
            else:
                logger.warning(
                    f"Search failed ({latency}ms, {self._consecutive_failures}/"
                    f"{self.CIRCUIT_BREAKER_THRESHOLD} before circuit break): {e}"
                )

            return SearchResponse(
                query=query, latency_ms=latency,
                success=False, error=str(e),
            )

    async def search_multi(self, queries: list[str]) -> SearchResponse:
        """
        Execute multiple search queries concurrently and merge results.

        v4.5: Used by Planner-lite's search_queries for broader retrieval coverage.
        Results are deduplicated by URL, sorted by score descending,
        and capped at max_results * 2 to avoid excessively long rag_section.

        Always returns (never raises) — pipeline continues without search on error.
        """
        if not queries:
            return SearchResponse(query="", success=False, error="No queries provided")

        start = time.monotonic()
        tasks = [self.search(q) for q in queries]
        responses = await asyncio.gather(*tasks)

        # Merge results, deduplicate by URL
        seen_urls: set[str] = set()
        merged: list[SearchResult] = []
        combined_answer_parts: list[str] = []
        any_success = False

        for resp in responses:
            if resp.success:
                any_success = True
                if resp.answer:
                    combined_answer_parts.append(resp.answer)
                for r in resp.results:
                    if r.url and r.url not in seen_urls:
                        seen_urls.add(r.url)
                        merged.append(r)

        # Sort by score descending, cap at max_results * 2
        merged.sort(key=lambda r: r.score, reverse=True)
        cap = self._max_results * 2
        merged = merged[:cap]

        latency = int((time.monotonic() - start) * 1000)
        combined_query = " | ".join(queries)

        if not any_success:
            return SearchResponse(
                query=combined_query, success=False,
                error="All multi-query searches failed", latency_ms=latency,
            )

        logger.info(
            f"Search multi completed: {len(queries)} queries, "
            f"{len(merged)} unique results (cap={cap}), {latency}ms"
        )

        return SearchResponse(
            query=combined_query,
            results=merged,
            answer=" ".join(combined_answer_parts) if combined_answer_parts else "",
            latency_ms=latency,
            success=True,
        )

    @staticmethod
    def format_for_prompt(
        response: SearchResponse,
        max_chars: int = 3000,
    ) -> str:
        """
        Format search results into a string for {rag_section} injection.

        Returns empty string if no results — prompt template will clean up
        the unused placeholder automatically.
        """
        if not response.success or not response.results:
            return ""

        parts: list[str] = []
        parts.append("## 搜索结果（统一检索，所有分析师共享相同信息源）\n")

        # v4.23: Show search queries so models can evaluate coverage gaps
        if response.query:
            parts.append(f"**检索词**: {response.query[:200]}\n")

        # Include Tavily's AI answer if available
        if response.answer:
            parts.append(f"**摘要**: {response.answer}\n")

        char_count = sum(len(p) for p in parts)
        for i, r in enumerate(response.results, 1):
            entry = f"**[{i}] {r.title}**\n{r.content}\n来源: {r.url}\n"
            if char_count + len(entry) > max_chars:
                break
            parts.append(entry)
            char_count += len(entry)

        return "\n".join(parts)
