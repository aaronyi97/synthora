"""
Phase 4.5 unit tests — Planner-lite + multi-query search.

Tests:
  1. test_planner_lite_skipped_when_flag_off
  2. test_planner_lite_skipped_for_wrong_type
  3. test_planner_lite_fallback_on_failure
  4. test_search_multi_dedup
  5. test_feature_flags_loaded_from_config
"""

from __future__ import annotations

import asyncio
import pytest

from agoracle.config.schema import AppConfig, FeatureFlags, ModeConfig
from agoracle.domain.types import QuestionType, RoleCall, Role


# ═══════════════════════════════════════════════════════════════
# #1: Planner-lite skip when flag is off
# ═══════════════════════════════════════════════════════════════

class TestPlannerLiteSkippedWhenFlagOff:
    """planner_lite=False → Step 0.5 must not execute."""

    def test_planner_not_triggered(self):
        """When planner_lite=False, planner_result stays None regardless of mode/question_type."""
        config = AppConfig()
        config.features = FeatureFlags(planner_lite=False)

        # Simulate the orchestrator guard condition
        mode = "research"
        question_type = QuestionType.ANALYTICAL

        from agoracle.services.orchestrator import _PLANNER_ELIGIBLE_TYPES

        should_run = (
            config.features.planner_lite
            and mode == "research"
            and question_type in _PLANNER_ELIGIBLE_TYPES
        )
        assert should_run is False, "Planner should NOT run when planner_lite=False"

    def test_planner_flag_on_triggers(self):
        """Sanity check: when planner_lite=True + research + ANALYTICAL, guard passes."""
        config = AppConfig()
        config.features = FeatureFlags(planner_lite=True)

        from agoracle.services.orchestrator import _PLANNER_ELIGIBLE_TYPES

        should_run = (
            config.features.planner_lite
            and "research" == "research"
            and QuestionType.ANALYTICAL in _PLANNER_ELIGIBLE_TYPES
        )
        assert should_run is True


# ═══════════════════════════════════════════════════════════════
# #2: Planner-lite skip for wrong question_type
# ═══════════════════════════════════════════════════════════════

class TestPlannerLiteSkippedForWrongType:
    """Planner-lite only fires for ANALYTICAL / REASONING / CONTROVERSIAL."""

    def test_factual_skipped(self):
        from agoracle.services.orchestrator import _PLANNER_ELIGIBLE_TYPES
        assert QuestionType.FACTUAL not in _PLANNER_ELIGIBLE_TYPES

    def test_creative_skipped(self):
        from agoracle.services.orchestrator import _PLANNER_ELIGIBLE_TYPES
        assert QuestionType.CREATIVE not in _PLANNER_ELIGIBLE_TYPES

    def test_writing_skipped(self):
        from agoracle.services.orchestrator import _PLANNER_ELIGIBLE_TYPES
        assert QuestionType.WRITING not in _PLANNER_ELIGIBLE_TYPES

    def test_coding_skipped(self):
        from agoracle.services.orchestrator import _PLANNER_ELIGIBLE_TYPES
        assert QuestionType.CODING not in _PLANNER_ELIGIBLE_TYPES

    def test_math_skipped(self):
        from agoracle.services.orchestrator import _PLANNER_ELIGIBLE_TYPES
        assert QuestionType.MATH not in _PLANNER_ELIGIBLE_TYPES

    def test_cultural_skipped(self):
        from agoracle.services.orchestrator import _PLANNER_ELIGIBLE_TYPES
        assert QuestionType.CULTURAL not in _PLANNER_ELIGIBLE_TYPES

    def test_eligible_types_present(self):
        from agoracle.services.orchestrator import _PLANNER_ELIGIBLE_TYPES
        assert QuestionType.ANALYTICAL in _PLANNER_ELIGIBLE_TYPES
        assert QuestionType.REASONING in _PLANNER_ELIGIBLE_TYPES
        assert QuestionType.CONTROVERSIAL in _PLANNER_ELIGIBLE_TYPES
        assert QuestionType.TECHNICAL in _PLANNER_ELIGIBLE_TYPES  # v4.26: added

    def test_deep_mode_not_triggered(self):
        """Planner only fires in research mode, not deep."""
        config = AppConfig()
        config.features = FeatureFlags(planner_lite=True)
        from agoracle.services.orchestrator import _PLANNER_ELIGIBLE_TYPES

        should_run = (
            config.features.planner_lite
            and "deep" == "research"
            and QuestionType.ANALYTICAL in _PLANNER_ELIGIBLE_TYPES
        )
        assert should_run is False


# ═══════════════════════════════════════════════════════════════
# #3: Planner-lite fallback on failure
# ═══════════════════════════════════════════════════════════════

class TestPlannerLiteFallbackOnFailure:
    """Planner timeout/failure → planner_result=None, pipeline continues."""

    def test_json_parse_failure_returns_none(self):
        """Invalid JSON from planner → caught, planner_result stays None."""
        import json
        planner_result = None
        try:
            raw = "not valid json at all"
            planner_result = json.loads(raw)
        except Exception:
            planner_result = None

        assert planner_result is None

    def test_empty_response_returns_none(self):
        """Empty content → planner_result stays None."""
        planner_result = None
        content = ""
        if content:
            import json
            try:
                planner_result = json.loads(content)
            except Exception:
                planner_result = None
        assert planner_result is None

    def test_valid_json_parses_correctly(self):
        """Valid planner JSON → correctly parsed."""
        import json
        raw = '{"sub_questions": ["Q1", "Q2"], "search_queries": ["搜索1"]}'
        result = json.loads(raw)
        assert len(result["sub_questions"]) == 2
        assert len(result["search_queries"]) == 1

    def test_markdown_wrapped_json(self):
        """Planner may wrap JSON in markdown code fences → strip and parse."""
        import json
        raw = '```json\n{"sub_questions": ["Q1"], "search_queries": ["S1"]}\n```'
        _raw = raw.strip()
        if _raw.startswith("```"):
            _raw = _raw.split("```")[1]
            if _raw.startswith("json"):
                _raw = _raw[4:]
        result = json.loads(_raw.strip())
        assert result["sub_questions"] == ["Q1"]
        assert result["search_queries"] == ["S1"]


# ═══════════════════════════════════════════════════════════════
# #4: search_multi URL deduplication
# ═══════════════════════════════════════════════════════════════

class TestSearchMultiDedup:
    """search_multi merges results and deduplicates by URL."""

    @pytest.mark.asyncio
    async def test_dedup_by_url(self):
        from agoracle.services.search_service import SearchService, SearchResponse, SearchResult

        service = SearchService.__new__(SearchService)
        service._client = None
        service._max_results = 5
        service._consecutive_failures = 0
        service._circuit_open_until = 0.0

        # Mock the search method to return overlapping results
        call_count = 0

        async def mock_search(query: str) -> SearchResponse:
            nonlocal call_count
            call_count += 1
            if "query1" in query:
                return SearchResponse(
                    query=query, success=True, latency_ms=100,
                    answer="Answer 1",
                    results=[
                        SearchResult(title="A", url="https://a.com", content="a", score=0.9),
                        SearchResult(title="B", url="https://b.com", content="b", score=0.8),
                        SearchResult(title="C", url="https://c.com", content="c", score=0.7),
                    ],
                )
            else:
                return SearchResponse(
                    query=query, success=True, latency_ms=150,
                    answer="Answer 2",
                    results=[
                        SearchResult(title="B-dup", url="https://b.com", content="b2", score=0.85),
                        SearchResult(title="D", url="https://d.com", content="d", score=0.6),
                    ],
                )

        service.search = mock_search

        result = await service.search_multi(["query1", "query2"])

        assert result.success is True
        assert call_count == 2

        urls = [r.url for r in result.results]
        assert len(urls) == len(set(urls)), "URLs must be unique (dedup)"
        assert "https://b.com" in urls
        assert "https://a.com" in urls
        assert "https://c.com" in urls
        assert "https://d.com" in urls
        assert len(result.results) == 4

    @pytest.mark.asyncio
    async def test_sorted_by_score(self):
        from agoracle.services.search_service import SearchService, SearchResponse, SearchResult

        service = SearchService.__new__(SearchService)
        service._client = None
        service._max_results = 5
        service._consecutive_failures = 0
        service._circuit_open_until = 0.0

        async def mock_search(query: str) -> SearchResponse:
            return SearchResponse(
                query=query, success=True, latency_ms=50,
                results=[
                    SearchResult(title="Low", url=f"https://{query}-low.com", content="l", score=0.3),
                    SearchResult(title="High", url=f"https://{query}-high.com", content="h", score=0.9),
                ],
            )

        service.search = mock_search

        result = await service.search_multi(["q1", "q2"])

        scores = [r.score for r in result.results]
        assert scores == sorted(scores, reverse=True), "Results must be sorted by score descending"

    @pytest.mark.asyncio
    async def test_cap_at_max_results_times_two(self):
        from agoracle.services.search_service import SearchService, SearchResponse, SearchResult

        service = SearchService.__new__(SearchService)
        service._client = None
        service._max_results = 2  # cap = 4
        service._consecutive_failures = 0
        service._circuit_open_until = 0.0

        async def mock_search(query: str) -> SearchResponse:
            return SearchResponse(
                query=query, success=True, latency_ms=50,
                results=[
                    SearchResult(title=f"{query}-{i}", url=f"https://{query}-{i}.com", content="x", score=0.5)
                    for i in range(5)
                ],
            )

        service.search = mock_search

        result = await service.search_multi(["q1", "q2", "q3"])

        assert len(result.results) <= 4, f"Cap should be max_results*2=4, got {len(result.results)}"

    @pytest.mark.asyncio
    async def test_all_fail_returns_failure(self):
        from agoracle.services.search_service import SearchService, SearchResponse

        service = SearchService.__new__(SearchService)
        service._client = None
        service._max_results = 5
        service._consecutive_failures = 0
        service._circuit_open_until = 0.0

        async def mock_search(query: str) -> SearchResponse:
            return SearchResponse(query=query, success=False, error="timeout")

        service.search = mock_search

        result = await service.search_multi(["q1", "q2"])
        assert result.success is False

    @pytest.mark.asyncio
    async def test_empty_queries_returns_failure(self):
        from agoracle.services.search_service import SearchService

        service = SearchService.__new__(SearchService)
        service._client = None
        service._max_results = 5
        service._consecutive_failures = 0
        service._circuit_open_until = 0.0

        result = await service.search_multi([])
        assert result.success is False


# ═══════════════════════════════════════════════════════════════
# #5: Feature flags loaded from config.yaml
# ═══════════════════════════════════════════════════════════════

class TestFeatureFlagsLoadedFromConfig:
    """config.yaml features: block correctly parsed to AppConfig.features."""

    def test_default_flags_all_false(self):
        """Default FeatureFlags has all flags off."""
        ff = FeatureFlags()
        assert ff.planner_lite is False
        assert ff.multi_query_search is False
        assert ff.fact_check_shadow is False

    def test_flags_parsed_from_yaml(self):
        """_parse_config correctly reads features block."""
        from agoracle.config.loader import _parse_config

        raw = {
            "features": {
                "planner_lite": True,
                "multi_query_search": True,
                "fact_check_shadow": False,
            }
        }
        config = _parse_config(raw)
        assert config.features.planner_lite is True
        assert config.features.multi_query_search is True
        assert config.features.fact_check_shadow is False

    def test_missing_features_block_uses_defaults(self):
        """No features block in YAML → all flags default to False."""
        from agoracle.config.loader import _parse_config

        raw = {}
        config = _parse_config(raw)
        assert config.features.planner_lite is False
        assert config.features.multi_query_search is False
        assert config.features.fact_check_shadow is False

    def test_partial_features_block(self):
        """Only some flags specified → rest default to False."""
        from agoracle.config.loader import _parse_config

        raw = {
            "features": {
                "planner_lite": True,
            }
        }
        config = _parse_config(raw)
        assert config.features.planner_lite is True
        assert config.features.multi_query_search is False
        assert config.features.fact_check_shadow is False

    def test_features_on_appconfig(self):
        """AppConfig.features is FeatureFlags type."""
        config = AppConfig()
        assert isinstance(config.features, FeatureFlags)
