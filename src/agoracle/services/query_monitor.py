"""
Query Monitor — 运行时模型质量监控体系 (v1.0)

三层架构:
  Layer 1: 零成本信号采集 — 复用 orchestrator 已产生的数据，写入 JSONL
           包含: 每个贡献者原始回答全文、Judge 评分、质量门结果、成本
  Layer 2: 异步轻量评估 — 查询完成后后台用 Claude Sonnet 4.6 独立打分
           对比: 聚合答案 vs 最佳单模型答案，5维度评分
           v1.1: 随机交换 A/B 消除位置偏差；传完整答案不截断
  Layer 3: 离线复盘 — 见 scripts/analyze_monitor.py

设计原则:
  - 零延迟影响: 所有写入/评估均在用户收到答案后异步执行
  - 完整数据: 保存每个贡献者原始回答，后期可用任意模型重新评估
  - 独立评估: Layer 2 用独立模型打分，避免 Judge 自评偏差
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter
    from agoracle.config.schema import AppConfig
    from agoracle.domain.types import ModelResponse, QueryResult

logger = logging.getLogger(__name__)

_MONITOR_LOG_PATH = Path(os.getenv(
    "QUERY_MONITOR_LOG_PATH",
    "data/logs/query_monitor.jsonl",
))

_LAYER2_EVALUATOR_MODEL = os.getenv("MONITOR_EVALUATOR_MODEL", "gpt52")  # v1.2: GPT-5.2 — 与 Extractor(Sonnet) 不同家族，交叉验证更有价值
_LAYER2_SAMPLE_RATE = float(os.getenv("MONITOR_SAMPLE_RATE", "0.3"))  # 30% 抽样

_LAYER2_PROMPT = """\
你是一个严格的答案质量评审员。请对比以下两份回答，分别打分（1-10整数）。
评分标准: accuracy=事实准确性, completeness=覆盖完整性, nuance=推理深度与细节

## 用户问题
{question}

## 回答 {label_a}（{desc_a}）
{answer_a}

## 回答 {label_b}（{desc_b}）
{answer_b}

请输出 JSON（无其他内容）:
{{
  "{key_a}_accuracy": <1-10>,
  "{key_a}_completeness": <1-10>,
  "{key_a}_nuance": <1-10>,
  "{key_b}_accuracy": <1-10>,
  "{key_b}_completeness": <1-10>,
  "{key_b}_nuance": <1-10>,
  "winner": "{label_a}" | "{label_b}" | "tie",
  "reason": "<一句话说明具体哪里更好，不接受'A更好'这种空洞理由>"
}}"""


def write_layer1(
    result: "QueryResult",
    contributor_responses: list["ModelResponse"],
    best_single_model: str = "",
    best_single_content: str = "",
    extractor_metadata: Any = None,
    question_type: str = "",
    adaptive_strategy: str = "",
    moa_applied: bool = False,
    refinement_api_calls: int = 0,
) -> None:
    """Layer 1: 零成本信号 — 同步写入 JSONL（在 orchestrator 结尾调用）。

    包含完整的贡献者原始回答，供后期离线复盘使用。
    v1.1: 新增 extractor_metadata — 记录 Extractor 的评审依据（best_model_reason, pairwise_evaluated）
    v1.2: 新增 question_type/adaptive_strategy/moa_applied/refinement_api_calls — 支持按题型/策略细粒度分析
    v1.3: refinement_rounds 重命名为 refinement_api_calls — 计数 API 调用次数（critic+refine），非逻辑轮次
    """
    try:
        _MONITOR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {
            "schema_version": "1.0",
            "ts": datetime.now().isoformat(),
            "query_id": result.query_id,
            "mode": result.resolved_mode,
            "question": result.question,

            # 质量门结果
            "gate_result": result.quality_gate_result,
            "confidence": round(result.confidence, 3),
            "consensus_type": result.consensus_type,
            "has_divergence": result.has_divergence,
            "best_single_score_gap": round(result.best_single_score_gap, 3),

            # 成本与性能
            "latency_ms": result.latency_ms,
            "total_tokens": result.total_tokens,
            "estimated_cost_usd": round(result.estimated_cost_usd, 6),
            "estimated_cost_rmb": round(result.estimated_cost_usd * 7.25, 4),
            "contributor_count": result.contributor_count,
            "total_model_calls": result.total_model_calls,

            # Judge 对每个模型的评分（零成本，已有数据）
            "model_evaluations": result.model_evaluations,

            # 最佳单模型
            "best_single_model": best_single_model,

            # Extractor 评审依据（v1.1: 供复盘时判断评审是否合理）
            "extractor_best_model_reason": getattr(extractor_metadata, "best_model_reason", "") if extractor_metadata else "",
            "extractor_pairwise_evaluated": getattr(extractor_metadata, "pairwise_evaluated", False) if extractor_metadata else False,

            # 管道策略字段（v1.2: 支持按题型/策略维度细粒度分析）
            "question_type": question_type,
            "adaptive_strategy": adaptive_strategy,
            "moa_applied": moa_applied,
            "refinement_api_calls": refinement_api_calls,  # v1.3: API calls (critic+refine), not logical rounds

            # 聚合答案（最终）
            "final_answer": result.final_answer,

            # 每个贡献者的原始回答全文（核心：供后期复盘）
            "contributor_responses": [
                {
                    "model_id": r.model_id,
                    "content": r.content,
                    "latency_ms": r.latency_ms,
                    "prompt_tokens": r.prompt_tokens,
                    "completion_tokens": r.completion_tokens,
                    "success": r.success,
                }
                for r in contributor_responses
            ],

            # 最佳单模型回答（用于 Layer 2 对比）
            "best_single_content": best_single_content,

            # Layer 2 评估结果（初始为空，异步填充）
            "layer2_eval": None,
        }
        with open(_MONITOR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        logger.debug(f"[monitor] Layer1 written: {result.query_id}")
    except Exception as e:
        logger.debug(f"[monitor] Layer1 write failed (non-critical): {e}")


async def run_layer2_async(
    query_id: str,
    question: str,
    final_answer: str,
    best_single_content: str,
    best_single_model: str,
    adapter: "OpenAIModelAdapter",
) -> None:
    """Layer 2: 异步轻量评估 — 用 Claude Sonnet 4.6 独立打分，结果追加写入 JSONL。

    v1.1 改进:
      - 评估模型升级: Gemini Flash → Claude Sonnet 4.6 (独立于贡献者和Judge)
      - 随机交换 A/B 顺序，消除位置偏差 (Zheng et al. 2023: 5-10% 位置偏好)
      - 传完整答案，不截断 (避免丢失深度回答后半段)
    按 LAYER2_SAMPLE_RATE 抽样，避免每次都调用。
    """
    import random
    if random.random() > _LAYER2_SAMPLE_RATE:
        return
    if not best_single_content or not final_answer:
        return
    if final_answer.strip() == best_single_content.strip():
        return  # 完全相同，无需对比

    try:
        from agoracle.domain.types import Role, RoleCall

        # v1.1: 随机交换 A/B 消除位置偏差
        swap = random.random() < 0.5
        # Bug2 fix (v3.6): 使用中性标签，不暴露答案来源身份（盲评）
        # Zheng et al. 2023: 暴露身份会引入 identity bias，降低评估一致性 15-20%
        if swap:
            label_a, label_b = "Beta", "Alpha"
            desc_a = "答案 Beta"
            desc_b = "答案 Alpha"
            answer_a_text, answer_b_text = best_single_content, final_answer
            key_a, key_b = "b", "a"
        else:
            label_a, label_b = "Alpha", "Beta"
            desc_a = "答案 Alpha"
            desc_b = "答案 Beta"
            answer_a_text, answer_b_text = final_answer, best_single_content
            key_a, key_b = "a", "b"

        prompt = _LAYER2_PROMPT.format(
            question=question,
            label_a=label_a,
            label_b=label_b,
            desc_a=desc_a,
            desc_b=desc_b,
            answer_a=answer_a_text,   # v1.1: 不截断
            answer_b=answer_b_text,   # v1.1: 不截断
            key_a=key_a,
            key_b=key_b,
        )
        call = RoleCall(
            call_id=f"monitor-l2-{uuid.uuid4().hex[:6]}",
            model_id=_LAYER2_EVALUATOR_MODEL,
            role=Role.METADATA_EXTRACTOR,
            system_prompt="你是严格的答案质量评审员，只输出 JSON，不要包含其他文字。",
            messages=[{"role": "user", "content": prompt}],
            timeout_seconds=60,  # Sonnet 比 Flash 慢，给足时间
        )
        resp = await adapter.call(call)
        if not resp.success or not resp.content:
            return

        raw = resp.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        eval_data = json.loads(raw.strip())

        # 归一化: 无论 A/B 是否交换，winner 统一记录为「aggregated」或「best_single」
        # swap=False: label_a="Alpha"(=aggregated), label_b="Beta"(=best_single)
        # swap=True:  label_a="Beta"(=best_single), label_b="Alpha"(=aggregated)
        # v3.7 fix: prompt 要求 LLM 输出 "Alpha"/"Beta"，但旧逻辑只匹配 "A"/"B"
        #           导致所有非 tie 结果被错误归类为 best_single。
        #           现在同时兼容全名（Alpha/Beta）和单字母（A/B）两种 LLM 输出。
        raw_winner = eval_data.get("winner", "tie")
        raw_winner_norm = raw_winner.strip().lower()
        if raw_winner_norm == "tie":
            eval_data["winner_normalized"] = "tie"
        elif raw_winner_norm in ("alpha", "a"):
            # label_a wins: swap=False→aggregated wins; swap=True→best_single wins
            eval_data["winner_normalized"] = "best_single" if swap else "aggregated"
        elif raw_winner_norm in ("beta", "b"):
            # label_b wins: swap=False→best_single wins; swap=True→aggregated wins
            eval_data["winner_normalized"] = "aggregated" if swap else "best_single"
        else:
            eval_data["winner_normalized"] = "tie"  # unknown value → treat as tie

        eval_data["swapped"] = swap
        eval_data["evaluator_model"] = _LAYER2_EVALUATOR_MODEL
        eval_data["ts"] = datetime.now().isoformat()

        _patch_layer2(query_id, eval_data)
        logger.debug(
            f"[monitor] Layer2 eval done: {query_id} "
            f"winner={eval_data.get('winner_normalized')} (swap={swap})"
        )
    except Exception as e:
        logger.debug(f"[monitor] Layer2 eval failed (non-critical): {e}")


def _patch_layer2(query_id: str, eval_data: dict) -> None:
    """Rewrite the matching JSONL line to add layer2_eval field."""
    try:
        if not _MONITOR_LOG_PATH.exists():
            return
        lines = _MONITOR_LOG_PATH.read_text(encoding="utf-8").splitlines()
        updated = []
        for line in lines:
            try:
                rec = json.loads(line)
                if rec.get("query_id") == query_id and rec.get("layer2_eval") is None:
                    rec["layer2_eval"] = eval_data
                    updated.append(json.dumps(rec, ensure_ascii=False))
                else:
                    updated.append(line)
            except Exception:
                updated.append(line)
        _MONITOR_LOG_PATH.write_text("\n".join(updated) + "\n", encoding="utf-8")
    except Exception as e:
        logger.debug(f"[monitor] Layer2 patch failed: {e}")
