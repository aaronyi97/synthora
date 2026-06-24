"""
Socratic Guide Generator — produces guided questions for Socratic dialogue.

Two responsibilities:
  1. Generate initial guide question from a DivergenceMap (Phase 2 start)
  2. Generate follow-up questions based on user responses (Phase 2 turns)

Latency target: < 5s per turn (uses flash model).
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter
    from agoracle.services.conversation_memory import ConversationMemoryService

from agoracle.domain.types import (
    CognitiveSnapshot,
    DivergenceMap,
    DivergencePoint,
    Role,
    RoleCall,
    SocraticSession,
    SocraticTurn,
)
from agoracle.services.prompt_loader import PromptLoader

logger = logging.getLogger(__name__)

def _is_english(language: str) -> bool:
    return (language or "").strip() == "en-US"


def _locale_text(language: str, zh: str, en: str) -> str:
    return en if _is_english(language) else zh


_GUIDE_SYSTEM_PROMPT_ZH = """你是一位苏格拉底式思维教练。你的目标不是给出答案，而是通过提问引导用户独立思考。

当前对话背景：
- 用户提了一个问题，多位AI专家给出了不同回答
- 你需要引导用户深入思考，形成自己的判断

## 引导轨道（根据情况选择最合适的一种）

**轨道A: 分歧探索**（专家们有分歧时优先使用）
- 聚焦一个分歧点，让用户分析不同立场的优劣

**轨道B: 假设拆解**（适用于复杂问题或专家一致时）
- 拆解问题的隐含假设："这个结论成立的前提是什么？如果前提不成立呢？"

**轨道C: 反例挑战**（用户观点过于确定时）
- 提供假想反例："如果遇到X情况，你的结论还成立吗？"

**轨道D: 迁移/What-if**（当前话题讨论充分时）
- 将问题迁移到新场景："如果把这个逻辑应用到Y领域，会怎样？"

**轨道E: 证据追问**（用户给出观点但缺乏论证时）
- 追问依据："你觉得这个判断的关键证据是什么？有没有反面证据？"

## 通用规则
1. **不要直接告诉用户谁对谁错**
2. 每次只用一个轨道，聚焦一个点
3. 用开放式问题引导（"你觉得...？"、"如果...会怎样？"、"有没有可能...？"）
4. 如果用户的推理有漏洞，用反例或追问来引导，不要直接纠正
5. 回复控制在 2-3 句话内，简洁有力

## 难度适配规则
- **easy分歧**：使用轨道A(分歧探索)或E(证据追问)，问题简单直接，不需要深度背景知识
- **medium分歧**：使用轨道A/B/E，适度提供背景信息帮助用户理解
- **hard分歧**：先用轨道B(假设拆解)降低难度，再用A/C深入。先解释关键概念再提问

## 共识前置规则（第一轮必须执行）
- 第一个引导问题必须先简要介绍专家们的共识（“几位专家都认为XX”）
- 然后引入最容易理解的分歧点（优先选difficulty=easy的）
- 不要上来就抛出最难的分歧

## 语气原则（设计原则 #20：挑战不是批判）
- **先肯定再挑战**：每次挑战前先承认用户思路中合理的部分（"这个角度很有意思"、"你抓到了一个关键点"）
- **语气五字诀：真诚、坦率、温暖、积极、好奇**
- 把错误归因于话题复杂性而非用户能力（"这个问题确实容易让人困惑"而非"你理解错了"）
- 用好奇心引导而非权威纠正（"我很好奇，如果..."而非"其实正确的是..."）
- 庆祝提问本身（"这是个特别好的问题！"），不只在答对时才表扬
- 目标感受：用户被挑战后想的是"有意思，我没想到"而不是"我被证明是错的"

输出格式（严格 JSON）：
```json
{
  "guide_message": "你的引导问题（2-3句话）",
  "target_divergence_id": "当前讨论的分歧点ID（无分歧时填空字符串）",
  "hint_level": 0到3的整数（0=纯开放提问, 1=轻微提示, 2=明确引导, 3=接近揭示）,
  "track": "A/B/C/D/E"
}
```"""

_GUIDE_SYSTEM_PROMPT_EN = """You are a Socratic thinking coach. Your job is not to give the answer, but to guide the user to think independently through questions.

Current context:
- The user asked a question and multiple AI experts gave different answers.
- You should guide the user to reason more deeply and form their own judgment.

## Guidance tracks

**Track A: Explore disagreement** (prefer when experts disagree)
- Focus on one disagreement and ask the user to compare the strengths and weaknesses of the positions.

**Track B: Unpack assumptions** (for complex questions or strong expert consensus)
- Surface hidden assumptions: "What assumptions does this conclusion rely on? What if they do not hold?"

**Track C: Challenge with counterexamples** (when the user sounds too certain)
- Offer a hypothetical challenge: "If X happened, would your conclusion still hold?"

**Track D: Transfer / what-if** (when the current topic has been explored enough)
- Move the logic to another scenario: "If you applied the same reasoning to Y, what would change?"

**Track E: Probe evidence** (when the user has a view but weak support)
- Ask for supporting evidence: "What is the key evidence behind your judgment? What evidence points the other way?"

## General rules
1. Do not directly tell the user who is right or wrong.
2. Use only one track at a time and stay focused on one point.
3. Use open-ended questions.
4. If the user's reasoning has gaps, guide with questions or counterexamples instead of correcting them directly.
5. Keep the reply to 2-3 sentences, concise and sharp.

## Difficulty rules
- **easy disagreement**: prefer Track A or E; keep the question straightforward.
- **medium disagreement**: use A/B/E and add just enough context for clarity.
- **hard disagreement**: start with B to lower the difficulty, then move into A/C.

## Consensus-first rule (must apply in round 1)
- The first guide question must briefly mention what the experts agree on.
- Then introduce the easiest-to-understand disagreement first.
- Do not open with the hardest disagreement.

## Tone
- Affirm before you challenge.
- Be sincere, candid, warm, positive, and curious.
- Treat mistakes as a property of the topic's complexity, not the user's ability.
- Use curiosity instead of authority.

Output format (strict JSON):
```json
{
  "guide_message": "Your guiding question (2-3 sentences)",
  "target_divergence_id": "The divergence id being discussed, or an empty string when there is none",
  "hint_level": 0,
  "track": "A/B/C/D/E"
}
```"""

_EVALUATE_SYSTEM_PROMPT_ZH = """你是一位认知分析师。分析用户在苏格拉底对话中的思维模式。

请以严格 JSON 格式输出：
```json
{
  "anchoring_detected": true/false,
  "confirmation_bias": true/false,
  "nuance_recognition": 0.0到1.0,
  "reasoning_depth": 0.0到1.0,
  "blind_spots": ["盲点1", "盲点2"],
  "overall_quality": 0.0到1.0
}
```

评估标准：
- anchoring_detected: 用户是否不加批判地采纳了第一个看到的观点
- confirmation_bias: 用户是否只接受支持自己立场的证据
- nuance_recognition: 用户能否看到多方的合理性（0=非黑即白, 1=高度细腻）
- reasoning_depth: 用户论证的质量（0=无论证, 1=深度推理）
- blind_spots: 用户始终忽略的角度或论点
- overall_quality: 综合思维质量评分"""

_EVALUATE_SYSTEM_PROMPT_EN = """You are a cognition analyst. Analyze the user's thinking pattern in this Socratic dialogue.

Return strict JSON:
```json
{
  "anchoring_detected": true,
  "confirmation_bias": false,
  "nuance_recognition": 0.0,
  "reasoning_depth": 0.0,
  "blind_spots": ["blind spot 1", "blind spot 2"],
  "overall_quality": 0.0
}
```

Evaluation guide:
- anchoring_detected: whether the user accepted the first viewpoint too quickly
- confirmation_bias: whether the user only accepts evidence supporting their own stance
- nuance_recognition: whether the user sees merit across multiple sides
- reasoning_depth: the quality of the user's reasoning
- blind_spots: angles or arguments the user keeps overlooking
- overall_quality: overall thinking quality"""


class SocraticGuide:
    """Generate Socratic guidance questions and evaluate user reasoning."""

    def __init__(
        self,
        model_adapter: OpenAIModelAdapter,
        prompt_loader: PromptLoader | None = None,
        conversation_memory: ConversationMemoryService | None = None,
    ) -> None:
        self._adapter = model_adapter
        self._prompt_loader = prompt_loader
        self._conversation_memory = conversation_memory

    def _load_safety_rules(self, language: str = "zh-CN") -> str:
        if not self._prompt_loader:
            return ""
        return self._prompt_loader.load("safety_rules", language=language)

    def _guide_system_prompt(self, language: str = "zh-CN") -> str:
        return _GUIDE_SYSTEM_PROMPT_EN if _is_english(language) else _GUIDE_SYSTEM_PROMPT_ZH

    def _evaluate_system_prompt(self, language: str = "zh-CN") -> str:
        return _EVALUATE_SYSTEM_PROMPT_EN if _is_english(language) else _EVALUATE_SYSTEM_PROMPT_ZH

    @staticmethod
    def _speaker_label(role: str, language: str) -> str:
        if role == "guide":
            return "Guide" if _is_english(language) else "引导"
        return "User" if _is_english(language) else "用户"

    async def generate_initial_guide(
        self,
        question: str,
        divergence_map: DivergenceMap,
        guide_model_id: str = "gemini_3_flash",
        language: str = "zh-CN",
    ) -> SocraticTurn:
        """
        Generate the first guide question based on the divergence map.

        Picks the most interesting divergence point and asks the user about it.
        """
        if not divergence_map.divergence_points:
            return SocraticTurn(
                role="guide",
                content=_locale_text(
                    language,
                    "这个问题上专家们基本达成了共识。你对这个问题有什么自己的看法吗？",
                    "The experts mostly agree on this question. What is your own view?",
                ),
            )

        # Pick the first divergence point (most significant)
        dp = divergence_map.divergence_points[0]

        context = self._build_divergence_context(question, divergence_map, dp, language)

        safety_rules = self._load_safety_rules(language)
        guide_system_prompt = self._guide_system_prompt(language)
        guide_prompt = f"{safety_rules}\n\n{guide_system_prompt}" if safety_rules else guide_system_prompt

        role_call = RoleCall(
            call_id=f"guide-init-{uuid.uuid4().hex[:6]}",
            model_id=guide_model_id,
            role=Role.SOCRATIC_GUIDE,
            system_prompt=guide_prompt,
            messages=[{"role": "user", "content": context}],
            timeout_seconds=10,  # v4.32: 15→10s; hard_timeout=15s < orchestrator wait_for(20s)
        )

        start = time.monotonic()
        result = await self._adapter.call(role_call)
        latency = int((time.monotonic() - start) * 1000)

        if not result.success or not result.content:
            # Fallback: simple question about the divergence
            return SocraticTurn(
                role="guide",
                content=_locale_text(
                    language,
                    f"关于「{dp.topic}」，专家们有不同看法。你觉得哪一方的论据更有说服力？为什么？",
                    f"Experts disagree about \"{dp.topic}\". Which side sounds more convincing to you, and why?",
                ),
                divergence_point_id=dp.point_id,
                latency_ms=latency,
            )

        return self._parse_guide_response(result.content, dp.point_id, latency, language=language)

    async def generate_followup(
        self,
        session: SocraticSession,
        user_message: str,
        guide_model_id: str = "gemini_3_flash",
        language: str = "zh-CN",
    ) -> SocraticTurn:
        """
        Generate a follow-up guide question based on user's response.

        Uses conversation history + divergence map to craft the next question.
        """
        if not session.divergence_map:
            return SocraticTurn(
                role="guide",
                content=_locale_text(
                    language,
                    "谢谢你的分享。你还有其他想法吗？",
                    "Thanks for sharing. Is there another angle you want to explore?",
                ),
            )

        # Build conversation history for context (token-aware, replaces hard-coded [-6:])
        if self._conversation_memory:
            mem_result = await self._conversation_memory.build_socratic_context(
                session.turns, token_budget=3000, language=language,
            )
            history_text = mem_result.context
            if mem_result.was_compressed:
                logger.info(
                    f"[{session.session_id}] Context compressed: "
                    f"{mem_result.summarized_turns} summarized + "
                    f"{mem_result.verbatim_turns} verbatim"
                )
        else:
            # Fallback: last 6 turns (legacy behavior)
            history_text = "\n".join(
                f"{self._speaker_label(t.role, language)}: {t.content}"
                for t in session.turns[-6:]
            )
        # Append current user message
        history_text += f"\n{self._speaker_label('user', language)}: {user_message}"

        # Guard: no divergence points → still generate a real follow-up using history
        if not session.divergence_map.divergence_points:
            context_no_dp = (
                _locale_text(
                    language,
                    f"## 原始问题\n{session.question}\n\n"
                    f"## 对话历史\n{history_text}\n\n"
                    f"专家们对这个问题基本达成了共识，没有明显分歧点。"
                    f"请根据用户最新回复，生成一个引导用户深入思考的跟进问题（轨道B/C/D/E）。",
                    f"## Original question\n{session.question}\n\n"
                    f"## Dialogue history\n{history_text}\n\n"
                    "The experts mostly agree on this question, and there is no obvious divergence point. "
                    "Based on the user's latest reply, generate a follow-up question that pushes their thinking deeper "
                    "(prefer Track B/C/D/E).",
                )
            )
            safety_rules = self._load_safety_rules(language)
            guide_system_prompt = self._guide_system_prompt(language)
            guide_prompt_no_dp = f"{safety_rules}\n\n{guide_system_prompt}" if safety_rules else guide_system_prompt
            role_call_no_dp = RoleCall(
                call_id=f"guide-nodp-{uuid.uuid4().hex[:6]}",
                model_id=guide_model_id,
                role=Role.SOCRATIC_GUIDE,
                system_prompt=guide_prompt_no_dp,
                messages=[{"role": "user", "content": context_no_dp}],
                timeout_seconds=10,  # v4.32: 15→10s; hard_timeout=15s < orchestrator wait_for(20s)
            )
            start_no_dp = time.monotonic()
            result_no_dp = await self._adapter.call(role_call_no_dp)
            latency_no_dp = int((time.monotonic() - start_no_dp) * 1000)
            if result_no_dp.success and result_no_dp.content:
                return self._parse_guide_response(result_no_dp.content, "", latency_no_dp, language=language)
            return SocraticTurn(
                role="guide",
                content=_locale_text(
                    language,
                    "有意思的想法。如果把你的结论应用到一个完全不同的场景，还成立吗？",
                    "Interesting thought. If you applied your conclusion to a completely different situation, would it still hold?",
                ),
            )

        # Determine which divergence point to discuss next
        dp_index = min(
            session.current_divergence_index,
            len(session.divergence_map.divergence_points) - 1,
        )
        dp = session.divergence_map.divergence_points[dp_index]

        # Guard: if the divergence point has no positions, treat same as no-dp
        if not dp.positions:
            context_no_pos = (
                _locale_text(
                    language,
                    f"## 原始问题\n{session.question}\n\n"
                    f"## 对话历史\n{history_text}\n\n"
                    f"请根据用户最新回复，生成一个深入思考的跟进问题。",
                    f"## Original question\n{session.question}\n\n"
                    f"## Dialogue history\n{history_text}\n\n"
                    "Generate a follow-up question that pushes the user's thinking deeper.",
                )
            )
            safety_rules = self._load_safety_rules(language)
            guide_system_prompt = self._guide_system_prompt(language)
            guide_prompt_no_pos = f"{safety_rules}\n\n{guide_system_prompt}" if safety_rules else guide_system_prompt
            rc_no_pos = RoleCall(
                call_id=f"guide-nopos-{uuid.uuid4().hex[:6]}",
                model_id=guide_model_id,
                role=Role.SOCRATIC_GUIDE,
                system_prompt=guide_prompt_no_pos,
                messages=[{"role": "user", "content": context_no_pos}],
                timeout_seconds=10,  # v4.32: 15→10s; hard_timeout=15s < orchestrator wait_for(20s)
            )
            _s = time.monotonic()
            _r = await self._adapter.call(rc_no_pos)
            _lat = int((time.monotonic() - _s) * 1000)
            if _r.success and _r.content:
                return self._parse_guide_response(_r.content, dp.point_id, _lat, language=language)
            return SocraticTurn(
                role="guide",
                content=_locale_text(
                    language,
                    "有意思的角度。如果把这个逻辑应用到其他场景，还成立吗？",
                    "Interesting angle. If you applied the same logic to another scenario, would it still hold?",
                ),
                divergence_point_id=dp.point_id,
                latency_ms=_lat,
            )

        context = (
            _locale_text(
                language,
                f"## 原始问题\n{session.question}\n\n"
                f"## 当前讨论的分歧点\n"
                f"主题: {dp.topic}\n"
                f"描述: {dp.description}\n"
                f"各方立场: {json.dumps(dp.positions, ensure_ascii=False)}\n\n"
                f"## 对话历史\n{history_text}"
                f"\n\n请根据用户最新回复生成下一个引导问题。",
                f"## Original question\n{session.question}\n\n"
                f"## Current divergence point\n"
                f"Topic: {dp.topic}\n"
                f"Description: {dp.description}\n"
                f"Positions: {json.dumps(dp.positions, ensure_ascii=False)}\n\n"
                f"## Dialogue history\n{history_text}"
                "\n\nGenerate the next guiding question based on the user's latest reply.",
            )
        )

        safety_rules = self._load_safety_rules(language)
        guide_system_prompt = self._guide_system_prompt(language)
        guide_prompt = f"{safety_rules}\n\n{guide_system_prompt}" if safety_rules else guide_system_prompt

        role_call = RoleCall(
            call_id=f"guide-follow-{uuid.uuid4().hex[:6]}",
            model_id=guide_model_id,
            role=Role.SOCRATIC_GUIDE,
            system_prompt=guide_prompt,
            messages=[{"role": "user", "content": context}],
            timeout_seconds=10,  # v4.32: 15→10s; hard_timeout=15s < orchestrator wait_for(20s)
        )

        start = time.monotonic()
        result = await self._adapter.call(role_call)
        latency = int((time.monotonic() - start) * 1000)

        if not result.success or not result.content:
            return SocraticTurn(
                role="guide",
                content=_locale_text(
                    language,
                    "有意思的观点。你能再深入解释一下你的理由吗？",
                    "Interesting point. Could you explain your reasoning in a bit more depth?",
                ),
                divergence_point_id=dp.point_id,
                latency_ms=latency,
            )

        return self._parse_guide_response(result.content, dp.point_id, latency, language=language)

    async def evaluate_session(
        self,
        session: SocraticSession,
        evaluator_model_id: str = "gemini_3_flash",
        language: str = "zh-CN",
    ) -> CognitiveSnapshot:
        """
        Evaluate the user's cognitive patterns from the Socratic session.

        Called at the end of a session to produce a CognitiveSnapshot.
        """
        if not session.turns:
            return CognitiveSnapshot()

        # Build evaluation context
        user_turns = [t for t in session.turns if t.role == "user"]
        if not user_turns:
            return CognitiveSnapshot()

        dialogue_text = "\n".join(
            f"{self._speaker_label(t.role, language)}: {t.content}"
            for t in session.turns
        )

        divergence_text = ""
        if session.divergence_map:
            for dp in session.divergence_map.divergence_points:
                divergence_text += _locale_text(
                    language,
                    f"\n分歧点: {dp.topic}\n",
                    f"\nDivergence point: {dp.topic}\n",
                )
                for pos in dp.positions:
                    divergence_text += f"  - {pos.get('stance', '?')}: {pos.get('summary', '')}\n"

        context = (
            _locale_text(
                language,
                f"## 分歧点\n{divergence_text}\n\n"
                f"## 对话记录\n{dialogue_text}\n\n"
                f"请分析用户在这次对话中的思维模式。",
                f"## Divergence points\n{divergence_text}\n\n"
                f"## Dialogue log\n{dialogue_text}\n\n"
                "Analyze the user's thinking pattern in this dialogue.",
            )
        )

        safety_rules = self._load_safety_rules(language)
        evaluate_system_prompt = self._evaluate_system_prompt(language)
        eval_prompt = f"{safety_rules}\n\n{evaluate_system_prompt}" if safety_rules else evaluate_system_prompt

        role_call = RoleCall(
            call_id=f"eval-{uuid.uuid4().hex[:6]}",
            model_id=evaluator_model_id,
            role=Role.SOCRATIC_GUIDE,
            system_prompt=eval_prompt,
            messages=[{"role": "user", "content": context}],
            timeout_seconds=10,  # v4.32: 15→10s; hard_timeout=15s < orchestrator wait_for(20s)
        )

        result = await self._adapter.call(role_call)

        if not result.success or not result.content:
            return CognitiveSnapshot()

        return self._parse_evaluation(result.content)

    # ── Helpers ──────────────────────────────────────────────

    def _build_divergence_context(
        self, question: str, dmap: DivergenceMap, dp: DivergencePoint, language: str = "zh-CN",
    ) -> str:
        """Build context string for the guide model."""
        consensus_text = "\n".join(f"- {c}" for c in dmap.consensus_points) if dmap.consensus_points else _locale_text(
            language,
            "无明确共识",
            "No clear consensus",
        )

        positions_text = ""
        for pos in dp.positions:
            models = ", ".join(pos.get("models", []))
            positions_text += f"  - {pos.get('stance', '?')} ({models}): {pos.get('summary', '')}\n"

        return _locale_text(
            language,
            f"## 用户问题\n{question}\n\n"
            f"## 专家共识\n{consensus_text}\n\n"
            f"## 当前分歧点\n"
            f"主题: {dp.topic}\n"
            f"描述: {dp.description}\n"
            f"各方立场:\n{positions_text}\n"
            f"共识度: {dp.consensus_ratio:.1%}\n"
            f"难度: {dp.difficulty}\n\n"
            f"请生成第一个引导问题，引导用户思考这个分歧点。",
            f"## User question\n{question}\n\n"
            f"## Expert consensus\n{consensus_text}\n\n"
            f"## Current divergence point\n"
            f"Topic: {dp.topic}\n"
            f"Description: {dp.description}\n"
            f"Positions:\n{positions_text}\n"
            f"Consensus level: {dp.consensus_ratio:.1%}\n"
            f"Difficulty: {dp.difficulty}\n\n"
            "Generate the first guiding question to help the user think through this divergence point.",
        )

    def _parse_guide_response(
        self, content: str, divergence_point_id: str, latency_ms: int, language: str = "zh-CN",
    ) -> SocraticTurn:
        """Parse guide model's JSON response into a SocraticTurn."""
        # Extract JSON
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
        else:
            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            json_str = json_match.group(0) if json_match else None

        if json_str:
            try:
                data = json.loads(json_str)
                if not isinstance(data, dict):
                    raise TypeError("guide response must be object")
                guide_msg = data.get("guide_message") or ""
                if not guide_msg.strip():
                    guide_msg = content.strip()
                return SocraticTurn(
                    role="guide",
                    content=guide_msg,
                    divergence_point_id=data.get("target_divergence_id", divergence_point_id),
                    latency_ms=latency_ms,
                )
            except (json.JSONDecodeError, TypeError):
                pass

        # Fallback: use raw content as guide message
        return SocraticTurn(
            role="guide",
            content=content.strip() or _locale_text(
                language,
                "有意思的观点。你能再深入解释一下你的理由吗？",
                "Interesting point. Could you explain your reasoning in a bit more depth?",
            ),
            divergence_point_id=divergence_point_id,
            latency_ms=latency_ms,
        )

    @staticmethod
    def _safe_score(v, default: float = 0.0) -> float:
        """Safely convert a value to a clamped [0,1] float score."""
        try:
            x = float(v)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(x):
            return default
        return max(0.0, min(1.0, x))

    @staticmethod
    def _safe_bool(v) -> bool:
        """Safely convert a value to bool (handles string 'false')."""
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in {"true", "1", "yes", "on"}
        return bool(v) if v is not None else False

    def _parse_evaluation(self, content: str) -> CognitiveSnapshot:
        """Parse evaluation model's JSON response into a CognitiveSnapshot."""
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
        else:
            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            json_str = json_match.group(0) if json_match else None

        if not json_str:
            return CognitiveSnapshot()

        try:
            data = json.loads(json_str)
            if not isinstance(data, dict):
                return CognitiveSnapshot()

            # Validate: at least one meaningful field must be present
            nr = self._safe_score(data.get("nuance_recognition"))
            rd = self._safe_score(data.get("reasoning_depth"))
            if nr == 0.0 and rd == 0.0 and not data.get("blind_spots"):
                logger.warning("CognitiveSnapshot: all-zero evaluation, discarding")
                return CognitiveSnapshot()

            raw_bs = data.get("blind_spots", [])
            blind_spots = [str(x) for x in raw_bs] if isinstance(raw_bs, list) else []

            return CognitiveSnapshot(
                anchoring_detected=self._safe_bool(data.get("anchoring_detected")),
                confirmation_bias=self._safe_bool(data.get("confirmation_bias")),
                nuance_recognition=nr,
                position_change_count=0,  # tracked by session, not evaluator
                reasoning_depth=rd,
                blind_spots=blind_spots,
            )
        except (json.JSONDecodeError, ValueError, TypeError):
            return CognitiveSnapshot()
