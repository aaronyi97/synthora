"""
Proactive Coach Service — the cognitive coaching engine.

Bridges observation (BehaviorAnalytics) and intervention (SocraticGuide):
  1. Detects improvement opportunities from user behavior patterns
  2. Proposes ImprovementPlans when sustained shallow interest is detected
  3. Generates micro-Socratic challenges woven into daily Q&A answers
  4. Tracks plan progress and adjusts difficulty
  5. Produces capability map data for frontend visualization

This is the killer differentiator: no competitor tracks cognitive growth
over time and proactively coaches users toward deeper understanding.

Integration points:
  - BehaviorAnalytics.check_companion_hints() → delegates to coach for plan-based hints
  - API /ask response → coach hint embedded in companion_hint field
  - API /capability-map → aggregated progress data for frontend
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agoracle.adapters.profile.json_profile import JsonProfileStore

from agoracle.domain.types import ImprovementPlan, UserProfile

logger = logging.getLogger(__name__)

# ── Constants ──
MAX_ACTIVE_PLANS = 5
PLAN_PROPOSAL_COOLDOWN_DAYS = 3   # don't propose new plans too often
CHALLENGE_COOLDOWN_HOURS = 4      # min hours between micro-challenges
MIN_TOPIC_FREQ_FOR_PLAN = 4       # topic must be asked 4+ times
MAX_DEPTH_FOR_PLAN = 2            # only propose if depth ≤ L2

_DEPTH_LABELS = {
    1: "L1 触达",
    2: "L2 框架",
    3: "L3 细节",
    4: "L4 方案",
    5: "L5 验证",
}

# Micro-challenge templates by difficulty level (1-5)
_CHALLENGE_TEMPLATES = {
    1: [
        "顺便想问你：关于「{topic}」，你觉得最核心的一个概念是什么？",
        "聊到这个，我好奇：你能用一句话概括「{topic}」的本质吗？",
    ],
    2: [
        "这让我想到一个问题：「{topic}」中，最常被误解的一点是什么？你怎么看？",
        "关于「{topic}」，如果要你给一个完全不懂的人解释，你会怎么说？",
    ],
    3: [
        "深入一步：在「{topic}」领域，你觉得当前最大的争议或分歧点在哪里？",
        "挑战一下：如果「{topic}」的主流观点是错的，最可能错在哪个假设上？",
    ],
    4: [
        "高阶问题：如果你要基于「{topic}」设计一个解决方案，你的第一步会是什么？为什么？",
        "思维实验：假设「{topic}」领域发生了颠覆性变化，谁会受益最大？谁会受损？",
    ],
    5: [
        "终极检验：你能用「{topic}」的知识，预测一个还没发生但很可能发生的趋势吗？",
        "反思：回顾你对「{topic}」的理解历程，哪个时刻你的认知发生了根本性的转变？",
    ],
}


class ProactiveCoachService:
    """
    Cognitive coaching engine that detects opportunities and drives
    progressive improvement through micro-Socratic challenges.
    """

    def __init__(self, profile_store: JsonProfileStore) -> None:
        self._store = profile_store

    # ── Plan Detection & Proposal ──

    async def detect_plan_opportunity(
        self, question: str, topic_tags: list[str], user_id: int = 0,
    ) -> dict | None:
        """
        Check if user's behavior pattern warrants proposing an improvement plan.

        Triggers when:
          1. Topic asked 4+ times
          2. Depth still ≤ L2 (framework level)
          3. No existing active plan for this topic
          4. Cooldown period respected (not too many proposals)

        Returns a plan proposal hint dict or None.
        """
        if not topic_tags:
            return None

        profile = await self._store.load(user_id)

        # Check cooldown: don't propose if we proposed recently
        today = datetime.now().strftime("%Y-%m-%d")
        last_proposal = self._get_meta(profile, "_last_plan_proposal")
        if last_proposal:
            try:
                days_since = (datetime.now() - datetime.fromisoformat(last_proposal)).days
                if days_since < PLAN_PROPOSAL_COOLDOWN_DAYS:
                    return None
            except (ValueError, TypeError):
                pass

        # Check if too many active plans already
        active_plans = [
            p for p in profile.improvement_plans
            if p.get("status") in ("proposed", "active")
        ]
        if len(active_plans) >= MAX_ACTIVE_PLANS:
            return None

        # Find topics that qualify
        for tag in topic_tags:
            tag_lower = tag.lower().strip()
            if not tag_lower:
                continue

            freq = profile.topic_frequency.get(tag, 0)
            depth = profile.topic_depth_map.get(tag_lower, 0)

            # Already has a plan for this topic?
            existing = any(
                p.get("topic", "").lower() == tag_lower
                and p.get("status") in ("proposed", "active")
                for p in profile.improvement_plans
            )
            if existing:
                continue

            if freq >= MIN_TOPIC_FREQ_FOR_PLAN and depth <= MAX_DEPTH_FOR_PLAN:
                # Create proposed plan
                plan = ImprovementPlan(
                    topic=tag,
                    current_level=depth,
                    target_level=4,  # aim for L4 (方案级)
                    difficulty=1,
                )
                profile.improvement_plans.append(asdict(plan))

                # Record proposal timestamp
                self._set_meta(profile, "_last_plan_proposal", datetime.now().isoformat())
                await self._store.save(profile, user_id)

                logger.info(
                    f"ProactiveCoach: proposed plan for user={user_id} "
                    f"topic={tag} (freq={freq}, depth=L{depth})"
                )

                return {
                    "type": "coach_plan_proposal",
                    "plan_id": plan.plan_id,
                    "topic": tag,
                    "current_depth": depth,
                    "current_depth_label": _DEPTH_LABELS.get(depth, f"L{depth}"),
                    "target_depth_label": _DEPTH_LABELS.get(4, "L4 方案"),
                    "times_explored": freq,
                    "message": (
                        f"我注意到你已经第 {freq} 次探索「{tag}」了，"
                        f"目前在 {_DEPTH_LABELS.get(depth, f'L{depth}')} 阶段。"
                        f"要不要我帮你制定一个深入计划，"
                        f"目标是达到 {_DEPTH_LABELS.get(4, 'L4 方案')} 级别？"
                    ),
                    "suggestions": [
                        {"mode": "accept_plan", "label": f"好的，帮我制定「{tag}」提升计划"},
                        {"mode": "deep", "label": "先用 Deep 模式深入一下"},
                    ],
                }

        return None

    async def activate_plan(
        self, plan_id: str, user_id: int = 0,
    ) -> dict | None:
        """Activate a proposed plan (user accepted the proposal)."""
        profile = await self._store.load(user_id)

        for plan in profile.improvement_plans:
            if plan.get("plan_id") == plan_id and plan.get("status") == "proposed":
                plan["status"] = "active"
                plan["updated_at"] = datetime.now().isoformat()
                await self._store.save(profile, user_id)
                logger.info(f"ProactiveCoach: plan {plan_id} activated for user={user_id}")
                return plan

        return None

    async def abandon_plan(
        self, plan_id: str, user_id: int = 0,
    ) -> dict | None:
        """Abandon (deactivate) a plan."""
        profile = await self._store.load(user_id)

        for plan in profile.improvement_plans:
            if plan.get("plan_id") == plan_id and plan.get("status") in ("proposed", "active"):
                plan["status"] = "abandoned"
                plan["updated_at"] = datetime.now().isoformat()
                await self._store.save(profile, user_id)
                logger.info(f"ProactiveCoach: plan {plan_id} abandoned for user={user_id}")
                return plan

        return None

    # ── Micro-Challenge Generation ──

    async def check_micro_challenge(
        self, question: str, topic_tags: list[str], user_id: int = 0,
    ) -> dict | None:
        """
        Check if we should inject a micro-Socratic challenge into the response.

        Only triggers for active plans where:
          1. Current question relates to the plan topic
          2. Cooldown period has passed since last challenge
          3. User has been engaging with challenges (not ignoring them)

        Returns a challenge hint dict or None.
        """
        if not topic_tags:
            return None

        profile = await self._store.load(user_id)
        now = datetime.now()

        active_plans = [
            p for p in profile.improvement_plans
            if p.get("status") == "active"
        ]
        if not active_plans:
            return None

        q_lower = question.lower()

        for plan in active_plans:
            plan_topic = plan.get("topic", "").lower()
            if not plan_topic:
                continue

            # Check if current question relates to plan topic
            topic_match = (
                plan_topic in q_lower
                or any(t.lower() == plan_topic for t in topic_tags)
            )
            if not topic_match:
                continue

            # Check cooldown
            last_date = plan.get("last_challenge_date", "")
            if last_date:
                try:
                    hours_since = (now - datetime.fromisoformat(last_date)).total_seconds() / 3600
                    if hours_since < CHALLENGE_COOLDOWN_HOURS:
                        continue
                except (ValueError, TypeError):
                    pass

            # Check engagement rate (skip if user ignores >50% of challenges)
            delivered = plan.get("challenges_delivered", 0)
            engaged = plan.get("challenges_engaged", 0)
            if delivered >= 4 and engaged / max(delivered, 1) < 0.25:
                continue  # User not engaging, don't annoy them

            # Generate challenge!
            difficulty = plan.get("difficulty", 1)
            difficulty = max(1, min(5, difficulty))
            import random
            templates = _CHALLENGE_TEMPLATES.get(difficulty, _CHALLENGE_TEMPLATES[1])
            template = random.choice(templates)
            challenge_text = template.format(topic=plan.get("topic", plan_topic))

            # Update plan stats
            plan["challenges_delivered"] = delivered + 1
            plan["last_challenge_date"] = now.isoformat()
            plan["updated_at"] = now.isoformat()
            await self._store.save(profile, user_id)

            logger.info(
                f"ProactiveCoach: micro-challenge for user={user_id} "
                f"plan={plan.get('plan_id')} topic={plan_topic} "
                f"difficulty={difficulty}"
            )

            return {
                "type": "coach_micro_challenge",
                "plan_id": plan.get("plan_id", ""),
                "topic": plan.get("topic", ""),
                "difficulty": difficulty,
                "challenge": challenge_text,
                "message": challenge_text,
                "plan_progress": self._compute_plan_progress(plan),
                "suggestions": [],
            }

        return None

    async def record_challenge_engagement(
        self, plan_id: str, user_id: int = 0,
    ) -> None:
        """Record that the user engaged with a micro-challenge (responded to it)."""
        profile = await self._store.load(user_id)

        for plan in profile.improvement_plans:
            if plan.get("plan_id") == plan_id and plan.get("status") == "active":
                plan["challenges_engaged"] = plan.get("challenges_engaged", 0) + 1
                plan["updated_at"] = datetime.now().isoformat()

                # Auto-adjust difficulty based on engagement
                engaged = plan["challenges_engaged"]
                if engaged >= 3 and plan.get("difficulty", 1) < 5:
                    plan["difficulty"] = min(5, plan.get("difficulty", 1) + 1)
                    plan["milestones"] = plan.get("milestones", [])
                    plan["milestones"].append(
                        f"难度提升到 {plan['difficulty']} (已完成 {engaged} 次挑战)"
                    )

                # Check if plan should be completed (depth reached target)
                current_depth = profile.topic_depth_map.get(
                    plan.get("topic", "").lower(), 0
                )
                if current_depth >= plan.get("target_level", 4):
                    plan["status"] = "completed"
                    plan["milestones"] = plan.get("milestones", [])
                    plan["milestones"].append(
                        f"达到目标深度 {_DEPTH_LABELS.get(current_depth, f'L{current_depth}')}!"
                    )
                    logger.info(
                        f"ProactiveCoach: plan {plan_id} COMPLETED "
                        f"for user={user_id} at L{current_depth}"
                    )

                await self._store.save(profile, user_id)
                break

    # ── Capability Map ──

    async def get_capability_map(self, user_id: int = 0) -> dict:
        """
        Generate capability map data for frontend visualization.

        Returns structured data for rendering as:
          - Skill radar chart (topics × depth levels)
          - Active plan progress bars
          - Growth trend indicators
          - Cognitive quadrant summary
        """
        profile = await self._store.load(user_id)

        # Topic capabilities
        topics = []
        for topic, depth in sorted(
            profile.topic_depth_map.items(),
            key=lambda x: x[1],
            reverse=True,
        )[:20]:  # top 20 topics
            freq = profile.topic_frequency.get(topic, 0)

            # Find active plan for this topic
            plan_progress = None
            for plan in profile.improvement_plans:
                if (plan.get("topic", "").lower() == topic.lower()
                        and plan.get("status") == "active"):
                    plan_progress = self._compute_plan_progress(plan)
                    break

            topics.append({
                "topic": topic,
                "level": depth,
                "level_label": _DEPTH_LABELS.get(depth, f"L{depth}"),
                "frequency": freq,
                "has_active_plan": plan_progress is not None,
                "plan_progress": plan_progress,
            })

        # Active plans summary
        active_plans = []
        for plan in profile.improvement_plans:
            if plan.get("status") in ("active", "proposed"):
                active_plans.append({
                    "plan_id": plan.get("plan_id"),
                    "topic": plan.get("topic"),
                    "status": plan.get("status"),
                    "current_level": plan.get("current_level"),
                    "target_level": plan.get("target_level"),
                    "difficulty": plan.get("difficulty"),
                    "progress": self._compute_plan_progress(plan),
                    "challenges_delivered": plan.get("challenges_delivered", 0),
                    "challenges_engaged": plan.get("challenges_engaged", 0),
                    "milestones": plan.get("milestones", []),
                })

        # Completed plans count
        completed_count = sum(
            1 for p in profile.improvement_plans
            if p.get("status") == "completed"
        )

        # Cognitive quadrant
        quadrant = profile.cognitive_quadrant_dist
        total_q = sum(quadrant.values()) or 1

        # Growth trend (reasoning quality over time)
        trend = profile.reasoning_improvement_trend[-10:]

        return {
            "has_data": bool(topics),
            "topics": topics,
            "active_plans": active_plans,
            "completed_plans_count": completed_count,
            "cognitive_quadrant": {
                k: {"count": v, "percentage": round(v / total_q * 100, 1)}
                for k, v in quadrant.items()
            },
            "reasoning_trend": trend,
            "total_topics_explored": len(profile.topic_depth_map),
            "topics_at_l3_plus": sum(
                1 for d in profile.topic_depth_map.values() if d >= 3
            ),
            "average_reasoning_quality": round(
                profile.average_reasoning_quality, 2
            ),
        }

    # ── Internal helpers ──

    @staticmethod
    def _compute_plan_progress(plan: dict) -> float:
        """Compute plan completion progress as 0.0-1.0."""
        current = plan.get("current_level", 1)
        target = plan.get("target_level", 4)
        if target <= current:
            return 1.0
        # Use challenges as a proxy for progress within levels
        delivered = plan.get("challenges_delivered", 0)
        engaged = plan.get("challenges_engaged", 0)
        # Each level requires roughly 3 engaged challenges to advance
        challenges_per_level = 3
        levels_to_go = target - current
        total_challenges_needed = levels_to_go * challenges_per_level
        if total_challenges_needed <= 0:
            return 1.0
        return min(1.0, round(engaged / total_challenges_needed, 2))

    @staticmethod
    def _get_meta(profile: UserProfile, key: str) -> str:
        """Get metadata from hourly_query_dist (reused as generic kv store)."""
        return profile.hourly_query_dist.get(key, "")

    @staticmethod
    def _set_meta(profile: UserProfile, key: str, value: str) -> None:
        """Set metadata in hourly_query_dist (reused as generic kv store)."""
        profile.hourly_query_dist[key] = value
