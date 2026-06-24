#!/usr/bin/env python3
"""
Synthora 单题深度诊断工具 (Question Diagnosis)

对单道题跑完整管道，展示每一步的详细输入输出，帮助定位质量问题。

用法:
  # 诊断某道题在 Deep 模式下的完整管道
  python3 scripts/diagnose_question.py --mode deep --question "量子纠缠是否允许超光速通信？"

  # 按题目 ID 诊断 (从题库中查找)
  python3 scripts/diagnose_question.py --mode deep --id factual_01

  # 同时显示评估分数
  python3 scripts/diagnose_question.py --mode deep --id factual_01 --eval

  # 使用 v2 题库
  python3 scripts/diagnose_question.py --mode deep --id factual_01 --questions-file scripts/test_questions_v2.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agoracle.adapters.judge.llm_judge import LLMJudge
from agoracle.adapters.judge.metadata_extractor import LLMMetadataExtractor
from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter
from agoracle.config.loader import load_config
from agoracle.domain.types import (
    Mode,
    OutputDepth,
    QueryContext,
    Role,
    RoleCall,
)
from agoracle.services.orchestrator import Orchestrator, ProgressReporter
from agoracle.services.prompt_loader import PromptLoader

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("diagnose")
logger.setLevel(logging.INFO)

DIMENSIONS = ["accuracy", "completeness", "nuance", "clarity", "balance"]


def _format_duration(seconds: float) -> str:
    s = int(seconds)
    if s >= 60:
        m, sec = divmod(s, 60)
        return f"{m}分{sec}秒"
    return f"{s}秒"


def _truncate(text: str, max_len: int = 200) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"... ({len(text)} chars total)"


class DiagnosticReporter(ProgressReporter):
    """Captures pipeline events for diagnostic display."""

    def __init__(self):
        self.events: list[dict] = []
        self._start = time.monotonic()

    def _ts(self) -> str:
        elapsed = time.monotonic() - self._start
        return f"{elapsed:.1f}s"

    async def on_stage_start(self, stage: str, detail: str = "") -> None:
        ts = self._ts()
        self.events.append({"type": "stage_start", "stage": stage, "detail": detail, "ts": ts})
        print(f"  [{ts}] ▶ {stage}: {detail}")

    async def on_contributor_done(self, model_id: str, success: bool, latency_ms: int) -> None:
        ts = self._ts()
        status = "✅" if success else "❌"
        self.events.append({
            "type": "contributor_done", "model_id": model_id,
            "success": success, "latency_ms": latency_ms, "ts": ts,
        })
        print(f"  [{ts}]   {status} {model_id}: {latency_ms}ms")

    async def on_judge_token(self, token: str) -> None:
        pass  # Don't print tokens in diagnostic mode

    async def on_stage_complete(self, stage: str, detail: str = "") -> None:
        ts = self._ts()
        self.events.append({"type": "stage_complete", "stage": stage, "detail": detail, "ts": ts})
        print(f"  [{ts}] ✓ {stage} complete: {detail}")


async def diagnose(
    question: str,
    mode: str,
    run_eval: bool = False,
) -> dict:
    """Run full pipeline diagnosis for one question."""
    config = load_config()
    adapter = OpenAIModelAdapter(config)
    prompt_loader = PromptLoader(PROJECT_ROOT / "prompts")
    judge = LLMJudge(adapter, prompt_loader)
    extractor = LLMMetadataExtractor(adapter, prompt_loader)
    orchestrator = Orchestrator(
        config=config,
        model_adapter=adapter,
        judge=judge,
        extractor=extractor,
        prompt_loader=prompt_loader,
    )

    mode_config = config.modes.get(mode)
    if not mode_config:
        print(f"  错误: 模式 '{mode}' 不存在")
        return {}

    print(f"\n{'='*70}")
    print(f"  Synthora 单题诊断")
    print(f"{'='*70}")
    print(f"  模式: {mode}")
    print(f"  问题: {question[:80]}{'...' if len(question) > 80 else ''}")
    print(f"  贡献者: {mode_config.contributors}")
    print(f"  Judge: {mode_config.judge}")
    print(f"  Question Critic: {mode_config.question_critic or '(无)'}")
    print(f"  Answer Critic: {mode_config.answer_critic or '(无)'}")
    print(f"  N-of-M: {mode_config.n_of_m or '等全部'}")
    print(f"{'='*70}")

    # Run with diagnostic reporter
    reporter = DiagnosticReporter()
    mode_enum = Mode(mode)
    context = QueryContext(
        query_id=f"diag-{uuid.uuid4().hex[:8]}",
        question=question,
        mode=mode_enum,
        resolved_mode=mode_enum,
        web_search_enabled=False,
        critique_enabled=mode_config.critique_always_on,
        output_depth=OutputDepth.LEVEL_3,
    )

    print(f"\n  ── 管道执行 ──")
    start = time.monotonic()
    result = await orchestrator.execute(context, progress=reporter)
    elapsed = time.monotonic() - start

    # Display result summary
    print(f"\n{'─'*70}")
    print(f"  ── 结果摘要 ──")
    print(f"{'─'*70}")
    print(f"  总耗时: {_format_duration(elapsed)} ({result.latency_ms}ms)")
    print(f"  贡献者数: {result.contributor_count}")
    print(f"  总模型调用: {result.total_model_calls}")
    print(f"  Quality Gate: {result.quality_gate_result}")
    print(f"  置信度: {result.confidence:.2f}")
    print(f"  共识类型: {result.consensus_type}")
    print(f"  有分歧: {result.has_divergence}")
    if result.divergence_summary:
        print(f"  分歧摘要: {result.divergence_summary}")
    if result.question_critique:
        qc = result.question_critique
        print(f"  Question Critique: has_issues={qc.has_issues}, severity={qc.severity}")
        if qc.analysis:
            print(f"    分析: {_truncate(qc.analysis)}")

    # Key insights
    if result.key_insights:
        print(f"\n  关键洞察:")
        for i, insight in enumerate(result.key_insights, 1):
            print(f"    {i}. {_truncate(insight, 100)}")

    # Model evaluations from metadata
    if result.model_evaluations:
        print(f"\n  模型评估 (来自 Metadata Extractor):")
        for mid, ev in result.model_evaluations.items():
            acc = ev.get("accuracy", 0)
            rea = ev.get("reasoning", 0)
            uni = ev.get("uniqueness", 0)
            score = (acc + rea) / 2
            print(f"    {mid:<30} acc={acc:.2f} rea={rea:.2f} uni={uni:.2f} (avg={score:.2f})")

    # Final answer (truncated)
    print(f"\n  ── 最终答案 ({len(result.final_answer)} 字) ──")
    if len(result.final_answer) > 500:
        print(f"  {result.final_answer[:500]}...")
        print(f"  ... (省略 {len(result.final_answer) - 500} 字)")
    else:
        print(f"  {result.final_answer}")

    # Optional: evaluate with independent model
    if run_eval:
        print(f"\n  ── 独立评估 (claude_sonnet) ──")
        eval_result = await _evaluate_answer(adapter, question, result.final_answer)
        if eval_result:
            total = sum(eval_result.get(d, 0) for d in DIMENSIONS)
            print(f"  总分: {total}/50")
            for d in DIMENSIONS:
                print(f"    {d:<15} {eval_result.get(d, 0)}/10")
            if "brief_comment" in eval_result:
                print(f"  点评: {eval_result['brief_comment']}")

        # Also evaluate best individual
        print(f"\n  ── 各模型单独评估 ──")
        model_ids = [mid for mid in mode_config.contributors if adapter.supports_model(mid)]
        for mid in model_ids:
            ind_answer, ind_latency = await _run_individual(adapter, prompt_loader, mid, question)
            if ind_answer and not ind_answer.startswith("[错误"):
                ind_eval = await _evaluate_answer(adapter, question, ind_answer)
                if ind_eval:
                    ind_total = sum(ind_eval.get(d, 0) for d in DIMENSIONS)
                    print(f"    {mid:<30} {ind_total}/50  ({ind_latency}ms, {len(ind_answer)} chars)")

    print(f"\n{'='*70}\n")
    return {"result": result, "events": reporter.events}


async def _run_individual(adapter, prompt_loader, model_id: str, question: str) -> tuple[str, int]:
    """Run a single model directly."""
    system_prompt = prompt_loader.render(
        "contributor", profile_section="", rag_section="", session_section="",
    )
    role_call = RoleCall(
        call_id=f"diag-{model_id}-{uuid.uuid4().hex[:6]}",
        model_id=model_id,
        role=Role.CONTRIBUTOR,
        system_prompt=system_prompt,
        messages=[{"role": "user", "content": question}],
        timeout_seconds=120,
        web_search=False,
    )
    start = time.monotonic()
    response = await adapter.call(role_call)
    latency = int((time.monotonic() - start) * 1000)
    if response.success:
        return response.content, latency
    return f"[错误: {response.error}]", latency


async def _evaluate_answer(adapter, question: str, answer: str) -> dict | None:
    """Evaluate an answer using claude_sonnet."""
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    from benchmark_quality import EVALUATOR_SYSTEM_PROMPT, QualityBenchmark

    user_message = (
        f"## 问题\n{question}\n\n"
        f"## 待评估的回答\n{answer}\n\n"
        f"请按照系统提示中的评分标准，严格打分并输出 JSON。"
    )
    role_call = RoleCall(
        call_id=f"eval-{uuid.uuid4().hex[:6]}",
        model_id="claude_sonnet",
        role=Role.JUDGE,
        system_prompt=EVALUATOR_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
        timeout_seconds=120,
    )
    response = await adapter.call(role_call)
    if not response.success:
        print(f"  评估失败: {response.error}")
        return None

    scores = QualityBenchmark._parse_evaluation(response.content)
    # Also try to extract brief_comment
    try:
        raw = response.content
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0]
        data = json.loads(raw)
        if "brief_comment" in data:
            scores["brief_comment"] = data["brief_comment"]
    except Exception:
        pass
    return scores


async def main():
    parser = argparse.ArgumentParser(description="Synthora 单题深度诊断")
    parser.add_argument("--mode", choices=["light", "deep", "research"], default="deep")
    parser.add_argument("--question", "-q", help="直接输入问题文本")
    parser.add_argument("--id", help="题目 ID (从题库中查找)")
    parser.add_argument("--eval", action="store_true", help="同时运行独立评估打分")
    parser.add_argument("--questions-file", help="题库文件路径")

    args = parser.parse_args()

    question = args.question

    if args.id and not question:
        # Load from question file
        q_file = args.questions_file or "scripts/test_questions.json"
        q_path = PROJECT_ROOT / q_file
        if not q_path.exists():
            # Try v2
            q_path = PROJECT_ROOT / "scripts" / "test_questions_v2.json"
        if q_path.exists():
            with open(q_path, "r", encoding="utf-8") as f:
                q_data = json.load(f)
            for q in q_data["questions"]:
                if q["id"] == args.id:
                    question = q["question"]
                    print(f"  找到题目 [{args.id}]: {question[:60]}...")
                    break
            if not question:
                print(f"  错误: 题目 ID '{args.id}' 未找到")
                return
        else:
            print(f"  错误: 题库文件不存在: {q_path}")
            return

    if not question:
        print("  错误: 请用 --question 或 --id 指定问题")
        return

    await diagnose(question, args.mode, run_eval=args.eval)


if __name__ == "__main__":
    asyncio.run(main())
