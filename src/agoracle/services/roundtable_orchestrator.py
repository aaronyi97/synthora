"""
Roundtable Orchestrator — dispute-driven multi-model decision engine (v2.2.2).

Flow:
  Route Guard → S1 (parallel expert opinions) → S2 (dispute map + user choice A)
  → [optional S3 debate] → user choice B → S4 (decision packet)

Session state machine with CAS transitions, owner binding, heartbeat, auto-draft.

Acceptance criteria (铁律 #3):
  - Independent endpoint, independent orchestrator
  - Has its own hard timeout and guarantees terminal SSE event (complete/error/expired)
  - CAS prevents concurrent dual-path advancement
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from agoracle.domain.types import QuestionType, Role, RoleCall, ModelResponse

logger = logging.getLogger(__name__)


def _is_english(language: str) -> bool:
    return (language or "").strip() == "en-US"


def _locale_text(language: str, zh: str, en: str) -> str:
    return en if _is_english(language) else zh

# ── Constants ──────────────────────────────────────────────

ROUNDTABLE_TIMEOUT_S = 300
EXPERT_TIMEOUT_S = 60
MODERATOR_S2_TIMEOUT_S = 45
MODERATOR_S4_TIMEOUT_S = 60
MAIN_DEBATER_TIMEOUT_S = 30
REVIEWER_TIMEOUT_S = 15
AUTO_DRAFT_TIMEOUT_S = 240   # 4 min — auto-draft if user idle (must be < ROUNDTABLE_TIMEOUT_S)
SESSION_EXPIRE_S = 7200      # 2h — auto_draft_sent → expired
HEARTBEAT_INTERVAL_S = 10   # SSE ping interval during awaiting states

# Route guard patterns — questions that are NOT suitable for roundtable.
# Only reject pure operation/factual/creative queries.
# "怎么看待"/"如何评价"/"怎么理解" are opinion/analysis questions — DO NOT reject.
_RULE_GUARD_REJECT_PATTERNS = [
    re.compile(r"^(帮我写|翻译|请帮|帮忙写|写一篇|写一个|生成一个)"),  # creative/writing tasks
    re.compile(r"^(什么是|的定义|怎么用|如何使用|怎么安装|如何安装|如何配置)"),  # pure factual/howto
    re.compile(r"^(请问|告诉我|解释一下)[^，,。？?]*$"),  # short fact lookups (no complex clause)
    re.compile(r"^(write|draft|translate|help me write|generate)\b", re.IGNORECASE),
    re.compile(r"^(what is|define|how (?:to|do i) use|how (?:to|do i) install|how (?:to|do i) configure)\b", re.IGNORECASE),
    re.compile(r"^(tell me|explain)\b[^,.?!]*$", re.IGNORECASE),
]

# Expert model pool — diverse perspectives for debate
# perplexity_sonar_pro: always-on search engine, provides real-time web data
_EXPERT_LABELS: dict[str, dict[str, str]] = {
    "claude_opus_thinking": {"zh-CN": "深度分析师", "en-US": "Deep Analyst"},
    "deepseek_reasoner": {"zh-CN": "逻辑推演师", "en-US": "Logic Strategist"},
    "kimi": {"zh-CN": "资料研究员", "en-US": "Research Specialist"},
    "gemini_31_pro_thinking": {"zh-CN": "多角度思考", "en-US": "Perspective Explorer"},
    "perplexity_sonar_pro": {"zh-CN": "实时信息专家", "en-US": "Realtime Information Expert"},
}

ROUNDTABLE_EXPERTS: dict[str, dict[str, str]] = {
    "claude_opus_thinking": {"style": "systematic"},
    "deepseek_reasoner": {"style": "analytical"},
    "kimi": {"style": "research"},
    "gemini_31_pro_thinking": {"style": "creative"},
    "perplexity_sonar_pro": {"style": "realtime"},
}
DEFAULT_EXPERT_COUNT = len(ROUNDTABLE_EXPERTS)
MIN_SUCCESSFUL_EXPERTS = max(2, -(-DEFAULT_EXPERT_COUNT * 3 // 5))

_MODERATOR_MODEL_DEFAULT = "claude_sonnet"


def _get_moderator_model(config: Any = None) -> str:
    """Read moderator model from config, fall back to default."""
    if config is not None:
        rt_moderator = getattr(config, "roundtable_moderator_model", None)
        if rt_moderator:
            return rt_moderator
        # Also check nested features config
        features = getattr(config, "features", None)
        if features:
            rt_moderator = getattr(features, "roundtable_moderator_model", None)
            if rt_moderator:
                return rt_moderator
    return _MODERATOR_MODEL_DEFAULT

_STYLE_HINTS: dict[str, str] = {
    "systematic": "请从系统性角度分析，列出主要因素和它们之间的关系。",
    "analytical": "请用严密的逻辑推理，关注论据的强度和潜在漏洞。",
    "balanced":   "请给出平衡的分析，考虑不同立场的合理性。",
    "creative":   "请从多个不同角度思考，包括非传统的视角。",
    "research":   "请综合已有信息，关注事实依据和数据支持。",
    "realtime":   "你拥有实时联网搜索能力。请主动搜索最新信息，用事实和数据支撑你的观点。特别关注时效性信息。",
}

_STYLE_HINTS_EN: dict[str, str] = {
    "systematic": "Analyze the problem systematically, listing the main factors and how they interact.",
    "analytical": "Use rigorous reasoning and pay close attention to the strength and weakness of the arguments.",
    "balanced": "Provide a balanced analysis and take the merits of different positions seriously.",
    "creative": "Think from multiple angles, including unconventional ones.",
    "research": "Synthesize available information and stay grounded in facts and evidence.",
    "realtime": "You have live web-search capability. Search proactively for up-to-date information and support your stance with timely facts and data.",
}

def _expert_label(model_id: str, language: str) -> str:
    labels = _EXPERT_LABELS.get(model_id)
    if not labels:
        return model_id
    locale = "en-US" if _is_english(language) else "zh-CN"
    return labels.get(locale) or labels.get("zh-CN") or model_id


def _style_hint(style: str, language: str) -> str:
    if _is_english(language):
        return _STYLE_HINTS_EN.get(style, "Give your best professional analysis.")
    return _STYLE_HINTS.get(style, "请给出你的专业分析。")


# ── Route Guard ────────────────────────────────────────────

@dataclass
class SuitabilityResult:
    """Result of route guard check."""
    suitability: str   # "high" / "medium" / "low"
    reason: str = ""


def rule_guard(question: str) -> str | None:
    """Rule-based first layer. Returns 'low' if unsuitable, None for LLM grey zone."""
    q = question.strip()
    # Keep short but meaningful Chinese decision questions in the LLM grey zone.
    if not q:
        return "low"
    for pat in _RULE_GUARD_REJECT_PATTERNS:
        if pat.search(q):
            return "low"
    return None


async def check_suitability(
    question: str,
    model_adapter: Any,
    config: Any = None,
    prompt_loader: Any = None,
    language: str = "zh-CN",
) -> SuitabilityResult:
    """Two-layer route guard: rules first, LLM for grey zone.

    Returns SuitabilityResult. Caller decides whether to proceed or redirect.
    """
    rule_result = rule_guard(question)
    if rule_result == "low":
        return SuitabilityResult(
            suitability="low",
            reason=_locale_text(
                language,
                "这个问题更适合直接回答或处理，不适合圆桌讨论。",
                "This question is better handled directly and is not a good fit for a roundtable debate.",
            ),
        )

    # LLM grey zone check via Sonnet (cheap, fast)
    try:
        system_prompt = _locale_text(
            language,
            "判断以下问题是否适合多模型圆桌讨论（需要多个可行选项的权衡决策）。\n"
            "输出严格JSON：{\"suitability\": \"high/medium/low\", \"reason\": \"一句话原因\"}\n"
            "high = 有多个可行选项需要权衡的决策\n"
            "medium = 可能有多角度但不确定\n"
            "low = 纯事实查询/操作指南/创作任务，不适合圆桌",
            "Determine whether the following question is suitable for a multi-model roundtable debate "
            "(a decision with multiple viable options that need to be weighed).\n"
            "Return strict JSON: {\"suitability\": \"high/medium/low\", \"reason\": \"one-sentence reason\"}\n"
            "high = a decision with multiple viable options and real trade-offs\n"
            "medium = there may be multiple angles, but it is uncertain\n"
            "low = a factual lookup, how-to request, or creative-writing task that does not fit a roundtable",
        )
        if prompt_loader:
            _safety = prompt_loader.load("safety_rules", language=language)
            if _safety:
                system_prompt = f"{_safety}\n\n{system_prompt}"
        role_call = RoleCall(
            call_id=str(uuid.uuid4()),
            model_id=_get_moderator_model(config),
            role=Role.CONTRIBUTOR,
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": question}],
            timeout_seconds=8,
        )
        response = await asyncio.wait_for(
            model_adapter.call(role_call), timeout=8.0
        )
        raw = response.content if hasattr(response, "content") else str(response)
        data = _parse_json_robust(raw)
        if data and "suitability" in data:
            suitability = data["suitability"]
            reason = data.get("reason", "")
            if not reason and suitability == "low":
                reason = _locale_text(
                    language,
                    "这个问题缺少需要多方权衡的决策张力，不适合圆桌讨论。",
                    "This question does not contain enough real trade-offs to benefit from a roundtable debate.",
                )
            return SuitabilityResult(
                suitability=suitability,
                reason=reason,
            )
    except Exception as e:
        logger.warning(f"[route_guard] LLM suitability check failed: {e}")

    # Default to medium if LLM fails — don't block user
    return SuitabilityResult(suitability="medium", reason="llm_fallback")


# ── Session State Machine ─────────────────────────────────

class SessionState(enum.Enum):
    INITIALIZING = "initializing"
    COLLECTING = "collecting"
    MAPPING = "mapping"
    AWAITING_A = "awaiting_A"
    DEBATING = "debating"
    AWAITING_B = "awaiting_B"
    DRAFTING = "drafting"
    AUTO_DRAFTING = "auto_drafting"
    COMPLETE = "complete"
    AUTO_DRAFT_SENT = "auto_draft_sent"
    ERROR = "error"
    EXPIRED = "expired"


_TERMINAL_STATES = {SessionState.COMPLETE, SessionState.ERROR, SessionState.EXPIRED}


class RoundtableSession:
    """Manages session lifecycle with CAS state transitions and owner binding.

    Persistence boundary: this object lives in-memory only. It holds
    asyncio.Lock, asyncio.Queue, and monotonic timestamps that are not
    serializable. SQLiteRoundtableStore persists *metadata* (session_id,
    owner, state, timestamps) for audit and cross-worker routing lookup,
    but does NOT support session recovery after process restart. Active
    sessions are lost on restart; the SQLite record transitions to
    'expired' on the next cleanup cycle.
    """

    def __init__(self, session_id: str, owner_user_id: int | str, language: str = "zh-CN"):
        self.session_id = session_id
        self.owner_user_id = owner_user_id
        self.language = language
        self._state = SessionState.INITIALIZING
        self._state_lock = asyncio.Lock()
        self._choice_queue: asyncio.Queue = asyncio.Queue()
        self._transitions: list[dict] = []
        self._created_at = time.monotonic()
        self._last_event_at = time.monotonic()
        self._idempotency_cache: dict[str, dict] = {}  # key -> response
        self._auto_draft_sent = False
        self._pre_draft_state: SessionState | None = None  # state before auto_draft
        self.auto_draft_packet: "DecisionPacket | None" = None  # cached auto-draft result
        self._pending_choice: "UserChoice | None" = None  # set by _await_choice_stream
        self.s1_success_count: int = 0
        self.question: str = ""
        self.expert_count: int = 0
        self.experts: list["ExpertOpinion"] = []
        self.dispute_map: "DisputeMap | None" = None
        self.rebuttals: list["Rebuttal"] = []
        self.debate_round: int = 0
        self.choice_point: str = ""
        self.user_inputs: list[str] = []
        self.user_preferences: list["UserPreference"] = []

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def is_terminal(self) -> bool:
        return self._state in _TERMINAL_STATES

    def touch(self) -> None:
        """Update last event timestamp."""
        self._last_event_at = time.monotonic()

    @property
    def elapsed_since_last_event(self) -> float:
        return time.monotonic() - self._last_event_at

    async def transition(
        self, expected: SessionState, target: SessionState, trigger: str,
    ) -> bool:
        """Atomic CAS transition. Returns True on success, False on mismatch."""
        async with self._state_lock:
            if self._state != expected:
                logger.warning(
                    f"[Session {self.session_id}] CAS fail: "
                    f"expected={expected.value}, actual={self._state.value}, trigger={trigger}"
                )
                return False
            self._state = target
            self._transitions.append({
                "from": expected.value, "to": target.value,
                "trigger": trigger, "at": time.monotonic(),
            })
            self.touch()
            return True

    async def force_state(self, target: SessionState, trigger: str) -> None:
        """Force state (for error/cleanup). Not CAS."""
        async with self._state_lock:
            old = self._state
            self._state = target
            self._transitions.append({
                "from": old.value, "to": target.value,
                "trigger": f"force:{trigger}", "at": time.monotonic(),
            })

    def check_idempotency(self, key: str) -> dict | None:
        """Return cached response for idempotency key, or None."""
        return self._idempotency_cache.get(key)

    def cache_idempotency(self, key: str, response: dict) -> None:
        self._idempotency_cache[key] = response

    def should_auto_draft(self) -> bool:
        return all([
            self._state in (SessionState.AWAITING_A, SessionState.AWAITING_B),
            self.elapsed_since_last_event > AUTO_DRAFT_TIMEOUT_S,
            self.s1_success_count >= MIN_SUCCESSFUL_EXPERTS,
            not self._auto_draft_sent,
        ])

    def should_expire(self) -> bool:
        return (
            self._state == SessionState.AUTO_DRAFT_SENT
            and self.elapsed_since_last_event > SESSION_EXPIRE_S
        )


# Module-level session store (single instance; Redis for multi-instance)
_sessions: dict[str, RoundtableSession] = {}

_SESSION_CLEANUP_DELAY_S = 1800  # 30 min after terminal state


def get_session(session_id: str) -> RoundtableSession | None:
    return _sessions.get(session_id)


def _schedule_cleanup(session_id: str) -> None:
    """Schedule deferred removal of a terminal session from the in-memory store."""
    async def _cleanup() -> None:
        await asyncio.sleep(_SESSION_CLEANUP_DELAY_S)
        _sessions.pop(session_id, None)
        logger.debug(f"[Session {session_id}] Cleaned up from store after terminal state")

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_cleanup())
    except RuntimeError:
        pass  # No running loop (e.g. in tests); skip cleanup scheduling


def _schedule_session_expiry(session: "RoundtableSession") -> None:
    """After AUTO_DRAFT_SENT, wait SESSION_EXPIRE_S then transition to EXPIRED.

    This is the correct place for the expiry check: the session is in
    AUTO_DRAFT_SENT state (not AWAITING), so should_expire() can fire.
    """
    async def _expire() -> None:
        await asyncio.sleep(SESSION_EXPIRE_S)
        if session.should_expire():
            await session.force_state(SessionState.EXPIRED, "session_expired")
            _schedule_cleanup(session.session_id)
            logger.info(f"[Session {session.session_id}] Expired after {SESSION_EXPIRE_S}s idle")

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_expire())
    except RuntimeError:
        pass  # No running loop (e.g. in tests); skip expiry scheduling


# ── Data Types ─────────────────────────────────────────────

@dataclass
class RoundtableConfig:
    """Configuration for a roundtable session (v2.2.2)."""
    expert_count: int = DEFAULT_EXPERT_COUNT
    expert_timeout_s: int = EXPERT_TIMEOUT_S
    main_debater_timeout_s: int = MAIN_DEBATER_TIMEOUT_S
    reviewer_timeout_s: int = REVIEWER_TIMEOUT_S
    moderator_s2_timeout_s: int = MODERATOR_S2_TIMEOUT_S
    moderator_s4_timeout_s: int = MODERATOR_S4_TIMEOUT_S
    total_timeout_s: int = ROUNDTABLE_TIMEOUT_S
    auto_draft_timeout_s: int = AUTO_DRAFT_TIMEOUT_S
    session_expire_s: int = SESSION_EXPIRE_S
    preference_confidence_threshold: float = 0.7
    interactive: bool = False


@dataclass
class Claim:
    """A single structured argument from an expert."""
    point: str
    evidence: str
    dimension: str


@dataclass
class ExpertOpinion:
    """S1: One expert's structured stance (v2.2.2)."""
    model_id: str
    label: str
    stance: str                              # explicit position, never "各有道理"
    confidence: float                        # 0.0–1.0
    my_dimensions: list[str] = field(default_factory=list)   # self-proposed 2-4 dims
    claims: list[Claim] = field(default_factory=list)        # ≤5
    risk_warning: str = ""
    blind_spot_warning: str = ""
    challenge_to_others: str = ""
    raw_response: str = ""                   # fallback display when JSON parse fails
    structured: bool = True                  # False when JSON parse failed
    degradation_note: str = ""
    latency_ms: int = 0
    success: bool = True
    error: str = ""


@dataclass
class ContentionSide:
    position: str
    supporting_claims: list[str] = field(default_factory=list)
    lead_expert: str = ""
    main_argument: str = ""


@dataclass
class ContentionPoint:
    topic: str
    severity: str                            # "high" / "medium" / "low"
    dispute_type: list[str] = field(default_factory=list)    # ["factual"] or ["factual","value"]
    factual_aspect: str = ""
    value_aspect: str = ""
    dimension_id: str = ""                   # snake_case id for preference mapping
    dimension_label: str = ""
    dimension_aliases: list[str] = field(default_factory=list)
    adjudication_note: str = ""
    sides: list[ContentionSide] = field(default_factory=list)
    why_it_matters: str = ""
    suggested_focus: bool = False


@dataclass
class ConsensusPoint:
    point: str
    strength: str                            # "strong" / "moderate" / "weak"
    agreed_by: list[str] = field(default_factory=list)


@dataclass
class DisputeMap:
    """S2: Moderator's structured dispute analysis (v2.2.2)."""
    synthesized_dimensions: list[str] = field(default_factory=list)
    dimension_sources: dict = field(default_factory=dict)    # dim_id -> [expert_labels]
    contention_points: list[ContentionPoint] = field(default_factory=list)
    consensus_points: list[ConsensusPoint] = field(default_factory=list)
    suggested_focus: str = ""
    echo_chamber_warning: str = ""           # set when ≥80% consensus
    clarifying_questions: list[str] = field(default_factory=list)  # ≤3
    degraded: bool = False


@dataclass
class Rebuttal:
    """S3: One expert's rebuttal/concession in the debate round."""
    model_id: str
    label: str
    role: str                                # "main_debater" / "reviewer"
    target_dispute: str = ""
    response_type: str = ""                  # "rebut" / "concede" / "revise"
    response: str = ""
    new_evidence: str = ""
    revised_stance: str = ""
    stance_changed: bool = False
    confidence: float = 0.5
    raw_response: str = ""
    structured: bool = True
    latency_ms: int = 0
    success: bool = True


@dataclass
class UserPreference:
    """Extracted user preference from inject/choice input."""
    dimension_id: str
    preference: str
    confidence: float                        # 0.0–1.0
    source: str                              # "explicit" / "inferred"


@dataclass
class StanceEvolution:
    expert: str
    r1_stance: str
    final_stance: str
    changed: bool
    changed_reason: str = ""


@dataclass
class ValueDisputeForUser:
    """Value dispute that needs user input to resolve."""
    point: str
    dimension_id: str = ""
    ask_user: str = ""


@dataclass
class DecisionOption:
    choice: str
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)
    best_when: str = ""
    risk: str = ""
    mitigation: str = ""


@dataclass
class UnresolvedItem:
    point: str
    reason: str
    how_to_resolve: str


@dataclass
class DecisionPacket:
    """S4: Final moderator verdict — the core deliverable (v2.2.2)."""
    final_summary: str
    conclusion_type: str = "draft"           # "recommendation" / "conditional" / "draft"
    confidence_basis: str = ""
    stance_evolution: list[StanceEvolution] = field(default_factory=list)
    options: list[DecisionOption] = field(default_factory=list)
    unresolved: list[UnresolvedItem] = field(default_factory=list)
    what_changes_my_mind: str = ""
    recommended_action: str = ""
    value_disputes_to_user: list[ValueDisputeForUser] = field(default_factory=list)
    echo_chamber_flag: bool = False
    degraded: bool = False
    degradation_reason: str = ""
    total_latency_ms: int = 0
    estimated_cost_usd: float = 0.0


@dataclass
class Dimension:
    """Normalized dimension for preference mapping."""
    id: str                                  # snake_case English
    label: str                               # display label (Chinese OK)
    aliases: list[str] = field(default_factory=list)


@dataclass
class RoundtableResult:
    """Complete roundtable result (v2.2.2)."""
    session_id: str = ""
    question: str = ""
    experts: list[ExpertOpinion] = field(default_factory=list)
    dispute_map: DisputeMap = field(default_factory=DisputeMap)
    rebuttals: list[list[Rebuttal]] = field(default_factory=list)  # one list per round
    decision_packet: DecisionPacket = field(
        default_factory=lambda: DecisionPacket(final_summary="")
    )
    rounds_completed: int = 1
    user_inputs: list[str] = field(default_factory=list)
    dimension_mapping: list[Dimension] = field(default_factory=list)


# ── SSE Event Types ────────────────────────────────────────

class RoundtableEvent:
    """Base roundtable stream event."""


class RoundtableStarted(RoundtableEvent):
    def __init__(self, session_id: str, expert_count: int, question: str):
        self.session_id = session_id
        self.expert_count = expert_count
        self.question = question


class ExpertDone(RoundtableEvent):
    def __init__(self, opinion: ExpertOpinion, done_count: int, total_count: int):
        self.opinion = opinion
        self.done_count = done_count
        self.total_count = total_count


class DisputesMapped(RoundtableEvent):
    """S2: Moderator has built the dispute map."""
    def __init__(self, dispute_map: DisputeMap):
        self.dispute_map = dispute_map


class AwaitingUserChoice(RoundtableEvent):
    """Waiting for user to pick deepen / conclude / inject."""
    def __init__(self, choice_point: str, timeout_s: int = 0, default_action: str = ""):
        self.choice_point = choice_point
        self.timeout_s = timeout_s
        self.default_action = default_action


class DebateStarted(RoundtableEvent):
    """S3: Debate round started."""
    def __init__(self, round_num: int, assignments: dict):
        self.round_num = round_num
        self.assignments = assignments  # {model_id: "main_debater"/"reviewer"}


class RebuttalDone(RoundtableEvent):
    """S3: One expert has submitted their rebuttal."""
    def __init__(self, rebuttal: Rebuttal, done_count: int, total_count: int):
        self.rebuttal = rebuttal
        self.done_count = done_count
        self.total_count = total_count


class DebateComplete(RoundtableEvent):
    """S3: Debate round complete."""
    def __init__(self, round_num: int, stance_changes: list[dict]):
        self.round_num = round_num
        self.stance_changes = stance_changes


class ModeratorStarted(RoundtableEvent):
    """Kept for backward-compat with app.py SSE mapping."""
    pass


class RoundtableComplete(RoundtableEvent):
    def __init__(self, result: RoundtableResult):
        self.result = result


class RoundtableError(RoundtableEvent):
    def __init__(
        self,
        error: str,
        billable: bool = True,
        *,
        phase: str = "",
        reason: str = "",
        detail: str = "",
    ):
        self.error = error
        self.billable = billable  # False when no model calls were made (e.g. insufficient_experts)
        self.phase = phase
        self.reason = reason
        self.detail = detail


class Heartbeat(RoundtableEvent):
    """SSE ping during awaiting states (every HEARTBEAT_INTERVAL_S)."""
    pass


class AutoDraft(RoundtableEvent):
    """5-min timeout auto-draft event. Session stays in auto_draft_sent."""
    def __init__(self, decision_packet: DecisionPacket):
        self.decision_packet = decision_packet


class SessionResumed(RoundtableEvent):
    """Sent on /resume to restore client state."""
    def __init__(self, choice_point: str, auto_draft_packet: DecisionPacket | None = None):
        self.choice_point = choice_point
        self.auto_draft_packet = auto_draft_packet


@dataclass
class UserChoice:
    action: str               # "deepen" | "conclude" | "inject"
    choice_point: str = ""    # "A" | "B" — which decision point this choice is for
    user_input: str | None = None
    focus_topic: str | None = None
    idempotency_key: str = ""


class RoundtablePhaseError(RuntimeError):
    """Structured phase-aware failure inside the roundtable pipeline."""

    def __init__(self, code: str, *, phase: str, reason: str, detail: str = ""):
        super().__init__(f"{code} ({phase}/{reason})")
        self.code = code
        self.phase = phase
        self.reason = reason
        self.detail = detail


def _is_timeout_like(error_text: str) -> bool:
    lowered = error_text.lower()
    return "timeout" in lowered or "timed out" in lowered


def _classify_upstream_failure(phase: str, error_text: str) -> tuple[str, str]:
    phase_slug = phase.lower()
    if _is_timeout_like(error_text):
        return (
            f"roundtable_{phase_slug}_upstream_model_timeout",
            "upstream_model_timeout_retry_exhausted",
        )
    return (
        f"roundtable_{phase_slug}_upstream_model_failure",
        "upstream_model_retry_exhausted",
    )


# ── Orchestrator ───────────────────────────────────────────

class RoundtableOrchestrator:
    """Independent orchestrator for roundtable debate sessions (v2.2.2)."""

    def __init__(
        self,
        model_adapter: Any,
        config: Any = None,
        prompt_loader: Any = None,
        session_store: Any = None,
    ):
        self._adapter = model_adapter
        self._config = config
        self._prompt_loader = prompt_loader
        self._session_store = session_store

    def _load_safety_rules(self, language: str = "zh-CN") -> str:
        if not self._prompt_loader:
            return ""
        return self._prompt_loader.load("safety_rules", language=language)

    async def execute_streaming(
        self,
        question: str,
        owner_user_id: int | str,
        question_type: str = QuestionType.UNKNOWN.value,
        rt_config: RoundtableConfig | None = None,
        session_id: str | None = None,
        language: str = "zh-CN",
    ) -> AsyncIterator[RoundtableEvent]:
        """Execute roundtable debate with streaming events.

        v2.2.2: Session state machine with CAS, heartbeat, auto-draft.
        Guarantees terminal event: RoundtableComplete, RoundtableError, or AutoDraft.
        """
        cfg = rt_config or RoundtableConfig()
        resume_session_id = session_id
        if resume_session_id:
            existing_session = get_session(resume_session_id)
            if existing_session is None:
                raise ValueError(f"roundtable session not found: {resume_session_id}")
            if existing_session.owner_user_id != owner_user_id:
                raise PermissionError(f"roundtable owner mismatch: {resume_session_id}")
            if existing_session.state in _TERMINAL_STATES:
                raise RuntimeError(f"roundtable session already ended: {resume_session_id}")
            session = existing_session
            if not getattr(session, "language", ""):
                session.language = language
            session_id = existing_session.session_id
            question = existing_session.question or question
            start = existing_session._created_at
        else:
            session_id = uuid.uuid4().hex[:16]
            session = RoundtableSession(session_id, owner_user_id, language=language)
            _sessions[session_id] = session
            if self._session_store:
                try:
                    await self._session_store.register(session_id, question, owner_user_id)
                except Exception as e:
                    logger.warning(f"[Roundtable] Failed to register session in store: {e}")
            start = time.monotonic()

            experts = self._select_experts(cfg.expert_count)
            session.question = question
            session.expert_count = len(experts)
            if len(experts) < 2:
                await session.force_state(SessionState.ERROR, "insufficient_experts")
                await self._store_state(session_id, "error")
                _schedule_cleanup(session_id)
                yield RoundtableError(
                    _locale_text(
                        session.language,
                        "圆桌讨论需要至少2个可用专家模型",
                        "Roundtable mode needs at least 2 available expert models.",
                    ),
                    billable=False,
                )
                return

            await session.transition(SessionState.INITIALIZING, SessionState.COLLECTING, "start")
            yield RoundtableStarted(session_id, len(experts), question)

        async def _terminal_error(
            code: str,
            *,
            phase: str,
            reason: str,
            detail: str = "",
            billable: bool = True,
        ) -> RoundtableError:
            logger.error(
                f"[roundtable:{session_id}] terminal_error "
                f"code={code} phase={phase} reason={reason} detail={detail or '-'}"
            )
            await session.force_state(SessionState.ERROR, code)
            await self._store_state(session_id, "error")
            _schedule_cleanup(session_id)
            return RoundtableError(
                code,
                billable=billable,
                phase=phase,
                reason=reason,
                detail=detail,
            )

        async def _insufficient_responses_error(
            *,
            phase: str,
            reason: str,
            detail: str,
        ) -> RoundtableError:
            logger.error(
                f"[roundtable:{session_id}] terminal_error "
                f"code=insufficient_responses phase={phase} reason={reason} detail={detail or '-'}"
            )
            await session.force_state(SessionState.ERROR, "insufficient_responses")
            await self._store_state(session_id, "error")
            _schedule_cleanup(session_id)
            return RoundtableError(
                _locale_text(
                    session.language,
                    "部分 AI 专家暂时不可用，请稍后重试",
                    "Some AI experts are temporarily unavailable. Please try again later.",
                ),
                billable=False,
                phase=phase,
                reason=reason,
                detail=detail,
            )

        async def _resume_existing_session() -> AsyncIterator[RoundtableEvent]:
            if not cfg.interactive and session.state in (
                SessionState.AWAITING_A,
                SessionState.AWAITING_B,
                SessionState.DEBATING,
            ):
                yield await _terminal_error(
                    "roundtable_interactive_disabled",
                    phase=session.state.value,
                    reason="interactive_mode_off",
                    detail="Session requires interactive mode but interactive=False",
                    billable=False,
                )
                return

            successful = [o for o in session.experts if o.success]
            dispute_map = session.dispute_map
            user_inputs = list(session.user_inputs)
            user_preferences = list(session.user_preferences)

            if session.state == SessionState.AUTO_DRAFT_SENT and session.auto_draft_packet is not None:
                yield AutoDraft(session.auto_draft_packet)
                return

            if session.state == SessionState.COLLECTING:
                experts = self._select_experts(cfg.expert_count)
                session.expert_count = max(session.expert_count, len(experts))
                existing_model_ids = {op.model_id for op in session.experts}
                remaining_experts = [
                    expert for expert in experts
                    if expert[0] not in existing_model_ids
                ]
                done_count = len(session.experts)
                deadline = time.monotonic() + cfg.total_timeout_s
                async for opinion in self._fan_out_experts_stream(
                    question,
                    remaining_experts,
                    cfg.expert_timeout_s,
                    session_id,
                    deadline,
                ):
                    session.experts.append(opinion)
                    done_count += 1
                    yield ExpertDone(opinion, done_count, len(experts))

                successful = [o for o in session.experts if o.success]
                session.s1_success_count = len(successful)
                if len(successful) < 2:
                    yield await _insufficient_responses_error(
                        phase="S1",
                        reason="insufficient_responses_after_resume",
                        detail="Resumed roundtable still has fewer than 2 successful experts",
                    )
                    return
                await session.transition(SessionState.COLLECTING, SessionState.MAPPING, "resume_s2_start")

            if session.state == SessionState.MAPPING:
                yield ModeratorStarted()
                try:
                    dispute_map = await asyncio.wait_for(
                        self._map_disputes(question, successful, session_id),
                        timeout=cfg.moderator_s2_timeout_s,
                    )
                except RoundtablePhaseError as exc:
                    if cfg.interactive:
                        yield await _terminal_error(
                            exc.code,
                            phase=exc.phase,
                            reason=exc.reason,
                            detail=exc.detail,
                        )
                        return
                    logger.warning(
                        f"[roundtable:{session_id}] Resume S2 fallback triggered after "
                        f"moderator failure code={exc.code} reason={exc.reason} detail={exc.detail or '-'}"
                    )
                    dispute_map = self._build_fallback_dispute_map(successful, language=language)
                except asyncio.TimeoutError:
                    logger.warning(
                        f"[roundtable:{session_id}] Resume S2 fallback triggered after "
                        f"{cfg.moderator_s2_timeout_s}s moderator timeout"
                    )
                    dispute_map = self._build_fallback_dispute_map(successful, language=language)
                session.dispute_map = dispute_map
                yield DisputesMapped(dispute_map)
                await session.transition(SessionState.MAPPING, SessionState.AWAITING_A, "resume_awaiting_choice_a")
                session.choice_point = "A"

            if session.state == SessionState.AWAITING_A:
                if dispute_map is None:
                    yield await _terminal_error(
                        "roundtable_resume_invalid_state",
                        phase="A",
                        reason="missing_dispute_map",
                        detail="Session resumed at awaiting_A without dispute_map",
                        billable=False,
                    )
                    return
                session.choice_point = "A"
                yield AwaitingUserChoice(choice_point="A")
                async for hb_event in self._await_choice_stream(
                    session, cfg, "A", question, successful, dispute_map
                ):
                    if isinstance(hb_event, AutoDraft):
                        yield hb_event
                        return
                    yield hb_event

                choice_a = session._pending_choice
                session._pending_choice = None
                if choice_a is None:
                    yield await _terminal_error(
                        "roundtable_choice_timeout",
                        phase="A",
                        reason="user_choice_timeout",
                        detail="User did not respond before roundtable deadline at choice point A",
                    )
                    return
                if choice_a.user_input:
                    user_inputs.append(choice_a.user_input)
                    session.user_inputs = list(user_inputs)
                if choice_a.action == "inject" and choice_a.user_input:
                    user_preferences.extend(self._extract_preferences(choice_a.user_input, dispute_map))
                    session.user_preferences = list(user_preferences)

                if choice_a.action == "conclude":
                    await session.transition(SessionState.AWAITING_A, SessionState.DRAFTING, "resume_user_conclude_a")
                    session.choice_point = ""
                    eligibility = self._check_recommendation_eligibility(
                        successful, dispute_map, [], user_preferences
                    )
                    try:
                        packet = await asyncio.wait_for(
                            self._build_decision_packet(
                                question,
                                successful,
                                dispute_map,
                                user_inputs,
                                session_id,
                                rebuttals=[],
                                user_preferences=user_preferences,
                                eligibility=eligibility,
                                interactive=cfg.interactive,
                            ),
                            timeout=cfg.moderator_s4_timeout_s,
                        )
                    except RoundtablePhaseError as exc:
                        yield await _terminal_error(
                            exc.code,
                            phase=exc.phase,
                            reason=exc.reason,
                            detail=exc.detail,
                        )
                        return
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"[roundtable:{session_id}] Resume S4 fallback triggered after "
                            f"{cfg.moderator_s4_timeout_s}s moderator timeout"
                        )
                        packet = self._build_fallback_decision_packet(
                            successful,
                            dispute_map,
                            reason="s4_moderator_timeout",
                            interactive=cfg.interactive,
                            language=language,
                        )
                    total_ms = int((time.monotonic() - start) * 1000)
                    packet.total_latency_ms = total_ms
                    packet.estimated_cost_usd = self._estimate_cost(session.experts)
                    result = RoundtableResult(
                        session_id=session_id,
                        question=question,
                        experts=session.experts,
                        dispute_map=dispute_map,
                        rebuttals=[],
                        decision_packet=packet,
                        rounds_completed=1,
                        user_inputs=user_inputs,
                        dimension_mapping=self._build_dimension_mapping(dispute_map),
                    )
                    await session.transition(SessionState.DRAFTING, SessionState.COMPLETE, "complete")
                    await self._store_state(session_id, "complete")
                    _schedule_cleanup(session_id)
                    yield RoundtableComplete(result)
                    return

                await session.transition(SessionState.AWAITING_A, SessionState.DEBATING, "resume_s3_start")
                session.choice_point = ""
                session.debate_round = 1
                session.rebuttals = []
                async for s3_event in self._run_debate_round(
                    question,
                    successful,
                    dispute_map,
                    user_inputs,
                    round_num=1,
                    session_id=session_id,
                    cfg=cfg,
                ):
                    if isinstance(s3_event, DebateStarted):
                        session.debate_round = s3_event.round_num
                    elif isinstance(s3_event, RebuttalDone):
                        session.rebuttals.append(s3_event.rebuttal)
                    yield s3_event

                await session.transition(SessionState.DEBATING, SessionState.AWAITING_B, "resume_awaiting_choice_b")
                session.choice_point = "B"

            if session.state == SessionState.DEBATING:
                if dispute_map is None:
                    yield await _terminal_error(
                        "roundtable_resume_invalid_state",
                        phase="S3",
                        reason="missing_dispute_map",
                        detail="Session resumed at debating without dispute_map",
                        billable=False,
                    )
                    return
                current_round = max(session.debate_round, 1)
                if current_round >= 2:
                    session.rebuttals = session.rebuttals[:len(successful)]
                else:
                    session.rebuttals = []
                    current_round = 1
                session.debate_round = current_round
                async for s3_event in self._run_debate_round(
                    question,
                    successful,
                    dispute_map,
                    user_inputs,
                    round_num=current_round,
                    session_id=session_id,
                    cfg=cfg,
                ):
                    if isinstance(s3_event, DebateStarted):
                        session.debate_round = s3_event.round_num
                    elif isinstance(s3_event, RebuttalDone):
                        session.rebuttals.append(s3_event.rebuttal)
                    yield s3_event

                if current_round == 1:
                    await session.transition(SessionState.DEBATING, SessionState.AWAITING_B, "resume_awaiting_choice_b")
                    session.choice_point = "B"
                else:
                    await session.transition(SessionState.DEBATING, SessionState.DRAFTING, "resume_s4_after_round2")
                    session.choice_point = ""

            if session.state == SessionState.AWAITING_B:
                if dispute_map is None:
                    yield await _terminal_error(
                        "roundtable_resume_invalid_state",
                        phase="B",
                        reason="missing_dispute_map",
                        detail="Session resumed at awaiting_B without dispute_map",
                        billable=False,
                    )
                    return
                session.choice_point = "B"
                yield AwaitingUserChoice(choice_point="B")
                async for hb_event in self._await_choice_stream(
                    session, cfg, "B", question, successful, dispute_map
                ):
                    if isinstance(hb_event, AutoDraft):
                        yield hb_event
                        return
                    yield hb_event

                choice_b = session._pending_choice
                session._pending_choice = None
                if choice_b is None:
                    yield await _terminal_error(
                        "roundtable_choice_timeout",
                        phase="B",
                        reason="user_choice_timeout",
                        detail="User did not respond before roundtable deadline at choice point B",
                    )
                    return
                if choice_b.user_input:
                    user_inputs.append(choice_b.user_input)
                    session.user_inputs = list(user_inputs)
                if choice_b.action == "inject" and choice_b.user_input:
                    user_preferences.extend(self._extract_preferences(choice_b.user_input, dispute_map))
                    session.user_preferences = list(user_preferences)

                if choice_b.action == "deepen":
                    await session.transition(SessionState.AWAITING_B, SessionState.DEBATING, "resume_s3_round2_start")
                    session.choice_point = ""
                    session.debate_round = 2
                    session.rebuttals = session.rebuttals[:len(successful)]
                    async for s3_event in self._run_debate_round(
                        question,
                        successful,
                        dispute_map,
                        user_inputs,
                        round_num=2,
                        session_id=session_id,
                        cfg=cfg,
                    ):
                        if isinstance(s3_event, DebateStarted):
                            session.debate_round = s3_event.round_num
                        elif isinstance(s3_event, RebuttalDone):
                            session.rebuttals.append(s3_event.rebuttal)
                        yield s3_event

                await session.transition(
                    SessionState.DEBATING if choice_b.action == "deepen" else SessionState.AWAITING_B,
                    SessionState.DRAFTING,
                    "resume_s4_start",
                )
                session.choice_point = ""

            if session.state == SessionState.DRAFTING:
                if dispute_map is None:
                    yield await _terminal_error(
                        "roundtable_resume_invalid_state",
                        phase="S4",
                        reason="missing_dispute_map",
                        detail="Session resumed at drafting without dispute_map",
                        billable=False,
                    )
                    return
                flat_rebuttals = list(session.rebuttals)
                eligibility = self._check_recommendation_eligibility(
                    successful, dispute_map, flat_rebuttals, user_preferences
                )
                try:
                    packet = await asyncio.wait_for(
                        self._build_decision_packet(
                            question,
                            successful,
                            dispute_map,
                            user_inputs,
                            session_id,
                            rebuttals=flat_rebuttals,
                            user_preferences=user_preferences,
                            eligibility=eligibility,
                            interactive=cfg.interactive,
                        ),
                        timeout=cfg.moderator_s4_timeout_s,
                    )
                except RoundtablePhaseError as exc:
                    yield await _terminal_error(
                        exc.code,
                        phase=exc.phase,
                        reason=exc.reason,
                        detail=exc.detail,
                    )
                    return
                except asyncio.TimeoutError:
                    logger.warning(
                        f"[roundtable:{session_id}] Resume S4 fallback triggered after "
                        f"{cfg.moderator_s4_timeout_s}s moderator timeout"
                    )
                    packet = self._build_fallback_decision_packet(
                        successful,
                        dispute_map,
                        reason="s4_moderator_timeout",
                        interactive=cfg.interactive,
                    )
                total_ms = int((time.monotonic() - start) * 1000)
                packet.total_latency_ms = total_ms
                packet.estimated_cost_usd = self._estimate_cost(session.experts)
                rounds_completed = 1 if session.debate_round == 0 else 3 if session.debate_round >= 2 else 2
                result = RoundtableResult(
                    session_id=session_id,
                    question=question,
                    experts=session.experts,
                    dispute_map=dispute_map,
                    rebuttals=[],
                    decision_packet=packet,
                    rounds_completed=rounds_completed,
                    user_inputs=user_inputs,
                    dimension_mapping=self._build_dimension_mapping(dispute_map),
                )
                await session.transition(SessionState.DRAFTING, SessionState.COMPLETE, "complete")
                await self._store_state(session_id, "complete")
                _schedule_cleanup(session_id)
                yield RoundtableComplete(result)

        if resume_session_id:
            async for resumed_event in _resume_existing_session():
                yield resumed_event
            return

        try:
            # ── S1: Independent stances (parallel fan-out) ──────────────
            opinions: list[ExpertOpinion] = []
            done_count = 0
            deadline = time.monotonic() + cfg.total_timeout_s
            async for opinion in self._fan_out_experts_stream(
                question, experts, cfg.expert_timeout_s, session_id, deadline
            ):
                opinions.append(opinion)
                session.experts = list(opinions)
                done_count += 1
                yield ExpertDone(opinion, done_count, len(experts))

            successful = [o for o in opinions if o.success]
            session.s1_success_count = len(successful)
            if len(successful) < 2:
                yield await _insufficient_responses_error(
                    phase="S1",
                    reason="insufficient_responses_after_fanout",
                    detail="Roundtable received fewer than 2 successful expert responses",
                )
                return

            # ── S2: Dispute mapping ──────────────────────────────────
            await session.transition(SessionState.COLLECTING, SessionState.MAPPING, "s2_start")
            yield ModeratorStarted()
            try:
                dispute_map = await asyncio.wait_for(
                    self._map_disputes(question, successful, session_id),
                    timeout=cfg.moderator_s2_timeout_s,
                )
            except RoundtablePhaseError as exc:
                if cfg.interactive:
                    yield await _terminal_error(
                        exc.code,
                        phase=exc.phase,
                        reason=exc.reason,
                        detail=exc.detail,
                    )
                    return
                logger.warning(
                    f"[roundtable:{session_id}] S2 fallback triggered after "
                    f"moderator failure code={exc.code} reason={exc.reason} detail={exc.detail or '-'}"
                )
                dispute_map = self._build_fallback_dispute_map(successful, language=language)
            except asyncio.TimeoutError:
                logger.warning(
                    f"[roundtable:{session_id}] S2 fallback triggered after "
                    f"{cfg.moderator_s2_timeout_s}s moderator timeout"
                )
                dispute_map = self._build_fallback_dispute_map(successful, language=language)
            session.dispute_map = dispute_map
            yield DisputesMapped(dispute_map)

            if not cfg.interactive:
                await session.transition(SessionState.MAPPING, SessionState.DRAFTING, "auto_conclude")
                session.choice_point = ""
                eligibility = self._check_recommendation_eligibility(successful, dispute_map, [], [])
                # Non-interactive mode bypasses debate and preference collection, so R1/R2 do not gate S4.
                eligibility["r1_ok"] = True
                eligibility["r1_uncovered"] = []
                eligibility["r2_ok"] = True
                eligibility["r2_missing_dimensions"] = []
                if eligibility["r4_ok"]:
                    eligibility["conclusion_type"] = "recommendation"
                try:
                    packet = await asyncio.wait_for(
                        self._build_decision_packet(
                            question,
                            successful,
                            dispute_map,
                            [],
                            session_id,
                            rebuttals=[],
                            user_preferences=[],
                            eligibility=eligibility,
                            interactive=cfg.interactive,
                        ),
                        timeout=cfg.moderator_s4_timeout_s,
                    )
                except RoundtablePhaseError as exc:
                    yield await _terminal_error(
                        exc.code,
                        phase=exc.phase,
                        reason=exc.reason,
                        detail=exc.detail,
                    )
                    return
                except asyncio.TimeoutError:
                    logger.warning(
                        f"[roundtable:{session_id}] S4 fallback triggered after "
                        f"{cfg.moderator_s4_timeout_s}s moderator timeout"
                    )
                    packet = self._build_fallback_decision_packet(
                        successful,
                        dispute_map,
                        reason="s4_moderator_timeout",
                        interactive=cfg.interactive,
                        language=language,
                    )
                total_ms = int((time.monotonic() - start) * 1000)
                packet.total_latency_ms = total_ms
                packet.estimated_cost_usd = self._estimate_cost(opinions)
                result = RoundtableResult(
                    session_id=session_id,
                    question=question,
                    experts=opinions,
                    dispute_map=dispute_map,
                    rebuttals=[],
                    decision_packet=packet,
                    rounds_completed=1,
                    user_inputs=[],
                    dimension_mapping=self._build_dimension_mapping(dispute_map),
                )
                await session.transition(SessionState.DRAFTING, SessionState.COMPLETE, "complete")
                await self._store_state(session_id, "complete")
                _schedule_cleanup(session_id)
                yield RoundtableComplete(result)
                return

            # ── Decision Point A ────────────────────────────────────
            await session.transition(SessionState.MAPPING, SessionState.AWAITING_A, "awaiting_choice_a")
            session.choice_point = "A"
            yield AwaitingUserChoice(choice_point="A")

            async for hb_event in self._await_choice_stream(
                session, cfg, "A", question, opinions, dispute_map
            ):
                if isinstance(hb_event, AutoDraft):
                    yield hb_event
                    return
                yield hb_event  # Heartbeat

            choice_a = session._pending_choice
            session._pending_choice = None
            if choice_a is None:
                yield await _terminal_error(
                    "roundtable_choice_timeout",
                    phase="A",
                    reason="user_choice_timeout",
                    detail="User did not respond before roundtable deadline at choice point A",
                )
                return
            user_inputs: list[str] = []
            if choice_a.user_input:
                user_inputs.append(choice_a.user_input)
            session.user_inputs = list(user_inputs)

            # Extract user preferences from inject input
            user_preferences: list[UserPreference] = []
            if choice_a.action == "inject" and choice_a.user_input:
                user_preferences = self._extract_preferences(
                    choice_a.user_input, dispute_map
                )
            session.user_preferences = list(user_preferences)

            if choice_a.action == "conclude":
                # Skip S3 — go directly to S4
                await session.transition(SessionState.AWAITING_A, SessionState.DRAFTING, "user_conclude_a")
                session.choice_point = ""
                eligibility = self._check_recommendation_eligibility(
                    successful, dispute_map, [], user_preferences
                )
                try:
                    packet = await asyncio.wait_for(
                        self._build_decision_packet(
                            question, successful, dispute_map, user_inputs,
                            session_id, rebuttals=[], user_preferences=user_preferences,
                            eligibility=eligibility,
                            interactive=cfg.interactive,
                        ),
                        timeout=cfg.moderator_s4_timeout_s,
                    )
                except RoundtablePhaseError as exc:
                    yield await _terminal_error(
                        exc.code,
                        phase=exc.phase,
                        reason=exc.reason,
                        detail=exc.detail,
                    )
                    return
                except asyncio.TimeoutError:
                    logger.warning(
                        f"[roundtable:{session_id}] S4 fallback triggered after "
                        f"{cfg.moderator_s4_timeout_s}s moderator timeout"
                    )
                    packet = self._build_fallback_decision_packet(
                        successful,
                        dispute_map,
                        reason="s4_moderator_timeout",
                        interactive=cfg.interactive,
                        language=language,
                    )
                total_ms = int((time.monotonic() - start) * 1000)
                packet.total_latency_ms = total_ms
                packet.estimated_cost_usd = self._estimate_cost(opinions)
                result = RoundtableResult(
                    session_id=session_id, question=question,
                    experts=opinions, dispute_map=dispute_map,
                    rebuttals=[], decision_packet=packet, rounds_completed=1,
                    user_inputs=user_inputs,
                    dimension_mapping=self._build_dimension_mapping(dispute_map),
                )
                await session.transition(SessionState.DRAFTING, SessionState.COMPLETE, "complete")
                await self._store_state(session_id, "complete")
                _schedule_cleanup(session_id)
                yield RoundtableComplete(result)
                return

            # User chose deepen or inject — run S3 debate
            await session.transition(SessionState.AWAITING_A, SessionState.DEBATING, "s3_start")
            session.choice_point = ""
            session.debate_round = 1

            all_rebuttals: list[list[Rebuttal]] = []
            async for s3_event in self._run_debate_round(
                question, successful, dispute_map, user_inputs,
                round_num=1, session_id=session_id, cfg=cfg,
            ):
                if isinstance(s3_event, DebateStarted):
                    session.debate_round = s3_event.round_num
                elif isinstance(s3_event, RebuttalDone):
                    session.rebuttals.append(s3_event.rebuttal)
                if isinstance(s3_event, DebateComplete):
                    if hasattr(s3_event, "_rebuttals"):
                        all_rebuttals.append(s3_event._rebuttals)
                yield s3_event

            # ── Decision Point B ────────────────────────────────────
            await session.transition(SessionState.DEBATING, SessionState.AWAITING_B, "awaiting_choice_b")
            session.choice_point = "B"
            yield AwaitingUserChoice(choice_point="B")

            async for hb_event in self._await_choice_stream(
                session, cfg, "B", question, opinions, dispute_map
            ):
                if isinstance(hb_event, AutoDraft):
                    yield hb_event
                    return
                yield hb_event  # Heartbeat

            choice_b = session._pending_choice
            session._pending_choice = None
            if choice_b is None:
                yield await _terminal_error(
                    "roundtable_choice_timeout",
                    phase="B",
                    reason="user_choice_timeout",
                    detail="User did not respond before roundtable deadline at choice point B",
                )
                return
            if choice_b.user_input:
                user_inputs.append(choice_b.user_input)
                session.user_inputs = list(user_inputs)
            if choice_b.action == "inject" and choice_b.user_input:
                extra_prefs = self._extract_preferences(choice_b.user_input, dispute_map)
                user_preferences.extend(extra_prefs)
                session.user_preferences = list(user_preferences)

            if choice_b.action == "deepen":
                await session.transition(SessionState.AWAITING_B, SessionState.DEBATING, "s3_round2_start")
                session.choice_point = ""
                session.debate_round = 2
                async for s3_event in self._run_debate_round(
                    question, successful, dispute_map, user_inputs,
                    round_num=2, session_id=session_id, cfg=cfg,
                ):
                    if isinstance(s3_event, DebateStarted):
                        session.debate_round = s3_event.round_num
                    elif isinstance(s3_event, RebuttalDone):
                        session.rebuttals.append(s3_event.rebuttal)
                    if isinstance(s3_event, DebateComplete) and hasattr(s3_event, "_rebuttals"):
                        all_rebuttals.append(s3_event._rebuttals)
                    yield s3_event

            flat_rebuttals = [r for rnd in all_rebuttals for r in rnd]

            # ── S4: Decision packet ──────────────────────────────────
            await session.transition(
                SessionState.DEBATING if choice_b.action == "deepen" else SessionState.AWAITING_B,
                SessionState.DRAFTING,
                "s4_start",
            )
            session.choice_point = ""
            eligibility = self._check_recommendation_eligibility(
                successful, dispute_map, flat_rebuttals, user_preferences
            )
            try:
                packet = await asyncio.wait_for(
                    self._build_decision_packet(
                        question, successful, dispute_map, user_inputs,
                        session_id, rebuttals=flat_rebuttals,
                        user_preferences=user_preferences, eligibility=eligibility,
                        interactive=cfg.interactive,
                    ),
                    timeout=cfg.moderator_s4_timeout_s,
                )
            except RoundtablePhaseError as exc:
                yield await _terminal_error(
                    exc.code,
                    phase=exc.phase,
                    reason=exc.reason,
                    detail=exc.detail,
                )
                return
            except asyncio.TimeoutError:
                logger.warning(
                    f"[roundtable:{session_id}] S4 fallback triggered after "
                    f"{cfg.moderator_s4_timeout_s}s moderator timeout"
                )
                packet = self._build_fallback_decision_packet(
                    successful,
                    dispute_map,
                    reason="s4_moderator_timeout",
                    interactive=cfg.interactive,
                    language=language,
                )
            total_ms = int((time.monotonic() - start) * 1000)
            packet.total_latency_ms = total_ms
            packet.estimated_cost_usd = self._estimate_cost(opinions)

            result = RoundtableResult(
                session_id=session_id, question=question,
                experts=opinions, dispute_map=dispute_map,
                rebuttals=all_rebuttals,
                decision_packet=packet,
                rounds_completed=3 if choice_b.action == "deepen" else 2,
                user_inputs=user_inputs,
                dimension_mapping=self._build_dimension_mapping(dispute_map),
            )
            await session.transition(SessionState.DRAFTING, SessionState.COMPLETE, "complete")
            await self._store_state(session_id, "complete")
            _schedule_cleanup(session_id)
            yield RoundtableComplete(result)

        except asyncio.TimeoutError:
            yield await _terminal_error(
                "roundtable_total_timeout",
                phase=session.state.value,
                reason="pipeline_timeout",
                detail=f"Roundtable pipeline exceeded {cfg.total_timeout_s}s",
            )
        except Exception as exc:
            logger.error(f"[roundtable:{session_id}] Error: {exc}", exc_info=True)
            yield await _terminal_error(
                "roundtable_processing_error",
                phase=session.state.value,
                reason="unhandled_exception",
                detail=str(exc),
            )

    # ── User choice waiting with heartbeat + auto-draft ───────────────

    async def _await_choice_stream(
        self,
        session: RoundtableSession,
        cfg: RoundtableConfig,
        choice_point: str,
        question: str,
        opinions: list,
        dispute_map: object,
    ) -> AsyncIterator[RoundtableEvent]:
        """Async generator: yields Heartbeat events every HEARTBEAT_INTERVAL_S.

        Terminates by yielding either:
          - nothing more (caller reads UserChoice from session._choice_queue)
          - AutoDraft event when 5-min idle fires (session → auto_draft_sent)
        Caller must check session._choice_queue after this generator ends.
        """
        awaiting_state = (
            SessionState.AWAITING_A if choice_point == "A" else SessionState.AWAITING_B
        )
        deadline = time.monotonic() + cfg.total_timeout_s

        while True:
            # Single-primitive wait: queue.get() is atomic, no separate Event needed.
            # Choice is stored in session._pending_choice so execute_streaming can read
            # it directly — no second queue.get() required, eliminating all race windows.
            try:
                choice = await asyncio.wait_for(
                    session._choice_queue.get(),
                    timeout=HEARTBEAT_INTERVAL_S,
                )
                if choice.choice_point == choice_point:
                    session._pending_choice = choice
                    return
                # Stale or mismatched choice (e.g. duplicate A-point click arriving at B-point).
                # Discard and keep waiting — do not put back to avoid re-consuming.
                logger.warning(
                    f"[Session {session.session_id}] Discarding stale choice: "
                    f"expected={choice_point}, got={choice.choice_point}, "
                    f"action={choice.action}, idem={choice.idempotency_key}"
                )
            except asyncio.TimeoutError:
                pass

            # Auto-draft check (must be before deadline check so it fires before hard timeout)
            if session.should_auto_draft():
                logger.info(
                    f"[Session {session.session_id}] Auto-draft triggered at {choice_point}"
                )
                session._pre_draft_state = awaiting_state
                session._auto_draft_sent = True
                try:
                    await session.transition(
                        awaiting_state, SessionState.AUTO_DRAFTING, "auto_draft"
                    )
                    packet = await asyncio.wait_for(
                        self._build_decision_packet(
                            question,
                            [o for o in opinions if o.success],
                            dispute_map,
                            [],
                            session.session_id,
                        ),
                        timeout=cfg.moderator_s4_timeout_s,
                    )
                    await session.transition(
                        SessionState.AUTO_DRAFTING, SessionState.AUTO_DRAFT_SENT, "auto_draft_sent"
                    )
                    packet.degraded = True
                    packet.degradation_reason = "auto_draft_timeout"
                    session.auto_draft_packet = packet
                    _schedule_session_expiry(session)  # P1-B: kick off background 2h expiry
                    yield AutoDraft(packet)
                except Exception as e:
                    logger.warning(
                        f"[Session {session.session_id}] Auto-draft build failed: {e}"
                    )
                    await session.force_state(SessionState.AUTO_DRAFT_SENT, "auto_draft_failed")
                    fallback = DecisionPacket(
                        final_summary=_locale_text(
                            session.language,
                            "自动草案生成失败，请重新选择。",
                            "The automatic draft could not be generated. Please choose again.",
                        ),
                        recommended_action="",
                        degraded=True,
                        degradation_reason="auto_draft_build_failed",
                    )
                    session.auto_draft_packet = fallback
                    _schedule_session_expiry(session)  # P1-B: kick off background 2h expiry
                    yield AutoDraft(fallback)
                return

            # Hard deadline exceeded
            if time.monotonic() > deadline:
                logger.warning(
                    f"[Session {session.session_id}] Hard deadline exceeded in await_choice"
                )
                return

            # Emit heartbeat ping
            yield Heartbeat()

    # ── S1: Fan-out expert opinions ──────────────────────────────────────

    async def _fan_out_experts_stream(
        self,
        question: str,
        experts: list[tuple[str, dict]],
        timeout_s: int,
        session_id: str,
        deadline: float,
    ) -> AsyncIterator[ExpertOpinion]:
        """Fan out to experts, yield each result as it completes (as_completed)."""
        language = getattr(get_session(session_id), "language", "zh-CN")

        async def _call_one(model_id: str, meta: dict) -> ExpertOpinion:
            expert_start = time.monotonic()
            remaining = max(deadline - expert_start, 1.0)
            actual_timeout = min(timeout_s, remaining)
            label = _expert_label(model_id, language)
            localized_meta = {**meta, "label": label}
            try:
                prompt = self._build_expert_prompt(question, localized_meta, language=language)
                # Enable web_search for models that support it
                enable_search = self._model_supports_search(model_id)
                response = await asyncio.wait_for(
                    self._call_model(
                        model_id=model_id,
                        system_prompt=prompt["system"],
                        user_prompt=prompt["user"],
                        timeout_s=int(actual_timeout),
                        web_search=enable_search,
                        language=language,
                    ),
                    timeout=actual_timeout,
                )
                latency = int((time.monotonic() - expert_start) * 1000)
                raw = response.content if hasattr(response, "content") else str(response)
                return self._parse_expert_opinion(model_id, label, raw, latency)
            except Exception as e:
                latency = int((time.monotonic() - expert_start) * 1000)
                logger.warning(f"[roundtable:{session_id}] Expert {model_id} failed: {e}")
                return ExpertOpinion(
                    model_id=model_id, label=label,
                    stance="", confidence=0.0,
                    raw_response="", latency_ms=latency,
                    success=False, error=str(e),
                )

        tasks = {asyncio.ensure_future(_call_one(mid, meta)): (mid, meta)
                 for mid, meta in experts}
        for coro in asyncio.as_completed(list(tasks)):
            yield await coro

    # ── S2: Dispute mapping ──────────────────────────────────────────────

    async def _map_disputes(
        self,
        question: str,
        opinions: list[ExpertOpinion],
        session_id: str,
    ) -> DisputeMap:
        """Moderator builds structured DisputeMap from expert opinions (v2.2.2)."""
        language = getattr(get_session(session_id), "language", "zh-CN")
        degraded = [o for o in opinions if not o.success]
        degraded_note = (
            _locale_text(
                language,
                f"\n注：{len(degraded)}位专家未响应: {[d.label for d in degraded]}。请基于已有回答建图。",
                f"\nNote: {len(degraded)} experts did not respond: {[d.label for d in degraded]}. Build the dispute map from the available responses.",
            )
            if degraded else ""
        )
        experts_unstructured = [o for o in opinions if not o.structured]
        unstructured_note = (
            _locale_text(
                language,
                f"\n注：以下专家回答未结构化，内容较难对比：{[u.label for u in experts_unstructured]}",
                f"\nNote: The following expert responses were not structured and are harder to compare: {[u.label for u in experts_unstructured]}",
            )
            if experts_unstructured else ""
        )
        expert_json = json.dumps(
            [
                {
                    "label": o.label,
                    "stance": o.stance,
                    "confidence": o.confidence,
                    "claims": [
                        {"point": c.point}
                        for c in o.claims[:2]
                    ],
                }
                for o in opinions if o.success
            ],
            ensure_ascii=False,
        )

        system_prompt = _locale_text(
            language,
            "中立主持人。分析专家立场，建立争议地图。\n"
            "⛔ 不许：表达倾向、判断谁对、用‘显然/当然’\n"
            "✅ 只做：提取维度、识别分歧共识、标注类型(可混合)、分配 dimension_id、生成引导问题"
            + degraded_note + unstructured_note + "\n\n"
            "输出严格JSON（不要任何其他内容）：\n"
            '{\n'
            '  "contention_points": [\n'
            '    {\n'
            '      "topic": "争议主题",\n'
            '      "severity": "high/medium/low",\n'
            '      "dispute_type": ["factual"] or ["factual","value"],\n'
            '      "dimension_id": "snake_case英文",\n'
            '      "dimension_label": "维度中文名",\n'
            '      "sides": [\n'
            '        {"position": "立场", "lead_expert": "专家标签", "supporting_claims": ["论据摘要"]}\n'
            '      ],\n'
            '      "suggested_focus": true\n'
            '    }\n'
            '  ],\n'
            '  "consensus_points": [\n'
            '    {"point": "内容", "strength": "strong/moderate/weak", "agreed_by": ["标签"]}\n'
            '  ],\n'
            '  "suggested_focus": "建议深入的争议主题"\n'
            '}\n\n'
            "规则：contention_points按severity降序，最多1个 suggested_focus=true",
            "You are a neutral moderator. Analyze the expert stances and build a dispute map.\n"
            "⛔ Do not: show preference, decide who is right, or use language like 'obviously' or 'of course'.\n"
            "✅ Only: extract dimensions, identify disagreements and consensus, tag dispute types (possibly mixed), assign dimension_id values, and generate follow-up focus points."
            + degraded_note + unstructured_note + "\n\n"
            "Return strict JSON only:\n"
            '{\n'
            '  "contention_points": [\n'
            '    {\n'
            '      "topic": "dispute topic",\n'
            '      "severity": "high/medium/low",\n'
            '      "dispute_type": ["factual"] or ["factual","value"],\n'
            '      "dimension_id": "snake_case_english",\n'
            '      "dimension_label": "display label",\n'
            '      "sides": [\n'
            '        {"position": "stance", "lead_expert": "expert label", "supporting_claims": ["claim summary"]}\n'
            '      ],\n'
            '      "suggested_focus": true\n'
            '    }\n'
            '  ],\n'
            '  "consensus_points": [\n'
            '    {"point": "content", "strength": "strong/moderate/weak", "agreed_by": ["labels"]}\n'
            '  ],\n'
            '  "suggested_focus": "the dispute topic most worth deepening"\n'
            '}\n\n'
            "Rule: sort contention_points by severity descending, and allow at most one suggested_focus=true.",
        )
        user_prompt = _locale_text(
            language,
            f"原始问题：{question}\n\n"
            f"各专家立场（JSON）：\n{expert_json}",
            f"Original question: {question}\n\n"
            f"Expert stances (JSON):\n{expert_json}",
        )
        try:
            response = await self._call_model(
                model_id=_get_moderator_model(self._config),
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                timeout_s=MODERATOR_S2_TIMEOUT_S,
                language=language,
            )
            if not response.success:
                error_text = response.error or "moderator model returned unsuccessful response"
                code, reason = _classify_upstream_failure("s2", error_text)
                raise RoundtablePhaseError(
                    code,
                    phase="S2",
                    reason=reason,
                    detail=error_text,
                )
            raw = response.content if hasattr(response, "content") else str(response)
            data = _parse_json_robust(raw)
            if not data:
                raise RoundtablePhaseError(
                    "roundtable_s2_invalid_response",
                    phase="S2",
                    reason="moderator_invalid_json",
                    detail="Moderator returned empty or non-JSON dispute map output",
                )
            return self._build_dispute_map(data, language=language)
        except RoundtablePhaseError:
            raise
        except Exception as e:
            logger.warning(f"[roundtable:{session_id}] DisputeMap failed: {e}")
            raise

    def _build_dispute_map(self, data: dict, *, language: str = "zh-CN") -> DisputeMap:
        contention_points: list[ContentionPoint] = []
        for cp in data.get("contention_points", []):
            sides = [
                ContentionSide(
                    position=s.get("position", ""),
                    supporting_claims=s.get("supporting_claims", []),
                    lead_expert=s.get("lead_expert", ""),
                    main_argument=s.get("main_argument", ""),
                )
                for s in cp.get("sides", [])
            ]
            dt_raw = cp.get("dispute_type", ["factual"])
            dispute_type = dt_raw if isinstance(dt_raw, list) else [dt_raw]
            contention_points.append(ContentionPoint(
                topic=cp.get("topic", ""),
                severity=cp.get("severity", "medium"),
                dispute_type=dispute_type,
                factual_aspect=cp.get("factual_aspect", ""),
                value_aspect=cp.get("value_aspect", ""),
                dimension_id=cp.get("dimension_id", ""),
                dimension_label=cp.get("dimension_label", ""),
                dimension_aliases=cp.get("dimension_aliases") or [],
                adjudication_note=cp.get("adjudication_note", ""),
                sides=sides,
                why_it_matters=cp.get("why_it_matters", ""),
                suggested_focus=bool(cp.get("suggested_focus", False)),
            ))

        consensus_points: list[ConsensusPoint] = []
        for cp in data.get("consensus_points", []):
            consensus_points.append(ConsensusPoint(
                point=cp.get("point", ""),
                strength=cp.get("strength", "moderate"),
                agreed_by=cp.get("agreed_by", []),
            ))

        total_points = len(contention_points) + len(consensus_points)
        consensus_ratio = len(consensus_points) / max(total_points, 1)
        echo_warning = data.get("echo_chamber_warning", "")
        if not echo_warning and consensus_ratio >= 0.8 and total_points >= 3:
            echo_warning = _locale_text(
                language,
                "❗ 注意：各专家观点高度一致（回声室风险），建议忽视分歧点再次确认。",
                "❗ Note: the experts are highly aligned (echo-chamber risk). Re-check the weak disagreement before treating it as settled.",
            )

        return DisputeMap(
            synthesized_dimensions=data.get("synthesized_dimensions", []),
            dimension_sources=data.get("dimension_sources", {}),
            contention_points=contention_points,
            consensus_points=consensus_points,
            suggested_focus=data.get("suggested_focus", ""),
            echo_chamber_warning=echo_warning,
            clarifying_questions=data.get("clarifying_questions", [])[:3],
            degraded=bool(data.get("degraded", False)),
        )

    def _build_fallback_dispute_map(
        self, opinions: list[ExpertOpinion], *, language: str = "zh-CN",
    ) -> DisputeMap:
        """Fallback S2 map when moderator timeout prevents structured dispute mapping."""
        support_keywords = (
            "支持", "赞成", "应该", "应当", "值得", "建议", "看好", "可投",
            "support", "agree", "recommend", "worth", "should", "favorable", "bullish",
        )
        against_keywords = (
            "反对", "不建议", "不该", "不应该", "不宜", "风险", "谨慎", "不要", "不值得",
            "oppose", "against", "not recommend", "risk", "cautious", "avoid", "shouldn't", "bearish",
        )

        def _bucket(opinion: ExpertOpinion) -> str:
            stance = opinion.stance or ""
            has_support = any(keyword in stance for keyword in support_keywords)
            has_against = any(keyword in stance for keyword in against_keywords)
            if has_support and not has_against:
                return "support"
            if has_against and not has_support:
                return "against"
            return "neutral"

        groups: dict[str, list[ExpertOpinion]] = {"support": [], "against": [], "neutral": []}
        for opinion in opinions:
            groups[_bucket(opinion)].append(opinion)

        contention_points: list[ContentionPoint] = []
        if groups["support"] and groups["against"]:
            sides: list[ContentionSide] = []
            for bucket, label in (
                ("support", _locale_text(language, "支持", "Support")),
                ("against", _locale_text(language, "谨慎/反对", "Caution / oppose")),
            ):
                lead = groups[bucket][0]
                supporting_claims = [claim.point for claim in lead.claims[:2] if claim.point]
                if not supporting_claims and lead.stance:
                    supporting_claims = [lead.stance]
                sides.append(
                    ContentionSide(
                        position=label,
                        supporting_claims=supporting_claims[:2],
                        lead_expert=lead.label,
                    )
                )
            contention_points.append(
                ContentionPoint(
                    topic=_locale_text(
                        language,
                        "当前问题正反立场分歧",
                        "Competing positions on the core question",
                    ),
                    severity="high",
                    dispute_type=["value"],
                    dimension_id="overall_position",
                    dimension_label=_locale_text(language, "整体立场", "Overall stance"),
                    sides=sides,
                    suggested_focus=True,
                )
            )

        consensus_points: list[ConsensusPoint] = []
        if len(groups["support"]) >= 2:
            consensus_points.append(
                ConsensusPoint(
                    point=_locale_text(
                        language,
                        "多位专家倾向支持当前方案",
                        "Multiple experts lean toward supporting the current option",
                    ),
                    strength="moderate",
                    agreed_by=[op.label for op in groups["support"]],
                )
            )
        if len(groups["against"]) >= 2:
            consensus_points.append(
                ConsensusPoint(
                    point=_locale_text(
                        language,
                        "多位专家倾向谨慎或反对当前方案",
                        "Multiple experts lean toward caution or opposition",
                    ),
                    strength="moderate",
                    agreed_by=[op.label for op in groups["against"]],
                )
            )
        if not consensus_points and groups["neutral"]:
            consensus_points.append(
                ConsensusPoint(
                    point=_locale_text(
                        language,
                        "部分专家态度偏中性，建议结合后续选择继续澄清",
                        "Some experts remain neutral, so the next decision step should clarify the trade-off.",
                    ),
                    strength="weak",
                    agreed_by=[op.label for op in groups["neutral"]],
                )
            )

        return DisputeMap(
            contention_points=contention_points,
            consensus_points=consensus_points,
            suggested_focus=contention_points[0].topic if contention_points else "",
            degraded=True,
        )

    # ── S3: Debate round ──────────────────────────────────────────

    async def _run_debate_round(
        self,
        question: str,
        opinions: list[ExpertOpinion],
        dispute_map: DisputeMap,
        user_inputs: list[str],
        round_num: int,
        session_id: str,
        cfg: RoundtableConfig,
    ) -> AsyncIterator[RoundtableEvent]:
        """Run one S3 debate round: main debater + reviewers."""
        language = getattr(get_session(session_id), "language", "zh-CN")
        # Identify the focus dispute
        focus = next(
            (cp for cp in dispute_map.contention_points if cp.suggested_focus),
            dispute_map.contention_points[0] if dispute_map.contention_points else None,
        )
        focus_topic = focus.topic if focus else _locale_text(
            language,
            "所有高严重度分歧",
            "all high-severity disputes",
        )

        # Role assignment: lead_expert of EACH side of the focus dispute becomes a main_debater.
        # This ensures opposing positions both get deep (~600-word) responses.
        lead_labels: set[str] = set()
        if focus and focus.sides:
            for side in focus.sides:
                if side.lead_expert:
                    lead_labels.add(side.lead_expert)
        assignments: dict[str, str] = {}
        for o in opinions:
            assignments[o.model_id] = "main_debater" if o.label in lead_labels else "reviewer"
        # Fallback: if label matching produced no main_debaters, assign first two experts
        if "main_debater" not in assignments.values() and len(opinions) >= 2:
            assignments[opinions[0].model_id] = "main_debater"
            assignments[opinions[1].model_id] = "main_debater"
        elif "main_debater" not in assignments.values() and opinions:
            assignments[opinions[0].model_id] = "main_debater"

        yield DebateStarted(round_num=round_num, assignments=assignments)

        deadline = time.monotonic() + cfg.total_timeout_s

        async def _call_rebuttal(model_id: str, meta_role: str, opinion: ExpertOpinion) -> Rebuttal:
            start_t = time.monotonic()
            timeout = cfg.main_debater_timeout_s if meta_role == "main_debater" else cfg.reviewer_timeout_s
            try:
                prompt = self._build_rebuttal_prompt(
                    question, opinion, opinions, dispute_map, focus_topic,
                    meta_role, user_inputs, language=language,
                )
                response = await asyncio.wait_for(
                    self._call_model(
                        model_id=model_id,
                        system_prompt=prompt["system"],
                        user_prompt=prompt["user"],
                        timeout_s=timeout,
                        language=language,
                    ),
                    timeout=min(timeout, max(deadline - time.monotonic(), 5.0)),
                )
                latency = int((time.monotonic() - start_t) * 1000)
                raw = response.content if hasattr(response, "content") else str(response)
                return self._parse_rebuttal(model_id, opinion.label, meta_role, raw, latency)
            except Exception as e:
                latency = int((time.monotonic() - start_t) * 1000)
                logger.warning(f"[roundtable:{session_id}] Rebuttal {model_id} failed: {e}")
                return Rebuttal(
                    model_id=model_id, label=opinion.label, role=meta_role,
                    success=False, latency_ms=latency,
                )

        tasks = {
            asyncio.ensure_future(_call_rebuttal(o.model_id, assignments.get(o.model_id, "reviewer"), o)): o
            for o in opinions
        }
        rebuttals: list[Rebuttal] = []
        done_count = 0
        for coro in asyncio.as_completed(list(tasks)):
            reb = await coro
            rebuttals.append(reb)
            done_count += 1
            yield RebuttalDone(reb, done_count, len(opinions))

        stance_changes = [
            {"expert": r.label, "changed": r.stance_changed, "revised_stance": r.revised_stance}
            for r in rebuttals if r.stance_changed
        ]
        ev = DebateComplete(round_num=round_num, stance_changes=stance_changes)
        ev._rebuttals = rebuttals  # type: ignore[attr-defined]
        yield ev

    def _build_rebuttal_prompt(self, question: str, opinion: ExpertOpinion,
                                all_opinions: list[ExpertOpinion], dispute_map: DisputeMap,
                                focus_topic: str, role: str,
                                user_inputs: list[str], language: str = "zh-CN") -> dict[str, str]:
        """Build S3 rebuttal prompt for a single expert."""
        opponent_claims = ""
        for o in all_opinions:
            if o.model_id != opinion.model_id and o.success:
                opponent_claims += f"【{o.label}】{o.stance}\n"

        other_summaries = "; ".join(
            f"{o.label}: {o.stance[:60]}"
            for o in all_opinions
            if o.model_id != opinion.model_id and o.success
        )
        user_ctx = _locale_text(
            language,
            f"\n用户补充：{'; '.join(user_inputs)}",
            f"\nUser context: {'; '.join(user_inputs)}",
        ) if user_inputs else ""

        if role == "main_debater":
            sys = _locale_text(
                language,
                f"你是「{opinion.label}」（主辩）。\n"
                f"你的R1立场：{opinion.stance}\n"
                f"最强反对方：\n{opponent_claims}\n"
                f"其他专家摘要：{other_summaries}"
                + user_ctx + "\n\n"
                "压力测试你的立场：\n"
                "- 致命弱点必须坦诚承认\n"
                "- 禁止‘部分同意但仍认为’模板\n"
                "- 如被分配立场不代表你的观点，先声明实际立场\n"
                "输出严格JSON：\n"
                '{ "target_dispute": "争议主题",\n'
                '  "response_type": "rebut/concede/revise",\n'
                '  "response": "回应(≤ 600字)",\n'
                '  "new_evidence": "新证据",\n'
                '  "revised_stance": "修改后立场(若没变化则重复原立场)",\n'
                '  "stance_changed": false,\n'
                '  "confidence": 0.7 }',
                f"You are \"{opinion.label}\" (main debater).\n"
                f"Your round-1 stance: {opinion.stance}\n"
                f"Strongest opposing side:\n{opponent_claims}\n"
                f"Other expert summaries: {other_summaries}"
                + user_ctx + "\n\n"
                "Stress-test your stance:\n"
                "- Acknowledge the most damaging weakness honestly.\n"
                "- Do not use a vague 'partly agree but still think' template.\n"
                "- If the assigned stance does not actually match your view, state your real stance first.\n"
                "Return strict JSON:\n"
                '{ "target_dispute": "dispute topic",\n'
                '  "response_type": "rebut/concede/revise",\n'
                '  "response": "response (<= 600 words)",\n'
                '  "new_evidence": "new evidence",\n'
                '  "revised_stance": "updated stance (repeat the original stance if unchanged)",\n'
                '  "stance_changed": false,\n'
                '  "confidence": 0.7 }',
            )
        else:
            sys = _locale_text(
                language,
                f"你是「{opinion.label}」（评审）。争议焦点：{focus_topic}\n"
                f"主辩立场：{opponent_claims}"
                "简评≤ 100字。\n"
                "输出严格JSON（格式同主辩）：\n"
                '{ "target_dispute": "", "response_type": "rebut/concede/revise",\n'
                '  "response": "简评", "new_evidence": "",\n'
                '  "revised_stance": "", "stance_changed": false, "confidence": 0.7 }',
                f"You are \"{opinion.label}\" (reviewer). Focus dispute: {focus_topic}\n"
                f"Main debater stance: {opponent_claims}"
                "Give a concise review in <= 100 words.\n"
                "Return strict JSON (same schema as the main debater):\n"
                '{ "target_dispute": "", "response_type": "rebut/concede/revise",\n'
                '  "response": "short review", "new_evidence": "",\n'
                '  "revised_stance": "", "stance_changed": false, "confidence": 0.7 }',
            )
        return {
            "system": sys,
            "user": _locale_text(language, f"决策问题：{question}", f"Decision question: {question}"),
        }

    def _parse_rebuttal(
        self, model_id: str, label: str, role: str, raw: str, latency_ms: int
    ) -> Rebuttal:
        data = _parse_json_robust(raw)
        if data:
            return Rebuttal(
                model_id=model_id, label=label, role=role,
                target_dispute=data.get("target_dispute", ""),
                response_type=data.get("response_type", "rebut"),
                response=data.get("response", ""),
                new_evidence=data.get("new_evidence", ""),
                revised_stance=data.get("revised_stance", ""),
                stance_changed=bool(data.get("stance_changed", False)),
                confidence=float(data.get("confidence", 0.5)),
                raw_response=raw, structured=True, latency_ms=latency_ms,
            )
        return Rebuttal(
            model_id=model_id, label=label, role=role,
            response=raw[:600], raw_response=raw, structured=False,
            latency_ms=latency_ms,
        )

    # ── Recommendation eligibility matrix ──────────────────────────

    def _check_recommendation_eligibility(
        self,
        opinions: list[ExpertOpinion],
        dispute_map: DisputeMap,
        rebuttals: list[Rebuttal],
        user_preferences: list[UserPreference],
    ) -> dict:
        """Code-driven recommendation eligibility. Returns checklist dict for S4 prompt."""
        high_factual = [
            cp for cp in dispute_map.contention_points
            if cp.severity == "high" and "factual" in cp.dispute_type
        ]
        high_value = [
            cp for cp in dispute_map.contention_points
            if cp.severity == "high" and "value" in cp.dispute_type
        ]

        # R1: all high-severity factual disputes responded to in S3
        rebuttal_targets = {r.target_dispute for r in rebuttals if r.success}
        r1_uncovered = [cp.topic for cp in high_factual if cp.topic not in rebuttal_targets]
        r1_ok = len(r1_uncovered) == 0

        # R2: all value disputes have user preference with confidence >= threshold
        pref_map = {p.dimension_id: p for p in user_preferences}
        r2_missing = [
            cp.dimension_id for cp in high_value
            if not cp.dimension_id
            or cp.dimension_id not in pref_map
            or pref_map[cp.dimension_id].confidence < 0.7
        ]
        r2_ok = len(r2_missing) == 0

        # R3: no high-severity unresolved — checked by S4 LLM (can't pre-check)
        r3_ok = True  # optimistic; LLM fills unresolved list

        # R4: enough successful experts
        r4_ok = len([o for o in opinions if o.success]) >= MIN_SUCCESSFUL_EXPERTS

        if r1_ok and r2_ok and r3_ok and r4_ok:
            conclusion_type = "recommendation"
        elif r4_ok is False:
            conclusion_type = "draft"
        else:
            conclusion_type = "conditional"

        return {
            "conclusion_type": conclusion_type,
            "r1_ok": r1_ok, "r1_uncovered": r1_uncovered,
            "r2_ok": r2_ok, "r2_missing_dimensions": r2_missing,
            "r3_ok": r3_ok, "r4_ok": r4_ok,
        }

    # ── User preference extraction ──────────────────────────────

    def _extract_preferences(
        self,
        user_input: str,
        dispute_map: DisputeMap,
    ) -> list[UserPreference]:
        """Layer 1 (rules): alias matching for value dimension preferences."""
        results: list[UserPreference] = []
        lower_input = user_input.lower()
        for cp in dispute_map.contention_points:
            if "value" not in cp.dispute_type or not cp.dimension_id:
                continue
            # Alias matching
            for alias in [cp.dimension_label] + cp.dimension_aliases:
                if alias and alias.lower() in lower_input:
                    results.append(UserPreference(
                        dimension_id=cp.dimension_id,
                        preference=user_input[:200],
                        confidence=0.9,
                        source="explicit",
                    ))
                    break
        return results

    # ── Dimension mapping ───────────────────────────────────────

    def _build_dimension_mapping(self, dispute_map: DisputeMap) -> list[Dimension]:
        """Extract normalized Dimension objects from DisputeMap."""
        dims: dict[str, Dimension] = {}
        for cp in dispute_map.contention_points:
            if cp.dimension_id and cp.dimension_id not in dims:
                dims[cp.dimension_id] = Dimension(
                    id=cp.dimension_id,
                    label=cp.dimension_label or cp.dimension_id,
                    aliases=cp.dimension_aliases,
                )
        return list(dims.values())

    # ── S4: Decision packet ────────────────────────────────────────

    async def _build_decision_packet(
        self,
        question: str,
        opinions: list[ExpertOpinion],
        dispute_map: DisputeMap,
        user_inputs: list[str],
        session_id: str,
        rebuttals: list[Rebuttal] = None,
        user_preferences: list[UserPreference] = None,
        eligibility: dict = None,
        interactive: bool = True,
    ) -> DecisionPacket:
        """Moderator outputs the final Decision Packet (v2.2.2)."""
        language = getattr(get_session(session_id), "language", "zh-CN")
        rebuttals = rebuttals or []
        user_preferences = user_preferences or []
        eligibility = eligibility or {"conclusion_type": "draft"}

        expert_summary = json.dumps(
            [
                {
                    "label": o.label,
                    "stance": o.stance,
                    "confidence": o.confidence,
                    "claims": [c.point for c in o.claims[:2] if c.point],
                }
                for o in opinions
            ],
            ensure_ascii=False,
        )
        rebuttal_summary = json.dumps(
            [
                {
                    "label": r.label, "role": r.role,
                    "response_type": r.response_type,
                    "response": r.response[:160],
                    "stance_changed": r.stance_changed,
                    "revised_stance": r.revised_stance,
                }
                for r in rebuttals if r.success
            ],
            ensure_ascii=False,
        ) if rebuttals else "[]"
        pref_summary = json.dumps(
            [{
                "dimension_id": p.dimension_id,
                "preference": p.preference,
                "confidence": p.confidence,
                "source": p.source,
            } for p in user_preferences],
            ensure_ascii=False,
        ) if user_preferences else "[]"
        dispute_summary = json.dumps(
            {
                "contention_points": [
                    {
                        "topic": cp.topic,
                        "severity": cp.severity,
                        "dispute_type": cp.dispute_type,
                        "dimension_id": cp.dimension_id,
                        "dimension_label": cp.dimension_label,
                        "suggested_focus": cp.suggested_focus,
                        "sides": [
                            {"position": s.position, "lead_expert": s.lead_expert,
                             "supporting_claims": s.supporting_claims[:2]}
                            for s in cp.sides
                        ],
                    }
                    for cp in dispute_map.contention_points
                ],
                "consensus_points": [
                    {"point": cp.point, "strength": cp.strength, "agreed_by": cp.agreed_by}
                    for cp in dispute_map.consensus_points
                ],
            },
            ensure_ascii=False,
        )
        user_ctx = _locale_text(
            language,
            f"\n用户补充：{'; '.join(user_inputs)}",
            f"\nUser context: {'; '.join(user_inputs)}",
        ) if user_inputs else ""
        eligibility_checklist = json.dumps(eligibility, ensure_ascii=False)
        degraded = len([o for o in opinions if o.success]) < MIN_SUCCESSFUL_EXPERTS
        degraded_note = (
            _locale_text(
                language,
                f"\n❗ 降级模式：仅{len([o for o in opinions if o.success])}位专家有效回应，强制输出 draft。",
                f"\n❗ Degraded mode: only {len([o for o in opinions if o.success])} experts responded successfully, so the output must stay at draft level.",
            )
            if degraded else ""
        )

        system_prompt = _locale_text(
            language,
            "裁决主持人。\n"
            "铁律：\n"
            "1. conclusion_type遵循检查表\n"
            "2. 事实可裁决，价値不可裁决\n"
            "3. 混合分歧先事实后价値\n"
            "4. what_changes_my_mind必须可测试\n"
            "5. recommended_action是约束匹配，不是价値判断\n"
            + degraded_note + "\n"
            "输出严格JSON：\n"
            '{ "conclusion_type": "recommendation/conditional/draft",\n'
            '  "confidence_basis": "为什么是这个类型",\n'
            '  "final_summary": "3-5句",\n'
            '  "stance_evolution": [{"expert":"", "r1_stance":"", "final_stance":"",\n'
            '    "changed":false, "changed_reason":""}],\n'
            '  "options": [{"choice":"", "pros":[], "cons":[], "best_when":"",\n'
            '    "risk":"", "mitigation":""}],\n'
            '  "unresolved": [{"point":"", "reason":"", "how_to_resolve":""}],\n'
            '  "what_changes_my_mind": "",\n'
            '  "recommended_action": "",\n'
            '  "value_disputes_to_user": [{"point":"", "dimension_id":"",\n'
            '    "ask_user":"请用户回答的问题"}] }',
            "You are the final moderator.\n"
            "Hard rules:\n"
            "1. conclusion_type must follow the eligibility checklist.\n"
            "2. Facts can be adjudicated; values cannot.\n"
            "3. For mixed disputes, resolve the factual part before the value part.\n"
            "4. what_changes_my_mind must be concrete and testable.\n"
            "5. recommended_action should match the constraints, not impose a value judgment.\n"
            + degraded_note + "\n"
            "Return strict JSON:\n"
            '{ "conclusion_type": "recommendation/conditional/draft",\n'
            '  "confidence_basis": "why this conclusion_type is justified",\n'
            '  "final_summary": "3-5 sentences",\n'
            '  "stance_evolution": [{"expert":"", "r1_stance":"", "final_stance":"",\n'
            '    "changed":false, "changed_reason":""}],\n'
            '  "options": [{"choice":"", "pros":[], "cons":[], "best_when":"",\n'
            '    "risk":"", "mitigation":""}],\n'
            '  "unresolved": [{"point":"", "reason":"", "how_to_resolve":""}],\n'
            '  "what_changes_my_mind": "",\n'
            '  "recommended_action": "",\n'
            '  "value_disputes_to_user": [{"point":"", "dimension_id":"",\n'
            '    "ask_user":"question for the user"}] }',
        )
        if interactive:
            user_prompt = _locale_text(
                language,
                f"## 推荐资格检查表\n{eligibility_checklist}\n\n"
                f"## 原始问题\n{question}{user_ctx}\n\n"
                f"## 专家立场\n{expert_summary}\n\n"
                f"## 争议地图\n{dispute_summary}\n\n"
                f"## S3交锋\n{rebuttal_summary}\n\n"
                f"## 用户偏好\n{pref_summary}",
                f"## Eligibility checklist\n{eligibility_checklist}\n\n"
                f"## Original question\n{question}{user_ctx}\n\n"
                f"## Expert stances\n{expert_summary}\n\n"
                f"## Dispute map\n{dispute_summary}\n\n"
                f"## S3 debate\n{rebuttal_summary}\n\n"
                f"## User preferences\n{pref_summary}",
            )
        else:
            user_prompt = _locale_text(
                language,
                f"## 综合方式\n基于专家独立意见的综合分析\n\n"
                f"## 推荐资格检查表\n{eligibility_checklist}\n\n"
                f"## 原始问题\n{question}{user_ctx}\n\n"
                f"## 专家立场\n{expert_summary}\n\n"
                f"## 争议地图\n{dispute_summary}",
                f"## Synthesis mode\nIndependent synthesis from expert opinions\n\n"
                f"## Eligibility checklist\n{eligibility_checklist}\n\n"
                f"## Original question\n{question}{user_ctx}\n\n"
                f"## Expert stances\n{expert_summary}\n\n"
                f"## Dispute map\n{dispute_summary}",
            )

        try:
            response = await self._call_model(
                model_id=_get_moderator_model(self._config),
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                timeout_s=MODERATOR_S4_TIMEOUT_S,
                language=language,
            )
            if not response.success:
                error_text = response.error or "moderator model returned unsuccessful response"
                code, reason = _classify_upstream_failure("s4", error_text)
                raise RoundtablePhaseError(
                    code,
                    phase="S4",
                    reason=reason,
                    detail=error_text,
                )
            raw = response.content if hasattr(response, "content") else str(response)
            data = _parse_json_robust(raw)
            if not data:
                raise RoundtablePhaseError(
                    "roundtable_s4_invalid_response",
                    phase="S4",
                    reason="moderator_invalid_json",
                    detail="Moderator returned empty or non-JSON decision packet output",
                )
            return self._build_decision_packet_from_data(
                data, eligibility, degraded,
                bool(dispute_map.echo_chamber_warning),
            )
        except RoundtablePhaseError:
            raise
        except Exception as e:
            logger.warning(f"[roundtable:{session_id}] DecisionPacket failed: {e}")
            raise

    def _build_fallback_decision_packet(
        self,
        opinions: list[ExpertOpinion],
        dispute_map: DisputeMap,
        *,
        reason: str,
        interactive: bool = True,
        language: str = "zh-CN",
    ) -> DecisionPacket:
        """Fallback S4 packet when moderator synthesis times out."""
        stance_evolution = [
            StanceEvolution(
                expert=op.label,
                r1_stance=op.stance,
                final_stance=op.stance,
                changed=False,
                changed_reason="",
            )
            for op in opinions
        ]
        unresolved = [
            UnresolvedItem(
                point=cp.topic,
                reason=_locale_text(
                    language,
                    "主持人未能在时限内完成决策综合",
                    "The moderator could not complete the decision synthesis in time.",
                ),
                how_to_resolve=(
                    _locale_text(
                        language,
                        "参考专家原始观点与争议焦点，必要时继续深入一轮。",
                        "Review the original expert views and dispute focus, then deepen the discussion if needed.",
                    )
                    if interactive
                    else _locale_text(
                        language,
                        "参考专家原始观点与争议焦点，按当前信息做出保守决策。",
                        "Review the original expert views and dispute focus, then make the most conservative decision allowed by the current information.",
                    )
                ),
            )
            for cp in dispute_map.contention_points[:3]
        ]
        value_disputes = [
            ValueDisputeForUser(
                point=cp.topic,
                dimension_id=cp.dimension_id,
                ask_user=_locale_text(
                    language,
                    f"你更看重「{cp.dimension_label or cp.topic}」的哪一侧？",
                    f"Which side of \"{cp.dimension_label or cp.topic}\" matters more to you?",
                ),
            )
            for cp in dispute_map.contention_points
            if "value" in cp.dispute_type
        ][:3]
        return DecisionPacket(
            final_summary=_locale_text(
                language,
                "主持人未能在时限内完成决策综合，以下为各专家原始观点及争议分析供参考",
                "The moderator could not finish the decision synthesis in time. The original expert views and dispute analysis are provided below for reference.",
            ),
            conclusion_type="draft",
            confidence_basis=_locale_text(
                language,
                "S4 主持人超时，已降级为草案输出",
                "The S4 moderator timed out, so the result has been downgraded to a draft.",
            ),
            stance_evolution=stance_evolution,
            unresolved=unresolved,
            recommended_action=(
                _locale_text(
                    language,
                    "先查看争议焦点与专家原始观点，再决定是否继续深入或补充偏好。",
                    "Review the dispute focus and the original expert views first, then decide whether to deepen the discussion or add preferences.",
                )
                if interactive
                else _locale_text(
                    language,
                    "先查看争议焦点与专家原始观点，再按当前信息选择更保守的行动。",
                    "Review the dispute focus and the original expert views first, then choose the more conservative action supported by the current information.",
                )
            ),
            value_disputes_to_user=value_disputes,
            echo_chamber_flag=bool(dispute_map.echo_chamber_warning),
            degraded=True,
            degradation_reason=reason,
        )

    def _build_decision_packet_from_data(
        self, data: dict, eligibility: dict, degraded: bool, echo_chamber_flag: bool,
    ) -> DecisionPacket:
        options: list[DecisionOption] = []
        for opt in data.get("options", []):
            options.append(DecisionOption(
                choice=opt.get("choice", ""),
                pros=opt.get("pros", []),
                cons=opt.get("cons", []),
                best_when=opt.get("best_when", ""),
                risk=opt.get("risk", ""),
                mitigation=opt.get("mitigation", ""),
            ))

        stance_evo: list[StanceEvolution] = []
        for s in data.get("stance_evolution", []):
            stance_evo.append(StanceEvolution(
                expert=s.get("expert", ""),
                r1_stance=s.get("r1_stance", ""),
                final_stance=s.get("final_stance", s.get("r2_stance", "")),
                changed=bool(s.get("changed", False)),
                changed_reason=s.get("changed_reason", ""),
            ))

        value_disputes: list[ValueDisputeForUser] = []
        for vd in data.get("value_disputes_to_user", []):
            value_disputes.append(ValueDisputeForUser(
                point=vd.get("point", ""),
                dimension_id=vd.get("dimension_id", ""),
                ask_user=vd.get("ask_user", ""),
            ))

        # Honor eligibility conclusion_type (code overrides LLM if needed)
        ct_code = eligibility.get("conclusion_type", "draft")
        ct_llm = data.get("conclusion_type", "draft")
        # LLM cannot upgrade; can only match or downgrade
        conclusion_type_order = {"recommendation": 2, "conditional": 1, "draft": 0}
        ct = ct_code if conclusion_type_order.get(ct_code, 0) <= conclusion_type_order.get(ct_llm, 0) else ct_llm

        unresolved: list[UnresolvedItem] = [
            UnresolvedItem(
                point=u.get("point", ""),
                reason=u.get("reason", ""),
                how_to_resolve=u.get("how_to_resolve", ""),
            )
            for u in data.get("unresolved", [])
        ]

        return DecisionPacket(
            final_summary=data.get("final_summary", ""),
            conclusion_type=ct,
            confidence_basis=data.get("confidence_basis", ""),
            stance_evolution=stance_evo,
            options=options,
            unresolved=unresolved,
            what_changes_my_mind=data.get("what_changes_my_mind", ""),
            recommended_action=data.get("recommended_action", ""),
            value_disputes_to_user=value_disputes,
            echo_chamber_flag=echo_chamber_flag,
            degraded=degraded,
            degradation_reason="insufficient_experts" if degraded else "",
        )

    # ── Prompt builders ──────────────────────────────────────────

    def _build_expert_prompt(
        self, question: str, meta: dict, language: str = "zh-CN",
    ) -> dict[str, str]:
        """Build S1 structured-output prompt for one expert (v2.2.2)."""
        style = _style_hint(meta.get("style", ""), language)
        return {
            "system": _locale_text(
                language,
                f"你是圆桌辩论中的「{meta['label']}」。{style}\n\n"
                "必须包含的基线维度：\n"
                "1. 对目标场景的适配性\n"
                "2. 实施成本与风险\n"
                "3. 长期可持续性\n"
                "可自由添加独特维度。\n\n"
                "输出严格JSON（不要任何其他内容）：\n"
                '{\n'
                '  "my_dimensions": ["维度1", "维度2"],\n'
                '  "stance": "你的明确立场（一句话，禁止各有道理）",\n'
                '  "confidence": 0.8,\n'
                '  "claims": [\n'
                '    {"point": "论据", "evidence": "支撑证据", "dimension": "对应维度"}\n'
                '  ],\n'
                '  "risk_warning": "这个立场最大的风险",\n'
                '  "blind_spot_warning": "你的经历/训练可能导致你忽视什么",\n'
                '  "challenge_to_others": "你预期的最强反对意见及预先回应"\n'
                '}\n\n'
                "规则：my_dimensions 2-4个，claims ≤ 5条，总输出 ≤ 500字",
                f"You are \"{meta['label']}\" in a roundtable debate. {style}\n\n"
                "You must cover these baseline dimensions:\n"
                "1. Fit for the target scenario\n"
                "2. Implementation cost and risk\n"
                "3. Long-term sustainability\n"
                "You may add distinctive dimensions when helpful.\n\n"
                "Return strict JSON only:\n"
                '{\n'
                '  "my_dimensions": ["dimension 1", "dimension 2"],\n'
                '  "stance": "your clear stance in one sentence; never say both sides are equally right",\n'
                '  "confidence": 0.8,\n'
                '  "claims": [\n'
                '    {"point": "claim", "evidence": "supporting evidence", "dimension": "related dimension"}\n'
                '  ],\n'
                '  "risk_warning": "the biggest risk in this stance",\n'
                '  "blind_spot_warning": "what your background or training may cause you to overlook",\n'
                '  "challenge_to_others": "the strongest counterargument you expect and your preemptive reply"\n'
                '}\n\n'
                "Rules: 2-4 my_dimensions, <= 5 claims, total output <= 500 words.",
            ),
            "user": question,
        }

    # ── Parsing helpers ──────────────────────────────────────────────────

    def _parse_expert_opinion(
        self, model_id: str, label: str, raw: str, latency_ms: int
    ) -> ExpertOpinion:
        """Parse structured expert opinion; gracefully degrade on bad JSON."""
        data = _parse_json_robust(raw)
        if data:
            claims = [
                Claim(
                    point=c.get("point", ""),
                    evidence=c.get("evidence", ""),
                    dimension=c.get("dimension", ""),
                )
                for c in data.get("claims", [])[:5]
            ]
            return ExpertOpinion(
                model_id=model_id,
                label=label,
                stance=data.get("stance", raw[:120]),
                confidence=float(data.get("confidence", 0.5)),
                my_dimensions=data.get("my_dimensions", [])[:4],
                claims=claims,
                risk_warning=data.get("risk_warning", ""),
                blind_spot_warning=data.get("blind_spot_warning", ""),
                challenge_to_others=data.get("challenge_to_others", ""),
                raw_response=raw,
                structured=True,
                latency_ms=latency_ms,
                success=True,
            )
        # JSON parse failed — degrade gracefully, still show raw text
        return ExpertOpinion(
            model_id=model_id,
            label=label,
            stance=raw[:120] if raw else "",
            confidence=0.5,
            raw_response=raw,
            structured=False,
            degradation_note="json_parse_failed",
            latency_ms=latency_ms,
            success=True,
        )

    # ── Model call bridge ────────────────────────────────────────────────

    async def _call_model(
        self, model_id: str, system_prompt: str,
        user_prompt: str, timeout_s: int = 30,
        web_search: bool = False,
        language: str = "zh-CN",
    ) -> ModelResponse:
        """Bridge: construct RoleCall and delegate to adapter.call().

        P0-F2: RoleCall requires messages list (not user_prompt kwarg).
        web_search: if True and model supports it, enables real-time search.
        """
        safety_rules = self._load_safety_rules(language)
        if safety_rules:
            system_prompt = f"{safety_rules}\n\n{system_prompt}"
        role_call = RoleCall(
            call_id=str(uuid.uuid4()),
            model_id=model_id,
            role=Role.CONTRIBUTOR,
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            timeout_seconds=timeout_s,
            web_search=web_search,
        )
        return await self._adapter.call(role_call)

    def _model_supports_search(self, model_id: str) -> bool:
        """Check if model supports web search via config."""
        if self._config and hasattr(self._config, "models"):
            mc = self._config.models.get(model_id)
            if mc:
                return getattr(mc, "supports_search", False)
        return False

    def _select_experts(self, count: int) -> list[tuple[str, dict]]:
        """Select available expert models up to count."""
        selected = []
        for model_id, meta in ROUNDTABLE_EXPERTS.items():
            if self._adapter.supports_model(model_id):
                selected.append((model_id, meta))
            if len(selected) >= count:
                break
        return selected

    def _estimate_cost(self, opinions: list[ExpertOpinion]) -> float:
        """Rough cost estimate: ~$0.08/expert + $0.04 S2 + $0.08 S4."""
        expert_cost = len([o for o in opinions if o.success]) * 0.08
        moderator_cost = 0.12   # S2 + S4 combined
        return round(expert_cost + moderator_cost, 4)

    async def _store_state(self, session_id: str, state: str) -> None:
        """Persist session state to SQLite store (best-effort, non-blocking)."""
        if self._session_store:
            try:
                await self._session_store.update_state(session_id, state)
            except Exception as e:
                logger.warning(f"[Roundtable] Failed to update store state: {e}")


# ── JSON parsing utility ──────────────────────────────────────────────────────

def _parse_json_robust(text: str) -> dict:
    """Try multiple strategies to extract JSON from LLM output.

    Strategy order:
    1. Direct json.loads
    2. Strip markdown fenced block (```json ... ```)
    3. Regex extract largest {...} block
    4. Return {} (caller handles graceful degradation)
    """
    text = text.strip()
    # 1. Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Markdown fenced block
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 3. Largest {...} block
    brace_match = re.search(r"\{[\s\S]*\}", text)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    return {}


# ── Dataclass serialization utility ──────────────────────────────────────────

def _dataclass_to_dict(obj) -> dict:
    """Recursively convert a dataclass instance to a plain dict (JSON-safe)."""
    import dataclasses
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _dataclass_to_dict(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, list):
        return [_dataclass_to_dict(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _dataclass_to_dict(v) for k, v in obj.items()}
    return obj
