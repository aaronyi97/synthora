"""
Divergence Analyzer — extracts structured divergence maps from contributor responses.

Used in Socratic mode (Phase 3) to identify where models agree and disagree.
Calls a fast model (gemini_3_flash) to analyze responses and produce a DivergenceMap.

Design:
  - Input: list of ModelResponse from Layer 1 fan-out
  - Output: DivergenceMap with consensus points + divergence points
  - Latency target: < 5s (uses flash model)
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter

from agoracle.domain.types import (
    DivergenceMap,
    DivergencePoint,
    ModelResponse,
    Role,
    RoleCall,
)
from agoracle.services.prompt_loader import PromptLoader

logger = logging.getLogger(__name__)

_ANALYZER_SYSTEM_PROMPT = """你是一个专业的分歧分析师。你的任务是分析多位专家对同一个问题的回答，找出他们的共识点和分歧点。

请以严格的 JSON 格式输出分析结果：

```json
{
  "consensus_points": ["共识点1", "共识点2", ...],
  "divergence_points": [
    {
      "topic": "分歧主题（简短标签）",
      "description": "1-2句话描述这个分歧",
      "positions": [
        {"stance": "支持/反对/中立", "summary": "该立场的核心论点", "models": ["专家1", "专家2"]},
        {"stance": "支持/反对/中立", "summary": "该立场的核心论点", "models": ["专家3"]}
      ],
      "consensus_ratio": 0.0到1.0之间的数字,
      "difficulty": "easy/medium/hard"
    }
  ],
  "overall_consensus_score": 0.0到1.0之间的数字
}
```

规则：
1. consensus_ratio: 0.0 = 完全分裂, 1.0 = 完全一致
2. difficulty: easy = 有明确对错, medium = 需要权衡, hard = 涉及价值观/哲学
3. 如果专家们观点确实一致，诚实报告共识而非强行制造分歧。但可以指出论证方式或侧重点的差异（标注difficulty为easy）
4. 每个分歧点的 positions 至少有 2 个不同立场
5. overall_consensus_score 反映整体一致程度
6. 只输出 JSON，不要其他文字"""


class DivergenceAnalyzer:
    """Analyze model responses to extract divergence maps."""

    def __init__(self, model_adapter: OpenAIModelAdapter, prompt_loader: PromptLoader | None = None) -> None:
        self._adapter = model_adapter
        self._prompt_loader = prompt_loader

    async def analyze(
        self,
        question: str,
        responses: list[ModelResponse],
        analyzer_model_id: str = "gemini_3_flash",
    ) -> DivergenceMap:
        """
        Analyze contributor responses and produce a DivergenceMap.

        Args:
            question: The original user question.
            responses: Successful contributor responses from Layer 1.
            analyzer_model_id: Model to use for analysis (should be fast).

        Returns:
            DivergenceMap with consensus and divergence points.
        """
        if len(responses) < 2:
            return DivergenceMap(
                model_count=len(responses),
                overall_consensus_score=1.0,
            )

        # Build user message with anonymized expert responses
        expert_sections = []
        for i, resp in enumerate(responses):
            # v3.0: 2000→4000 — 推理模型回答常超2000字, 分歧细节往往在后半段
            truncated = resp.content[:4000]
            if len(resp.content) > 4000:
                truncated += "\n[...已截断...]"
            expert_sections.append(f"### 专家 {i + 1}\n{truncated}")

        user_message = (
            f"## 问题\n{question}\n\n"
            f"## 专家回答\n\n" + "\n\n".join(expert_sections)
        )

        # Content safety: prepend safety rules (原則 #25)
        system_prompt = _ANALYZER_SYSTEM_PROMPT
        if self._prompt_loader:
            safety_rules = self._prompt_loader.load("safety_rules")
            if safety_rules:
                system_prompt = f"{safety_rules}\n\n{system_prompt}"

        role_call = RoleCall(
            call_id=f"diverge-{uuid.uuid4().hex[:6]}",
            model_id=analyzer_model_id,
            role=Role.DIVERGENCE_ANALYZER,
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            timeout_seconds=20,  # v4.32: 90→20s; streaming wait_for=30s, need margin for no-retry fast-fail
        )

        start = time.monotonic()
        result = await self._adapter.call(role_call)
        latency = int((time.monotonic() - start) * 1000)

        if not result.success or not result.content:
            logger.warning(f"DivergenceAnalyzer failed: {result.error}")
            return self._fallback_map(responses, latency)

        return self._parse_response(result.content, len(responses), latency)

    def _parse_response(
        self, content: str, model_count: int, latency_ms: int
    ) -> DivergenceMap:
        """Parse the analyzer's JSON response into a DivergenceMap."""
        # Extract JSON from response (may be wrapped in markdown code block)
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
        else:
            # Try to find raw JSON
            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            if json_match:
                json_str = json_match.group(0)
            else:
                logger.warning("DivergenceAnalyzer: no JSON found in response")
                return DivergenceMap(model_count=model_count, analysis_latency_ms=latency_ms)

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.warning(f"DivergenceAnalyzer: JSON parse error: {e}")
            return DivergenceMap(model_count=model_count, analysis_latency_ms=latency_ms)

        # Build DivergencePoints
        divergence_points = []
        for dp_data in data.get("divergence_points", []):
            dp = DivergencePoint(
                topic=dp_data.get("topic", ""),
                description=dp_data.get("description", ""),
                positions=dp_data.get("positions", []),
                consensus_ratio=float(dp_data.get("consensus_ratio", 0.5)),
                difficulty=dp_data.get("difficulty", "medium"),
            )
            divergence_points.append(dp)

        return DivergenceMap(
            consensus_points=data.get("consensus_points", []),
            divergence_points=divergence_points,
            overall_consensus_score=float(data.get("overall_consensus_score", 0.5)),
            model_count=model_count,
            analysis_latency_ms=latency_ms,
        )

    def _fallback_map(
        self, responses: list[ModelResponse], latency_ms: int
    ) -> DivergenceMap:
        """Fallback when analyzer fails — create a minimal divergence map."""
        # Build positions from actual responses so guide has real content to work with
        positions = []
        for i, r in enumerate(responses[:3]):
            snippet = r.content[:200].replace("\n", " ") if r.content else ""
            positions.append({
                "stance": f"专家{i+1}的观点",
                "summary": snippet or "无内容",
                "models": [f"专家{i+1}"],
            })
        if not positions:
            positions = [
                {"stance": "支持", "summary": "尚无足够数据分析", "models": []},
                {"stance": "中立", "summary": "需要更多信息才能判断", "models": []},
            ]
        return DivergenceMap(
            consensus_points=[],
            divergence_points=[
                DivergencePoint(
                    topic="各专家的不同角度",
                    description="专家们从不同角度分析了这个问题，你认为哪个角度最有说服力？",
                    positions=positions,
                    consensus_ratio=0.5,
                    difficulty="medium",
                )
            ],
            overall_consensus_score=0.5,
            model_count=len(responses),
            analysis_latency_ms=latency_ms,
        )
