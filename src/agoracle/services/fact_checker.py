"""
Fact Checker — extracts factual claims from contributor responses and verifies them
against search results.

v4.21: Runs between fan-out and Judge synthesis.
Input: list of ModelResponse + search citations
Output: list of FactCheckResult (claim + verdict + source)

Design:
  - Uses a fast model (gemini_3_flash) to extract claims
  - Cross-references claims against SEARCH_CITATIONS URLs
  - Results injected into Judge input as [FACT_CHECK] section
  - Latency target: < 5s (uses flash model, single call)
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter

from agoracle.domain.types import (
    ModelResponse,
    Role,
    RoleCall,
)
from agoracle.services.prompt_loader import PromptLoader

logger = logging.getLogger(__name__)


@dataclass
class FactCheckResult:
    """One factual claim and its verification status."""
    claim: str                    # The factual statement
    source_text: str = ""         # Which contributor said it
    verdict: str = "unverified"   # "verified" | "unverified" | "contradicted" | "partial"
    evidence: str = ""            # Supporting/contradicting evidence from search
    citation_index: int = -1      # Index into search_citations (-1 = no match)


@dataclass
class FactCheckReport:
    """Aggregate fact-check results for all contributor responses."""
    claims: list[FactCheckResult] = field(default_factory=list)
    verified_count: int = 0
    unverified_count: int = 0
    contradicted_count: int = 0
    not_checked_count: int = 0  # v4.22c: claims with no search evidence to check against
    latency_ms: int = 0
    # v4.22: Perplexity-extracted verified facts (claim → citation URL)
    verified_facts_section: str = ""

    def to_judge_section(self) -> str:
        """Format as a section to inject into Judge input.

        v4.22: Prepends [VERIFIED_FACTS] from Perplexity before [FACT_CHECK].
        """
        parts = []

        # v4.22: Perplexity verified facts come first — highest authority
        if self.verified_facts_section:
            parts.append(self.verified_facts_section)

        if self.claims:
            lines = [
                f"## [FACT_CHECK] 事实核查结果（{self.verified_count} 已验证 / "
                f"{self.unverified_count} 未验证 / {self.contradicted_count} 矛盾 / "
                f"{self.not_checked_count} 未核查）\n"
            ]
            for i, c in enumerate(self.claims, 1):
                icon = {"verified": "✅", "unverified": "⚠️", "contradicted": "❌", "partial": "🟡", "not_checked": "❓"}.get(c.verdict, "⚠️")
                line = f"{i}. {icon} **{c.verdict.upper()}**: {c.claim}"
                if c.evidence:
                    line += f"\n   → {c.evidence}"
                if c.citation_index >= 0:
                    line += f" [来源 {c.citation_index + 1}]"
                lines.append(line)
            lines.append(
                "\n**Judge 指令**: 对 ⚠️ UNVERIFIED 的声明，必须删除具体数字或标注"
                " `⚠️ 未经搜索验证`；对 ❌ CONTRADICTED 的声明，以搜索来源为准。"
                " 对 ❓ NOT_CHECKED 的声明（无搜索证据可比对），视同 UNVERIFIED 处理。"
            )
            parts.append("\n".join(lines))

        # v4.22c: When both VERIFIED_FACTS and FACT_CHECK exist, clarify priority
        if self.verified_facts_section and self.claims:
            parts.append(
                "**优先级规则**: 同一事实同时出现在 [VERIFIED_FACTS] 和 [FACT_CHECK] 中时\u2014\u2014"
                "✅ 标注的以 [VERIFIED_FACTS] 为准；"
                "🟡 标注的与 [FACT_CHECK] 同级处理，不得压过明确反证。"
            )

        return "\n\n".join(parts)


_PERPLEXITY_FACTS_PROMPT = """你是一个精确的事实提取器。你会收到一段来自 Perplexity 联网搜索的回答，以及该回答引用的来源 URL 列表。

你的任务：提取回答中所有**可核查的事实性声明**，并尽可能判断每条声明对应哪个来源编号（0-based索引）。

可核查的事实性声明包括：
- 具体数字/百分比/统计数据
- 公司/组织的具体行动或决策
- 时间节点和事件
- 引用的研究或报告结论（含来源）
- 行业趋势的量化描述

**不要提取**：
- 纯观点/推理/分析（无具体数据支撑）
- 广泛共识的常识
- 定性描述（不带具体数字）

输出严格 JSON 格式：
```json
{
  "facts": [
    {
      "claim": "2024年全球AI芯片市场规模达670亿美元",
      "citation_index": 0
    },
    {
      "claim": "OpenAI估值在2024年12月达到1570亿美元",
      "citation_index": 1
    },
    {
      "claim": "Meta在2024年AI研发投入超过350亿美元",
      "citation_index": -1
    }
  ]
}
```

注意：如果无法确定声明对应哪个来源，citation_index 填 -1。只输出 JSON，不输出其他内容。"""


_EXTRACT_PROMPT = """你是一个精确的事实核查提取器。从以下多个专家回答中提取所有**可核查的事实性声明**。

可核查的事实性声明包括：
- 引用的论文/研究（作者、年份、结论）
- 具体数字/百分比/统计数据
- 公司/组织的具体行动或决策
- 时间节点和事件
- 行业趋势的量化描述

**不要提取**：
- 纯观点/推理/预测（如"AI可能会..."）
- 定性描述（如"显著提升"不带具体数字）
- 广泛共识的常识

约束：最多提取 15 条最重要的可核查声明，按重要性排序。
优先级：涉及具体数字/统计 > 引用论文/研究 > 时间节点/事件 > 行业趋势。
如果可核查声明超过 15 条，只保留最重要的 15 条。

输出严格 JSON 格式：
```json
{
  "claims": [
    {
      "claim": "Peng等(2023)发现使用GitHub Copilot的开发者完成任务速度提升55.8%",
      "source": "专家1"
    },
    {
      "claim": "2024-2025年外包巨头初级岗位缩减超过20%",
      "source": "专家2"
    }
  ]
}
```

只输出 JSON，不输出其他内容。"""

_VERIFY_PROMPT = """你是一个事实核查验证器。你会收到一组事实性声明和一组搜索来源摘要。

你的任务：判断每条声明是否被搜索来源支持。

判定标准：
- **verified**: 搜索来源中有明确支持该声明的信息（数字匹配、论文确认等）
- **partial**: 搜索来源部分支持（方向一致但数字不完全匹配，或场景有差异）
- **contradicted**: 搜索来源中有与该声明矛盾的信息
- **unverified**: 搜索来源中完全没有相关信息
- **low_quality_source**: 声明只被论坛/博客/自媒体级别来源支持，缺乏学术或机构来源

特别注意：
- 论文的结论不能跨领域外推（写作实验结论 ≠ 编程领域结论，必须标注 partial 并说明）
- 单一来源的行业数据如果能搜到反例，标注 contradicted

B1 — 引文书目完整性检查：
对每条引用了论文/研究/报告的声明，检查是否包含完整书目信息（至少：作者+年份+标题+出处）。
缺失任一项时，在 evidence 中注明 "citation_incomplete: true，缺少：[缺失项]"。

B2 — 来源质量分级：
对搜索返回的来源，在 source_quality 字段标注质量等级：
- "academic"：同行评审论文、官方技术报告（arXiv/DOI可查）
- "institutional"：政府/监管机构文件、公司官方白皮书/博客
- "media"：主流新闻媒体报道
- "forum"：论坛、博客、自媒体、交易所内容页等
关键技术/数据声明仅被 forum 级来源支撑时，verdict 设为 low_quality_source。

输出严格 JSON 格式：
```json
{
  "results": [
    {
      "claim_index": 0,
      "verdict": "verified",
      "evidence": "Peng et al. 2023论文确认平均完成时间从160.89分钟降至71.17分钟",
      "citation_index": 2,
      "source_quality": "academic",
      "citation_incomplete": false
    },
    {
      "claim_index": 1,
      "verdict": "unverified",
      "evidence": "搜索结果中未找到支持'外包巨头缩减>20%'的权威来源",
      "source_quality": "forum",
      "citation_incomplete": true
    }
  ]
}
```

只输出 JSON，不输出其他内容。"""


class FactChecker:
    """Extract factual claims from contributor responses and verify against search."""

    def __init__(
        self,
        model_adapter: "OpenAIModelAdapter",
        prompt_loader: PromptLoader,
    ) -> None:
        self._adapter = model_adapter
        self._prompts = prompt_loader

    async def check(
        self,
        question: str,
        responses: list[ModelResponse],
        search_citations: list[dict] | None = None,
        checker_model_id: str = "gemini_3_flash",
        perplexity_citations: list[str] | None = None,
    ) -> FactCheckReport:
        """Extract claims and verify them against search results.

        Two-step process:
        1. Extract factual claims from all contributor responses
        2. Verify each claim against search_citations

        v4.22: Added perplexity_citations parameter. When provided, runs
        _extract_perplexity_facts() concurrently to build a [VERIFIED_FACTS]
        section with claim→URL mapping, which is prepended before [FACT_CHECK].

        If no search_citations, all claims are marked unverified.
        """
        import asyncio as _asyncio
        t0 = time.perf_counter()
        report = FactCheckReport()

        if not responses:
            return report

        # v4.22: Find Perplexity response for structured fact extraction
        _perplexity_resp: ModelResponse | None = None
        for r in responses:
            if r.success and r.content and "perplexity" in (r.model_id or "").lower():
                _perplexity_resp = r
                break

        # Launch Perplexity facts extraction concurrently with general claims extraction
        _perplexity_task = None
        _effective_citations = perplexity_citations or []
        if _perplexity_resp and _effective_citations:
            _perplexity_task = _asyncio.create_task(
                self._extract_perplexity_facts(
                    _perplexity_resp.content,
                    _effective_citations,
                    checker_model_id,
                )
            )

        # Step 1: Extract claims from all contributors
        claims = await self._extract_claims(question, responses, checker_model_id)

        # Step 2: Verify claims
        results: list[FactCheckResult] = []
        if claims:
            if search_citations:
                results = await self._verify_claims(claims, search_citations, checker_model_id)
            else:
                # v4.22c: "not_checked" (no evidence) vs "unverified" (checked, not found)
                results = [
                    FactCheckResult(claim=c["claim"], source_text=c.get("source", ""), verdict="not_checked")
                    for c in claims
                ]

        report.claims = results
        report.verified_count = sum(1 for r in results if r.verdict == "verified")
        report.unverified_count = sum(1 for r in results if r.verdict == "unverified")
        report.contradicted_count = sum(1 for r in results if r.verdict == "contradicted")
        report.not_checked_count = sum(1 for r in results if r.verdict == "not_checked")

        # v4.22: Collect Perplexity verified facts
        if _perplexity_task is not None:
            try:
                report.verified_facts_section = await _asyncio.wait_for(
                    _asyncio.shield(_perplexity_task), timeout=12.0
                )
                if report.verified_facts_section:
                    logger.info(
                        f"FactChecker: Perplexity verified_facts_section "
                        f"({len(report.verified_facts_section)} chars)"
                    )
            except Exception as _pfe:
                logger.warning(f"FactChecker: Perplexity facts extraction failed: {_pfe}")

        report.latency_ms = int((time.perf_counter() - t0) * 1000)

        logger.info(
            f"FactChecker: {len(results)} claims — "
            f"{report.verified_count} verified, {report.unverified_count} unverified, "
            f"{report.contradicted_count} contradicted, {report.not_checked_count} not_checked "
            f"({report.latency_ms}ms)"
        )

        return report

    async def _extract_perplexity_facts(
        self,
        perplexity_content: str,
        citation_urls: list[str],
        model_id: str,
    ) -> str:
        """v4.22: Extract structured fact→URL mappings from Perplexity response.

        Uses _PERPLEXITY_FACTS_PROMPT to map each factual claim in the Perplexity
        response to the specific citation URL that supports it.
        Returns a formatted [VERIFIED_FACTS] section string for injection into Judge.
        """
        if not perplexity_content or not citation_urls:
            return ""

        # Build URL list for the prompt
        url_list = "\n".join(f"[{i}] {url}" for i, url in enumerate(citation_urls))
        user_msg = (
            f"## 来源 URL 列表（0-based索引）\n{url_list}\n\n"
            f"## Perplexity 联网搜索回答\n{perplexity_content[:4000]}"
        )

        role_call = RoleCall(
            call_id=f"perplexity-facts-{uuid.uuid4().hex[:8]}",
            model_id=model_id,
            role=Role.METADATA_EXTRACTOR,
            system_prompt=_PERPLEXITY_FACTS_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            timeout_seconds=12,
        )

        try:
            result = await self._adapter.call(role_call)
            if not result.success:
                logger.warning(f"Perplexity facts extraction failed: {result.error}")
                return ""
            parsed = _parse_json(result.content)
            facts = parsed.get("facts", [])
            if not facts:
                return ""

            lines = [
                f"## [VERIFIED_FACTS] Perplexity 实时搜索事实提取\n"
                "以下事实来自实时联网搜索。"
                "✅ 标注 = 有明确 URL 来源（🟢一级），Judge 可直接引用；"
                "🟡 标注 = 来自 Perplexity 搜索但未映射到具体 URL，仅作候选证据。\n"
            ]
            for i, f in enumerate(facts, 1):
                claim = f.get("claim", "")
                # v4.22c: Defensive int() — flash may return string "0"
                try:
                    cidx = int(f.get("citation_index", -1))
                except (TypeError, ValueError):
                    cidx = -1
                if not claim:
                    continue
                if 0 <= cidx < len(citation_urls):
                    url = citation_urls[cidx]
                    lines.append(f"{i}. ✅ {claim}\n   → 来源: [{url}]({url})")
                else:
                    lines.append(f"{i}. 🟡 {claim}\n   → 来源: Perplexity搜索（具体URL未映射）")

            lines.append(
                "\n**Judge 指令**: "
                "✅ 标注的事实优先级最高，当其他贡献者矛盾时以此为准。"
                "🟡 标注的事实基本可信，但不得压过明确反证（如 [FACT_CHECK] 中的 ❌ CONTRADICTED）。"
            )

            logger.info(
                f"Perplexity facts: extracted {len(facts)} facts from "
                f"{len(citation_urls)} citations"
            )
            return "\n".join(lines)

        except Exception as e:
            logger.warning(f"Perplexity facts extraction error: {e}")
            return ""

    async def _extract_claims(
        self,
        question: str,
        responses: list[ModelResponse],
        model_id: str,
    ) -> list[dict]:
        """Extract factual claims from contributor responses."""
        resp_text = []
        for i, r in enumerate(responses, 1):
            if r.success and r.content:
                resp_text.append(f"### 专家{i}的回答\n{r.content[:3000]}")  # cap to avoid token overflow

        if not resp_text:
            return []

        user_msg = f"## 问题\n{question}\n\n" + "\n\n".join(resp_text)

        role_call = RoleCall(
            call_id=f"factcheck-extract-{uuid.uuid4().hex[:8]}",
            model_id=model_id,
            role=Role.METADATA_EXTRACTOR,  # reuse existing role (lightweight)
            system_prompt=_EXTRACT_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            timeout_seconds=15,
        )

        try:
            result = await self._adapter.call(role_call)
            if not result.success:
                logger.warning(f"FactChecker extract failed: {result.error}")
                return []
            parsed = _parse_json(result.content)
            return parsed.get("claims", [])[:15]  # v4.26: hard cap — prompt asks ≤15, this enforces it
        except Exception as e:
            logger.warning(f"FactChecker extract error: {e}")
            return []

    async def _verify_claims(
        self,
        claims: list[dict],
        search_citations: list[dict],
        model_id: str,
    ) -> list[FactCheckResult]:
        """Verify extracted claims against search citations."""
        # Build search context
        search_ctx = []
        for i, c in enumerate(search_citations, 1):
            url = c.get("url", "")
            title = c.get("title", url)
            snippet = c.get("snippet", c.get("content", ""))[:500]
            search_ctx.append(f"[来源{i}] {title}\nURL: {url}\n摘要: {snippet}")

        claims_text = "\n".join(
            f"{i}. {c['claim']} (来自: {c.get('source', '未知')})"
            for i, c in enumerate(claims)
        )

        user_msg = (
            f"## 待核查声明\n{claims_text}\n\n"
            f"## 搜索来源\n" + "\n\n".join(search_ctx)
        )

        role_call = RoleCall(
            call_id=f"factcheck-verify-{uuid.uuid4().hex[:8]}",
            model_id=model_id,
            role=Role.METADATA_EXTRACTOR,
            system_prompt=_VERIFY_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            timeout_seconds=15,
        )

        try:
            result = await self._adapter.call(role_call)
            if not result.success:
                logger.warning(f"FactChecker verify failed: {result.error}")
                return [
                    FactCheckResult(claim=c["claim"], source_text=c.get("source", ""), verdict="unverified")
                    for c in claims
                ]

            parsed = _parse_json(result.content)
            verify_results = parsed.get("results", [])

            # Map back to FactCheckResult
            output: list[FactCheckResult] = []
            for c_idx, c in enumerate(claims):
                # Find matching verification result
                vr = next((v for v in verify_results if v.get("claim_index") == c_idx), None)
                if vr:
                    output.append(FactCheckResult(
                        claim=c["claim"],
                        source_text=c.get("source", ""),
                        verdict=vr.get("verdict", "unverified"),
                        evidence=vr.get("evidence", ""),
                        citation_index=_safe_int(vr.get("citation_index", -1)),

                    ))
                else:
                    output.append(FactCheckResult(
                        claim=c["claim"],
                        source_text=c.get("source", ""),
                        verdict="unverified",
                    ))
            return output

        except Exception as e:
            logger.warning(f"FactChecker verify error: {e}")
            return [
                FactCheckResult(claim=c["claim"], source_text=c.get("source", ""), verdict="unverified")
                for c in claims
            ]


def _safe_int(val: object, default: int = -1) -> int:
    """v4.22c: Safely convert LLM JSON value to int (flash may return \"0\" as string)."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _parse_json(text: str) -> dict:
    """Extract JSON from model output (may have markdown fences)."""
    # Try direct parse
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try extracting from code fence
    m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)```", text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    logger.warning(f"FactChecker: no JSON found in response")
    return {}
