"""
Cognitive Tracker — accumulates CognitiveSnapshots into UserProfile.

Each Socratic session produces a CognitiveSnapshot. This service:
  1. Loads the current UserProfile from disk
  2. Merges the new snapshot into the cognitive profile fields
  3. Saves the updated profile back to disk

This is the data moat: over time, the profile builds a rich picture
of the user's thinking patterns, biases, and growth areas.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agoracle.adapters.profile.json_profile import JsonProfileStore

from agoracle.domain.types import (
    CognitiveSnapshot,
    SocraticSession,
    UserProfile,
)

logger = logging.getLogger(__name__)


class CognitiveTracker:
    """Accumulate cognitive data from Socratic sessions into UserProfile."""

    def __init__(self, profile_store: JsonProfileStore) -> None:
        self._store = profile_store

    async def record_session(self, session: SocraticSession, user_id: int = 0) -> UserProfile:
        """
        Record a completed Socratic session into the user's cognitive profile.

        Requires explicit opt-in: profile.cognitive_tracking_consent must be True.

        Updates:
          - mode_usage_history: increment socratic count
          - socratic_completion_rate: rolling average of natural completions
          - average_reasoning_quality: rolling average of reasoning scores
          - last_challenge_date: today's ISO date
          - cognitive_quadrant_dist: update based on session outcome
          - growth_zone_topics / comfort_zone_topics: update from blind spots
        """
        profile = await self._store.load(user_id)

        # Consent gate: skip cognitive data recording without consent
        if not getattr(profile, 'cognitive_tracking_consent', False):
            logger.info(
                f"CognitiveTracker: skipping session {session.session_id} "
                f"(cognitive_tracking_consent not granted)"
            )
            return profile

        snapshot = session.cognitive_snapshot

        # --- Mode usage tracking ---
        mode_count = profile.mode_usage_history.get("socratic", 0) + 1
        profile.mode_usage_history["socratic"] = mode_count

        # --- Socratic completion rate (exponential moving average) ---
        completed = 1.0 if session.completed_naturally else 0.0
        if profile.socratic_completion_rate == 0.0 and mode_count == 1:
            profile.socratic_completion_rate = completed
        else:
            # EMA with alpha=0.3 — recent sessions weigh more
            alpha = 0.3
            profile.socratic_completion_rate = (
                alpha * completed
                + (1 - alpha) * profile.socratic_completion_rate
            )

        # --- Average reasoning quality (EMA) ---
        if snapshot and snapshot.reasoning_depth > 0:
            if profile.average_reasoning_quality == 0.0 and mode_count == 1:
                profile.average_reasoning_quality = snapshot.reasoning_depth
            else:
                alpha = 0.3
                profile.average_reasoning_quality = (
                    alpha * snapshot.reasoning_depth
                    + (1 - alpha) * profile.average_reasoning_quality
                )

        # --- Last challenge date ---
        profile.last_challenge_date = datetime.now().strftime("%Y-%m-%d")

        # --- Cognitive quadrant distribution ---
        if snapshot:
            self._update_quadrants(profile, session, snapshot)

        # --- Growth/comfort zone topics ---
        if snapshot and snapshot.blind_spots:
            for bs in snapshot.blind_spots:
                if bs not in profile.growth_zone_topics:
                    profile.growth_zone_topics.append(bs)
            # Keep list manageable
            profile.growth_zone_topics = profile.growth_zone_topics[-20:]

        # --- Bias history (store in satisfaction_history as cognitive events) ---
        if snapshot:
            event = {
                "type": "socratic_session",
                "date": datetime.now().isoformat(),
                "session_id": session.session_id,
                "reasoning_depth": snapshot.reasoning_depth,
                "nuance_recognition": snapshot.nuance_recognition,
                "anchoring_detected": snapshot.anchoring_detected,
                "confirmation_bias": snapshot.confirmation_bias,
                "blind_spots": snapshot.blind_spots,
                "guide_rounds": session.guide_rounds_used,
                "completed_naturally": session.completed_naturally,
                "revealed_early": session.revealed,
            }
            profile.satisfaction_history.append(event)
            # Keep last 100 events
            profile.satisfaction_history = profile.satisfaction_history[-100:]

        await self._store.save(profile, user_id)

        logger.info(
            f"CognitiveTracker: recorded session {session.session_id}, "
            f"sessions={mode_count}, "
            f"quality={profile.average_reasoning_quality:.2f}, "
            f"completion_rate={profile.socratic_completion_rate:.2f}"
        )

        return profile

    def _update_quadrants(
        self,
        profile: UserProfile,
        session: SocraticSession,
        snapshot: CognitiveSnapshot,
    ) -> None:
        """
        Update cognitive quadrant distribution based on session outcome.

        Quadrants (Johari Window / Dunning-Kruger mapping):
          - known_known: things user knows they know (high reasoning + high nuance)
          - known_unknown: things user knows they don't know (asked good questions)
          - unknown_known: intuitive knowledge (decent reasoning but can't articulate)
          - unknown_unknown: blind spots (biases detected + low nuance)
        """
        rd = snapshot.reasoning_depth
        nr = snapshot.nuance_recognition
        has_bias = snapshot.anchoring_detected or snapshot.confirmation_bias

        if rd >= 0.6 and nr >= 0.6:
            profile.cognitive_quadrant_dist["known_known"] += 1
        elif rd < 0.4 and has_bias:
            profile.cognitive_quadrant_dist["unknown_unknown"] += 1
        elif rd >= 0.4 and nr < 0.4:
            profile.cognitive_quadrant_dist["unknown_known"] += 1
        else:
            profile.cognitive_quadrant_dist["known_unknown"] += 1

    async def delete_cognitive_data(self, user_id: int = 0) -> UserProfile:
        """
        Erase all cognitive tracking data from the user's profile.

        Preserves non-cognitive fields (preferences, mode history).
        """
        profile = await self._store.load(user_id)
        profile.cognitive_quadrant_dist = {
            "known_known": 0, "known_unknown": 0,
            "unknown_known": 0, "unknown_unknown": 0,
        }
        profile.growth_zone_topics = []
        profile.comfort_zone_topics = []
        profile.socratic_completion_rate = 0.0
        profile.average_reasoning_quality = 0.0
        profile.last_challenge_date = ""
        profile.reasoning_improvement_trend = []
        # Remove cognitive events from satisfaction_history
        profile.satisfaction_history = [
            e for e in profile.satisfaction_history
            if not (isinstance(e, dict) and e.get("type") == "socratic_session")
        ]
        await self._store.save(profile, user_id)
        logger.info(f"CognitiveTracker: cognitive data erased for user {user_id}")
        return profile

    async def get_cognitive_summary(self, user_id: int = 0) -> str:
        """
        Generate a concise cognitive profile summary for prompt injection.

        Used by the SocraticGuide to personalize guidance based on
        the user's historical thinking patterns.
        """
        profile = await self._store.load(user_id)

        sessions = profile.mode_usage_history.get("socratic", 0)
        if sessions == 0:
            return "新用户，暂无认知画像数据。"

        parts = [
            f"苏格拉底对话次数: {sessions}",
            f"平均推理质量: {profile.average_reasoning_quality:.0%}",
            f"自然完成率: {profile.socratic_completion_rate:.0%}",
        ]

        # Quadrant summary
        q = profile.cognitive_quadrant_dist
        total = sum(q.values()) or 1
        dominant = max(q, key=q.get)
        quadrant_labels = {
            "known_known": "能力区（知道自己知道）",
            "known_unknown": "学习区（知道自己不知道）",
            "unknown_known": "直觉区（不知道自己知道）",
            "unknown_unknown": "盲区（不知道自己不知道）",
        }
        parts.append(f"主要认知区间: {quadrant_labels.get(dominant, dominant)} ({q[dominant]}/{total})")

        if profile.growth_zone_topics:
            parts.append(f"成长方向: {', '.join(profile.growth_zone_topics[-5:])}")

        return "\n".join(parts)
