"""
JSON file-based user profile storage.

Phase 0/1 implementation: load/save UserProfile as JSON.
v2.7: Per-user profile isolation — each user gets their own JSON file.
Phase 5: learning algorithm populates profile from interaction history.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agoracle.domain.types import UserProfile

logger = logging.getLogger(__name__)

_PROFILE_SUMMARY_LABELS = {
    "zh-CN": {
        "lang_pref": "语言偏好",
        "depth_pref": "深度偏好",
        "user_rules": "用户规则",
        "expert_domains": "擅长领域",
        "depth_levels": {1: "初识", 2: "框架", 3: "深入", 4: "精通", 5: "验证"},
        "deep_topics": "深度话题（可跳过基础直接深入）",
        "new_topics": "新接触话题（需要更多背景解释）",
        "high_freq": "高频关注",
        "recent": "最近讨论过（可适当关联，无需强行提及）",
        "new_user": "新用户，暂无画像数据",
    },
    "en-US": {
        "lang_pref": "Language preference",
        "depth_pref": "Depth preference",
        "user_rules": "User rules",
        "expert_domains": "Expert domains",
        "depth_levels": {1: "Novice", 2: "Framework", 3: "Deep", 4: "Mastery", 5: "Verified"},
        "deep_topics": "Deep topics (skip basics, go straight to advanced)",
        "new_topics": "New topics (provide more background)",
        "high_freq": "Frequently explored",
        "recent": "Recently discussed (may reference naturally, no need to force)",
        "new_user": "New user, no profile data yet",
    },
}


def _normalize_locale(raw: str | None) -> str:
    if not raw:
        return "zh-CN"
    value = raw.strip().replace("_", "-").lower()
    if value.startswith("en"):
        return "en-US"
    if value.startswith("zh"):
        return "zh-CN"
    return "zh-CN"


class JsonProfileStore:
    """
    User profile store backed by per-user JSON files.

    Storage layout (v2.7):
      {base_dir}/user_{user_id}.json   (per-user profile)
      {base_dir}/default.json          (fallback for user_id=0)

    Backward-compatible: load()/save() with no user_id uses default.
    """

    def __init__(self, profile_path: str | Path) -> None:
        self._base_dir = Path(profile_path).parent
        self._base_dir.mkdir(parents=True, exist_ok=True)
        # Legacy path for backward compat with existing single-file profile
        self._legacy_path = Path(profile_path)

    def _path_for(self, user_id: int = 0) -> Path:
        """Get the profile file path for a given user."""
        if user_id:
            uid = int(user_id)  # enforce int to prevent path traversal
            return self._base_dir / f"user_{uid}.json"
        # Legacy default
        return self._legacy_path

    async def load(self, user_id: int = 0) -> UserProfile:
        """Load user profile from JSON file, applying TTL cleanup (P1-4)."""
        path = self._path_for(user_id)
        if not path.exists():
            logger.debug(f"Profile not found at {path}, creating default for user {user_id}")
            profile = UserProfile()
            await self.save(profile, user_id)
            return profile

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            profile = self._deserialize(data)
            dirty = self._apply_ttl(profile)
            if dirty:
                await self.save(profile, user_id)
            return profile
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Corrupt profile file {path}: {e}, returning default")
            return UserProfile()

    @staticmethod
    def _apply_ttl(profile: UserProfile) -> bool:
        """Apply data lifecycle TTL rules (P1-4, 禁令 #19).

        Returns True if any data was trimmed (caller should save).
        """
        dirty = False

        # topic_sequence: max 200 entries (already enforced on write, but safety net)
        if len(profile.topic_sequence) > 200:
            profile.topic_sequence = profile.topic_sequence[-200:]
            dirty = True

        # daily_mode_dist: max 90 days
        if len(profile.daily_mode_dist) > 90:
            sorted_dates = sorted(profile.daily_mode_dist.keys())
            for old_date in sorted_dates[:-90]:
                del profile.daily_mode_dist[old_date]
            dirty = True

        # satisfaction_history: max 100 entries
        if len(profile.satisfaction_history) > 100:
            profile.satisfaction_history = profile.satisfaction_history[-100:]
            dirty = True

        # session_durations_min: max 100 entries
        if len(profile.session_durations_min) > 100:
            profile.session_durations_min = profile.session_durations_min[-100:]
            dirty = True

        # reasoning_improvement_trend: max 50 data points
        if len(profile.reasoning_improvement_trend) > 50:
            profile.reasoning_improvement_trend = profile.reasoning_improvement_trend[-50:]
            dirty = True

        return dirty

    async def save(self, profile: UserProfile, user_id: int = 0) -> None:
        """Save user profile to JSON file (atomic write)."""
        path = self._path_for(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        try:
            data = self._serialize(profile)
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception as e:
            logger.error(f"Failed to save profile for user {user_id}: {e}")
            if tmp.exists():
                tmp.unlink()

    async def get_summary(self, user_id: int = 0, language: str | None = None) -> str:
        """Generate a concise text summary for prompt injection.

        Includes topic depth awareness (P1-2): tells the model how familiar
        the user is with specific topics, so answers can skip basics for
        advanced users or provide more context for newcomers.
        """
        profile = await self.load(user_id)
        locale = _normalize_locale(language or profile.preferred_language)
        labels = _PROFILE_SUMMARY_LABELS[locale]
        preferred_language = _normalize_locale(profile.preferred_language)

        parts: list[str] = []
        parts.append(f"{labels['lang_pref']}: {preferred_language}")
        parts.append(f"{labels['depth_pref']}: {profile.preferred_depth}")

        if profile.explicit_rules:
            parts.append(f"{labels['user_rules']}: {'; '.join(profile.explicit_rules[:5])}")

        if profile.topic_expertise:
            top_topics = sorted(
                profile.topic_expertise.items(),
                key=lambda x: x[1],
                reverse=True,
            )[:5]
            expertise_str = ", ".join(f"{t}({s:.1f})" for t, s in top_topics)
            parts.append(f"{labels['expert_domains']}: {expertise_str}")

        # P1-2: Topic depth awareness — adjust answer depth based on familiarity
        depth_map = getattr(profile, "topic_depth_map", {})
        if depth_map:
            advanced = [t for t, d in depth_map.items() if d >= 3]
            beginner = [t for t, d in depth_map.items() if d <= 1]
            if advanced:
                parts.append(
                    f"{labels['deep_topics']}: {', '.join(advanced[:8])}"
                )
            if beginner:
                parts.append(
                    f"{labels['new_topics']}: {', '.join(beginner[:5])}"
                )

        # Frequently explored topics (behavioral signal)
        freq = profile.topic_frequency
        if freq:
            top_freq = sorted(freq.items(), key=lambda x: x[1], reverse=True)[:5]
            if top_freq[0][1] >= 3:
                if locale == "en-US":
                    freq_summary = ", ".join(f"{t}({c}x)" for t, c in top_freq)
                else:
                    freq_summary = ", ".join(f"{t}({c}次)" for t, c in top_freq)
                parts.append(
                    f"{labels['high_freq']}: {freq_summary}"
                )

        # Memory-lite: inject recent turns for cross-session continuity
        recent_turns = getattr(profile, "recent_turns", [])
        if recent_turns:
            snippets = []
            for t in recent_turns[-3:]:
                summary = t.get("summary") or t.get("question_snippet", "")
                topic = t.get("topic", "")
                if summary:
                    label = f"[{topic}] {summary}" if topic else summary
                    snippets.append(label)
            if snippets:
                parts.append(f"{labels['recent']}: {' | '.join(snippets)}")

        return "\n".join(parts) if parts else labels["new_user"]

    @staticmethod
    def _serialize(profile: UserProfile) -> dict:
        """Convert UserProfile to JSON-serializable dict."""
        return {
            # PreferenceProfile
            "preferred_language": profile.preferred_language,
            "preferred_depth": profile.preferred_depth,
            "explicit_rules": profile.explicit_rules,
            "topic_frequency": profile.topic_frequency,
            "topic_expertise": profile.topic_expertise,
            "topic_last_asked": profile.topic_last_asked,
            "mode_preference": profile.mode_preference,
            "satisfaction_history": profile.satisfaction_history,
            # CognitiveProfile (v2.0)
            "cognitive_tracking_consent": profile.cognitive_tracking_consent,
            "cognitive_quadrant_dist": profile.cognitive_quadrant_dist,
            "comfort_zone_topics": profile.comfort_zone_topics,
            "growth_zone_topics": profile.growth_zone_topics,
            "mode_usage_history": profile.mode_usage_history,
            "socratic_completion_rate": profile.socratic_completion_rate,
            "average_reasoning_quality": profile.average_reasoning_quality,
            "last_challenge_date": profile.last_challenge_date,
            # CBA Behavioral Analytics (v2.7 — ADR-014)
            "reasoning_improvement_trend": profile.reasoning_improvement_trend,
            "topic_sequence": profile.topic_sequence,
            "hourly_query_dist": profile.hourly_query_dist,
            "daily_mode_dist": profile.daily_mode_dist,
            "session_durations_min": profile.session_durations_min,
            # Topic Depth Map (v2.0 §5.4.3)
            "topic_depth_map": profile.topic_depth_map,
            # Proactive Coaching
            "improvement_plans": profile.improvement_plans,
            # Memory-lite (方案A)
            "recent_turns": profile.recent_turns,
        }

    @staticmethod
    def _deserialize(data: dict) -> UserProfile:
        """Convert dict to UserProfile."""
        return UserProfile(
            preferred_language=data.get("preferred_language", "zh-CN"),
            preferred_depth=data.get("preferred_depth", "detailed"),
            explicit_rules=data.get("explicit_rules", []),
            topic_frequency=data.get("topic_frequency", {}),
            topic_expertise=data.get("topic_expertise", {}),
            topic_last_asked=data.get("topic_last_asked", {}),
            mode_preference=data.get("mode_preference", {}),
            satisfaction_history=data.get("satisfaction_history", []),
            cognitive_tracking_consent=data.get("cognitive_tracking_consent", False),
            cognitive_quadrant_dist=data.get("cognitive_quadrant_dist", {
                "known_known": 0, "known_unknown": 0,
                "unknown_known": 0, "unknown_unknown": 0,
            }),
            comfort_zone_topics=data.get("comfort_zone_topics", []),
            growth_zone_topics=data.get("growth_zone_topics", []),
            mode_usage_history=data.get("mode_usage_history", {}),
            socratic_completion_rate=data.get("socratic_completion_rate", 0.0),
            average_reasoning_quality=data.get("average_reasoning_quality", 0.0),
            last_challenge_date=data.get("last_challenge_date", ""),
            # CBA fields (v2.7)
            reasoning_improvement_trend=data.get("reasoning_improvement_trend", []),
            topic_sequence=data.get("topic_sequence", []),
            hourly_query_dist=data.get("hourly_query_dist", {}),
            daily_mode_dist=data.get("daily_mode_dist", {}),
            session_durations_min=data.get("session_durations_min", []),
            topic_depth_map=data.get("topic_depth_map", {}),
            improvement_plans=data.get("improvement_plans", []),
            recent_turns=data.get("recent_turns", []),
        )
