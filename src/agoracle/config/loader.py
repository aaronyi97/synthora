"""
Configuration loader — reads config.yaml + .env, validates, returns AppConfig.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from agoracle.config.schema import (
    AppConfig,
    FeatureFlags,
    JudgeConfig,
    MemoryConfig,
    ModeConfig,
    ModelConfig,
    QuotaConfig,
    SearchConfig,
)

logger = logging.getLogger(__name__)

def _find_project_root() -> Path:
    """
    Find project root directory.

    Priority:
      1. AGORACLE_ROOT environment variable (explicit override)
      2. Walk up from cwd looking for config.yaml
      3. Walk up from this file's location (fallback for installed packages)
    """
    # 1. Env var override
    env_root = os.getenv("AGORACLE_ROOT")
    if env_root:
        return Path(env_root).resolve()

    # 2. Walk up from cwd
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / "config.yaml").exists():
            return parent

    # 3. Fallback: relative to this file
    return Path(__file__).resolve().parent.parent.parent.parent


PROJECT_ROOT = _find_project_root()


def load_config(
    config_path: str | Path | None = None,
    env_path: str | Path | None = None,
) -> AppConfig:
    """
    Load and validate application configuration.

    Args:
        config_path: Path to config.yaml. Defaults to PROJECT_ROOT/config.yaml.
        env_path: Path to .env file. Defaults to PROJECT_ROOT/.env.

    Returns:
        Validated AppConfig instance.
    """
    # Load .env
    if env_path is None:
        env_path = PROJECT_ROOT / ".env"
    if Path(env_path).exists():
        load_dotenv(env_path)
        logger.info(f"Loaded .env from {env_path}")

    # Load config.yaml
    if config_path is None:
        config_path = PROJECT_ROOT / "config.yaml"
    config_path = Path(config_path)

    if not config_path.exists():
        logger.warning(f"Config file not found at {config_path}, using defaults")
        return _build_default_config()

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    return _parse_config(raw)


def _parse_config(raw: dict) -> AppConfig:
    """Parse raw YAML dict into typed AppConfig."""
    config = AppConfig()

    # Parse models
    for model_id, model_data in raw.get("models", {}).items():
        mc = ModelConfig(
            id=model_id,
            name=model_data.get("name", model_id),
            provider=model_data.get("provider", "openai"),
            model_name=model_data.get("model_name", ""),
            api_key_env=model_data.get("api_key_env", ""),
            api_key_env_list=model_data.get("api_key_env_list", []),
            base_url_env=model_data.get("base_url_env", ""),
            supports_search=model_data.get("supports_search", False),
            supports_streaming=model_data.get("supports_streaming", True),
            max_tokens=model_data.get("max_tokens", 4096),
            timeout_seconds=model_data.get("timeout_seconds", 30),
            temperature=model_data.get("temperature", None),
            thinking_enabled=model_data.get("thinking_enabled", False),
            search_style=model_data.get("search_style", "extra_body"),
            reasoning_effort=model_data.get("reasoning_effort", None),
            max_concurrency=model_data.get("max_concurrency", 0),
            cost_per_1m_input=model_data.get("cost_per_1m_input", 0.0),
            cost_per_1m_output=model_data.get("cost_per_1m_output", 0.0),
        )
        config.models[model_id] = mc

    # Parse modes
    _known_mode_fields = {f.name for f in ModeConfig.__dataclass_fields__.values()}
    for mode_name, mode_data in raw.get("modes", {}).items():
        # Warn about config keys that ModeConfig doesn't understand (silent drop)
        unknown = set(mode_data.keys()) - _known_mode_fields
        if unknown:
            logger.warning(
                f"Mode '{mode_name}' has config keys not in ModeConfig schema "
                f"(will be ignored): {sorted(unknown)}"
            )
        md = ModeConfig(
            name=mode_name,
            contributors=mode_data.get("contributors", []),
            judge=mode_data.get("judge", ""),
            extractor=mode_data.get("extractor", ""),
            question_critic=mode_data.get("question_critic", ""),
            answer_critic=mode_data.get("answer_critic", ""),
            n_of_m=mode_data.get("n_of_m", 0),
            critique_always_on=mode_data.get("critique_always_on", False),
            max_timeout_seconds=mode_data.get("max_timeout_seconds", 30),
            refinement_timeout_seconds=mode_data.get("refinement_timeout_seconds", 60),
            disable_best_single=mode_data.get("disable_best_single", False),
            best_single_override_gap=mode_data.get("best_single_override_gap", 0.0),
            default_output_depth=mode_data.get("default_output_depth", "level_1"),
            max_refinement_rounds=mode_data.get("max_refinement_rounds", 1),
            auto_escalate=mode_data.get("auto_escalate", False),
            smart_routing=mode_data.get("smart_routing", True),
            moa_layers=mode_data.get("moa_layers", 1),
            divergence_analyzer=mode_data.get("divergence_analyzer", ""),
            guide_generator=mode_data.get("guide_generator", ""),
            max_guide_rounds=mode_data.get("max_guide_rounds", 5),
            reveal_on_demand=mode_data.get("reveal_on_demand", True),
            skip_judge=mode_data.get("skip_judge", False),
            preflight_clarity_check=mode_data.get("preflight_clarity_check", ""),
        )
        config.modes[mode_name] = md

    # Parse judge
    judge_data = raw.get("judge", {})
    qg = judge_data.get("quality_gate", {})
    config.judge = JudgeConfig(
        light_model=judge_data.get("light_model", "gemini_3_pro"),
        deep_model=judge_data.get("deep_model", "claude_opus"),
        extractor_model=judge_data.get("extractor_model", "gemini_3_flash"),
        quality_gate_enabled=judge_data.get("quality_gate_enabled", True),
        judge_fallback_chain=judge_data.get("judge_fallback_chain", ["claude_sonnet_thinking", "gemini_3_pro"]),
        refine_fallback=judge_data.get("refine_fallback", "claude_sonnet_thinking"),
        extractor_fallback_chain=judge_data.get("extractor_fallback_chain", ["gemini_3_flash"]),
        best_single_gap_threshold=float(qg.get("best_single_gap_threshold", 0.3)),
        best_single_min_score=float(qg.get("best_single_min_score", 0.7)),
        low_confidence_avg_threshold=float(qg.get("low_confidence_avg_threshold", 0.4)),
        low_confidence_meta_threshold=float(qg.get("low_confidence_meta_threshold", 0.3)),
        divergence_confidence_threshold=float(qg.get("divergence_confidence_threshold", 0.5)),
        answer_critic_confidence_threshold=float(qg.get("answer_critic_confidence_threshold", 0.8)),
        moa_weak_exclude_margin=float(qg.get("moa_weak_exclude_margin", 0.25)),
    )

    # Parse search
    search_data = raw.get("search", {})
    config.search = SearchConfig(
        enabled=search_data.get("enabled", True),
        provider=search_data.get("provider", "tavily"),
        api_key_env=search_data.get("api_key_env", "TAVILY_API_KEY"),
        max_results=search_data.get("max_results", 5),
        search_depth=search_data.get("search_depth", "basic"),
        include_answer=search_data.get("include_answer", True),
        timeout_seconds=search_data.get("timeout_seconds", 15),
        max_chars=search_data.get("max_chars", 3000),
        modes=search_data.get("modes", ["deep", "research"]),
    )

    # Parse memory
    mem_data = raw.get("memory", {})
    config.memory = MemoryConfig(
        session_dir=mem_data.get("session_dir", "data/sessions"),
        knowledge_dir=mem_data.get("knowledge_dir", "data/chromadb"),
        notes_dir=mem_data.get("notes_dir", "data/notes"),
        logs_dir=mem_data.get("logs_dir", "data/logs"),
        profile_path=mem_data.get("profile_path", "data/profile.json"),
        session_timeout_minutes=mem_data.get("session_timeout_minutes", 30),
        max_session_turns=mem_data.get("max_session_turns", 50),
    )

    # Parse quota
    quota_data = raw.get("quota", {})
    config.quota = QuotaConfig(
        light=quota_data.get("light", 100),
        deep=quota_data.get("deep", 20),
        research=quota_data.get("research", 5),
        socratic=quota_data.get("socratic", 10),
        roundtable=quota_data.get("roundtable", 5),
        enabled=quota_data.get("enabled", True),
    )

    # Parse feature flags
    ff_data = raw.get("features", {})
    config.features = FeatureFlags(
        planner_lite=ff_data.get("planner_lite", False),
        multi_query_search=ff_data.get("multi_query_search", False),
        semantic_check=ff_data.get("semantic_check", True),
        fact_check_shadow=ff_data.get("fact_check_shadow", False),
        iterative_search=ff_data.get("iterative_search", False),
        post_synthesis_verify=ff_data.get("post_synthesis_verify", False),
        nsg_enabled=ff_data.get("nsg_enabled", True),
        guidance_v1=ff_data.get("guidance_v1", True),
        roundtable_enabled=ff_data.get("roundtable_enabled", False),
        roundtable_moderator_model=ff_data.get("roundtable_moderator_model", ""),
    )

    # Auth
    config.auth_password = os.getenv("AUTH_PASSWORD", "")
    config.session_secret = os.getenv("SESSION_SECRET", "")

    # Fail-closed: SESSION_SECRET must be set in production (CWE-330)
    if os.getenv("ENV", "development").strip().lower() == "production" and len(config.session_secret) < 32:
        raise RuntimeError(
            "FATAL: SESSION_SECRET must be ≥32 characters in production. "
            "Generate one: python3 -c \"import secrets; print(secrets.token_hex(32))\""
        )
    config.host = raw.get("server", {}).get("host", "127.0.0.1")
    config.port = raw.get("server", {}).get("port", 8000)

    # Gate 6: warn about unknown config fields (catch orphan keys like cognitive_profile_path)
    _warn_unknown_fields(raw)

    _validate_config(config)
    return config


def _warn_unknown_fields(raw: dict) -> None:
    """Warn about config.yaml keys that no section consumes."""
    known_top = {"models", "modes", "judge", "search", "memory", "quota", "server", "features"}
    for key in raw:
        if key not in known_top:
            logger.warning(f"config.yaml: unknown top-level key '{key}' (not consumed)")

    known_memory = {"session_dir", "knowledge_dir", "notes_dir", "logs_dir",
                    "profile_path", "session_timeout_minutes", "max_session_turns"}
    for key in raw.get("memory", {}):
        if key not in known_memory:
            logger.warning(f"config.yaml memory: unknown key '{key}' (not consumed by MemoryConfig)")

    known_quota = {"enabled", "light", "deep", "research", "socratic"}
    for key in raw.get("quota", {}):
        if key not in known_quota:
            logger.warning(f"config.yaml quota: unknown key '{key}'")


def _validate_config(config: AppConfig) -> None:
    """Validate configuration and log warnings."""
    # Check that mode model references exist
    for mode_name, mode in config.modes.items():
        for model_id in mode.contributors:
            if model_id not in config.models:
                logger.warning(
                    f"Mode '{mode_name}' references unknown contributor '{model_id}'"
                )
        for role_field in ("judge", "extractor", "question_critic", "answer_critic"):
            ref = getattr(mode, role_field, "")
            if ref and ref not in config.models:
                logger.warning(
                    f"Mode '{mode_name}' {role_field} '{ref}' not in models"
                )

    # Check judge config references
    for role_field in ("light_model", "deep_model", "extractor_model", "refine_fallback"):
        ref = getattr(config.judge, role_field, "")
        if ref and ref not in config.models:
            logger.warning(
                f"Judge config {role_field} '{ref}' not in models"
            )
    for fb_id in config.judge.judge_fallback_chain:
        if fb_id not in config.models:
            logger.warning(
                f"Judge config judge_fallback_chain references unknown model '{fb_id}'"
            )

    # Check API keys are set
    for model_id, model in config.models.items():
        if model.api_key_env and not os.getenv(model.api_key_env):
            logger.warning(
                f"Model '{model_id}' API key env '{model.api_key_env}' not set"
            )


def _build_default_config() -> AppConfig:
    """Build a minimal default config when config.yaml is missing."""
    return AppConfig()
