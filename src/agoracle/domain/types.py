"""
Core domain types — the data structures that flow through the entire pipeline.

Design principles:
  - Frozen dataclasses where immutability matters (RoleCall, RouteDecision)
  - Regular dataclasses where mutation is needed (QueryResult, Session)
  - Zero external dependencies — only stdlib
  - Every field that future phases need is defined NOW (even if initially None/empty)

v2.0 additions (2026-02-12):
  - Intent enum (ANSWER / GROWTH) for dual-mode architecture
  - OutputDepth enum (LEVEL_1/2/3) for divergence visibility
  - Mode.SOCRATIC reserved for Phase 3
  - Role.DIVERGENCE_ANALYZER / SOCRATIC_GUIDE reserved for Phase 3
  - CognitiveProfile fields in UserProfile (Phase 5 implementation)
  - SocraticSession stub (Phase 3 implementation)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


# ============================================================
# Enums
# ============================================================

class Mode(str, Enum):
    """User-facing orchestration modes."""
    AUTO = "auto"
    LIGHT = "light"
    DEEP = "deep"
    RESEARCH = "research"
    SOCRATIC = "socratic"  # v2.0: Growth Intent — Phase 3 implementation


class Intent(str, Enum):
    """User intent — determines which pipeline runs (v2.0)."""
    ANSWER = "answer"    # Answer Intent: optimize for answer quality (L1)
    GROWTH = "growth"    # Growth Intent: optimize for cognitive development (L0/L1')


class OutputDepth(str, Enum):
    """How much divergence info to expose to the user (v2.0)."""
    LEVEL_1 = "level_1"  # final_answer only (default for Light)
    LEVEL_2 = "level_2"  # + divergence summary (default for Deep)
    LEVEL_3 = "level_3"  # + individual model responses (default for Research)


class Role(str, Enum):
    """Roles a model can play in a single query pipeline."""
    CONTRIBUTOR = "contributor"
    QUESTION_CRITIC = "question_critic"
    JUDGE = "judge"
    JUDGE_REFINE = "judge_refine"
    ANSWER_CRITIC = "answer_critic"
    METADATA_EXTRACTOR = "metadata_extractor"      # parallel with Judge
    DIVERGENCE_ANALYZER = "divergence_analyzer"    # v2.0: Socratic — Phase 3
    SOCRATIC_GUIDE = "socratic_guide"              # v2.0: Socratic — Phase 3


class QualityGateResult(str, Enum):
    """Judge's quality gate decision."""
    SYNTHESIZED = "synthesized"          # normal synthesis from multiple models
    BEST_SINGLE = "best_single"          # one model clearly superior, adopted directly
    LOW_CONFIDENCE = "low_confidence"    # all models uncertain, flagged to user


class ConsensusType(str, Enum):
    """How models reached agreement."""
    INDEPENDENT = "independent_verification"   # different reasoning paths, same conclusion
    PARROT = "parrot_consensus"                # suspiciously similar wording/sources
    MIXED = "mixed"                            # some independent, some parrot
    UNKNOWN = "unknown"                        # not enough info to determine


class KnowledgeCategory(str, Enum):
    """Types of knowledge entries in RAG."""
    FACT = "fact"
    OPINION = "opinion"
    DECISION = "decision"
    METHOD = "method"
    CORRECTION = "correction"


class QuestionType(str, Enum):
    """Question type classification for smart aggregation routing (Phase 2).

    Determines whether full aggregation is beneficial:
      - ANALYTICAL/TECHNICAL/REASONING: aggregation adds value (+15~+22 in benchmark)
      - FACTUAL/CREATIVE/WRITING/CODING/MATH: single strong model often better or equal
      - CULTURAL/META_COGNITION: best-single preferred (synthesis dilutes nuance)
    """
    FACTUAL = "factual"           # fact lookup, single correct answer
    ANALYTICAL = "analytical"     # multi-perspective analysis, reasoning chains
    TECHNICAL = "technical"       # code, architecture, system design
    CONTROVERSIAL = "controversial"  # opinion-based, no single correct answer
    CREATIVE = "creative"         # creative brainstorming — aggregation hurts
    CULTURAL = "cultural"         # culture, history, cross-cultural comparison
    REASONING = "reasoning"       # math, logic, formal reasoning
    META_COGNITION = "meta_cognition"  # thinking about thinking, methodology, frameworks
    WRITING = "writing"           # prose writing tasks — style cannot be merged
    CODING = "coding"             # code generation / debugging — correctness is binary
    MATH = "math"                 # pure math / calculation — unique correct answer
    REALTIME = "realtime"         # news, prices, current events — requires search-capable model
    UNKNOWN = "unknown"           # can't classify → default to full pipeline


class FreshnessDomain(str, Enum):
    """How quickly knowledge becomes stale."""
    STABLE = "stable"       # math theorems, physical laws — never stale
    EVOLVING = "evolving"   # frameworks, best practices — slow decay
    VOLATILE = "volatile"   # prices, news, rankings — fast decay


class GuidanceActionType(str, Enum):
    """Action types for guidance suggestions.

    Each action maps to a concrete user-facing operation:
      - QUERY_*: re-issue question in a different mode
      - EXPLORE_DIVERGENCE: drill into a specific disagreement point
      - SHOW_INDIVIDUAL: reveal raw contributor answers
      - COGNITIVE_CHALLENGE: Socratic micro-challenge for growth
      - ROUNDTABLE: enter multi-model roundtable discussion
      - DONE: explicitly signal "no further action needed"
    """
    QUERY_DEEP = "query_deep"
    QUERY_RESEARCH = "query_research"
    QUERY_SOCRATIC = "query_socratic"
    QUERY_SINGLE = "query_single"
    QUERY_FOLLOWUP = "query_followup"
    EXPLORE_DIVERGENCE = "explore_divergence"
    SHOW_INDIVIDUAL = "show_individual"
    COGNITIVE_CHALLENGE = "cognitive_challenge"
    ROUNDTABLE = "roundtable"
    DONE = "done"


class GuidanceIntensity(str, Enum):
    """How prominently the guidance panel is rendered in the frontend.

    none:   hidden — no suggestions shown (e.g. simple factual answers)
    light:  single-line chip + dismiss link
    medium: small card with confidence statement + 1-2 chips
    rich:   full card with rationale, time/cost estimates, confirm buttons
    """
    NONE = "none"
    LIGHT = "light"
    MEDIUM = "medium"
    RICH = "rich"


# ============================================================
# Pipeline Core Types
# ============================================================

@dataclass(frozen=True)
class RouteDecision:
    """Router's output — determines how the pipeline runs."""
    mode: Mode
    web_search_enabled: bool
    critique_enabled: bool
    confidence: float = 1.0  # router's confidence in its decision
    intent: Intent = Intent.ANSWER                  # v2.0: dual-mode intent
    output_depth: OutputDepth = OutputDepth.LEVEL_1  # v2.0: divergence visibility
    question_type: "QuestionType" = QuestionType.UNKNOWN  # v2.4: smart aggregation


@dataclass
class Attachment:
    """A file attachment (image or document) for multimodal queries."""
    file_id: str
    filename: str
    content_type: str  # e.g. image/jpeg, application/pdf
    file_path: str     # absolute path on disk
    size_bytes: int = 0


@dataclass(frozen=True)
class RoleCall:
    """
    One atomic, isolated API call to a model in a specific role.

    frozen=True ensures no code can tamper with the context after creation.
    This is the architectural guarantee of role isolation.
    """
    call_id: str
    model_id: str
    role: Role
    system_prompt: str
    messages: list[dict[str, Any]]
    timeout_seconds: int = 30
    web_search: bool = False


@dataclass
class ModelResponse:
    """A single model's response to a RoleCall."""
    call_id: str
    model_id: str
    role: Role
    content: str
    latency_ms: int
    success: bool = True
    error: str | None = None
    prompt_tokens: int = 0       # v2.3: token usage tracking
    completion_tokens: int = 0   # v2.3: token usage tracking
    retry_count: int = 0         # v2.8.6: number of retries that occurred (0 = first attempt succeeded)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class QuestionCritique:
    """Output of the question critic."""
    has_issues: bool
    issue_type: str | None = None        # false_premise / logical_fallacy / ambiguity
    analysis: str | None = None
    suggested_reformulation: str | None = None
    severity: str = "low"                # low / medium / high


@dataclass
class ModelEvaluation:
    """Judge's internal evaluation of one model's response (not shown to user)."""
    model_id: str
    accuracy: float = 0.0       # 0-1
    reasoning: float = 0.0      # 0-1
    uniqueness: float = 0.0     # 0-1
    adopted_weight: str = "medium"  # low / medium / high / highest


@dataclass
class JudgeSynthesis:
    """Judge's output — the final answer (pure text, no metadata burden)."""
    final_answer: str
    latency_ms: int
    success: bool = True
    error: str | None = None
    prompt_tokens: int = 0       # v4.1 cost tracking: Judge input tokens
    completion_tokens: int = 0   # v4.1 cost tracking: Judge output tokens
    model_id: str = ""           # v4.1 cost tracking: which model was used


@dataclass
class MetadataExtraction:
    """
    Extracted metadata (runs in parallel with Judge, by a fast model).
    Separated from Judge so Judge can focus 100% on answer quality.
    """
    key_insights: list[str] = field(default_factory=list)
    topic_tags: list[str] = field(default_factory=list)
    confidence: float = 0.0
    consensus_type: ConsensusType = ConsensusType.UNKNOWN
    has_divergence: bool = False
    divergence_summary: str | None = None
    model_evaluations: dict[str, ModelEvaluation] = field(default_factory=dict)
    pairwise_evaluated: bool = False  # v2.3: True if scores derived from pairwise comparisons
    best_model: str = ""              # v3.1: PairRanker winner — model_id of the best answer
    best_model_reason: str = ""       # v3.1: Why this model was ranked #1
    insight_agreements: dict[str, int] = field(default_factory=dict)  # v5.3: insight text → model agreement count (for consensus_map)
    prompt_tokens: int = 0            # v4.1 cost tracking: Extractor input tokens
    completion_tokens: int = 0        # v4.1 cost tracking: Extractor output tokens
    extractor_model_id: str = ""      # v4.1 cost tracking: which model was used


# ============================================================
# v2.5 Socratic Mode — Phase 3 implementation
# ============================================================

@dataclass
class DivergencePoint:
    """One specific point where models disagree."""
    point_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    topic: str = ""                     # short label, e.g. "AI是否能产生真正的创意"
    description: str = ""               # 1-2 sentence explanation of the disagreement
    positions: list[dict[str, str]] = field(default_factory=list)
    # Each position: {"stance": "支持/反对/中立", "summary": "...", "models": ["model_a", "model_b"]}
    consensus_ratio: float = 0.0        # 0-1, how much agreement (0 = total split, 1 = all agree)
    difficulty: str = "medium"          # easy / medium / hard — for Cognitive Profile training


@dataclass
class DivergenceMap:
    """
    Structured analysis of where models agree and disagree.

    Generated by DivergenceAnalyzer from Layer 1 contributor responses.
    This is the core data structure that powers Socratic guidance.
    """
    consensus_points: list[str] = field(default_factory=list)   # things all models agree on
    divergence_points: list[DivergencePoint] = field(default_factory=list)
    overall_consensus_score: float = 0.0   # 0-1, how much overall agreement
    model_count: int = 0
    analysis_latency_ms: int = 0


@dataclass
class SocraticTurn:
    """One turn in the Socratic dialogue (system prompt + user response)."""
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    role: str = ""                      # "guide" or "user"
    content: str = ""
    divergence_point_id: str | None = None  # which divergence point this turn addresses
    user_stance: str | None = None      # user's position on the divergence point
    latency_ms: int = 0
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class CognitiveSnapshot:
    """
    Snapshot of cognitive patterns observed in one Socratic session.

    Accumulated across sessions → CognitiveProfile in UserProfile.
    """
    anchoring_detected: bool = False    # user adopted first-seen position without critical evaluation
    confirmation_bias: bool = False     # user only accepted evidence supporting their initial stance
    nuance_recognition: float = 0.0     # 0-1, ability to see valid points on multiple sides
    position_change_count: int = 0      # how many times user changed stance during session
    reasoning_depth: float = 0.0        # 0-1, quality of justifications given
    blind_spots: list[str] = field(default_factory=list)  # topics/angles user consistently missed


@dataclass
class SocraticSession:
    """
    Tracks a complete Socratic mode session (v2.5 — Phase 3).

    Lifecycle:
      1. Heavy compute: fan-out → divergence analysis → cache
      2. Multi-turn dialogue: guide questions ↔ user responses
      3. Reveal: show full answer + divergence map
      4. Cognitive snapshot: record thinking patterns
    """
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    question: str = ""                  # original user question (must be set by start_session)
    language: str = "zh-CN"

    # Phase 1 output (cached)
    divergence_map: DivergenceMap | None = None
    full_answer: str = ""               # Judge synthesis (revealed at end)
    contributor_responses: list[dict[str, str]] = field(default_factory=list)
    # Each: {"model_id": "...", "content": "..."}

    # Phase 2 dialogue
    turns: list[SocraticTurn] = field(default_factory=list)
    current_divergence_index: int = 0   # which divergence point we're discussing
    max_guide_rounds: int = 5

    # Outcome
    guide_rounds_used: int = 0
    user_conclusion: str = ""
    completed_naturally: bool = True     # True = user completed reasoning, False = revealed early
    revealed: bool = False               # True = user asked to see the answer

    # Cognitive analysis
    cognitive_snapshot: CognitiveSnapshot | None = None
    reasoning_quality_score: float = 0.0  # 0-1, overall quality

    # Timing
    phase1_latency_ms: int = 0          # heavy compute phase
    total_dialogue_ms: int = 0          # all turns combined
    created_at: datetime = field(default_factory=datetime.now)


# ============================================================
# Query Context & Result
# ============================================================

@dataclass
class QueryContext:
    """
    Complete context flowing through the pipeline.

    Created by Context Enricher, consumed by Orchestrator.
    Fields for future phases are defined now but start as None/empty.
    """
    query_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    question: str = ""
    mode: Mode = Mode.AUTO
    resolved_mode: Mode = Mode.LIGHT  # after Router resolves Auto

    # Router decisions
    intent: Intent = Intent.ANSWER  # v2.0: dual-mode intent
    web_search_enabled: bool = True
    critique_enabled: bool = False
    output_depth: OutputDepth = OutputDepth.LEVEL_1  # v2.0: divergence visibility
    question_type: QuestionType = QuestionType.UNKNOWN  # v2.4: smart aggregation

    # Memory injection points (Phase 0: empty, Phase 4+: populated)
    rag_results: list[KnowledgeEntry] = field(default_factory=list)
    user_profile_summary: str = ""
    session_history: list[Turn] = field(default_factory=list)
    language: str = "zh-CN"

    # v2.3: Token carryover from previous pipeline stage (auto-escalation)
    inherited_tokens: int = 0

    # v2.7: User identity for per-user profile isolation
    user_id: int = 0

    # v3.2: Multimodal attachments (images, documents)
    attachments: list[Attachment] = field(default_factory=list)

    # v5.1: Companion Dispatcher — prevents re-routing on recursive execute() (AUTO_ESCALATE)
    dispatcher_routed: bool = False

    # v5.1: CompanionBubble query_single — direct single-model override (bypasses Dispatcher)
    single_model_override: str | None = None

    # Timestamps
    timestamp: datetime = field(default_factory=datetime.now)


# ============================================================
# v5.2: Canonical Guidance Protocol
# ============================================================

@dataclass
class GuidanceOutput:
    """Canonical guidance protocol — SINGLE source of truth for post-answer guidance.

    All guidance data flows through this structure. Legacy fields (next_steps,
    companion_guide) are derived from this via adapter functions in guidance_compat.py.
    Active sources are now `dispatcher` and `none`; the old `nsg` producer is retired.
    Introduced in protocol_version 2026-03-04.
    """
    source: str = "none"                 # "dispatcher" | "none"
    confidence_statement: str = ""
    confidence_level: str = "medium"     # high / medium / low
    message: str = ""                    # Dispatcher natural language guidance
    suggestions: list["GuidanceSuggestion"] = field(default_factory=list)
    intensity: str = GuidanceIntensity.NONE.value  # none / light / medium / rich
    is_folded: bool = True               # default folded; high-value scenarios unfold
    show_dismiss: bool = False
    route_reason: str = ""               # Dispatcher route reason (1-line)
    trigger: str = ""                    # fold / divergence / low_confidence / clarify


# ============================================================
# Guidance Suggestion Types
# ============================================================

@dataclass
class GuidanceSuggestion:
    """A single actionable guidance suggestion shown to the user.

    Every suggestion is one-click executable. The frontend reads
    action_type + action_payload to dispatch the correct handler.
    """
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    label: str = ""                     # user-facing text, ≤25 chars
    action_type: str = GuidanceActionType.DONE.value
    action_payload: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""                 # why this suggestion, ≤40 chars
    estimated_seconds: int = 0          # expected wait time
    estimated_cost_usd: float = 0.0     # transparency: rough cost
    requires_confirm: bool = False      # True for expensive/navigating actions


@dataclass
class NextStepGuidance:
    """Legacy compatibility wrapper for the retired `next_steps` field.

    This structure is still present in API/domain contracts for backward
    compatibility, but new guidance is produced via canonical `GuidanceOutput`.
    """
    confidence_statement: str = ""      # e.g. "综合评估：中等信心，专家基本一致"
    confidence_level: str = "medium"    # high / medium / low
    intensity: str = GuidanceIntensity.NONE.value
    suggestions: list[GuidanceSuggestion] = field(default_factory=list)
    show_dismiss: bool = True           # always show "结束本轮" unless intensity=none


@dataclass
class QueryResult:
    """
    Complete result of one query — the system's full output.

    User sees: final_answer only.
    System stores: everything (for quality monitoring, knowledge extraction, feedback).
    """
    query_id: str = ""
    question: str = ""
    mode: str = ""
    resolved_mode: str = ""

    # Answer
    final_answer: str = ""

    # Metadata (from parallel Extractor, not from Judge)
    key_insights: list[str] = field(default_factory=list)
    topic_tags: list[str] = field(default_factory=list)
    confidence: float = 0.0
    consensus_type: str = ConsensusType.UNKNOWN.value
    has_divergence: bool = False
    divergence_summary: str | None = None

    # Internal (not shown to user)
    model_evaluations: dict[str, Any] = field(default_factory=dict)
    quality_gate_result: str = QualityGateResult.SYNTHESIZED.value
    best_single_score_gap: float = 0.0  # v2.3.2: score gap at gate decision (for threshold tuning)
    question_critique: QuestionCritique | None = None
    contributor_count: int = 0
    total_model_calls: int = 0

    # Timing & Cost
    latency_ms: int = 0
    total_tokens: int = 0              # v2.3: sum of all model calls
    estimated_cost_usd: float = 0.0    # v2.3: rough cost estimate
    timestamp: datetime = field(default_factory=datetime.now)

    # Feedback (Phase 1: minimal feedback)
    feedback: Feedback | None = None

    # v2.7.6: Fast path provenance (skip_judge mode)
    fast_path: bool = False

    # v2.7.8h: LOW_CONFIDENCE actionable suggestions (原则#22 用户主权)
    low_confidence_actions: list[dict[str, str]] = field(default_factory=list)

    # v2.0: Divergence visibility (Phase 2 implementation)
    output_depth: str = OutputDepth.LEVEL_1.value
    divergence_report: str | None = None          # Level 2+: structured divergence summary
    individual_responses: list[dict[str, Any]] | None = None  # Level 3: raw model answers

    # v2.0: Socratic session (Phase 3 implementation)
    socratic_session: SocraticSession | None = None

    # v3.3: Draft answers — intermediate versions for user comparison
    # [{"stage": "fan_out_best", "model_id": "...", "content": "..."}, ...]
    draft_answers: list[dict[str, Any]] = field(default_factory=list)

    # v4.18: Search citations for inline rendering (Tavily + perplexity)
    # [{"url": "...", "title": "..."}, ...]
    search_citations: list[dict[str, Any]] = field(default_factory=list)

    # v4.20: Structured divergence points from DivergenceAnalyzer (Deep/Research mode)
    # [{"topic": "...", "description": "...", "positions": [...], "consensus_ratio": 0.5, "difficulty": "medium"}]
    divergence_points: list[dict[str, Any]] = field(default_factory=list)

    # A4: Consensus points from DivergenceAnalyzer (≤3 items) for "交叉验证" display
    consensus_points: list[str] = field(default_factory=list)

    # v4.22c: Fact-check warnings (shown separately from final_answer, e.g. in sidebar)
    # Populated when BEST_SINGLE bypasses Judge but FactChecker found contradictions.
    # ["⚠️ claim X contradicted by search source Y", ...]
    fact_warnings: list[str] = field(default_factory=list)

    # Legacy compatibility field retained for older clients; derived from `guidance`.
    next_steps: NextStepGuidance | None = None

    # Legacy compatibility field retained for older clients; derived from `guidance`.
    # {"message": str, "actions": list, "trigger": str, "is_silent": bool}
    companion_guide: dict[str, Any] | None = None

    # v5.2: Canonical guidance protocol — SINGLE SOURCE OF TRUTH for post-answer guidance.
    # When present, next_steps and companion_guide are derived from this (see guidance_compat.py).
    # Legacy fields kept for backward compatibility with pre-v5.2 frontends.
    guidance: "GuidanceOutput | None" = None

    # v5.2: Clarify response marker — skips billing and history save in app.py
    is_clarify: bool = False


# ============================================================
# Session & Memory Types
# ============================================================

@dataclass
class Turn:
    """One turn in a conversation session."""
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    question: str = ""
    final_answer_summary: str = ""     # v5.0: expanded to ≤2000 chars for richer context
    key_insights: list[str] = field(default_factory=list)
    mode: str = ""
    answer_outline: str = ""           # v5.0: pipe-separated key insights for mid-tier context
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class Session:
    """A conversation session containing multiple turns."""
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    turns: list[Turn] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    last_active: datetime = field(default_factory=datetime.now)


@dataclass
class KnowledgeEntry:
    """
    One piece of knowledge in the RAG store.

    Designed for Phase 4 implementation but defined now so
    Judge output structure and event schemas are stable.
    """
    entry_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    # Content
    insight: str = ""
    source_question: str = ""
    source_answer_excerpt: str = ""
    source_session_id: str = ""
    source_turn_index: int = 0
    follow_up_questions: list[str] = field(default_factory=list)

    # Classification
    category: str = KnowledgeCategory.FACT.value
    topic_tags: list[str] = field(default_factory=list)

    # Quality
    confidence: float = 0.0
    consensus_level: int = 0
    consensus_type: str = ConsensusType.UNKNOWN.value

    # Lifecycle
    created_at: datetime = field(default_factory=datetime.now)
    last_accessed: datetime = field(default_factory=datetime.now)
    access_count: int = 0
    is_outdated: bool = False
    superseded_by: str | None = None
    freshness_domain: str = FreshnessDomain.EVOLVING.value


@dataclass
class ImprovementPlan:
    """
    A user's cognitive improvement plan for a specific topic/skill.

    Created by ProactiveCoachService when it detects a user has
    sustained interest in a topic but hasn't deepened beyond L2.
    The plan drives progressive Socratic micro-challenges woven
    into daily Q&A until the user reaches the target depth.
    """
    plan_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    topic: str = ""                         # target topic (normalized)
    status: str = "proposed"                # proposed → active → completed → abandoned
    current_level: int = 1                  # L1-L5 at plan creation
    target_level: int = 4                   # target depth (usually L4)
    difficulty: int = 1                     # 1-5, auto-adjusted based on performance
    challenges_delivered: int = 0           # micro-challenges injected so far
    challenges_engaged: int = 0             # user actually responded to challenge
    last_challenge_date: str = ""           # ISO date
    milestones: list[str] = field(default_factory=list)  # completed milestones
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class Feedback:
    """User feedback on a query result."""
    rating: str = ""      # useful / inaccurate / too_shallow / too_slow
    comment: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class UserProfile:
    """User profile for personalization (Phase 5 full implementation)."""

    # === PreferenceProfile (Answer Intent) ===
    preferred_language: str = "zh-CN"
    preferred_depth: str = "detailed"
    explicit_rules: list[str] = field(default_factory=list)
    topic_frequency: dict[str, int] = field(default_factory=dict)
    topic_expertise: dict[str, float] = field(default_factory=dict)
    topic_last_asked: dict[str, str] = field(default_factory=dict)  # ISO datetime strings
    mode_preference: dict[str, float] = field(default_factory=dict)
    satisfaction_history: list[dict[str, Any]] = field(default_factory=list)

    # === CognitiveProfile (Growth Intent) — v2.0 Phase 5 === #
    cognitive_tracking_consent: bool = False   # explicit opt-in for cognitive data collection
    cognitive_quadrant_dist: dict[str, int] = field(default_factory=lambda: {
        "known_known": 0,
        "known_unknown": 0,
        "unknown_known": 0,
        "unknown_unknown": 0,
    })
    comfort_zone_topics: list[str] = field(default_factory=list)
    growth_zone_topics: list[str] = field(default_factory=list)
    mode_usage_history: dict[str, int] = field(default_factory=dict)
    socratic_completion_rate: float = 0.0
    average_reasoning_quality: float = 0.0
    last_challenge_date: str = ""

    # === CBA Behavioral Analytics (ADR-014) — Phase 3 data accumulation === #
    reasoning_improvement_trend: list[float] = field(default_factory=list)

    # Dimension 1: Divergent-Convergent Dynamics
    topic_sequence: list[dict[str, Any]] = field(default_factory=list)
    # Each entry: {"tags": [...], "mode": "light", "ts": "ISO8601"}
    # Used to compute: topic switch rate, single-topic dwell time, depth progression

    # Dimension 2: Cognitive Engagement Pattern
    hourly_query_dist: dict[str, int] = field(default_factory=dict)
    # key: "HH" (00-23), value: query count
    daily_mode_dist: dict[str, dict[str, int]] = field(default_factory=dict)
    # key: "YYYY-MM-DD", value: {"light": N, "deep": N, ...}
    session_durations_min: list[float] = field(default_factory=list)
    # Recent session durations in minutes (rolling window, max 50)

    # Topic Depth Map (v2.0 §5.4.3): tracks exploration depth per topic (L1-L5)
    topic_depth_map: dict[str, int] = field(default_factory=dict)
    # key: normalized topic tag, value: depth level (1-5)
    # L1=触达, L2=框架, L3=细节, L4=方案, L5=验证

    # === Proactive Coaching (v2.7.9d) === #
    improvement_plans: list[dict[str, Any]] = field(default_factory=list)
    # Serialized ImprovementPlan objects. Max 5 active plans at once.
    # Plan lifecycle: proposed → active → completed/abandoned

    # === Memory-lite (方案A: LLM摘要) === #
    recent_turns: list[dict[str, Any]] = field(default_factory=list)
    # Rolling window of last 3 query summaries for cross-session continuity.
    # Each entry: {"ts": ISO8601, "question_snippet": str(<=100), "summary": str(<=60), "mode": str, "topic": str}
    # summary generated by gemini-flash fire-and-forget after each QueryCompleted.
    # Max 3 entries; oldest dropped when exceeded.
