"""
Companion Dispatcher — the single routing and guidance brain for answer flows.

Responsibilities injected into Orchestrator.execute():
  1. dispatch_route() — pre-pipeline routing, mode/model/strategy selection
  2. dispatch_guide() — post-answer guidance generation

Its route output also powers the user-facing companion/waiting-state messaging
shown before and during execution. No separate NSG or CompanionHint layer
produces guidance anymore.

Model: Claude Sonnet 4.6 (non-streaming, ≤200 tokens output)
Fallback: deterministic in-process fallback on Sonnet failure
Timeout: 5s per call
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from agoracle.domain.types import (
    Mode,
    QuestionType,
    QualityGateResult,
    RoleCall,
    Role,
)

if TYPE_CHECKING:
    from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter
    from agoracle.config.schema import AppConfig
    from agoracle.services.failure_monitor import FailureMonitor

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────

DISPATCHER_MODEL_ID = "claude_sonnet"
QUALITY_CHECK_MODEL_ID = "gemini_3_flash"
DISPATCHER_TIMEOUT_S = 10
QUALITY_CHECK_TIMEOUT_S = 3
QUALITY_CHECK_MAX_TOKENS = 200

# ── Single-model recommendation pool ────────────────────────
# Order matters: index 0 = primary recommendation, 1 = first fallback, etc.

SINGLE_MODEL_RECOMMENDATIONS: dict[str, list[str]] = {
    "coding":    ["gpt53_codex", "claude_opus_thinking", "deepseek_reasoner"],
    "math":      ["deepseek_reasoner", "gemini_31_pro_thinking", "claude_opus_thinking"],
    "writing":   ["claude_opus_thinking", "claude_sonnet", "gpt52"],
    "creative":  ["claude_opus_thinking", "kimi", "gpt52_all"],
    "factual":   ["perplexity_sonar", "gemini_3_flash", "kimi"],
    "reasoning": ["deepseek_reasoner", "claude_opus_thinking", "gemini_31_pro_thinking"],
    "realtime":  ["perplexity_sonar_pro", "perplexity_sonar", "kimi"],  # news/volatile queries
}

_SINGLE_MODEL_PROMPT_ORDER: tuple[str, ...] = (
    "coding",
    "math",
    "writing",
    "creative",
    "factual",
    "reasoning",
    "realtime",
)

_SINGLE_MODEL_PROMPT_NOTES: dict[str, str] = {
    "realtime": "（新闻/价格/实时数据必须优先搜索模型）",
}


def _single_model_mapping_prompt_lines() -> str:
    """Render route prompt mapping from SINGLE_MODEL_RECOMMENDATIONS to avoid drift."""
    lines: list[str] = []
    for q_type in _SINGLE_MODEL_PROMPT_ORDER:
        models = SINGLE_MODEL_RECOMMENDATIONS.get(q_type, [])
        if not models:
            continue
        suffix = _SINGLE_MODEL_PROMPT_NOTES.get(q_type, "")
        lines.append(f"{q_type} → {', '.join(models)}{suffix}")
    return "\n".join(lines)


# ── Dynamic ETA per question type (seconds) ─────────────────
# p50 = median response time, p90 = 90th percentile (used as display ETA)

SINGLE_MODEL_ETA: dict[str, dict[str, int]] = {
    "coding_simple":   {"p50": 15, "p90": 30},
    "coding_medium":   {"p50": 30, "p90": 45},
    "coding_complex":  {"p50": 45, "p90": 60},
    "math":            {"p50": 20, "p90": 40},
    "writing":         {"p50": 20, "p90": 45},
    "creative":        {"p50": 15, "p90": 30},
    "factual":         {"p50": 10, "p90": 20},
    "reasoning":       {"p50": 30, "p90": 60},
    "realtime":        {"p50": 8,  "p90": 15},
}

# ── Capability labels for "more" fold (model_id -> label) ───

CAPABILITY_LABELS: dict[str, dict[str, str]] = {
    "deepseek_reasoner":      {"label": "🧠 最强推理", "model_label": "由 DeepSeek 驱动"},
    "claude_opus_thinking":   {"label": "🧠 深度分析", "model_label": "由 Opus 驱动"},
    "gpt53_codex":            {"label": "💻 代码专家", "model_label": "由 Codex 驱动"},
    "kimi":                   {"label": "🔍 联网搜索+分析", "model_label": "由 Kimi 驱动"},
    "perplexity_sonar":       {"label": "🔍 实时搜索", "model_label": "由 Perplexity 驱动"},
    "gemini_3_flash":         {"label": "⚡ 最快响应", "model_label": "由 Flash 驱动"},
    "gemini_31_pro_thinking": {"label": "🧠 推理分析", "model_label": "由 Gemini Pro 驱动"},
    "gpt52":                  {"label": "✍️ 通用写作", "model_label": "由 GPT-5.2 驱动"},
    "gpt52_all":              {"label": "✍️ 创意写作", "model_label": "由 GPT-5.2 All 驱动"},
    "claude_sonnet":          {"label": "✍️ 精细写作", "model_label": "由 Sonnet 驱动"},
}


# ============================================================
# Data Classes
# ============================================================

@dataclass
class DispatcherInput:
    """Input signal for Dispatcher routing/guidance."""
    question: str = ""
    question_type: str = QuestionType.UNKNOWN.value
    session_summary: str = ""
    model_health: dict[str, str] = field(default_factory=dict)
    previous_result_meta: dict[str, Any] = field(default_factory=dict)
    user_verbosity: str = "normal"
    was_auto_escalated: bool = False
    user_usage_count: int = 0
    user_preferences: dict[str, Any] = field(default_factory=dict)


@dataclass
class DispatcherOutput:
    """Output of Dispatcher routing/guidance."""
    # === Route decision ===
    strategy: str = "pipeline"          # pipeline | single_model | clarify | done
    mode: str = "deep"                  # light | deep | research (when strategy=pipeline)
    single_model_id: str | None = None  # when strategy=single_model
    skip_judge: bool = False
    contributors_override: list[str] | None = None

    # === User-visible ===
    companion_message: str = ""         # ≤2 sentences (empty = silent route)
    route_reason: str = ""              # 1-line reason for the routing decision
    suggested_actions: list[dict[str, Any]] = field(default_factory=list)
    is_silent_route: bool = False       # True = don't show Companion bubble

    # === Post-guide trigger ===
    post_guide_trigger: str = "fold"    # fold | divergence | low_confidence

    # === Meta ===
    dispatcher_confidence: float = 0.8
    fallback_hint: str = ""
    show_model_hint: bool = False       # show "you can specify a model" reminder

    # === Quality check (single_model only) ===
    quality_confidence: float = -1.0    # -1 = not checked; 0-1 from Flash

    # === Estimated time ===
    estimated_seconds: int = 0          # displayed on buttons


@dataclass
class DispatcherLog:
    """Per-query Dispatcher decision log."""
    query_id: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    question_snippet: str = ""
    question_type: str = ""
    dispatcher_strategy: str = ""
    dispatcher_mode: str = ""
    dispatcher_model_id: str | None = None
    dispatcher_confidence: float = 0.0
    route_reason: str = ""
    user_actual_choice: str = ""
    user_override: bool = False
    was_auto_escalated: bool = False
    final_confidence: float = 0.0
    dispatcher_latency_ms: int = 0
    post_guide_triggered: bool = False
    is_fallback: bool = False


# ============================================================
# Companion Dispatcher
# ============================================================

class CompanionDispatcher:
    """
    Unified routing + guidance layer.

    Injected into Orchestrator — NOT an independent service.
    Two entry points: dispatch_route() and dispatch_guide().
    """

    def __init__(
        self,
        config: "AppConfig",
        model_adapter: "OpenAIModelAdapter",
        failure_monitor: "FailureMonitor | None" = None,
    ) -> None:
        self._config = config
        self._adapter = model_adapter
        self._failure_monitor = failure_monitor

    # ────────────────────────────────────────────────────────
    # Public API: Pre-pipeline routing
    # ────────────────────────────────────────────────────────

    async def dispatch_route(
        self,
        dispatcher_input: DispatcherInput,
    ) -> DispatcherOutput:
        """
        Pre-pipeline routing decision (Smart Path only).

        Called at the start of Orchestrator.execute() when mode=Auto.
        Fast Path (user chose Deep/Research) skips this entirely.

        Returns DispatcherOutput with strategy/mode/model decision.
        Falls back to deterministic local routing on any failure.
        """
        start = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self._call_route_sonnet(dispatcher_input),
                timeout=DISPATCHER_TIMEOUT_S,
            )
            result_latency = int((time.monotonic() - start) * 1000)
            logger.info(
                f"[Dispatcher] Route OK in {result_latency}ms: "
                f"question_type={dispatcher_input.question_type} "
                f"strategy={result.strategy} mode={result.mode} "
                f"model={result.single_model_id or 'pipeline'} "
                f"confidence={result.dispatcher_confidence:.2f} "
                f"silent={result.is_silent_route}"
            )
            return result
        except asyncio.TimeoutError:
            elapsed = int((time.monotonic() - start) * 1000)
            fallback = self._fallback_route(dispatcher_input)
            fallback_semantics = fallback.route_reason or (
                "single_model_fallback"
                if fallback.strategy == "single_model"
                else f"pipeline:{fallback.mode}"
            )
            logger.warning(
                f"[Dispatcher] Route TIMEOUT after {elapsed}ms "
                f"question_type={dispatcher_input.question_type} "
                f"→ fallback={fallback_semantics} strategy={fallback.strategy} "
                f"model={fallback.single_model_id or 'pipeline'}"
            )
            return fallback
        except Exception as e:
            elapsed = int((time.monotonic() - start) * 1000)
            fallback = self._fallback_route(dispatcher_input)
            fallback_semantics = fallback.route_reason or (
                "single_model_fallback"
                if fallback.strategy == "single_model"
                else f"pipeline:{fallback.mode}"
            )
            logger.warning(
                f"[Dispatcher] Route ERROR after {elapsed}ms: {e} "
                f"question_type={dispatcher_input.question_type} "
                f"→ fallback={fallback_semantics} strategy={fallback.strategy} "
                f"model={fallback.single_model_id or 'pipeline'}"
            )
            return fallback

    # ────────────────────────────────────────────────────────
    # Public API: Post-pipeline guidance
    # ────────────────────────────────────────────────────────

    async def dispatch_guide(
        self,
        result_meta: dict[str, Any],
        dispatcher_input: DispatcherInput,
    ) -> DispatcherOutput:
        """
        Post-pipeline guidance (both paths).

        Only calls Sonnet for high-value scenarios:
          - divergence_count >= 2
          - confidence < 0.5
        Otherwise returns a folded (collapsed) guidance with no LLM call.
        """
        confidence = result_meta.get("confidence", 0.0)
        divergence_count = result_meta.get("divergence_count", 0)
        quality_gate = str(
            result_meta.get("quality_gate_result", QualityGateResult.SYNTHESIZED.value)
        ).strip().lower()
        fast_path = bool(result_meta.get("fast_path"))

        # High-value scenario: call Sonnet
        if divergence_count >= 2 or confidence < 0.5:
            trigger = "divergence" if divergence_count >= 2 else "low_confidence"
            start = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    self._call_guide_sonnet(result_meta, dispatcher_input),
                    timeout=DISPATCHER_TIMEOUT_S,
                )
                result.post_guide_trigger = trigger
                elapsed = int((time.monotonic() - start) * 1000)
                logger.info(
                    f"[Dispatcher] Guide OK ({trigger}) in {elapsed}ms"
                )
                return result
            except Exception as e:
                elapsed = int((time.monotonic() - start) * 1000)
                logger.warning(
                    f"[Dispatcher] Guide {type(e).__name__} after {elapsed}ms, "
                    f"falling back"
                )
                return self._fallback_guide(result_meta, trigger)

        # B-6: Fast Path two-step enhancement — fold scenario but try Sonnet for richer guidance.
        # Uses a shorter timeout (5s) so it doesn't noticeably delay the response.
        # On success: returns enriched guidance with "fold" trigger (still collapsible, but has content).
        # On failure/timeout: falls back to a deterministic folded guidance with one Deep action.
        _fast_path = (
            quality_gate in (QualityGateResult.BEST_SINGLE.value, "fast_path")
            or fast_path
        )
        if _fast_path:
            start = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    self._call_guide_sonnet(result_meta, dispatcher_input),
                    timeout=5.0,  # shorter timeout for fast-path enhancement
                )
                result.post_guide_trigger = "fold"  # still foldable, but now has content
                elapsed = int((time.monotonic() - start) * 1000)
                logger.info(
                    f"[Dispatcher] Guide fast-path enhance OK in {elapsed}ms"
                )
                return result
            except Exception as e:
                elapsed = int((time.monotonic() - start) * 1000)
                logger.warning(
                    f"[Dispatcher] Guide fast-path enhance {type(e).__name__} after {elapsed}ms, "
                    f"using deterministic folded guidance"
                )
                return self._fallback_guide(result_meta, "fold")

        # Normal fold: no LLM call, no content
        # is_silent_route=False so the fold placeholder bar renders in CompanionBubble
        return DispatcherOutput(
            strategy="done",
            post_guide_trigger="fold",
            companion_message="",
            is_silent_route=False,
        )

    # ────────────────────────────────────────────────────────
    # Public API: Single-model quality check (Flash)
    # ────────────────────────────────────────────────────────

    async def quality_check(
        self,
        question: str,
        answer: str,
        model_id: str,
    ) -> float:
        """
        Lightweight quality check using Flash after single-model calls.

        Returns confidence 0.0-1.0.
        On failure, returns 0.75 (assume OK, don't block user).
        """
        prompt = (
            "你是一个回答质量评估器。评估以下回答的质量。\n\n"
            f"问题：{question[:500]}\n\n"
            f"回答（来自 {model_id}）：{answer[:2000]}\n\n"
            "请只输出一个 0 到 1 的数字，表示回答质量的信心分数：\n"
            "- 1.0 = 高质量、准确、完整\n"
            "- 0.5 = 一般，可能有遗漏或不准确\n"
            "- 0.0 = 低质量、错误、不相关\n\n"
            "只输出数字，不要任何解释。"
        )
        try:
            role_call = RoleCall(
                call_id=f"qc_{int(time.time())}",
                model_id=QUALITY_CHECK_MODEL_ID,
                role=Role.METADATA_EXTRACTOR,
                system_prompt="You are a quality evaluator. Output only a number 0-1.",
                messages=[{"role": "user", "content": prompt}],
                timeout_seconds=QUALITY_CHECK_TIMEOUT_S,
            )
            response = await asyncio.wait_for(
                self._adapter.call(role_call),
                timeout=QUALITY_CHECK_TIMEOUT_S + 1,
            )
            if response.success and response.content.strip():
                score = float(response.content.strip().split()[0])
                return max(0.0, min(1.0, score))
        except (asyncio.TimeoutError, ValueError, Exception) as e:
            logger.warning(f"[Dispatcher] Quality check failed: {e}")
        return 0.75  # default: assume OK

    # ────────────────────────────────────────────────────────
    # Public API: Get next fallback model
    # ────────────────────────────────────────────────────────

    def get_fallback_model(
        self,
        question_type: str,
        failed_model_id: str,
    ) -> str | None:
        """
        Get the next model from SINGLE_MODEL_RECOMMENDATIONS
        after a timeout/failure, skipping degraded models.

        Returns model_id or None if no alternatives available.
        """
        q_type = self._normalize_question_type(question_type)
        candidates = SINGLE_MODEL_RECOMMENDATIONS.get(q_type, [])
        for candidate in candidates:
            if candidate == failed_model_id:
                continue
            if self._failure_monitor and self._failure_monitor.is_degraded(candidate):
                continue
            if self._adapter.supports_model(candidate):
                return candidate
        return None

    # ────────────────────────────────────────────────────────
    # Public API: Build suggested actions for frontend
    # ────────────────────────────────────────────────────────

    def build_route_actions(
        self,
        output: DispatcherOutput,
        question_type: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """
        Build CompanionAction list from DispatcherOutput for frontend rendering.

        Returns ≤2 main actions + optional more_actions for the fold.
        """
        actions: list[dict[str, Any]] = []

        if output.strategy == "single_model" and output.single_model_id:
            eta = self._get_eta(question_type)
            cap = CAPABILITY_LABELS.get(output.single_model_id, {})
            actions.append({
                "label": f"⚡ {cap.get('label', output.single_model_id)} ~{eta}s",
                "capability_label": cap.get("label", ""),
                "model_label": cap.get("model_label", ""),
                "action_type": "query_single",
                "action_payload": {
                    "model_id": output.single_model_id,
                    "question_type": question_type,
                },
                "estimated_seconds": eta,
                "requires_confirm": False,
            })
            # Add Deep as second option
            actions.append({
                "label": "🔬 Deep 多专家 ~2min",
                "action_type": "query_deep",
                "action_payload": {"mode": "deep"},
                "estimated_seconds": 120,
                "requires_confirm": False,
            })

        elif output.strategy == "pipeline" and output.mode == "deep":
            actions.append({
                "label": "🔬 Deep 多专家 ~2min",
                "action_type": "query_deep",
                "action_payload": {"mode": "deep"},
                "estimated_seconds": 120,
                "requires_confirm": False,
            })

        # Build "more" fold: alternative single models
        more_actions = self._build_more_actions(question_type, output.single_model_id)

        return actions, more_actions

    # ────────────────────────────────────────────────────────
    # Internal: Sonnet calls
    # ────────────────────────────────────────────────────────

    async def _call_route_sonnet(
        self,
        dispatcher_input: DispatcherInput,
    ) -> DispatcherOutput:
        """Call Sonnet for pre-pipeline routing decision."""
        system_prompt = self._build_route_system_prompt(dispatcher_input)
        user_content = (
            f"用户问题：{dispatcher_input.question}\n"
            f"题型分类：{dispatcher_input.question_type}\n"
        )
        if dispatcher_input.session_summary:
            user_content += f"会话摘要：{dispatcher_input.session_summary[:500]}\n"
        if dispatcher_input.previous_result_meta:
            prev = dispatcher_input.previous_result_meta
            user_content += (
                f"上一轮：confidence={prev.get('confidence', '?')}, "
                f"mode={prev.get('mode', '?')}, "
                f"best_model={prev.get('best_model', '?')}\n"
            )

        role_call = RoleCall(
            call_id=f"disp_route_{int(time.time())}",
            model_id=DISPATCHER_MODEL_ID,
            role=Role.METADATA_EXTRACTOR,  # reuse existing role, lightweight call
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_content}],
            timeout_seconds=DISPATCHER_TIMEOUT_S,
        )

        response = await self._adapter.call(role_call)
        if not response.success:
            raise RuntimeError(f"Sonnet route call failed: {response.error}")

        return self._parse_route_response(response.content, dispatcher_input)

    async def _call_guide_sonnet(
        self,
        result_meta: dict[str, Any],
        dispatcher_input: DispatcherInput,
    ) -> DispatcherOutput:
        """Call Sonnet for post-pipeline guidance (high-value scenarios only)."""
        system_prompt = (
            "你是 Synthora 的智能助手。根据以下查询结果元数据，"
            "生成一条简短的中文引导建议（≤2句话）。\n\n"
            "如果有明显分歧，指出分歧点并建议深入。\n"
            "如果信心低，建议用更强的模式重新分析。\n"
            "回复必须且只能是一个 JSON 对象。\n"
            "不允许输出任何解释、前后文、Markdown 代码块或额外文字。\n"
            "合法示例：{\"message\":\"专家意见有分歧，建议继续深入比较。\",\"action_type\":\"explore_divergence\"}\n"
            "输出格式：{\"message\": \"...\", \"action_type\": \"...\"}\n"
            "action_type 可选：explore_divergence, query_deep, query_followup, done"
        )
        user_content = (
            f"原始问题：{dispatcher_input.question[:300]}\n"
            f"confidence: {result_meta.get('confidence', 0)}\n"
            f"divergence_count: {result_meta.get('divergence_count', 0)}\n"
            f"quality_gate: {result_meta.get('quality_gate_result', 'unknown')}\n"
            f"key_insights: {result_meta.get('key_insights', [])[:3]}\n"
        )
        if dispatcher_input.was_auto_escalated:
            user_content += "注意：本轮经历了 AUTO_ESCALATE (Light→Deep)。\n"

        role_call = RoleCall(
            call_id=f"disp_guide_{int(time.time())}",
            model_id=DISPATCHER_MODEL_ID,
            role=Role.METADATA_EXTRACTOR,
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_content}],
            timeout_seconds=DISPATCHER_TIMEOUT_S,
        )

        response = await self._adapter.call(role_call)
        if not response.success:
            raise RuntimeError(f"Sonnet guide call failed: {response.error}")

        return self._parse_guide_response(response.content, result_meta)

    # ────────────────────────────────────────────────────────
    # Internal: Prompt building
    # ────────────────────────────────────────────────────────

    def _build_route_system_prompt(self, dispatcher_input: DispatcherInput) -> str:
        """Build the system prompt for Sonnet routing call."""
        # Collect degraded models
        degraded = []
        if self._failure_monitor:
            for model_id in self._config.models:
                if self._failure_monitor.is_degraded(model_id):
                    degraded.append(model_id)

        degraded_str = ", ".join(degraded) if degraded else "无"

        # Build model capabilities summary from config
        model_caps = []
        for mid, mc in self._config.models.items():
            if mid in degraded:
                continue
            model_caps.append(
                f"  {mid}: {mc.name} (timeout={mc.timeout_seconds}s)"
            )
        model_caps_str = "\n".join(model_caps[:15])  # cap at 15 to limit prompt size
        realtime_candidates = SINGLE_MODEL_RECOMMENDATIONS.get("realtime", [])
        realtime_ordered = (
            ", ".join(realtime_candidates)
            if realtime_candidates
            else "perplexity_sonar_pro, perplexity_sonar, kimi"
        )
        single_model_mapping_lines = _single_model_mapping_prompt_lines()

        return f"""你是 Synthora 的智能助手和路由调度器。

## 策略选择
- pipeline: 走多模型聚合管道（可选 light/deep/research）
- single_model: 直接让一个最合适的模型回答
- clarify: 问题不清楚，需要先问用户（最多 1 轮）

## 可用模型
{model_caps_str}

## 当前不可用模型
{degraded_str}

## 单模型推荐映射
{single_model_mapping_lines}

## 决策规则
1. 简单事实问题 → pipeline:light
2. 编程/数学/写作 → 优先 single_model（聚合对这些题型价值低）
3. 分析/技术/推理 → pipeline:deep
4. 仅当用户明确要求“深度研究报告”且不属于纯实时问询时，才考虑 pipeline:research
5. 新闻/价格/今日动态（realtime 类型） → 必须 single_model 搜索模型（优先级 {realtime_ordered}）
6. 问题模糊/缺关键信息 → clarify（只问 1 轮）
7. 用户自然语言指定了模型（如"让 Opus 回答"、"用 DeepSeek 分析"） → single_model

## 静默规则
- confidence ≥0.9 且策略是 pipeline:light → is_silent_route=true
- 追问（有 previous_result_meta 且同题型同策略）→ is_silent_route=true

## route_reason 格式
必须输出，1 句话。格式："因为：[题型特征] + [上下文] → [选择理由]"

## 指定模型提醒
- user_usage_count < 5 → show_model_hint=true
- 否则 show_model_hint=false

## 输出格式（严格 JSON，不要任何解释）
{{
  "strategy": "pipeline|single_model|clarify",
  "mode": "light|deep|research",
  "single_model_id": null,
  "companion_message": "",
  "route_reason": "因为：...",
  "is_silent_route": false,
  "dispatcher_confidence": 0.8,
  "show_model_hint": false,
  "clarify_message": ""
}}"""

    # ────────────────────────────────────────────────────────
    # Internal: Response parsing
    # ────────────────────────────────────────────────────────

    def _parse_route_response(
        self,
        content: str,
        dispatcher_input: DispatcherInput,
    ) -> DispatcherOutput:
        """Parse Sonnet's JSON route response into DispatcherOutput."""
        try:
            # Strip markdown code fences if present
            cleaned = content.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                # Remove first and last lines (``` markers)
                lines = [l for l in lines if not l.strip().startswith("```")]
                cleaned = "\n".join(lines)

            data = json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning(f"[Dispatcher] JSON parse failed, content: {content[:200]}")
            return self._fallback_route(dispatcher_input)

        strategy = data.get("strategy", "pipeline")
        mode = data.get("mode", "deep")
        single_model_id = data.get("single_model_id")
        confidence = float(data.get("dispatcher_confidence", 0.8))
        is_silent = bool(data.get("is_silent_route", False))
        q_type = self._normalize_question_type(dispatcher_input.question_type)

        # Validate single_model_id exists and is available
        if strategy == "single_model" and single_model_id:
            if not self._adapter.supports_model(single_model_id):
                alt = self.get_fallback_model(q_type, single_model_id)
                if alt:
                    logger.info(
                        f"[Dispatcher] {single_model_id} unsupported, "
                        f"switching to fallback single_model={alt}"
                    )
                    single_model_id = alt
                else:
                    fallback = self._fallback_route(dispatcher_input)
                    logger.warning(
                        f"[Dispatcher] {single_model_id} unsupported, "
                        f"using rule fallback strategy={fallback.strategy} "
                        f"model={fallback.single_model_id or 'pipeline'} "
                        f"reason={fallback.route_reason}"
                    )
                    return fallback
            elif self._failure_monitor and self._failure_monitor.is_degraded(single_model_id):
                # Try next in recommendation list
                alt = self.get_fallback_model(q_type, single_model_id)
                if alt:
                    logger.info(
                        f"[Dispatcher] {single_model_id} degraded, "
                        f"switching to {alt}"
                    )
                    single_model_id = alt
                else:
                    fallback = self._fallback_route(dispatcher_input)
                    logger.warning(
                        f"[Dispatcher] {single_model_id} degraded and no alt model, "
                        f"using rule fallback strategy={fallback.strategy} "
                        f"model={fallback.single_model_id or 'pipeline'} "
                        f"reason={fallback.route_reason}"
                    )
                    return fallback

        # Handle clarify strategy
        companion_message = data.get("companion_message", "")
        if strategy == "clarify":
            companion_message = data.get("clarify_message", "") or companion_message

        route_reason = data.get("route_reason", "")
        # Realtime must be described as a search single-model route.
        if strategy == "single_model" and q_type == "realtime":
            if (
                not route_reason
                or "deep" in route_reason.lower()
                or "pipeline" in route_reason.lower()
                or "聚合" in route_reason
                or "管道" in route_reason
            ):
                route_reason = "因为：single_model_realtime（realtime）→ 搜索模型直答"

        # Estimate time
        estimated_seconds = self._get_eta(q_type) if strategy == "single_model" else 0

        return DispatcherOutput(
            strategy=strategy,
            mode=mode,
            single_model_id=single_model_id,
            skip_judge=(strategy == "single_model"),
            contributors_override=[single_model_id] if single_model_id else None,
            companion_message=companion_message,
            route_reason=route_reason,
            is_silent_route=is_silent,
            dispatcher_confidence=confidence,
            show_model_hint=bool(data.get("show_model_hint", False)),
            estimated_seconds=estimated_seconds,
        )

    def _parse_guide_response(
        self,
        content: str,
        result_meta: dict[str, Any],
    ) -> DispatcherOutput:
        """Parse Sonnet's JSON guide response into DispatcherOutput."""
        cleaned = content.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            cleaned = "\n".join(lines).strip()

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            json_start = cleaned.find("{")
            json_end = cleaned.rfind("}")
            if json_start != -1 and json_end != -1 and json_start < json_end:
                try:
                    data = json.loads(cleaned[json_start:json_end + 1])
                except json.JSONDecodeError:
                    logger.warning(f"[Dispatcher] Guide JSON parse failed: {content[:200]}")
                    return self._raw_text_guide(content)
            else:
                logger.warning(f"[Dispatcher] Guide JSON parse failed: {content[:200]}")
                return self._raw_text_guide(content)

        if not isinstance(data, dict):
            logger.warning(f"[Dispatcher] Guide JSON parse returned non-object: {content[:200]}")
            return self._raw_text_guide(content)

        action_type = data.get("action_type", "done")
        message = data.get("message", "")

        actions = []
        if action_type == "explore_divergence":
            actions.append({
                "label": "🔍 深入分歧点",
                "action_type": "explore_divergence",
                "action_payload": {"mode": "deep"},
                "estimated_seconds": 120,
                "requires_confirm": False,
            })
        elif action_type == "query_deep":
            actions.append({
                "label": "🔬 Deep 重新分析",
                "action_type": "query_deep",
                "action_payload": {"mode": "deep"},
                "estimated_seconds": 120,
                "requires_confirm": False,
            })

        return DispatcherOutput(
            strategy="done",
            companion_message=message,
            suggested_actions=actions,
            is_silent_route=False,
        )

    @staticmethod
    def _raw_text_guide(content: str) -> DispatcherOutput:
        """Preserve raw guide text instead of dropping it when JSON is invalid."""
        return DispatcherOutput(
            strategy="done",
            post_guide_trigger="fold",
            companion_message=content.strip()[:500],
            suggested_actions=[{
                "label": "🔬 用 Deep 重新分析",
                "action_type": "query_deep",
                "action_payload": {"mode": "deep"},
                "estimated_seconds": 120,
                "requires_confirm": False,
            }],
            is_silent_route=False,
        )

    # ────────────────────────────────────────────────────────
    # Internal: deterministic fallbacks (0ms, 0 cost)
    # ────────────────────────────────────────────────────────

    def _fallback_route(self, dispatcher_input: DispatcherInput) -> DispatcherOutput:
        """
        Deterministic routing fallback when Sonnet fails.

        Simple heuristic based on question_type.
        Always falls back to Deep (safe default).
        """
        q_type = self._normalize_question_type(dispatcher_input.question_type)

        # Single-model candidates for specific question types
        # realtime: news/volatile queries — always route to search-enabled model, never deep
        single_types = {"coding", "math", "writing", "realtime"}
        if q_type in single_types:
            candidates = SINGLE_MODEL_RECOMMENDATIONS.get(q_type, [])
            for candidate in candidates:
                if self._adapter.supports_model(candidate):
                    if not (self._failure_monitor and self._failure_monitor.is_degraded(candidate)):
                        eta = self._get_eta(q_type)
                        route_reason = (
                            "因为：single_model_realtime_fallback（realtime）→ 搜索模型直答"
                            if q_type == "realtime"
                            else f"因为：single_model_fallback（{q_type}）→ light 单模型直答（skip_judge）"
                        )
                        return DispatcherOutput(
                            strategy="single_model",
                            mode="light",
                            single_model_id=candidate,
                            skip_judge=True,
                            contributors_override=[candidate],
                            companion_message="",
                            route_reason=route_reason,
                            is_silent_route=False,
                            dispatcher_confidence=0.6,
                            estimated_seconds=eta,
                        )

        # Default: Deep pipeline
        return DispatcherOutput(
            strategy="pipeline",
            mode="deep",
            companion_message="",
            route_reason="因为：pipeline_fallback（deep）→ Deep 管道",
            is_silent_route=False,
            dispatcher_confidence=0.5,
            fallback_hint="dispatcher_timeout",
        )

    def _fallback_guide(
        self,
        result_meta: dict[str, Any],
        trigger: str,
    ) -> DispatcherOutput:
        """
        Deterministic guidance fallback when Sonnet fails.

        Generates a simple local message matching the trigger type.
        UI stays the same bubble form (user can't tell it's a fallback).
        """
        confidence = result_meta.get("confidence", 0.0)
        divergence_count = result_meta.get("divergence_count", 0)

        if trigger == "divergence":
            message = f"专家在 {divergence_count} 个观点上存在分歧，可以深入了解。"
            actions = [{
                "label": "🔍 深入分歧点",
                "action_type": "explore_divergence",
                "action_payload": {"mode": "deep"},
                "estimated_seconds": 120,
                "requires_confirm": False,
            }]
        elif trigger == "low_confidence":
            message = "回答信心不高，建议用深度模式重新分析。"
            actions = [{
                "label": "🔬 Deep 重新分析",
                "action_type": "query_deep",
                "action_payload": {"mode": "deep"},
                "estimated_seconds": 120,
                "requires_confirm": False,
            }]
        elif trigger == "fold":
            message = (
                "当前回答已完成快速直采；如果你想看更完整的分析过程，可以用 Deep 继续展开。"
                if (
                    str(result_meta.get("quality_gate_result", "")).strip().lower()
                    == QualityGateResult.BEST_SINGLE.value
                    or bool(result_meta.get("fast_path"))
                )
                else "如果你想把这轮答案展开得更完整，可以用 Deep 继续分析。"
            )
            actions = [{
                "label": "🔬 用 Deep 重新分析",
                "action_type": "query_deep",
                "action_payload": {"mode": "deep"},
                "estimated_seconds": 120,
                "requires_confirm": False,
            }]
        else:
            message = ""
            actions = []

        return DispatcherOutput(
            strategy="done",
            companion_message=message,
            suggested_actions=actions,
            post_guide_trigger=trigger,
            is_silent_route=False,
            fallback_hint="guide_fallback",
        )

    # ────────────────────────────────────────────────────────
    # Internal: Helpers
    # ────────────────────────────────────────────────────────

    def _normalize_question_type(self, question_type: str) -> str:
        """Normalize question_type to match SINGLE_MODEL_RECOMMENDATIONS keys."""
        mapping = {
            "coding": "coding",
            "math": "math",
            "writing": "writing",
            "creative": "creative",
            "factual": "factual",
            "reasoning": "reasoning",
            "technical": "reasoning",
            "analytical": "reasoning",
            "controversial": "reasoning",
            "cultural": "factual",
            "meta_cognition": "reasoning",
            "unknown": "reasoning",
            # Realtime/news queries: route to search-enabled single model, NOT deep pipeline
            "realtime": "realtime",
            "volatile": "realtime",
            "news": "realtime",
        }
        return mapping.get(question_type, "reasoning")

    def _get_eta(self, question_type: str) -> int:
        """Get estimated seconds (p90) for a question type."""
        # Try exact match first
        if question_type in SINGLE_MODEL_ETA:
            return SINGLE_MODEL_ETA[question_type]["p90"]
        # Try with _medium suffix for coding
        medium_key = f"{question_type}_medium"
        if medium_key in SINGLE_MODEL_ETA:
            return SINGLE_MODEL_ETA[medium_key]["p90"]
        # Default
        return 45

    def _build_more_actions(
        self,
        question_type: str,
        excluded_model_id: str | None,
    ) -> list[dict[str, Any]]:
        """Build the 'more' fold actions — alternative models with capability labels."""
        q_type = self._normalize_question_type(question_type)
        candidates = SINGLE_MODEL_RECOMMENDATIONS.get(q_type, [])
        more = []

        for mid in candidates:
            if mid == excluded_model_id:
                continue
            if not self._adapter.supports_model(mid):
                continue
            if self._failure_monitor and self._failure_monitor.is_degraded(mid):
                continue
            cap = CAPABILITY_LABELS.get(mid, {})
            eta = self._get_eta(q_type)
            more.append({
                "label": cap.get("label", mid),
                "capability_label": cap.get("label", ""),
                "model_label": cap.get("model_label", f"由 {mid} 驱动"),
                "action_type": "query_single",
                "action_payload": {"model_id": mid},
                "estimated_seconds": eta,
                "requires_confirm": False,
            })

        return more

    # ────────────────────────────────────────────────────────
    # Public: Logging helper
    # ────────────────────────────────────────────────────────

    @staticmethod
    def create_log(
        query_id: str,
        dispatcher_input: DispatcherInput,
        output: DispatcherOutput,
        latency_ms: int,
        is_fallback: bool = False,
    ) -> DispatcherLog:
        """Create a DispatcherLog entry for monitoring."""
        return DispatcherLog(
            query_id=query_id,
            question_snippet=dispatcher_input.question[:100],
            question_type=dispatcher_input.question_type,
            dispatcher_strategy=output.strategy,
            dispatcher_mode=output.mode,
            dispatcher_model_id=output.single_model_id,
            dispatcher_confidence=output.dispatcher_confidence,
            route_reason=output.route_reason,
            was_auto_escalated=dispatcher_input.was_auto_escalated,
            dispatcher_latency_ms=latency_ms,
            is_fallback=is_fallback,
        )
