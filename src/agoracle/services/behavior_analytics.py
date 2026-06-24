"""
Behavior Analytics — CBA data pipeline (ADR-014).

EventBus subscriber that listens to QueryCompleted events and accumulates
observable behavioral metrics into UserProfile. No inference, no labels —
only countable, measurable patterns.

Dimensions tracked (Phase 3):
  1. Divergent-Convergent Dynamics: topic switching, dwell time
  2. Cognitive Engagement Pattern: hourly distribution, mode preferences
  3. Topic Depth Map (v2.0 §5.4.3): L1-L5 per topic
  4. DepthGate (v2.0 §5.4.2): pre-check for repeated shallow topics
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agoracle.adapters.profile.json_profile import JsonProfileStore
    from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter

from agoracle.domain.events import QueryCompleted

logger = logging.getLogger(__name__)

# Max entries in rolling windows to prevent unbounded growth
MAX_TOPIC_SEQUENCE = 200
MAX_SESSION_DURATIONS = 50

SUMMARY_PROMPTS = {
    "zh-CN": "你是一个摘要助手，只输出简短的中文摘要，不超过50字。",
    "en-US": "You are a summary assistant. Output a brief English summary in under 50 words.",
}

SUMMARY_USER_MESSAGES = {
    "zh-CN": "一句话总结这个问题的核心主题：\n{question}",
    "en-US": "Summarize the core theme of this question in one sentence:\n{question}",
}

NARRATIVE_SUMMARY_PROMPTS = {
    "zh-CN": "你是一个行为分析助手。请只输出自然、简短的中文总结，不要列表，不超过120字。",
    "en-US": "You are a behavior analysis assistant. Output a concise natural English summary only, no bullets, under 120 words.",
}

_MODE_LABELS = {
    "zh-CN": {
        "light": "Light（快速探索）",
        "deep": "Deep（深度分析）",
        "research": "Research（全面研究）",
        "socratic": "Socratic（思维训练）",
    },
    "en-US": {
        "light": "Light (quick exploration)",
        "deep": "Deep (deep analysis)",
        "research": "Research (broad investigation)",
        "socratic": "Socratic (thinking practice)",
    },
}

_DEPTH_LABELS = {
    "zh-CN": {1: "L1 触达", 2: "L2 框架", 3: "L3 细节", 4: "L4 方案", 5: "L5 验证"},
    "en-US": {1: "L1 Exposure", 2: "L2 Framework", 3: "L3 Detail", 4: "L4 Strategy", 5: "L5 Verification"},
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


class BehaviorAnalytics:
    """Accumulate behavioral data from QueryCompleted events into UserProfile."""

    def __init__(self, profile_store: JsonProfileStore, proactive_coach=None, model_adapter=None) -> None:
        self._store = profile_store
        self._coach = proactive_coach  # ProactiveCoachService (v2.7.9d)
        self._model_adapter = model_adapter  # Memory-lite: for LLM summarization

    async def on_query_completed(self, event: QueryCompleted) -> None:
        """
        EventBus handler for QueryCompleted.

        Updates:
          - topic_sequence: append {tags, mode, ts}
          - topic_frequency: increment counts per tag
          - mode_usage_history: increment mode count
          - hourly_query_dist: increment hour bucket
          - daily_mode_dist: increment date+mode bucket
        """
        user_id = event.user_id or 0
        profile = await self._store.load(user_id)

        now = event.timestamp or datetime.now()
        ts_iso = now.isoformat()
        hour_key = f"{now.hour:02d}"
        date_key = now.strftime("%Y-%m-%d")
        mode = event.resolved_mode or event.mode or "unknown"

        # --- Dimension 1: Divergent-Convergent Dynamics ---

        # Append to topic sequence (rolling window)
        profile.topic_sequence.append({
            "tags": event.topic_tags or [],
            "mode": mode,
            "ts": ts_iso,
        })
        if len(profile.topic_sequence) > MAX_TOPIC_SEQUENCE:
            profile.topic_sequence = profile.topic_sequence[-MAX_TOPIC_SEQUENCE:]

        # Update topic frequency
        for tag in (event.topic_tags or []):
            profile.topic_frequency[tag] = profile.topic_frequency.get(tag, 0) + 1
            profile.topic_last_asked[tag] = ts_iso

        # --- Dimension 2: Cognitive Engagement Pattern ---

        # Mode usage
        profile.mode_usage_history[mode] = profile.mode_usage_history.get(mode, 0) + 1

        # Hourly distribution
        profile.hourly_query_dist[hour_key] = profile.hourly_query_dist.get(hour_key, 0) + 1

        # Daily mode distribution
        if date_key not in profile.daily_mode_dist:
            profile.daily_mode_dist[date_key] = {}
        profile.daily_mode_dist[date_key][mode] = (
            profile.daily_mode_dist[date_key].get(mode, 0) + 1
        )

        # Trim daily_mode_dist to last 90 days to prevent unbounded growth
        if len(profile.daily_mode_dist) > 90:
            sorted_dates = sorted(profile.daily_mode_dist.keys())
            for old_date in sorted_dates[:-90]:
                del profile.daily_mode_dist[old_date]

        # --- Topic Depth Map (v2.0 §5.4.3) ---
        for tag in (event.topic_tags or []):
            tag_lower = tag.lower().strip()
            if not tag_lower:
                continue
            freq = profile.topic_frequency.get(tag, 0)
            current_depth = profile.topic_depth_map.get(tag_lower, 0)
            new_depth = self._compute_topic_depth(freq, mode, current_depth)
            if new_depth > current_depth:
                profile.topic_depth_map[tag_lower] = new_depth

        await self._store.save(profile, user_id)
        logger.debug(
            f"BehaviorAnalytics: recorded query {event.query_id} "
            f"user={user_id} mode={mode} tags={event.topic_tags}"
        )

        # Memory-lite: fire-and-forget LLM summary (方案A)
        if self._model_adapter and event.question and user_id:
            asyncio.ensure_future(
                self._write_memory_turn(event, user_id)
            )

    async def _write_memory_turn(self, event: QueryCompleted, user_id: int) -> None:
        """Memory-lite 方案A: 调用 gemini-flash 生成50字摘要，写入 recent_turns（fire-and-forget）。

        摘要格式: 简短描述"讨论了什么"，不超过60字。
        失败时静默降级（不影响主流程）。
        """
        try:
            from agoracle.domain.types import RoleCall, Role
            import uuid

            question_snippet = (event.question or "")[:100]
            topic = (event.topic_tags or [""])[0] if event.topic_tags else ""
            language = _normalize_locale(getattr(event, "language", None))

            role_call = RoleCall(
                call_id=f"memory-{uuid.uuid4().hex[:8]}",
                model_id="gemini_3_flash",
                role=Role.CONTRIBUTOR,
                system_prompt=SUMMARY_PROMPTS[language],
                messages=[{
                    "role": "user",
                    "content": SUMMARY_USER_MESSAGES[language].format(question=question_snippet),
                }],
                timeout_seconds=10,
            )
            response = await self._model_adapter.call(role_call)
            summary = (response.content or "").strip()[:60] if response and response.success else question_snippet[:60]

            turn: dict[str, Any] = {
                "ts": event.timestamp.isoformat() if event.timestamp else datetime.now().isoformat(),
                "question_snippet": question_snippet,
                "summary": summary,
                "mode": event.resolved_mode or event.mode or "light",
                "topic": topic,
            }

            # 写入 profile，保持最近3条
            profile = await self._store.load(user_id)
            profile.recent_turns.append(turn)
            if len(profile.recent_turns) > 3:
                profile.recent_turns = profile.recent_turns[-3:]
            await self._store.save(profile, user_id)
            logger.debug(f"Memory-lite: saved turn for user={user_id} topic={topic} summary={summary[:20]}...")
        except Exception as e:
            logger.debug(f"Memory-lite: _write_memory_turn failed (silent skip): {e}")

    @staticmethod
    def _compute_topic_depth(frequency: int, mode: str, current_depth: int) -> int:
        """
        Compute topic depth level (L1-L5) based on observable signals.

        Rules (v2.0 §5.4.3):
          L1 (触达): topic mentioned 1 time
          L2 (框架): 2-3 queries on same topic
          L3 (细节): 4+ queries OR Deep/Research mode used
          L4 (方案): Socratic mode used on topic OR 7+ queries with Deep+
          L5 (验证): requires explicit user action (not auto-promoted)
        """
        depth = current_depth

        if frequency >= 1 and depth < 1:
            depth = 1  # L1: touched
        if frequency >= 2 and depth < 2:
            depth = 2  # L2: framework
        if frequency >= 4 and depth < 3:
            depth = 3  # L3: detail
        # Mode-based promotion
        if mode in ("deep", "research") and frequency >= 2 and depth < 3:
            depth = 3  # Deep/Research = serious exploration → L3
        if mode == "socratic" and depth < 4:
            depth = 4  # Socratic = forming own judgment → L4
        if frequency >= 7 and mode in ("deep", "research") and depth < 4:
            depth = 4  # Many deep queries → L4

        # L5 is never auto-promoted (requires user verification)
        return min(depth, 5)

    async def check_depth_gate(
        self, topic_tags: list[str], user_id: int = 0
    ) -> dict | None:
        """
        DepthGate pre-check (v2.0 §5.4.2).

        Triggers when ALL conditions met:
          1. Topic touched ≥ 3 times
          2. Depth never exceeded L2
          3. Current mode is Light (user choosing quick path)

        Returns suggestion dict or None if no gate triggered.
        Per ADHD design: "default action + exit key" — suggest Deep,
        allow skip. Max 3 triggers per day.
        """
        if not topic_tags:
            return None

        profile = await self._store.load(user_id)

        # Check daily trigger count (stored in daily_mode_dist metadata)
        today = datetime.now().strftime("%Y-%m-%d")
        gate_key = f"_depth_gate_{today}"
        daily_triggers = profile.hourly_query_dist.get(gate_key, 0)
        if daily_triggers >= 3:
            return None  # Max 3 per day

        for tag in topic_tags:
            tag_lower = tag.lower().strip()
            freq = profile.topic_frequency.get(tag, 0)
            depth = profile.topic_depth_map.get(tag_lower, 0)

            if freq >= 3 and depth <= 2:
                # Trigger! Record and return suggestion
                profile.hourly_query_dist[gate_key] = daily_triggers + 1
                await self._store.save(profile, user_id)

                return {
                    "type": "depth_gate",
                    "topic": tag,
                    "times_asked": freq,
                    "current_depth": depth,
                    "message": (
                        f"你已经第 {freq} 次探索「{tag}」了，"
                        f"但还停留在初步了解阶段。"
                        f"想深入一下吗？"
                    ),
                    "suggestions": [
                        {"mode": "deep", "label": "建立完整框架（Deep 模式）"},
                        {"mode": "socratic", "label": "形成你自己的判断（Socratic 模式）"},
                    ],
                }

        return None

    def compute_divergent_convergent_metrics(
        self, topic_sequence: list[dict], window: int = 20
    ) -> dict:
        """
        Compute divergent-convergent dynamics from recent topic sequence.

        Returns dict with:
          - new_topics: count of first-time topics in window
          - deepened_topics: count of repeated topics in window
          - switch_rate: new / total (0-1, higher = more divergent)
          - top_recurring: most frequently explored topics
        """
        recent = topic_sequence[-window:] if len(topic_sequence) > window else topic_sequence
        if not recent:
            return {
                "new_topics": 0, "deepened_topics": 0,
                "switch_rate": 0.0, "top_recurring": [],
                "total_in_window": 0,
            }

        # Track which tags appeared before the window
        all_prior_tags = set()
        for entry in topic_sequence[:-window] if len(topic_sequence) > window else []:
            all_prior_tags.update(entry.get("tags", []))

        # Count new vs recurring in window
        tag_counts: dict[str, int] = {}
        new_tags = set()
        for entry in recent:
            for tag in entry.get("tags", []):
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
                if tag not in all_prior_tags:
                    new_tags.add(tag)

        recurring = {t: c for t, c in tag_counts.items() if t not in new_tags and c >= 2}
        top_recurring = sorted(recurring, key=recurring.get, reverse=True)[:5]

        total = len(recent)
        new_count = len(new_tags)
        deep_count = len(recurring)

        return {
            "new_topics": new_count,
            "deepened_topics": deep_count,
            "switch_rate": round(new_count / max(new_count + deep_count, 1), 2),
            "top_recurring": top_recurring,
            "total_in_window": total,
        }

    def compute_engagement_metrics(self, profile) -> dict:
        """
        Compute cognitive engagement pattern from profile data.

        Returns dict with:
          - peak_hours: top 3 most active hours
          - mode_distribution: {mode: percentage}
          - total_queries: total query count
          - favorite_mode: most used mode
        """
        # Peak hours — filter out non-numeric metadata entries (e.g. _depth_gate_*, _last_plan_proposal)
        hourly = {k: v for k, v in profile.hourly_query_dist.items()
                  if isinstance(v, (int, float)) and not k.startswith("_")}
        if hourly:
            sorted_hours = sorted(hourly.items(), key=lambda x: x[1], reverse=True)
            peak_hours = [h for h, _ in sorted_hours[:3]]
        else:
            peak_hours = []

        # Mode distribution
        mode_usage = profile.mode_usage_history
        total = sum(mode_usage.values()) or 1
        mode_dist = {m: round(c / total * 100, 1) for m, c in mode_usage.items()}
        favorite = max(mode_usage, key=mode_usage.get) if mode_usage else "none"

        return {
            "peak_hours": peak_hours,
            "mode_distribution": mode_dist,
            "total_queries": sum(mode_usage.values()),
            "favorite_mode": favorite,
        }

    async def get_narrative_summary(self, user_id: int = 0, language: str | None = None) -> dict:
        """
        Generate narrative-style behavioral summary for frontend display.

        Returns structured data that the frontend renders as natural language,
        NOT raw numbers. Per ADR-014 §3.4: "show patterns, don't label."
        """
        profile = await self._store.load(user_id)
        locale = _normalize_locale(language or profile.preferred_language)

        dc = self.compute_divergent_convergent_metrics(profile.topic_sequence)
        eng = self.compute_engagement_metrics(profile)

        total = eng["total_queries"]
        if total == 0:
            return {
                "has_data": False,
                "narrative": (
                    "还没有足够的交互数据来分析你的思维模式。继续使用，我会逐渐了解你。"
                    if locale == "zh-CN"
                    else "There is not enough interaction data yet to analyze your thinking patterns. Keep using Synthora and I will learn gradually."
                ),
            }

        # Build narrative pieces
        narratives = []

        # Divergent-Convergent narrative
        if dc["total_in_window"] >= 5:
            if dc["switch_rate"] > 0.6:
                narratives.append(
                    (
                        f"最近 {dc['total_in_window']} 次查询中，你探索了 {dc['new_topics']} 个新方向，"
                        f"深入了 {dc['deepened_topics']} 个已有话题。你目前处于**发散探索**阶段。"
                        if locale == "zh-CN"
                        else f"In your last {dc['total_in_window']} queries, you opened {dc['new_topics']} new directions and deepened {dc['deepened_topics']} existing topics. You are in a divergent exploration phase."
                    )
                )
            elif dc["switch_rate"] < 0.3:
                narratives.append(
                    (
                        f"最近 {dc['total_in_window']} 次查询中，你持续深入了 {dc['deepened_topics']} 个话题。"
                        f"你目前处于**专注收敛**阶段。"
                        if locale == "zh-CN"
                        else f"In your last {dc['total_in_window']} queries, you kept deepening {dc['deepened_topics']} topics. You are in a focused convergence phase."
                    )
                )
            else:
                narratives.append(
                    (
                        f"最近 {dc['total_in_window']} 次查询中，"
                        f"新话题 {dc['new_topics']} 个、深化话题 {dc['deepened_topics']} 个，"
                        f"发散与收敛比较**均衡**。"
                        if locale == "zh-CN"
                        else f"In your last {dc['total_in_window']} queries, you explored {dc['new_topics']} new topics and deepened {dc['deepened_topics']} existing ones. Your exploration and convergence are fairly balanced."
                    )
                )

            if dc["top_recurring"]:
                narratives.append(
                    (
                        f"你持续关注的话题：{', '.join(dc['top_recurring'][:3])}。"
                        if locale == "zh-CN"
                        else f"Topics you keep returning to: {', '.join(dc['top_recurring'][:3])}."
                    )
                )

        # Engagement narrative
        if eng["peak_hours"]:
            hour_labels = [f"{h}:00" for h in eng["peak_hours"][:2]]
            narratives.append(
                (
                    f"你的认知活跃高峰在 {' 和 '.join(hour_labels)}。"
                    if locale == "zh-CN"
                    else f"Your cognitive peak hours are {' and '.join(hour_labels)}."
                )
            )

        if eng["favorite_mode"] != "none":
            mode_label = _MODE_LABELS[locale]
            fav = mode_label.get(eng["favorite_mode"], eng["favorite_mode"])
            pct = eng["mode_distribution"].get(eng["favorite_mode"], 0)
            narratives.append(
                (
                    f"你最常使用 {fav} 模式（{pct}%）。"
                    if locale == "zh-CN"
                    else f"Your most used mode is {fav} ({pct}%)."
                )
            )

        # Topic depth narrative
        depth_map = profile.topic_depth_map
        if depth_map:
            depth_labels = _DEPTH_LABELS[locale]
            deep_topics = {t: d for t, d in depth_map.items() if d >= 3}
            if deep_topics:
                top_deep = sorted(deep_topics.items(), key=lambda x: x[1], reverse=True)[:3]
                if locale == "zh-CN":
                    depth_parts = [f"「{t}」({depth_labels.get(d, f'L{d}')})" for t, d in top_deep]
                    narratives.append(f"你探索最深的话题：{', '.join(depth_parts)}。")
                else:
                    depth_parts = [f"\"{t}\" ({depth_labels.get(d, f'L{d}')})" for t, d in top_deep]
                    narratives.append(f"Your deepest topics so far: {', '.join(depth_parts)}.")

        if self._model_adapter and narratives:
            try:
                from agoracle.domain.types import Role, RoleCall
                import uuid

                role_call = RoleCall(
                    call_id=f"behavior-summary-{uuid.uuid4().hex[:8]}",
                    model_id="gemini_3_flash",
                    role=Role.CONTRIBUTOR,
                    system_prompt=NARRATIVE_SUMMARY_PROMPTS[locale],
                    messages=[{"role": "user", "content": "\n".join(narratives)}],
                    timeout_seconds=10,
                )
                response = await self._model_adapter.call(role_call)
                if response.success and response.content:
                    narrative = response.content.strip()
                else:
                    narrative = "\n".join(narratives)
            except Exception as e:
                logger.debug(f"BehaviorAnalytics narrative summary fallback: {e}")
                narrative = "\n".join(narratives)
        else:
            narrative = "\n".join(narratives)

        return {
            "has_data": True,
            "narrative": narrative,
            "metrics": {
                "divergent_convergent": dc,
                "engagement": eng,
                "topic_depth_map": depth_map,
            },
            "total_queries": total,
        }
