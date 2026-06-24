"""
Configuration schema — typed dataclasses for all config sections.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ModelConfig:
    """Configuration for one AI model."""
    id: str = ""
    name: str = ""
    provider: str = ""            # openai / gemini / anthropic / moonshot / deepseek
    model_name: str = ""          # actual model name for API (e.g. "gpt-5.3-codex")
    api_key_env: str = ""         # env var name for API key (single key)
    api_key_env_list: list[str] = field(default_factory=list)  # multiple keys for rotation (overrides api_key_env)
    base_url_env: str = ""        # env var name for base URL
    supports_search: bool = False
    supports_streaming: bool = True
    max_tokens: int = 4096
    timeout_seconds: int = 30

    # Per-model overrides (None = use adapter defaults)
    temperature: float | None = None          # None → adapter default (0.7); K2.5 requires 1.0/0.6
    thinking_enabled: bool = False            # Kimi K2.5 / models that accept a thinking param
    search_style: str = "extra_body"          # "extra_body" | "kimi_builtin" | "none"
    reasoning_effort: str | None = None       # GPT-5.4+: "none" | "low" | "medium" | "high" | "xhigh"
    max_concurrency: int = 0                  # 0 = unlimited; >0 = max concurrent API calls (Semaphore)
    proxy_env: str = ""                        # optional: env var name holding a per-model proxy URL (empty = direct connection)

    # v2.5: Per-model pricing (USD per 1M tokens)
    cost_per_1m_input: float = 0.0            # 0 = unknown/free
    cost_per_1m_output: float = 0.0           # 0 = unknown/free


@dataclass
class ModeConfig:
    """Configuration for one orchestration mode."""
    name: str = ""
    contributors: list[str] = field(default_factory=list)    # model IDs
    judge: str = ""               # model ID for Judge
    extractor: str = ""           # model ID for metadata extractor (parallel)
    question_critic: str = ""     # model ID for question critique
    answer_critic: str = ""       # model ID for answer critique (Deep only)
    n_of_m: int = 0               # 0 = wait for all; >0 = proceed when N models respond
    critique_always_on: bool = False
    max_timeout_seconds: int = 30
    disable_best_single: bool = False  # v2.2: if True, Quality Gate never uses BEST_SINGLE path
    best_single_override_gap: float = 0.0  # v3.5: override disable_best_single when score_gap exceeds this
    default_output_depth: str = "level_1"  # v2.0: default output depth for this mode
    max_refinement_rounds: int = 1    # v2.3: how many Answer Critic → Judge Refine cycles
    auto_escalate: bool = False       # v2.3: if True, auto-escalate to Deep on LOW_CONFIDENCE
    smart_routing: bool = True           # v2.4: if False, skip question_type pipeline adjustment
    moa_layers: int = 1                  # v2.4.3: Mixture of Agents layers (1=standard, 2=MoA second pass)

    # v2.5: Socratic mode fields (Phase 3)
    divergence_analyzer: str = ""         # model ID for divergence analysis (fast model)
    guide_generator: str = ""             # model ID for Socratic guide questions (fast model)
    max_guide_rounds: int = 5             # max Socratic dialogue rounds before auto-reveal
    reveal_on_demand: bool = True         # allow user to request early reveal

    # v5.2: per-refinement hard timeout (seconds) — avoids a single slow critic blocking the pipeline
    refinement_timeout_seconds: int = 60

    # v2.7.5: Light fast path + Deep preflight
    skip_judge: bool = False               # skip Judge synthesis, return best single response directly
    preflight_clarity_check: str = ""      # model ID for question clarity pre-check ("" = disabled)


@dataclass
class JudgeConfig:
    """Configuration for Judge behavior."""
    light_model: str = ""         # model ID for Light mode Judge
    deep_model: str = ""          # model ID for Deep/Research Judge
    extractor_model: str = ""     # model ID for parallel metadata extraction
    quality_gate_enabled: bool = True

    # Fallback chains (config-driven, no hardcoded model IDs in orchestrator)
    judge_fallback_chain: list[str] = field(default_factory=lambda: ["claude_sonnet_thinking", "gemini_3_pro"])
    refine_fallback: str = "claude_sonnet_thinking"
    extractor_fallback_chain: list[str] = field(default_factory=lambda: ["gemini_3_flash"])  # v4.15: fallback when primary extractor fails

    # Quality Gate thresholds (tunable via config.yaml)
    best_single_gap_threshold: float = 0.3      # score gap to adopt best single
    best_single_min_score: float = 0.7         # min score for best single
    low_confidence_avg_threshold: float = 0.4  # avg score below → low confidence
    low_confidence_meta_threshold: float = 0.3 # metadata confidence below → low confidence
    divergence_confidence_threshold: float = 0.5  # divergence + conf below → low confidence
    answer_critic_confidence_threshold: float = 0.8  # below → trigger answer critic
    moa_weak_exclude_margin: float = 0.25              # v4.1: MoA 弱过滤 margin（composite < avg - margin → 排除）


@dataclass
class SearchConfig:
    """Configuration for the unified search layer (Phase 2)."""
    enabled: bool = True                  # master switch
    provider: str = "tavily"              # "tavily" | "serpapi" | "none"
    api_key_env: str = "TAVILY_API_KEY"   # env var name for search API key
    max_results: int = 5                  # number of search results to fetch
    search_depth: str = "basic"           # "basic" (fast) or "advanced" (slower, better)
    include_answer: bool = True           # include provider's AI summary
    timeout_seconds: int = 15             # search timeout
    max_chars: int = 3000                 # max chars injected into prompt
    modes: list[str] = field(default_factory=lambda: ["deep", "research"])  # modes that use search


@dataclass
class MemoryConfig:
    """Configuration for memory subsystems."""
    session_dir: str = "data/sessions"
    knowledge_dir: str = "data/chromadb"
    notes_dir: str = "data/notes"
    logs_dir: str = "data/logs"
    profile_path: str = "data/profile.json"
    session_timeout_minutes: int = 30
    max_session_turns: int = 50


@dataclass
class QuotaConfig:
    """Per-user daily usage quota (v2.7.5)."""
    light: int = 100               # max Light queries per user per day
    deep: int = 20                 # max Deep queries per user per day
    research: int = 5              # max Research queries per user per day
    socratic: int = 10             # max Socratic sessions per user per day
    roundtable: int = 5            # max Roundtable sessions per user per day
    enabled: bool = True           # master switch


@dataclass
class FeatureFlags:
    """Feature flags for gradual rollout (Phase 4.5+). Each flag is independently togglable."""
    # Phase 4.5: Planner-lite — gemini_flash outputs sub_questions injected into contributor prompts
    # Only fires for ANALYTICAL / REASONING / CONTROVERSIAL question types in Research mode
    planner_lite: bool = False

    # Phase 4.5: multi-query search — Planner search_queries run as parallel Tavily calls
    # Requires planner_lite=True to generate search_queries; independent switch for attribution
    multi_query_search: bool = False

    # Phase 4.1: semantic check — gemini_flash pairwise judgment of synthesis vs best_single
    # True = v4.1 behavior (pairwise LLM check); False = v3.10 behavior (length heuristic < 70%)
    # Set to False to isolate v4.1's impact in A/B benchmark experiments
    semantic_check: bool = True

    # Phase 5: fact consistency check in shadow mode — scores only, does NOT block/redirect
    # Set to True to collect unsupported_claim_rate baseline before enabling enforcement
    fact_check_shadow: bool = False

    # Phase 5.1: iterative search — gap detection after fan-out, supplementary Tavily search
    # gemini_flash identifies topics in contributor answers not covered by initial search
    # → generates 2-3 gap_queries → search_multi → extends _tavily_search_results
    # Only fires in research mode when Tavily search is active
    iterative_search: bool = False

    # Phase 5.2: post-synthesis verification — gemini_flash scans Judge output for new
    # unverified claims introduced during synthesis (e.g. from [OTHERS] section)
    # Appends warnings to fact_warnings; does NOT modify final_answer
    # Only fires in research mode when Tavily search results are available
    post_synthesis_verify: bool = False

    # v5.0: Next-Step Guidance — unified post-answer suggestions
    # true = generate suggestions after each answer; false = skip entirely
    nsg_enabled: bool = True

    # v5.2: Roundtable mode — multi-model public debate
    roundtable_enabled: bool = False

    # v5.2: Roundtable moderator model — used for S2 dispute mapping + S4 decision synthesis
    roundtable_moderator_model: str = ""

    # v5.2: Canonical guidance protocol — GuidanceOutput as single source of truth
    # true = GuidanceOutput is the single source; legacy next_steps/companion_guide are derived
    guidance_v1: bool = True


@dataclass
class AppConfig:
    """Top-level application configuration."""
    models: dict[str, ModelConfig] = field(default_factory=dict)
    modes: dict[str, ModeConfig] = field(default_factory=dict)
    judge: JudgeConfig = field(default_factory=JudgeConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    quota: QuotaConfig = field(default_factory=QuotaConfig)
    features: FeatureFlags = field(default_factory=FeatureFlags)
    auth_password: str = ""
    session_secret: str = ""
    host: str = "127.0.0.1"
    port: int = 8000
