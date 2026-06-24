"""
Metadata Extractor adapter — extracts structured metadata from model responses.

Runs IN PARALLEL with Judge, using a fast model (e.g. Gemini Flash).
Produces key_insights, topic_tags, confidence, model_evaluations.
This keeps the evaluation independent from the synthesizer.
"""

from __future__ import annotations

import json
import logging
import random
import re
import uuid

from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter
from agoracle.domain.types import (
    ConsensusType,
    MetadataExtraction,
    ModelEvaluation,
    ModelResponse,
    Role,
    RoleCall,
)
from agoracle.services.prompt_loader import PromptLoader

logger = logging.getLogger(__name__)


class LLMMetadataExtractor:
    """
    Extract structured metadata from model responses using a fast LLM.

    Produces MetadataExtraction with key_insights, tags, confidence,
    model_evaluations — used by QualityGate for gate decisions.
    """

    def __init__(
        self,
        model_adapter: OpenAIModelAdapter,
        prompt_loader: PromptLoader,
    ) -> None:
        self._adapter = model_adapter
        self._prompts = prompt_loader

    async def extract(
        self,
        question: str,
        responses: list[ModelResponse],
        extractor_model_id: str = "gemini_3_flash",
        fallback_models: list[str] | None = None,
    ) -> MetadataExtraction:
        """Extract metadata from model responses.

        If extractor_model_id fails, tries each model in fallback_models in order.
        """
        system_prompt = self._prompts.load("metadata_extractor")
        if not system_prompt:
            logger.error("metadata_extractor prompt not found")
            return self._statistical_fallback(responses)

        # Content safety: prepend safety rules (原則 #25)
        safety_rules = self._prompts.load("safety_rules")
        if safety_rules:
            system_prompt = f"{safety_rules}\n\n{system_prompt}"

        # v3.0: Extractor de-bias — 防止对同族模型的隐性偏好 (3份审计报告一致建议)
        # Extractor(gemini)同时是Contributor → self-preference风险
        system_prompt += (
            "\n\n【评分中立性要求】你必须保持绝对中立。"
            "严禁对任何特定模型家族（Google/Anthropic/OpenAI/Moonshot/DeepSeek）有偏好。"
            "必须完全基于回答内容的客观质量评分，忽略格式/风格/长度差异。"
            "回答长度不应影响评分——只看内容的准确性和推理深度。"
        )

        # Format input with anonymized model IDs to prevent LLM from
        # mangling real IDs (e.g. "claude_opus" instead of "claude_opus_thinking")
        # Shuffle to eliminate position bias (Zheng et al. 2023: 5-10% preference for first response)
        shuffled_responses = list(responses)
        random.shuffle(shuffled_responses)
        id_map: dict[str, str] = {}      # anonymous → real
        parts = [f"## 用户问题\n{question}", "## 各模型回答"]
        anon_idx = 0
        for resp in shuffled_responses:
            if resp.success and resp.content:
                anon_idx += 1
                anon_id = f"model_{anon_idx}"
                id_map[anon_id] = resp.model_id
                parts.append(f"### {anon_id}\n{resp.content}")
        user_message = "\n\n".join(parts)

        # Dynamic timeout: use model's configured timeout, fallback to 15s
        mc = self._adapter._config.models.get(extractor_model_id) if hasattr(self._adapter, '_config') else None
        timeout = mc.timeout_seconds if mc and hasattr(mc, 'timeout_seconds') else 15

        role_call = RoleCall(
            call_id=f"extractor-{uuid.uuid4().hex[:8]}",
            model_id=extractor_model_id,
            role=Role.METADATA_EXTRACTOR,
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            timeout_seconds=timeout,
        )

        response = await self._adapter.call(role_call)
        _prompt_tok = response.prompt_tokens
        _completion_tok = response.completion_tokens

        if not response.success:
            logger.warning(f"Metadata extraction failed ({extractor_model_id}): {response.error}")
            # v4.15: Try fallback models before giving up
            for fb_model in (fallback_models or []):
                if fb_model == extractor_model_id:
                    continue
                logger.info(f"Metadata extractor: falling back to '{fb_model}'")
                fb_mc = self._adapter._config.models.get(fb_model) if hasattr(self._adapter, '_config') else None
                fb_timeout = fb_mc.timeout_seconds if fb_mc and hasattr(fb_mc, 'timeout_seconds') else 15
                fb_call = RoleCall(
                    call_id=f"extractor-fb-{uuid.uuid4().hex[:8]}",
                    model_id=fb_model,
                    role=Role.METADATA_EXTRACTOR,
                    system_prompt=role_call.system_prompt,
                    messages=role_call.messages,
                    timeout_seconds=fb_timeout,
                )
                fb_resp = await self._adapter.call(fb_call)
                if fb_resp.success:
                    response = fb_resp
                    extractor_model_id = fb_model
                    _prompt_tok = fb_resp.prompt_tokens
                    _completion_tok = fb_resp.completion_tokens
                    break
                logger.warning(f"Metadata extractor fallback '{fb_model}' also failed: {fb_resp.error}")
            else:
                return self._statistical_fallback(responses)

        result = self._parse_extraction(response.content, responses)

        # Retry once if parsing fell back to defaults (no model_evaluations)
        if not result.model_evaluations:
            logger.info("Metadata parse returned no evaluations, retrying with strict JSON instruction")
            retry_call = RoleCall(
                call_id=f"extractor-retry-{uuid.uuid4().hex[:8]}",
                model_id=extractor_model_id,
                role=Role.METADATA_EXTRACTOR,
                system_prompt=system_prompt + "\n\n重要：请只输出纯 JSON，不要包含任何其他文字。",
                messages=[{"role": "user", "content": user_message}],
                timeout_seconds=timeout,
            )
            retry_resp = await self._adapter.call(retry_call)
            if retry_resp.success:
                result = self._parse_extraction(retry_resp.content, responses)
                _prompt_tok += retry_resp.prompt_tokens
                _completion_tok += retry_resp.completion_tokens

        # Remap anonymous model IDs back to real IDs
        if result.model_evaluations and id_map:
            remapped: dict[str, ModelEvaluation] = {}
            for anon_id, ev in result.model_evaluations.items():
                real_id = id_map.get(anon_id, anon_id)
                remapped[real_id] = ModelEvaluation(
                    model_id=real_id,
                    accuracy=ev.accuracy,
                    reasoning=ev.reasoning,
                    uniqueness=ev.uniqueness,
                )
            # v3.1: also remap best_model anonymous ID → real ID
            remapped_best = id_map.get(result.best_model, result.best_model) if result.best_model else ""
            result = MetadataExtraction(
                key_insights=result.key_insights,
                topic_tags=result.topic_tags,
                confidence=result.confidence,
                consensus_type=result.consensus_type,
                has_divergence=result.has_divergence,
                divergence_summary=result.divergence_summary,
                model_evaluations=remapped,
                pairwise_evaluated=result.pairwise_evaluated,
                best_model=remapped_best,
                best_model_reason=result.best_model_reason,
                insight_agreements=result.insight_agreements,
                prompt_tokens=_prompt_tok,
                completion_tokens=_completion_tok,
                extractor_model_id=extractor_model_id,
            )
        else:
            result.prompt_tokens = _prompt_tok
            result.completion_tokens = _completion_tok
            result.extractor_model_id = extractor_model_id

        return result

    def _parse_extraction(
        self, raw_content: str, responses: list[ModelResponse]
    ) -> MetadataExtraction:
        """Parse the LLM's JSON output into MetadataExtraction.

        Uses multiple extraction strategies:
          1. Markdown code block extraction
          2. Regex-based JSON object finder
          3. Raw string as JSON
          4. Regex fallback for individual fields
          5. Statistical fallback from response data
        """
        data = self._extract_json(raw_content)

        if data is not None:
            return self._build_from_json(data)

        # JSON extraction failed — try regex fallback for individual fields
        logger.warning("JSON extraction failed, attempting regex fallback")
        result = self._regex_fallback(raw_content, responses)
        if result is not None:
            return result

        # All parsing failed — use statistical fallback
        logger.warning("All parsing failed, using statistical fallback")
        return self._statistical_fallback(responses)

    @staticmethod
    def _extract_json(raw: str) -> dict | None:
        """Try multiple strategies to extract a JSON object from LLM output."""
        raw = raw.strip()

        # Strategy 1: Try the whole string as JSON (cheapest check)
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

        # Strategy 2: Markdown ```json ... ``` block
        if "```json" in raw:
            try:
                json_str = raw.split("```json")[1].split("```")[0].strip()
                return json.loads(json_str)
            except (json.JSONDecodeError, IndexError):
                pass

        # Strategy 3: Markdown ``` ... ``` block (no language tag)
        if "```" in raw:
            try:
                json_str = raw.split("```")[1].split("```")[0].strip()
                return json.loads(json_str)
            except (json.JSONDecodeError, IndexError):
                pass

        # Strategy 4: Scan for JSON objects at each '{' using raw_decode
        # Handles arbitrary nesting depth, unlike regex
        decoder = json.JSONDecoder()
        best: dict | None = None
        best_len = 0
        for i, ch in enumerate(raw):
            if ch == "{":
                try:
                    obj, end = decoder.raw_decode(raw, i)
                    if isinstance(obj, dict) and (end - i) > best_len:
                        best = obj
                        best_len = end - i
                except json.JSONDecodeError:
                    continue
        return best

    @staticmethod
    def _build_from_json(data: dict) -> MetadataExtraction:
        """Build MetadataExtraction from a parsed JSON dict.

        Supports two evaluation formats:
          - pairwise_comparisons (v2.3+): derive scores from win rates
          - model_evaluations (legacy): use absolute scores directly
        """
        model_evals: dict[str, ModelEvaluation] = {}

        # Prefer pairwise comparisons if present (more stable than absolute scores)
        pairwise = data.get("pairwise_comparisons", [])
        is_pairwise = bool(pairwise and isinstance(pairwise, list))
        if is_pairwise:
            model_evals = LLMMetadataExtractor._pairwise_to_scores(pairwise)
        else:
            # Fallback to legacy absolute model_evaluations
            for model_id, eval_data in data.get("model_evaluations", {}).items():
                if isinstance(eval_data, dict):
                    model_evals[model_id] = ModelEvaluation(
                        model_id=model_id,
                        accuracy=float(eval_data.get("accuracy", 0.0)),
                        reasoning=float(eval_data.get("reasoning", 0.0)),
                        uniqueness=float(eval_data.get("uniqueness", 0.0)),
                    )

        ct_str = str(data.get("consensus_type", "")).lower().strip()
        consensus_map = {
            "independent_verification": ConsensusType.INDEPENDENT,
            "independent": ConsensusType.INDEPENDENT,
            "parrot_consensus": ConsensusType.PARROT,
            "parrot": ConsensusType.PARROT,
            "mixed": ConsensusType.MIXED,
            "single_fast": ConsensusType.UNKNOWN,  # Light模式单模型，无共识概念
            "single": ConsensusType.UNKNOWN,
        }
        consensus_type = consensus_map.get(ct_str, ConsensusType.UNKNOWN)

        # v3.1: Extract best_model (explicit from LLM, or derive from pairwise scores)
        best_model = data.get("best_model", "")
        best_model_reason = data.get("best_model_reason", "")
        if not best_model and model_evals:
            # Fallback: derive from pairwise scores — highest combined score wins
            best_eval = max(
                model_evals.values(),
                key=lambda e: e.accuracy + e.reasoning,
            )
            best_model = best_eval.model_id

        # B-8: Parse key_insights — support both legacy str format and new {text, agreed_models} format
        raw_insights = data.get("key_insights", [])[:5]
        parsed_insights: list[str] = []
        insight_agreements: dict[str, int] = {}
        for item in raw_insights:
            if isinstance(item, dict):
                text = item.get("text", "")
                agreed = item.get("agreed_models", [])
                if text:
                    if agreed and isinstance(agreed, list):
                        parsed_insights.append(f"{text} [{', '.join(str(m) for m in agreed)}]")
                        insight_agreements[text] = len(agreed)
                    else:
                        parsed_insights.append(text)
            elif isinstance(item, str) and item:
                parsed_insights.append(item)

        return MetadataExtraction(
            key_insights=parsed_insights,
            topic_tags=data.get("topic_tags", [])[:5],
            confidence=float(data.get("confidence", 0.5)),
            consensus_type=consensus_type,
            has_divergence=bool(data.get("has_divergence", False)),
            divergence_summary=data.get("divergence_summary"),
            model_evaluations=model_evals,
            pairwise_evaluated=is_pairwise,
            best_model=best_model,
            best_model_reason=best_model_reason,
            insight_agreements=insight_agreements,
        )

    @staticmethod
    def _pairwise_to_scores(
        comparisons: list[dict],
    ) -> dict[str, ModelEvaluation]:
        """Convert pairwise comparison results into per-model scores.

        For each model, win rate = wins / comparisons_involved.
        Tie counts as 0.5 win for each side.
        """
        accuracy_wins: dict[str, float] = {}
        reasoning_wins: dict[str, float] = {}
        uniqueness_wins: dict[str, float] = {}
        comparison_count: dict[str, int] = {}

        for comp in comparisons:
            if not isinstance(comp, dict):
                continue
            a = comp.get("model_a", "")
            b = comp.get("model_b", "")
            if not a or not b:
                continue

            for mid in (a, b):
                comparison_count[mid] = comparison_count.get(mid, 0) + 1
                accuracy_wins.setdefault(mid, 0.0)
                reasoning_wins.setdefault(mid, 0.0)
                uniqueness_wins.setdefault(mid, 0.0)

            # Accuracy
            wa = comp.get("winner_accuracy", "tie")
            if wa == a:
                accuracy_wins[a] += 1.0
            elif wa == b:
                accuracy_wins[b] += 1.0
            else:  # tie
                accuracy_wins[a] += 0.5
                accuracy_wins[b] += 0.5

            # Reasoning
            wr = comp.get("winner_reasoning", "tie")
            if wr == a:
                reasoning_wins[a] += 1.0
            elif wr == b:
                reasoning_wins[b] += 1.0
            else:  # tie
                reasoning_wins[a] += 0.5
                reasoning_wins[b] += 0.5

            # Uniqueness (v3.3: new dimension — unique perspective/source/angle)
            # Gracefully absent in old records: defaults to tie (0.5 each)
            wu = comp.get("winner_uniqueness", "tie")
            if wu == a:
                uniqueness_wins[a] += 1.0
            elif wu == b:
                uniqueness_wins[b] += 1.0
            else:  # tie or missing
                uniqueness_wins[a] += 0.5
                uniqueness_wins[b] += 0.5

        evals: dict[str, ModelEvaluation] = {}
        for mid in comparison_count:
            n = comparison_count[mid]
            if n == 0:
                continue
            acc = accuracy_wins.get(mid, 0.0) / n
            reas = reasoning_wins.get(mid, 0.0) / n
            uniq = uniqueness_wins.get(mid, 0.0) / n
            evals[mid] = ModelEvaluation(
                model_id=mid,
                accuracy=round(acc, 3),
                reasoning=round(reas, 3),
                uniqueness=round(uniq, 3),
            )

        return evals

    @staticmethod
    def _regex_fallback(
        raw: str, responses: list[ModelResponse]
    ) -> MetadataExtraction | None:
        """Try to extract key fields from non-JSON text using regex."""
        conf_match = re.search(r'["\']?confidence["\']?\s*[:=]\s*([\d.]+)', raw, re.I)
        div_match = re.search(r'["\']?has_divergence["\']?\s*[:=]\s*(true|false)', raw, re.I)

        # Only use regex fallback if we could extract at least confidence
        if not conf_match:
            return None

        confidence = max(0.0, min(1.0, float(conf_match.group(1))))
        has_div = div_match.group(1).lower() == "true" if div_match else False

        # Try to extract key_insights as quoted strings near the field name
        insights_match = re.search(
            r'key_insights["\']?\s*[:=]\s*\[([^\]]+)\]', raw, re.I
        )
        insights: list[str] = []
        if insights_match:
            insights = re.findall(r'"([^"]{5,100})"', insights_match.group(1))[:5]

        logger.info(f"Regex fallback extracted: confidence={confidence}, divergence={has_div}")
        return MetadataExtraction(
            confidence=confidence,
            has_divergence=has_div,
            key_insights=insights,
        )

    @staticmethod
    def _statistical_fallback(
        responses: list[ModelResponse],
    ) -> MetadataExtraction:
        """Compute fallback metadata from response statistics.

        Instead of a hardcoded 0.5 confidence, derive a rough estimate from:
          - Success rate of model calls
          - Average response length (longer = more substance)
          - Response length variance (low variance = agreement)
        """
        successful = [r for r in responses if r.success and r.content]
        if not successful:
            return MetadataExtraction(confidence=0.3)

        success_rate = len(successful) / max(len(responses), 1)
        lengths = [len(r.content) for r in successful]
        avg_len = sum(lengths) / len(lengths)
        len_factor = min(avg_len / 2000.0, 1.0)

        # Low length variance among responses suggests agreement
        if len(lengths) >= 2:
            mean_len = avg_len
            variance = sum((l - mean_len) ** 2 for l in lengths) / len(lengths)
            std_dev = variance ** 0.5
            cv = std_dev / mean_len if mean_len > 0 else 1.0  # coefficient of variation
            agreement_factor = max(0.0, 1.0 - cv)  # low CV = high agreement
        else:
            agreement_factor = 0.5

        confidence = 0.25 + 0.30 * success_rate + 0.20 * len_factor + 0.15 * agreement_factor
        confidence = max(0.2, min(0.85, confidence))  # clamp to [0.2, 0.85]

        logger.info(
            f"Statistical fallback: confidence={confidence:.2f} "
            f"(success_rate={success_rate:.1f}, len_factor={len_factor:.2f}, "
            f"agreement={agreement_factor:.2f})"
        )
        return MetadataExtraction(confidence=confidence)
