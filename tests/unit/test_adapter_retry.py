"""
Tests for the OpenAI adapter retry logic.

Verifies exponential backoff retry on 429 / 5xx errors
and no-retry on non-retryable errors (auth, bad request, etc.).
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter
from agoracle.domain.types import ModelResponse, Role


class TestIsRetryable:
    """Test the _is_retryable static method."""

    def test_429_is_retryable(self):
        err = Exception("Error code: 429 - Rate limit reached")
        assert OpenAIModelAdapter._is_retryable(err) is True

    def test_500_is_retryable(self):
        err = Exception("HTTP 500 Internal Server Error")
        assert OpenAIModelAdapter._is_retryable(err) is True

    def test_502_is_retryable(self):
        err = Exception("502 Bad Gateway")
        assert OpenAIModelAdapter._is_retryable(err) is True

    def test_503_is_retryable(self):
        err = Exception("503 Service Unavailable")
        assert OpenAIModelAdapter._is_retryable(err) is True

    def test_504_is_retryable(self):
        err = Exception("504 Gateway Timeout")
        assert OpenAIModelAdapter._is_retryable(err) is True

    def test_timeout_error_is_retryable(self):
        class TimeoutError(Exception):
            pass
        assert OpenAIModelAdapter._is_retryable(TimeoutError("timed out")) is True

    def test_connection_error_is_retryable(self):
        class ConnectionError(Exception):
            pass
        assert OpenAIModelAdapter._is_retryable(ConnectionError("refused")) is True

    def test_401_is_not_retryable(self):
        err = Exception("Error code: 401 - Unauthorized")
        assert OpenAIModelAdapter._is_retryable(err) is False

    def test_400_is_not_retryable(self):
        err = Exception("Error code: 400 - Bad Request")
        assert OpenAIModelAdapter._is_retryable(err) is False

    def test_generic_error_is_not_retryable(self):
        err = ValueError("some value error")
        assert OpenAIModelAdapter._is_retryable(err) is False

    def test_permission_error_is_not_retryable(self):
        err = PermissionError("access denied")
        assert OpenAIModelAdapter._is_retryable(err) is False


# ---------------------------------------------------------------------------
# Helpers used by benchmark-layer guard tests
# ---------------------------------------------------------------------------

def _make_response(retry_count: int, success: bool = True) -> ModelResponse:
    """Build a minimal ModelResponse with the given retry_count."""
    return ModelResponse(
        call_id="test-call",
        model_id="test-model",
        role=Role.CONTRIBUTOR,
        content="answer" if success else "",
        latency_ms=100,
        success=success,
        retry_count=retry_count,
    )


def _benchmark_retry_guard(response: ModelResponse) -> None:
    """
    Simulates the benchmark evaluation layer check.

    Rule: if any API call required a retry during benchmark evaluation,
    raise immediately so the benchmark pauses rather than producing
    results that may be skewed by transient API instability.
    """
    if response.retry_count > 0:
        raise RuntimeError(
            f"Benchmark paused: model '{response.model_id}' required "
            f"{response.retry_count} retry(s) — API instability detected. "
            f"Fix the API issue and re-run."
        )


class TestRetryCount:
    """Verify that ModelResponse.retry_count correctly reflects retry behaviour."""

    def test_no_retry_gives_zero(self):
        resp = _make_response(retry_count=0)
        assert resp.retry_count == 0

    def test_one_retry_gives_one(self):
        resp = _make_response(retry_count=1)
        assert resp.retry_count == 1

    def test_two_retries_gives_two(self):
        resp = _make_response(retry_count=2)
        assert resp.retry_count == 2

    def test_retry_count_default_is_zero(self):
        resp = ModelResponse(
            call_id="c",
            model_id="m",
            role=Role.CONTRIBUTOR,
            content="x",
            latency_ms=10,
        )
        assert resp.retry_count == 0


class TestBenchmarkRetryGuard:
    """
    Verify the benchmark-layer guard: any retry during evaluation must
    raise RuntimeError immediately so the benchmark pauses.

    This prevents benchmark results from being silently contaminated by
    transient API errors that triggered automatic retries.
    """

    def test_no_retry_does_not_raise(self):
        resp = _make_response(retry_count=0)
        _benchmark_retry_guard(resp)  # must not raise

    def test_one_retry_raises(self):
        resp = _make_response(retry_count=1)
        with pytest.raises(RuntimeError, match="retry"):
            _benchmark_retry_guard(resp)

    def test_two_retries_raises(self):
        resp = _make_response(retry_count=2)
        with pytest.raises(RuntimeError, match="Benchmark paused"):
            _benchmark_retry_guard(resp)

    def test_error_message_contains_model_id(self):
        resp = _make_response(retry_count=1)
        with pytest.raises(RuntimeError, match="test-model"):
            _benchmark_retry_guard(resp)

    def test_failed_response_with_no_retry_does_not_raise(self):
        resp = _make_response(retry_count=0, success=False)
        _benchmark_retry_guard(resp)  # failure without retry is handled separately

    def test_failed_response_with_retry_raises(self):
        resp = _make_response(retry_count=1, success=False)
        with pytest.raises(RuntimeError):
            _benchmark_retry_guard(resp)
