from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class StatusResponse(BaseModel):
    status: str


class StatusMessageResponse(StatusResponse):
    message: str


class SendCodeResponse(StatusMessageResponse):
    pass


class AuthResponse(StatusResponse):
    username: str
    display_name: str
    is_admin: bool


class LogoutResponse(StatusResponse):
    pass


class AuthMeResponse(BaseModel):
    username: str
    display_name: str
    is_admin: bool
    query_count: int
    preferred_language: str


class SetLanguageRequest(BaseModel):
    language: str


class ChangePasswordResponse(StatusMessageResponse):
    pass


class DeleteAccountResponse(StatusMessageResponse):
    pass


class FeedbackResponse(StatusResponse):
    pass


class SearchCitationModel(BaseModel):
    title: str = ""
    url: str = ""
    snippet: str = ""
    source: str = ""


class DraftAnswerModel(BaseModel):
    stage: str = ""
    model_id: str = ""
    content: str = ""


class UploadResponse(BaseModel):
    file_id: str
    filename: Optional[str]
    content_type: str
    size_bytes: int
    is_image: bool


class AvailableModelInfo(BaseModel):
    id: str
    name: str
    available: bool


class AvailableModelsResponse(BaseModel):
    models: list[AvailableModelInfo]


class PricingModelInfo(BaseModel):
    id: str
    name: str
    cost_per_1m_input: float
    cost_per_1m_output: float


class PricingResponse(BaseModel):
    pricing: list[PricingModelInfo]
    currency: str
    unit: str


class HealthResponse(BaseModel):
    status: str
    version: Optional[str] = None
    models_available: Optional[int] = None
    models_total: Optional[int] = None
    session_store: Optional[str] = None
    conversation_store: Optional[str] = None
    git_hash: Optional[str] = None
    socratic_n_of_m: Optional[int] = None


class ModeInfo(BaseModel):
    id: str
    label: str
    desc: str
    detail: str
    icon: str
    order: int
    contributor_count: int
    n_of_m: int
    has_judge: bool
    has_critique: bool
    has_preflight: bool
    max_timeout_seconds: int


class ModesResponse(BaseModel):
    modes: list[ModeInfo]


class QuotaModeUsage(BaseModel):
    mode: str
    used_lifetime: int
    used_today: int
    credit_cost: int
    credits_spent: int


class QuotaResponse(BaseModel):
    enabled: bool
    total_credits: int
    credits_used: int
    credits_remaining: int
    modes: list[QuotaModeUsage]


class DivergencePosition(BaseModel):
    stance: str
    summary: str
    models: list[str]


class UsageResponse(BaseModel):
    usage: dict[str, int]
    limits: dict[str, int]
    remaining: dict[str, int]


class CapabilityTopicItem(BaseModel):
    topic: str
    level: int
    level_label: str
    frequency: int
    has_active_plan: bool
    plan_progress: Optional[float]


class ImprovementPlanSummary(BaseModel):
    plan_id: str
    topic: str
    status: str
    current_level: int
    target_level: int
    difficulty: int
    progress: float
    challenges_delivered: int
    challenges_engaged: int
    milestones: list[str]


class CognitiveQuadrantBucket(BaseModel):
    count: int
    percentage: float


class CapabilityMapResponse(BaseModel):
    has_data: bool
    topics: list[CapabilityTopicItem]
    active_plans: list[ImprovementPlanSummary]
    completed_plans_count: int
    cognitive_quadrant: dict[str, CognitiveQuadrantBucket]
    reasoning_trend: list[float]
    total_topics_explored: int
    topics_at_l3_plus: int
    average_reasoning_quality: float


class CognitiveSummaryData(BaseModel):
    quadrant_dist: dict[str, int]
    comfort_zone_topics: list[str]
    growth_zone_topics: list[str]
    mode_usage: dict[str, int]
    socratic_sessions: int
    completion_rate: float
    avg_reasoning_quality: float
    reasoning_trend: list[float]
    last_challenge_date: Optional[str] = None


class CognitiveSummaryResponse(BaseModel):
    summary: str
    cognitive_tracking_consent: bool
    data: CognitiveSummaryData


class DivergentConvergentMetricsResponse(BaseModel):
    new_topics: int
    deepened_topics: int
    switch_rate: float
    top_recurring: list[str]
    total_in_window: int


class EngagementMetricsResponse(BaseModel):
    peak_hours: list[str]
    mode_distribution: dict[str, int]
    total_queries: int
    favorite_mode: str


class BehaviorMetricsResponse(BaseModel):
    divergent_convergent: DivergentConvergentMetricsResponse
    engagement: EngagementMetricsResponse
    topic_depth_map: dict[str, int]


class BehaviorSummaryResponse(BaseModel):
    has_data: bool
    narrative: str
    metrics: Optional[BehaviorMetricsResponse] = None
    total_queries: Optional[int] = None


class ProfileExportResponse(BaseModel):
    exported_at: str
    user_id: int
    profile: Optional[dict[str, Any]] = None
    query_history: Optional[list["HistoryItem"]] = None
    usage: Optional[dict[str, dict[str, int]]] = None


class GrowthSummaryResponse(BaseModel):
    total_topics: int
    deep_topics: int
    mastered_topics: int
    growth_score: int


class GrowthTopicItem(BaseModel):
    topic: str
    depth: int
    depth_label: str
    frequency: int
    expertise: float


class GrowthResponse(BaseModel):
    has_data: bool
    summary: GrowthSummaryResponse
    topics: list[GrowthTopicItem]


class RecentTurnItem(BaseModel):
    ts: str
    question_snippet: str
    summary: str
    mode: str
    topic: str


class RecentTurnsResponse(BaseModel):
    recent_turns: list[RecentTurnItem]


class CognitiveConsentResponse(StatusResponse):
    cognitive_tracking_consent: bool


class DeleteCognitiveResponse(StatusMessageResponse):
    remaining_fields: list[str]


class ImprovementPlanRecord(BaseModel):
    plan_id: str
    topic: str
    current_level: int
    target_level: int
    status: str
    difficulty: int
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_challenge_date: Optional[str] = None
    challenges_delivered: int = 0
    challenges_engaged: int = 0
    milestones: list[str] = Field(default_factory=list)


class ImprovementPlansListResponse(BaseModel):
    plans: list[ImprovementPlanRecord]


class ImprovementPlanActionResponse(BaseModel):
    status: str
    plan: Optional[ImprovementPlanRecord] = None


class AdminUsageResponse(BaseModel):
    date: str
    limits: dict[str, int]
    users: dict[str, dict[str, int]]


class AdminUserUsageResponse(BaseModel):
    user_id: int
    today: dict[str, int]
    limits: dict[str, int]
    history: dict[str, dict[str, int]]


class AdminTopUserQueriesItem(BaseModel):
    user_id: int
    total_queries: int
    light: int = 0
    socratic: int = 0
    deep: int = 0
    research: int = 0


class AdminTopUserCostItem(BaseModel):
    user_id: int
    query_count: int
    total_cost_usd: float


class AdminCostReportResponse(BaseModel):
    date: str
    mode_call_counts: dict[str, int]
    mode_costs_usd: dict[str, float]
    total_cost_usd: float
    top_users_by_queries: list[AdminTopUserQueriesItem]
    top_users_by_cost: list[AdminTopUserCostItem]
    cost_basis: str


class SocraticStartDivergenceMap(BaseModel):
    consensus_points: list[str]
    divergence_count: int
    overall_consensus: float


class SocraticStartResponse(BaseModel):
    session_id: str
    phase1_latency_ms: int
    max_guide_rounds: int
    divergence_map: SocraticStartDivergenceMap
    initial_guide: str


class SocraticRespondResponse(BaseModel):
    guide_message: str
    round: int
    max_rounds: int
    latency_ms: int


class SocraticRevealDivergencePoint(BaseModel):
    topic: str
    description: str
    positions: list[DivergencePosition]
    consensus_ratio: float
    difficulty: str


class HistoryItem(BaseModel):
    query_id: str
    session_id: Optional[str] = None
    question: str
    mode: str
    final_answer: str
    confidence: float
    contributor_count: int
    latency_ms: int
    estimated_cost_usd: float
    user_marked_usable: Optional[bool] = None
    created_at: str
    quality_gate: str = ""
    best_single_answer: str = ""
    has_divergence: bool = False
    divergence_summary: str = ""
    key_insights: list[str]
    divergence_points: list[SocraticRevealDivergencePoint]


class HistoryResponse(BaseModel):
    history: list[HistoryItem]
    total: int


class SocraticRevealDivergenceMap(BaseModel):
    consensus_points: list[str]
    divergence_points: list[SocraticRevealDivergencePoint]


class CognitiveSnapshot(BaseModel):
    reasoning_depth: int
    nuance_recognition: int
    anchoring_detected: bool
    confirmation_bias: bool
    blind_spots: list[str]


class SocraticRevealResponse(BaseModel):
    full_answer: str
    divergence_map: SocraticRevealDivergenceMap
    cognitive_snapshot: CognitiveSnapshot
    guide_rounds_used: int


class RoundtableCheckResponse(BaseModel):
    suitability: str
    reason: str


class RoundtableChoiceResponse(BaseModel):
    ok: bool


class StanceEvolution(BaseModel):
    expert: str
    r1_stance: str
    final_stance: str
    changed: bool
    changed_reason: str = ""


class DecisionOption(BaseModel):
    choice: str
    pros: list[str]
    cons: list[str]
    best_when: str = ""
    risk: str = ""
    mitigation: str = ""


class UnresolvedItem(BaseModel):
    point: str
    reason: str
    how_to_resolve: str


class ValueDisputeForUser(BaseModel):
    point: str
    dimension_id: str = ""
    ask_user: str = ""


class DecisionPacket(BaseModel):
    final_summary: str
    conclusion_type: str = "draft"
    confidence_basis: str = ""
    stance_evolution: list[StanceEvolution]
    options: list[DecisionOption]
    unresolved: list[UnresolvedItem]
    what_changes_my_mind: str = ""
    recommended_action: str = ""
    value_disputes_to_user: list[ValueDisputeForUser]
    echo_chamber_flag: bool = False
    degraded: bool = False
    degradation_reason: str = ""
    total_latency_ms: int = 0
    estimated_cost_usd: float = 0.0


class RoundtableResumeStateSnapshot(BaseModel):
    question: str = ""
    expert_count: int = 0
    experts: list[dict[str, Any]] = Field(default_factory=list)
    dispute_map: Optional[dict[str, Any]] = None
    rebuttals: list[dict[str, Any]] = Field(default_factory=list)
    debate_round: int = 0
    choice_point: Optional[str] = None


class RoundtableResumeResponse(BaseModel):
    status: str
    state: Optional[str] = None
    session_id: Optional[str] = None
    choice_point: Optional[str] = None
    decision_packet: Optional[DecisionPacket] = None
    state_snapshot: Optional[RoundtableResumeStateSnapshot] = None
