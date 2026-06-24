"""
OpenAI-compatible model adapter — handles ALL models in the system.

All models (GPT, Claude proxy, Gemini proxy, Kimi, DeepSeek) use
OpenAI-compatible API format. This single adapter covers them all.

Each call is stateless — no conversation history between calls.
This is the architectural guarantee of role isolation.

v2.1 changes:
  - Per-model temperature (from ModelConfig, falls back to 0.7)
  - Per-request timeout override (from RoleCall.timeout_seconds)
  - Web search passthrough (extra_body or Kimi $web_search builtin)
  - Thinking parameter for Kimi K2.5 (extra_body.thinking)

v2.2 changes:
  - Automatic retry with exponential backoff for 429 / 5xx errors
  - Configurable MAX_RETRIES and base delay
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from contextvars import ContextVar
from typing import Any, AsyncIterator

import httpx
from openai import AsyncOpenAI

from agoracle.config.schema import AppConfig, ModelConfig
from agoracle.domain.types import ModelResponse, Role, RoleCall

logger = logging.getLogger(__name__)

DEFAULT_TEMPERATURE = 0.7

# ── Pricing table ($/K tokens) ─────────────────────────────────────────
# Pricing configuration (USD per 1K tokens) used for cost estimation/logging.
# Format: model_name_pattern -> (price_per_1k_tokens, 0, is_per_call)
# is_per_call=True means flat $ per call regardless of tokens
_PRICE_TABLE: list[tuple[str, float, float, bool]] = [
    # GPT-5.x thinking variants — flat $0.05/call
    ("gpt-5.2-thinking",                  0.05,   0.05,   True),
    ("gpt-5.2-all",                        0.05,   0.05,   True),
    ("gpt-5.3-codex-high",                 0.05,   0.05,   True),
    # GPT-5.x standard — $0.00154/K tokens
    ("gpt-5.2-2025-12-11",                 0.00154, 0.00154, False),
    ("gpt-5.3-codex-low",                  0.00154, 0.00154, False),
    ("gpt-5.3-codex-medium",               0.00154, 0.00154, False),
    ("gpt-5.3-codex",                      0.00154, 0.00154, False),
    ("gpt-5.2",                            0.00154, 0.00154, False),
    # GPT-5.2-pro — $0.063/K tokens
    ("gpt-5.2-pro",                        0.063,  0.063,  False),
    # Claude Opus thinking — $0.01775/K tokens
    ("claude-opus-4-6",                    0.01775, 0.01775, False),
    ("claude-opus-4-5",                    0.01775, 0.01775, False),
    # Claude Sonnet thinking — $0.01294/K tokens
    ("claude-sonnet-4-6",                  0.01294, 0.01294, False),
    ("claude-sonnet-4-5",                  0.01294, 0.01294, False),
    # Claude Haiku — $0.0008/K tokens
    ("claude-haiku-4-5",                   0.0008, 0.0008, False),
    # Gemini 3.1 Pro customtools — $0.00772/K tokens
    ("gemini-3.1-pro",                     0.00772, 0.00772, False),
    # Gemini 3 Pro — $0.00411/K tokens
    ("gemini-3-pro",                       0.00411, 0.00411, False),
    # Gemini 3 Flash — $0.0005/K tokens
    ("gemini-3-flash",                     0.0005, 0.0005, False),
    # Kimi — $0.002/K tokens
    ("moonshot",                           0.002,  0.002,  False),
    ("kimi",                               0.002,  0.002,  False),
    # DeepSeek — $0.001/K tokens
    ("deepseek",                           0.001,  0.001,  False),
]
_CNY_PER_USD = 1.35  # USD→CNY conversion rate


class _NullAsyncContext:
    """No-op async context manager — used in call_stream when no Semaphore is configured."""
    async def __aenter__(self):
        return self
    async def __aexit__(self, *_):
        pass


def _estimate_cost_cny(model_name: str, p_tokens: int, c_tokens: int) -> str:
    """Estimate cost in CNY based on the pricing table. Returns formatted string."""
    model_lower = model_name.lower()
    for pattern, in_price, out_price, is_per_call in _PRICE_TABLE:
        if pattern in model_lower:
            if is_per_call:
                cost_usd = in_price  # flat per call
            else:
                cost_usd = (p_tokens * in_price + c_tokens * out_price) / 1000
            cost_cny = cost_usd * _CNY_PER_USD
            return f"¥{cost_cny:.4f}"
    return "¥?(未知模型价格)"


# ── Retry configuration ──────────────────────────────────
MAX_RETRIES = 2           # up to 2 retries (3 total attempts)
RETRY_BASE_DELAY = 1.0    # seconds; actual delay = base * 2^attempt
# v2.8: Kimi $web_search tool_call loop — max rounds to prevent infinite loops
MAX_TOOL_CALL_ROUNDS = 3  # Kimi typically needs 1 round; cap at 3 for safety
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
# v2.2.1: Errors that should NEVER be retried (permanent failures)
NON_RETRYABLE_KEYWORDS = {
    "insufficient_user_quota",
    "insufficient_quota",
    "billing",
    "account_deactivated",
    "invalid_api_key",
    "authentication",
    "no available accounts",
}

_QUOTA_KEYWORDS = {"insufficient_user_quota", "insufficient_quota", "billing", "no available accounts"}


def _classify_error(error: Exception) -> str:
    """Return a structured error tag for downstream user-facing message mapping.

    Preserves semantic meaning without exposing raw exception text.
    """
    error_str = str(error).lower()
    if any(kw in error_str for kw in _QUOTA_KEYWORDS):
        return "QUOTA_EXHAUSTED: model call failed"
    if "timeout" in type(error).__name__.lower() or "timed out" in error_str:
        return "UPSTREAM_TIMEOUT_RETRY_EXHAUSTED: model call failed"
    return f"{type(error).__name__}: model call failed"


class OpenAIModelAdapter:
    """
    Universal model adapter using OpenAI-compatible API.

    One adapter instance manages connections to all configured models.
    Each model gets its own AsyncOpenAI client (different base_url/api_key).
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._clients: dict[str, AsyncOpenAI] = {}          # single-key models
        self._key_clients: dict[str, list[AsyncOpenAI]] = {}  # all clients per model
        self._model_configs: dict[str, ModelConfig] = config.models
        self._startup_skipped_models: list[dict[str, Any]] = []
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._rr_counters: dict[str, int] = {}               # v2.8.6: round-robin index per model
        # v4.2: Per-request cost tracker via ContextVar for concurrent-safe isolation.
        # v2.8.8: Replaced shared instance list with ContextVar — concurrent requests
        # were calling reset_cost_tracker() at pipeline start and clearing each other's data.
        # ContextVar gives each asyncio Task its own tracker list.
        # Format: list of (model_id, prompt_tokens, completion_tokens, cost_usd)
        self._cost_tracker_var: ContextVar[list[tuple[str, int, int, float]] | None] = ContextVar(
            f"cost_tracker_{id(self)}", default=None
        )
        self._init_clients()

    # v2.8.9+: per-model degraded key registry — maps model_id -> {client -> degraded_until ts}
    # Auth-failed keys are quarantined for _KEY_COOLDOWN_SECS before re-admission.
    _KEY_COOLDOWN_SECS: int = 60

    def _init_clients(self) -> None:
        """Initialize AsyncOpenAI client(s) for each model.
        Supports multi-key rotation via api_key_env_list.
        """
        # Per-model degraded key registry (populated at runtime, not at init)
        self._degraded_keys: dict[str, dict[int, float]] = {}  # model_id -> {id(client) -> degraded_until}
        self._startup_skipped_models = []

        for model_id, mc in self._model_configs.items():
            base_url = os.getenv(mc.base_url_env, "")

            # F-09 / D-8-1 guard: non-OpenAI models must have an explicit base_url.
            # An empty base_url causes the SDK to fall back to api.openai.com/v1,
            # which will 401 for every API key — silent catastrophic failure.
            _is_openai_official = (mc.provider or "").lower() in ("openai", "openai_official", "")
            if not base_url and mc.base_url_env and not _is_openai_official:
                self._record_startup_skip(
                    model_id=model_id,
                    reason="base_url_env_resolved_empty",
                    detail=(
                        f"base_url_env='{mc.base_url_env}' resolved to empty string. "
                        "Non-OpenAI model skipped to avoid silent 401 fallback."
                    ),
                    configured_envs=[mc.base_url_env],
                )
                continue

            # Collect all available keys: api_key_env_list takes priority over api_key_env
            key_envs = mc.api_key_env_list if mc.api_key_env_list else ([mc.api_key_env] if mc.api_key_env else [])
            configured_envs = [env for env in key_envs if env]  # env names that were specified
            api_keys = [os.getenv(env, "") for env in key_envs]
            resolved_keys = [k for k in api_keys if k]  # non-empty values

            # F-09 / D-8-2 guard: if env names were configured but ALL resolved empty,
            # that is a misconfiguration (wrong env name), not an intentional skip.
            if configured_envs and not resolved_keys:
                self._record_startup_skip(
                    model_id=model_id,
                    reason="configured_envs_resolved_empty",
                    detail=(
                        f"{len(configured_envs)} configured key env var(s) resolved to empty string. "
                        "Check config.yaml env names and .env loading."
                    ),
                    configured_envs=configured_envs,
                )
                continue
            api_keys = resolved_keys

            if not api_keys:
                self._record_startup_skip(
                    model_id=model_id,
                    reason="api_key_env_not_configured",
                    detail="No api_key_env/api_key_env_list configured for this model.",
                    configured_envs=configured_envs,
                )
                continue

            # Per-model proxy support via config.yaml proxy_env field.
            # optional per-model proxy via a user-provided env var (leave proxy_env empty for direct connection).
            proxy_url = None
            if mc.proxy_env:
                proxy_url = os.getenv(mc.proxy_env, "")
            if not proxy_url:
                # Fallback: legacy HTTPS_PROXY for backwards compat
                proxy_url = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or ""
                # Only apply global HTTPS_PROXY to models that explicitly opt in via proxy_env
                if not mc.proxy_env:
                    proxy_url = ""
            http_client = (
                httpx.AsyncClient(proxy=proxy_url, timeout=mc.timeout_seconds or None)
                if proxy_url else None
            )
            if proxy_url:
                logger.info(f"Model '{model_id}': using proxy {mc.proxy_env or 'HTTPS_PROXY'} → {proxy_url[:30]}...")

            clients = [
                AsyncOpenAI(
                    api_key=key,
                    base_url=base_url if base_url else None,
                    timeout=mc.timeout_seconds or None,  # 0 = no timeout
                    http_client=http_client,
                )
                for key in api_keys
            ]

            self._key_clients[model_id] = clients
            self._clients[model_id] = clients[0]          # default (single-key compat)

            # Per-model concurrency limiter
            # v4.12: Scale semaphore by key count — each key comes from a separate
            # account with independent per-account concurrency limits (e.g. Kimi
            # per-account=2, 3 accounts → effective=6). Round-robin distributes
            # calls evenly so each key stays within its per-account limit.
            key_count = len(clients)
            if mc.max_concurrency > 0:
                effective_concurrency = mc.max_concurrency * key_count
                self._semaphores[model_id] = asyncio.Semaphore(effective_concurrency)

            logger.info(
                f"Initialized {'multi-key(' + str(key_count) + ')' if key_count > 1 else 'client'} "
                f"for '{model_id}' (model={mc.model_name}, base_url={base_url or 'default'}"
                f"{', max_concurrency=' + str(mc.max_concurrency) + '×' + str(key_count) + '=' + str(mc.max_concurrency * key_count) if mc.max_concurrency > 0 else ''})"
            )

    def _record_startup_skip(
        self,
        *,
        model_id: str,
        reason: str,
        detail: str,
        configured_envs: list[str] | None = None,
    ) -> None:
        configured_envs = [env for env in (configured_envs or []) if env]
        record: dict[str, Any] = {
            "model_id": model_id,
            "reason": reason,
            "detail": detail,
        }
        if configured_envs:
            record["configured_envs"] = configured_envs
        self._startup_skipped_models.append(record)
        logger.warning(
            "[STARTUP MODEL SKIPPED] model_id=%s reason=%s configured_envs=%s detail=%s",
            model_id,
            reason,
            configured_envs or [],
            detail,
        )

    def _mark_key_degraded(self, model_id: str, client: AsyncOpenAI) -> None:
        """Mark a key as degraded for _KEY_COOLDOWN_SECS seconds.

        v2.9: Per-key quarantine for auth/quota failures. During cooldown the
        key is skipped in rotation; it re-enters the pool automatically after
        the TTL expires (no external service required).
        """
        if model_id not in self._degraded_keys:
            self._degraded_keys[model_id] = {}
        until = time.monotonic() + self._KEY_COOLDOWN_SECS
        self._degraded_keys[model_id][id(client)] = until
        logger.warning(
            f"Key for '{model_id}' marked DEGRADED for {self._KEY_COOLDOWN_SECS}s "
            f"(id={id(client)})"
        )

    def _active_clients(self, model_id: str) -> list[AsyncOpenAI]:
        """Return the subset of _key_clients that are NOT in cooldown.

        If all keys are degraded (e.g. entire pool bad), returns the full list
        so the caller still has a chance rather than a hard stop.
        """
        all_clients = self._key_clients.get(model_id, [])
        if not all_clients:
            return all_clients
        degraded_map = self._degraded_keys.get(model_id, {})
        now = time.monotonic()
        # Expire stale entries
        expired = [cid for cid, until in degraded_map.items() if now >= until]
        for cid in expired:
            del degraded_map[cid]
        active = [c for c in all_clients if id(c) not in degraded_map]
        if not active:
            logger.warning(
                f"All keys for '{model_id}' are in cooldown — using full pool as fallback"
            )
            return all_clients
        return active

    def supports_model(self, model_id: str) -> bool:
        """Check if this adapter has a client for the given model."""
        return model_id in self._clients

    def get_cost_tracker(self) -> list[tuple[str, int, int, float]]:
        """Return the current cost tracker data for this request context.
        
        v4.2: Returns list of (model_id, prompt_tokens, completion_tokens, cost_usd)
        for all API calls made since last reset.
        v2.8.8: Uses ContextVar for per-request isolation under concurrent load.
        """
        return list(self._cost_tracker_var.get(None) or [])

    def reset_cost_tracker(self) -> None:
        """Reset the cost tracker for this request context.
        
        v4.2: Called at the start of each pipeline execution to isolate
        per-query cost tracking.
        v2.8.8: Sets a fresh list in the ContextVar; other concurrent
        requests retain their own separate lists.
        """
        self._cost_tracker_var.set([])

    # ────────────────────────────────────────────────────────
    # Build request kwargs (shared by call and call_stream)
    # ────────────────────────────────────────────────────────

    def _build_create_kwargs(
        self,
        mc: ModelConfig,
        role_call: RoleCall,
        messages: list[dict[str, str]],
        *,
        stream: bool = False,
    ) -> dict[str, Any]:
        """
        Build the kwargs dict for client.chat.completions.create().

        Handles:
          - Per-model temperature (falls back to DEFAULT_TEMPERATURE)
          - Per-request timeout override from RoleCall
          - Thinking parameter for Kimi K2.5
          - Web search passthrough (extra_body or Kimi builtin)
          - Streaming flag
        """
        # Temperature: per-model override or adapter default
        temperature = mc.temperature if mc.temperature is not None else DEFAULT_TEMPERATURE

        kwargs: dict[str, Any] = {
            "model": mc.model_name,
            "messages": messages,
            "max_tokens": mc.max_tokens,
            "temperature": temperature,
            "timeout": role_call.timeout_seconds or None,  # 0 = no timeout
        }

        if stream:
            kwargs["stream"] = True

        if mc.reasoning_effort:
            kwargs["reasoning_effort"] = mc.reasoning_effort

        # ── Extra body parameters ──────────────────────────────
        extra_body: dict[str, Any] = {}

        # v2.8.4 FIX: Kimi K2.5 thinking + $web_search tool_call round 2
        # requires reasoning_content in assistant message, but OpenAI SDK
        # strips it during serialization.  Solution: when web_search is
        # requested on Kimi, explicitly DISABLE thinking (temperature=0.6).
        # This avoids the reasoning_content requirement entirely while
        # still getting high-quality search-augmented answers.
        use_thinking = mc.thinking_enabled
        is_kimi_web_search = (
            mc.thinking_enabled
            and role_call.web_search
            and mc.supports_search
            and mc.search_style == "kimi_builtin"
        )
        if is_kimi_web_search:
            use_thinking = False
            extra_body["thinking"] = {"type": "disabled"}
            kwargs["temperature"] = 0.6  # K2.5 requires 0.6 when thinking disabled

        if use_thinking:
            extra_body["thinking"] = {"type": "enabled"}

        # Web search
        if role_call.web_search and mc.supports_search:
            if mc.search_style == "kimi_builtin":
                kwargs["tools"] = [
                    {
                        "type": "builtin_function",
                        "function": {"name": "$web_search"},
                    }
                ]
            elif mc.search_style == "none":
                pass  # Model doesn't support search params
            else:
                # Default: extra_body approach (works for most proxies)
                extra_body["web_search"] = True

        if extra_body:
            kwargs["extra_body"] = extra_body

        return kwargs

    # ────────────────────────────────────────────────────────
    # Non-streaming call
    # ────────────────────────────────────────────────────────

    async def call(self, role_call: RoleCall) -> ModelResponse:
        """
        Make a non-streaming model call with automatic retry.

        Retries up to MAX_RETRIES times on 429 / 5xx errors with
        exponential backoff.  Respects per-model concurrency limits.
        """
        model_id = role_call.model_id
        client = self._clients.get(model_id)

        if not client:
            return ModelResponse(
                call_id=role_call.call_id,
                model_id=model_id,
                role=role_call.role,
                content="",
                latency_ms=0,
                success=False,
                error=f"No client initialized for model '{model_id}'",
            )

        mc = self._model_configs[model_id]
        sem = self._semaphores.get(model_id)
        start = time.monotonic()

        # Build messages list (once — reused across retries)
        messages: list[dict[str, str]] = []
        if role_call.system_prompt:
            messages.append({"role": "system", "content": role_call.system_prompt})
        messages.extend(role_call.messages)

        create_kwargs = self._build_create_kwargs(mc, role_call, messages)

        # v2.8: Track whether this model uses Kimi builtin tool_call flow
        is_kimi_builtin = (
            mc.search_style == "kimi_builtin"
            and role_call.web_search
            and mc.supports_search
        )

        last_error: Exception | None = None

        # v2.4: Hard wall-clock timeout per attempt. 0 = no timeout (None).
        hard_timeout = (role_call.timeout_seconds * 1.5) if role_call.timeout_seconds else None

        # v2.9: Use active (non-degraded) key pool for round-robin selection.
        _all_clients = self._key_clients.get(model_id, [client])
        _active = self._active_clients(model_id) if len(_all_clients) > 1 else _all_clients
        if len(_active) > 1:
            idx = self._rr_counters.get(model_id, 0) % len(_active)
            self._rr_counters[model_id] = idx + 1
            client = _active[idx]
        elif _active:
            client = _active[0]

        for attempt in range(1 + MAX_RETRIES):
            # Rotate to a different key on retry (if multiple keys available)
            if attempt > 0 and len(_all_clients) > 1:
                client = random.choice([c for c in _all_clients if c is not client] or _all_clients)
                logger.info(f"Model '{model_id}' rotating to next API key (attempt {attempt + 1})")
            try:
                # v2.8: Tool call loop for Kimi $web_search
                # Kimi returns finish_reason="tool_calls" with empty content on first call.
                # We must append assistant message + tool results, then call again.
                # Loop until we get actual content or hit MAX_TOOL_CALL_ROUNDS.
                current_kwargs = dict(create_kwargs)  # shallow copy for tool_call mutations
                tool_call_round = 0

                while True:
                    if sem:
                        if attempt == 0 and tool_call_round == 0:
                            logger.debug(
                                f"Model '{model_id}' waiting for semaphore "
                                f"({role_call.role.value})"
                            )
                        async with sem:
                            response = await asyncio.wait_for(
                                client.chat.completions.create(**current_kwargs),
                                timeout=hard_timeout,
                            )
                    else:
                        response = await asyncio.wait_for(
                            client.chat.completions.create(**current_kwargs),
                            timeout=hard_timeout,
                        )

                    choice = response.choices[0]
                    finish_reason = getattr(choice, "finish_reason", None)

                    # v2.8: Handle Kimi $web_search tool_call loop
                    if (
                        is_kimi_builtin
                        and finish_reason == "tool_calls"
                        and tool_call_round < MAX_TOOL_CALL_ROUNDS
                        and hasattr(choice.message, "tool_calls")
                        and choice.message.tool_calls
                    ):
                        tool_call_round += 1
                        # Append assistant message (with tool_calls) to conversation.
                        # v2.8.4 FIX: Must manually build the dict instead of appending
                        # the SDK object directly.  The OpenAI SDK serializer corrupts
                        # builtin_function → function type, causing Kimi API errors.
                        current_kwargs["messages"] = list(current_kwargs["messages"])
                        assistant_msg: dict[str, Any] = {
                            "role": "assistant",
                            "content": choice.message.content or "",
                            "tool_calls": [
                                {
                                    "id": tc.id,
                                    "type": "builtin_function",
                                    "function": {
                                        "name": tc.function.name,
                                        "arguments": tc.function.arguments,
                                    },
                                }
                                for tc in choice.message.tool_calls
                            ],
                        }
                        current_kwargs["messages"].append(assistant_msg)

                        # For each tool_call, append a tool result message
                        # Kimi builtin $web_search: just echo back the arguments
                        # (Kimi server does the actual search)
                        for tc in choice.message.tool_calls:
                            tc_name = tc.function.name
                            try:
                                tc_args = json.loads(tc.function.arguments)
                            except (json.JSONDecodeError, TypeError):
                                tc_args = tc.function.arguments or ""

                            if tc_name == "$web_search":
                                tool_result = tc_args  # echo back — Kimi handles search
                            else:
                                tool_result = f"Error: unknown tool '{tc_name}'"

                            current_kwargs["messages"].append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "name": tc_name,
                                "content": json.dumps(tool_result, ensure_ascii=False)
                                    if isinstance(tool_result, (dict, list))
                                    else str(tool_result),
                            })

                        logger.info(
                            f"Model '{model_id}' tool_call round {tool_call_round}: "
                            f"{len(choice.message.tool_calls)} tool_calls "
                            f"(finish_reason={finish_reason}), continuing..."
                        )
                        continue  # Make another API call with tool results

                    # Normal response (finish_reason="stop" or content available)
                    break

                elapsed_ms = int((time.monotonic() - start) * 1000)
                content = choice.message.content or ""

                # v2.8.8: Detect tool_call exhaustion — loop exited via break while still
                # in tool_calls state (limit reached). Return explicit failure instead of
                # silently passing empty content as success.
                if (
                    is_kimi_builtin
                    and tool_call_round >= MAX_TOOL_CALL_ROUNDS
                    and finish_reason == "tool_calls"
                    and not content
                ):
                    logger.warning(
                        f"Model '{model_id}' Kimi tool_call exhausted after "
                        f"{tool_call_round} rounds with no text content "
                        f"(finish_reason=tool_calls) — returning failure"
                    )
                    return ModelResponse(
                        call_id=role_call.call_id,
                        model_id=model_id,
                        role=role_call.role,
                        content="",
                        latency_ms=elapsed_ms,
                        success=False,
                        error="TOOL_CALL_EXHAUSTED",
                    )

                if tool_call_round > 0:
                    logger.info(
                        f"Model '{model_id}' Kimi search completed after "
                        f"{tool_call_round} tool_call round(s)"
                    )

                # v2.3: Capture token usage
                p_tokens = 0
                c_tokens = 0
                if hasattr(response, "usage") and response.usage:
                    p_tokens = getattr(response.usage, "prompt_tokens", 0) or 0
                    c_tokens = getattr(response.usage, "completion_tokens", 0) or 0

                if attempt > 0:
                    logger.info(
                        f"Model '{model_id}' succeeded on retry {attempt}"
                    )

                cost_str = _estimate_cost_cny(mc.model_name, p_tokens, c_tokens)
                logger.info(
                    f"Model '{model_id}' ({role_call.role.value}) responded "
                    f"in {elapsed_ms}ms, {len(content)} chars, "
                    f"{p_tokens}+{c_tokens} tokens, 费用≈{cost_str}"
                    f"{f', {tool_call_round} search rounds' if tool_call_round else ''}"
                )

                # v4.2: Record to per-request cost tracker (using config.yaml pricing)
                cost_usd = (
                    p_tokens * mc.cost_per_1m_input / 1_000_000
                    + c_tokens * mc.cost_per_1m_output / 1_000_000
                )
                _tracker = self._cost_tracker_var.get(None)
                if _tracker is None:
                    _tracker = []
                    self._cost_tracker_var.set(_tracker)
                _tracker.append((model_id, p_tokens, c_tokens, cost_usd))

                # v4.22: Extract Perplexity API citations (list of URLs)
                _resp_metadata: dict[str, Any] = {}
                # v4.22c: Dual-field fallback — Perplexity API may use "citations" or "search_results"
                _raw_citations = getattr(response, "citations", None) or getattr(response, "search_results", None)
                if _raw_citations:
                    _resp_metadata["citations"] = _raw_citations
                    logger.info(
                        f"Model '{model_id}': extracted {len(_raw_citations)} "
                        f"Perplexity citations"
                    )

                return ModelResponse(
                    call_id=role_call.call_id,
                    model_id=model_id,
                    role=role_call.role,
                    content=content,
                    latency_ms=elapsed_ms,
                    success=True,
                    prompt_tokens=p_tokens,
                    completion_tokens=c_tokens,
                    retry_count=attempt,
                    metadata=_resp_metadata,
                )

            except Exception as e:
                last_error = e

                # Check if the error is retryable
                # v2.2.1: Timeout errors only get 1 retry (model is inherently slow)
                # v4.8: Judge/JudgeRefine timeout → 0 retries (no-retry fast-fail).
                # Rationale: Judge timeout on long answers (e.g. cultural/analytical) is
                # structural — same model + same content will timeout again. Retrying wastes
                # the per-question budget (180s × 2 = 360s) and triggers total timeout,
                # preventing fallback to BEST_SINGLE. Fast-fail lets orchestrator degrade
                # gracefully instead of killing the entire question.
                # v4.9: question_critic timeout → 0 retries (same rationale as judge).
                # question_critic runs claude_sonnet which repeatedly hits 180s timeout;
                # 3 retries × 60s = 3min blocks an entire gunicorn worker. Fast-fail
                # lets pipeline skip critic and proceed with final answer immediately.
                # v4.32: socratic_guide/divergence_analyzer added — these roles have
                # tight outer wait_for timeouts; retrying wastes time and can exceed
                # the outer timeout, causing Socratic mode to appear stuck.
                _is_no_retry_role = role_call.role.value in (
                    "judge", "judge_refine", "question_critic",
                    "socratic_guide", "divergence_analyzer", "socratic_refiner"
                )
                if self._is_timeout(e) and _is_no_retry_role:
                    max_retries_for_error = 0
                elif self._is_timeout(e):
                    max_retries_for_error = 1
                else:
                    max_retries_for_error = MAX_RETRIES
                if attempt < max_retries_for_error and self._is_retryable(e):
                    delay = RETRY_BASE_DELAY * (2 ** attempt) * random.uniform(0.5, 1.5)
                    logger.warning(
                        f"Model '{model_id}' attempt {attempt + 1} failed "
                        f"(type={type(e).__name__}), retrying in {delay:.1f}s…"
                    )
                    await asyncio.sleep(delay)
                    continue

                # v2.9: Key rotation + quarantine for auth/quota errors on multi-key models.
                # API keys are per-account — one key expired/quota-exhausted
                # doesn't mean all keys are invalid. Quarantine the bad key and rotate.
                # No backoff delay: auth failures are instant (~200ms), waiting won't help.
                if (
                    len(_all_clients) > 1
                    and attempt < max_retries_for_error
                    and self._is_key_specific_error(e)
                ):
                    self._mark_key_degraded(model_id, client)
                    # Pick a fresh active key for next attempt
                    _fresh = [c for c in self._active_clients(model_id) if c is not client]
                    if _fresh:
                        client = random.choice(_fresh)
                    logger.warning(
                        f"Model '{model_id}' key-specific error on attempt {attempt + 1}, "
                        f"key quarantined + rotated ({attempt + 1}/{1 + MAX_RETRIES} attempts): "
                        f"{type(e).__name__}"
                    )
                    continue

                # Non-retryable or out of retries
                break

        # All attempts exhausted
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.error(
            f"Model '{model_id}' ({role_call.role.value}) failed "
            f"after {elapsed_ms}ms ({attempt + 1} attempts): {last_error}"
        )
        return ModelResponse(
            call_id=role_call.call_id,
            model_id=model_id,
            role=role_call.role,
            content="",
            latency_ms=elapsed_ms,
            success=False,
            error=_classify_error(last_error),
        )

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        """Check if an error should trigger a retry.

        v2.2.1: Quota/billing errors are NEVER retried (permanent failures).
        Timeout errors are retried but with reduced attempts (handled by caller).
        """
        error_str = str(error).lower()

        # v2.2.1: Permanent failures — never retry
        for keyword in NON_RETRYABLE_KEYWORDS:
            if keyword in error_str:
                return False

        # OpenAI SDK wraps HTTP status in the exception message
        for code in RETRYABLE_STATUS_CODES:
            if str(code) in str(error):
                return True
        # Also retry on generic connection / timeout errors
        error_type = type(error).__name__
        if any(kw in error_type for kw in ("Timeout", "Connection", "Network")):
            return True
        return False

    @staticmethod
    def _is_timeout(error: Exception) -> bool:
        """Check if an error is a timeout (used to limit timeout retries)."""
        error_type = type(error).__name__
        error_str = str(error).lower()
        return "timeout" in error_type.lower() or "timed out" in error_str

    @staticmethod
    def _is_key_specific_error(error: Exception) -> bool:
        """Check if error is likely per-key/per-account (worth rotating to another key).

        v2.8.9: For multi-key models, auth/quota errors are per-account. One key
        failing doesn't mean all keys are invalid. Callers should try other keys
        before giving up entirely.

        v2.9: Status-code check takes priority over fragile string matching.
        Some API gateways may return non-English messages; checking the numeric
        status code is more reliable than substring matching.
        """
        # Primary: check HTTP status code if available (proxy-safe)
        status_code = getattr(error, "status_code", None)
        if status_code in (401, 403):
            return True
        # Fallback: string-based keyword matching
        error_str = str(error).lower()
        return any(kw in error_str for kw in NON_RETRYABLE_KEYWORDS)

    # ────────────────────────────────────────────────────────
    # Streaming call
    # ────────────────────────────────────────────────────────

    async def call_stream(self, role_call: RoleCall) -> AsyncIterator[str]:
        """Make a streaming model call. Yields text chunks.

        v4.16 fixes:
          - Round-robin key selection (mirrors non-streaming call())
          - async-with Semaphore so acquire/release is always balanced
          - One-shot retry on retryable errors (429/5xx/connection).
            Streaming cannot resume mid-stream, so we retry from scratch
            with a rotated key. Max 1 retry (2 total attempts).
        """
        model_id = role_call.model_id
        if model_id not in self._clients:
            logger.error(f"No client for model '{model_id}'")
            raise ValueError(f"Model '{model_id}' has no initialized client — cannot stream")

        mc = self._model_configs[model_id]
        sem = self._semaphores.get(model_id)

        messages: list[dict[str, str]] = []
        if role_call.system_prompt:
            messages.append({"role": "system", "content": role_call.system_prompt})
        messages.extend(role_call.messages)

        create_kwargs = self._build_create_kwargs(mc, role_call, messages, stream=True)

        # v2.9: Use active (non-degraded) key pool for round-robin selection.
        _all_clients = self._key_clients.get(model_id, [self._clients[model_id]])
        _active_s = self._active_clients(model_id) if len(_all_clients) > 1 else _all_clients
        if len(_active_s) > 1:
            idx = self._rr_counters.get(model_id, 0) % len(_active_s)
            self._rr_counters[model_id] = idx + 1
            client = _active_s[idx]
        else:
            client = _active_s[0] if _active_s else _all_clients[0]

        last_error: Exception | None = None
        yielded_any = False  # v2.8.8: track whether any tokens were yielded to caller
        _stream_chars = 0   # v2.8.8: accumulate chars for post-stream cost estimation
        _stream_start = time.monotonic()
        # v2.9: Hard deadline for the entire streaming call (create + token delivery).
        # Mirrors the non-streaming hard_timeout logic; prevents stream from hanging
        # indefinitely when upstream sends chunks very slowly or stops mid-response.
        _effective_timeout = role_call.timeout_seconds or mc.timeout_seconds
        _stream_hard_timeout = (_effective_timeout * 1.5) if _effective_timeout else None
        for attempt in range(2):  # 1 retry max for streaming
            if attempt > 0:
                # Only retry if nothing was yielded yet (non-idempotent: partial output
                # cannot be recalled, retrying would concatenate two complete responses)
                if yielded_any:
                    logger.warning(
                        f"Streaming '{model_id}' mid-stream failure after yielding tokens "
                        f"— skipping retry to prevent output corruption: {last_error}"
                    )
                    break
                # Rotate to a different key on retry
                if len(_all_clients) > 1:
                    client = random.choice(
                        [c for c in _all_clients if c is not client] or _all_clients
                    )
                delay = RETRY_BASE_DELAY * random.uniform(0.8, 1.5)
                logger.warning(
                    f"Streaming '{model_id}' retry (attempt {attempt + 1}) "
                    f"after {delay:.1f}s: {last_error}"
                )
                await asyncio.sleep(delay)

            try:
                # v2.8.8: Acquire semaphore only for the .create() call (connection setup),
                # then release it before iterating the stream. This prevents the slot from
                # being held for the entire token delivery duration (5-30s), which would
                # block all other concurrent requests from this model.
                _ctx = sem if sem else _NullAsyncContext()
                async with _ctx:
                    stream = await client.chat.completions.create(**create_kwargs)
                # Semaphore released here — iterate stream without holding the slot.
                # v2.9: Check elapsed time each token to enforce a hard deadline.
                # This prevents indefinite hangs when upstream sends very slowly.
                async for chunk in stream:  # type: ignore[union-attr]
                    delta = chunk.choices[0].delta
                    if delta.content:
                        yielded_any = True
                        _stream_chars += len(delta.content)
                        yield delta.content
                    if _stream_hard_timeout and time.monotonic() - _stream_start > _stream_hard_timeout:
                        logger.warning(
                            f"Streaming '{model_id}' hit hard deadline "
                            f"({_stream_hard_timeout:.0f}s), truncating"
                        )
                        break
                # v2.8.8: Record streaming cost after successful completion.
                # Streaming API does not return usage; estimate: chars/4 ≈ tokens (English).
                # Chinese chars ≈ 1.5 tokens, mixed content roughly balances to chars/4.
                # Prompt tokens are not tracked here (no system prompt length available);
                # completion cost dominates for Judge streaming (long outputs).
                if _stream_chars > 0:
                    _est_c_tokens = max(1, _stream_chars // 4)
                    _stream_cost_usd = _est_c_tokens * mc.cost_per_1m_output / 1_000_000
                    _st = self._cost_tracker_var.get([])
                    _st.append((model_id, 0, _est_c_tokens, _stream_cost_usd))
                    self._cost_tracker_var.set(_st)
                    logger.debug(
                        f"Streaming '{model_id}' cost estimate: "
                        f"{_stream_chars} chars ≈ {_est_c_tokens} tokens, "
                        f"¥{_stream_cost_usd * 7.2:.5f} (estimated)"
                    )
                return  # success — stop retry loop
            except Exception as e:
                last_error = e
                if not self._is_retryable(e):
                    # v2.9: Key quarantine + rotation for auth/quota errors on multi-key models.
                    if (
                        len(_all_clients) > 1
                        and self._is_key_specific_error(e)
                        and not yielded_any
                    ):
                        self._mark_key_degraded(model_id, client)
                        _fresh_s = [c for c in self._active_clients(model_id) if c is not client]
                        if _fresh_s:
                            client = random.choice(_fresh_s)
                        logger.warning(
                            f"Streaming '{model_id}' key-specific error, "
                            f"key quarantined + rotated: {type(e).__name__}"
                        )
                        continue  # key rotation happens at top of loop
                    break
                # timeout → no retry for streaming (same policy as judge role in call())
                if self._is_timeout(e):
                    break

        if last_error:
            logger.error(f"Streaming failed for '{model_id}' after {attempt + 1} attempt(s): {last_error}")
            raise last_error

    @property
    def available_models(self) -> list[str]:
        """List model IDs with initialized clients."""
        return list(self._clients.keys())

    @property
    def startup_skipped_models(self) -> list[dict[str, Any]]:
        """Structured startup skips for health/deploy/reporting."""
        records: list[dict[str, Any]] = []
        for record in self._startup_skipped_models:
            cloned = dict(record)
            if "configured_envs" in cloned:
                cloned["configured_envs"] = list(cloned["configured_envs"])
            records.append(cloned)
        return records
