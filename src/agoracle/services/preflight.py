"""
Preflight Clarity Check (v2.7.5) — Deep/Research question pre-screening.

Uses a fast model (gemini_flash, <2s) to assess whether a user's question
is clear enough to warrant an expensive Deep/Research pipeline run.

Only triggers when clarity is LOW. Clear questions pass through instantly.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING

from agoracle.domain.types import RoleCall, Role

if TYPE_CHECKING:
    from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter

logger = logging.getLogger(__name__)

CLARITY_SYSTEM_PROMPT = """你是一个问题清晰度评估器。评估用户的问题是否足够清晰，可以直接进行深度分析。

评估维度：
1. 问题是否有明确的主题？
2. 问题是否有具体的方向或角度？
3. 问题的范围是否合理（不会太宽泛）？

返回 JSON 格式：
{
  "clarity": "high" | "medium" | "low",
  "reason": "简短说明",
  "suggested_questions": ["如果clarity=low，给出2-3个更具体的改进建议"]
}

只有当问题非常模糊时才标记为 low。大多数正常问题应该是 high 或 medium。
medium 的问题可以直接处理，不需要确认。"""

# v3.3 → v4.17: Structured question optimization prompt for Deep/Research
# Now outputs per-change breakdown so users can see/edit each modification.
OPTIMIZE_SYSTEM_PROMPT = """你是一个专业的问题优化器。用户即将使用深度分析模式，请帮助优化问题，提升回答质量。

你的任务：
1. 理解用户的真实意图
2. 如果问题已经清晰具体，needs_confirmation=false，optimization_points 为空列表
3. 如果问题有改进空间，列出每一处具体改动，并拼装 optimized_question

返回 JSON 格式（严格，不要输出任何其他文字）：
{
  "optimized_question": "最终优化后的完整问题",
  "needs_confirmation": true/false,
  "optimization_points": [
    {
      "type": "补充范围" | "明确角度" | "收窄焦点" | "修正表述" | "增加约束",
      "original": "原来的表述（直接引用原问题片段，无改动则留空字符串）",
      "revised": "改后的表述",
      "reason": "为什么这样改（一句话，≤20字）"
    }
  ]
}

规则：
- optimization_points 每条对应一处独立改动，最多 4 条
- 如果原问题已足够清晰，optimization_points=[]，needs_confirmation=false
- 不改变用户的核心意图和语言风格
- 不要强行关联用户背景、历史话题或偏好；只有用户明确提到相关背景时才可保留
- 所有文字用中文（英文问题也用中文说明 reason）"""


def _load_safety_prefix() -> str:
    """Load safety rules prefix (best-effort)."""
    try:
        from agoracle.services.prompt_loader import PromptLoader
        from agoracle.config.loader import PROJECT_ROOT
        _loader = PromptLoader(PROJECT_ROOT / "prompts")
        _safety = _loader.load("safety_rules")
        return f"{_safety}\n\n" if _safety else ""
    except Exception:
        return ""


def _parse_json_response(text: str) -> dict:
    """Extract JSON from model response (handles markdown code blocks)."""
    text = text.strip()
    if "```" in text:
        for block in text.split("```"):
            block = block.strip()
            if block.startswith("json"):
                block = block[4:].strip()
            if block.startswith("{"):
                text = block
                break
    return json.loads(text)


async def check_question_clarity(
    question: str,
    model_adapter: "OpenAIModelAdapter",
    model_id: str,
    user_profile_summary: str = "",
) -> dict | None:
    """
    Assess question clarity using a fast model.

    Returns None if clarity is acceptable (high/medium).
    Returns clarification dict if clarity is low.
    """
    if not question or not model_id:
        return None

    if len(question.strip()) < 5:
        return None

    try:
        system = _load_safety_prefix() + CLARITY_SYSTEM_PROMPT
        if user_profile_summary:
            system += f"\n\n用户背景参考：\n{user_profile_summary}"

        call = RoleCall(
            call_id=f"preflight-{uuid.uuid4().hex[:8]}",
            model_id=model_id,
            role=Role.CONTRIBUTOR,
            system_prompt=system,
            messages=[{"role": "user", "content": question}],
        )

        response = await model_adapter.call(call)
        if not response.success or not response.content:
            return None

        data = _parse_json_response(response.content)
        clarity = data.get("clarity", "high").lower()

        if clarity == "low":
            return {
                "type": "clarification_needed",
                "clarity": clarity,
                "reason": data.get("reason", ""),
                "suggested_questions": data.get("suggested_questions", []),
                "message": (
                    f"你的问题可能比较宽泛。{data.get('reason', '')}\n"
                    f"为了给你更好的回答，你可以考虑更具体一些："
                ),
            }

        return None

    except (json.JSONDecodeError, KeyError, Exception) as e:
        logger.debug(f"Preflight clarity check failed (non-critical): {e}")
        return None


async def optimize_question(
    question: str,
    model_adapter: "OpenAIModelAdapter",
    model_id: str,
    user_profile_summary: str = "",
) -> dict | None:
    """
    v3.3: Always-on question optimizer for Deep/Research.

    Returns an optimized version of the question for user confirmation.
    Returns None on error (pipeline proceeds with original question).
    """
    if not question or not model_id:
        return None

    if len(question.strip()) < 5:
        return None

    try:
        system = _load_safety_prefix() + OPTIMIZE_SYSTEM_PROMPT

        call = RoleCall(
            call_id=f"optimize-{uuid.uuid4().hex[:8]}",
            model_id=model_id,
            role=Role.CONTRIBUTOR,
            system_prompt=system,
            messages=[{"role": "user", "content": question}],
        )

        response = await model_adapter.call(call)
        if not response.success or not response.content:
            return None

        data = _parse_json_response(response.content)
        optimized = data.get("optimized_question", "").strip()
        needs_confirm = data.get("needs_confirmation", True)
        points: list[dict] = data.get("optimization_points", [])

        # Backward-compat: if old prompt returned changes_summary, keep it
        changes = data.get("changes_summary", "").strip()
        if not changes and points:
            changes = f"共 {len(points)} 处调整"

        if not optimized:
            return None

        # If question is already perfect, skip confirmation
        if not needs_confirm and optimized == question.strip():
            return None

        return {
            "type": "question_confirmation",
            "original_question": question,
            "optimized_question": optimized,
            "changes_summary": changes,
            "needs_confirmation": needs_confirm,
            "optimization_points": points,  # v4.17: per-change breakdown for UI
        }

    except (json.JSONDecodeError, KeyError, Exception) as e:
        logger.debug(f"Question optimization failed (non-critical): {e}")
        return None
