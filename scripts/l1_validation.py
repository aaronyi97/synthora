#!/usr/bin/env python3
"""
L1 Validation — benchmark orchestrated answers vs single-model baseline.

Runs 5 standard questions through:
  1. Full orchestration pipeline (Light or Deep mode)
  2. Claude Opus standalone (as baseline)

Then saves results as JSON for manual/automated comparison.

Usage:
  python scripts/l1_validation.py
  python scripts/l1_validation.py --mode deep
  python scripts/l1_validation.py --questions 3
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# ── Standard Questions ──────────────────────────────────────
# 5 categories: factual, reasoning, comparison, code, creative
STANDARD_QUESTIONS = [
    {
        "id": "q1_factual",
        "category": "factual",
        "question": "量子计算中的量子纠缠是什么？它和经典物理的关联（correlation）有什么本质区别？",
        "evaluation_criteria": "准确性、是否提到Bell不等式、EPR paradox、non-locality",
    },
    {
        "id": "q2_reasoning",
        "category": "reasoning",
        "question": "为什么分布式系统中不可能同时满足一致性（Consistency）、可用性（Availability）和分区容错性（Partition Tolerance）？请从第一性原理推导。",
        "evaluation_criteria": "推导完整性、是否真的从网络分区场景出发、有无实际系统举例",
    },
    {
        "id": "q3_comparison",
        "category": "comparison",
        "question": "对比 Rust 和 Go 在系统编程领域的优劣势。在什么场景下应该选择 Rust，什么场景下选择 Go？",
        "evaluation_criteria": "多维度对比（性能、安全性、开发效率、生态）、是否有具体场景建议",
    },
    {
        "id": "q4_code",
        "category": "code",
        "question": "实现一个 Python 异步限流器（rate limiter），支持滑动窗口算法，每秒最多允许 N 个请求。给出完整代码和使用示例。",
        "evaluation_criteria": "代码正确性、是否真的是滑动窗口（非固定窗口）、有无并发安全考虑",
    },
    {
        "id": "q5_creative",
        "category": "creative",
        "question": "如果让你设计一个面向 2030 年的教育系统，结合 AI 技术，你会如何设计？请给出具体的架构和创新点。",
        "evaluation_criteria": "创新性、可行性、是否有具体技术方案而非空泛描述",
    },
]


async def run_orchestrated(question: str, mode: str = "light") -> dict:
    """Run question through the full orchestration pipeline."""
    from agoracle.adapters.judge.llm_judge import LLMJudge
    from agoracle.adapters.judge.metadata_extractor import LLMMetadataExtractor
    from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter
    from agoracle.config.loader import PROJECT_ROOT, load_config
    from agoracle.domain.router import route
    from agoracle.domain.types import Intent, Mode, OutputDepth, QueryContext, RouteDecision
    from agoracle.services.event_bus import EventBus
    from agoracle.services.orchestrator import Orchestrator
    from agoracle.services.prompt_loader import PromptLoader

    config = load_config()
    prompt_loader = PromptLoader(PROJECT_ROOT / "prompts")
    adapter = OpenAIModelAdapter(config)
    judge = LLMJudge(adapter, prompt_loader)
    extractor = LLMMetadataExtractor(adapter, prompt_loader)
    event_bus = EventBus()

    orchestrator = Orchestrator(
        config=config,
        model_adapter=adapter,
        judge=judge,
        extractor=extractor,
        prompt_loader=prompt_loader,
        event_bus=event_bus,
    )

    resolved_mode = Mode(mode)
    mode_config = config.modes.get(mode)

    context = QueryContext(
        question=question,
        mode=resolved_mode,
        resolved_mode=resolved_mode,
        intent=Intent.ANSWER,
        web_search_enabled=True,
        critique_enabled=mode_config.critique_always_on if mode_config else False,
        output_depth=OutputDepth.LEVEL_2,
    )

    start = time.monotonic()
    result = await orchestrator.execute(context)
    elapsed = int((time.monotonic() - start) * 1000)

    return {
        "source": f"orchestrated_{mode}",
        "answer": result.final_answer,
        "confidence": result.confidence,
        "quality_gate": result.quality_gate_result,
        "contributors": result.contributor_count,
        "has_divergence": result.has_divergence,
        "latency_ms": elapsed,
    }


async def run_baseline(question: str, model_id: str = "claude_opus") -> dict:
    """Run question through a single model (baseline)."""
    from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter
    from agoracle.config.loader import load_config
    from agoracle.domain.types import Role, RoleCall

    config = load_config()
    adapter = OpenAIModelAdapter(config)

    if not adapter.supports_model(model_id):
        return {
            "source": f"baseline_{model_id}",
            "answer": f"Model '{model_id}' not available",
            "latency_ms": 0,
            "error": "model_unavailable",
        }

    role_call = RoleCall(
        call_id="baseline-test",
        model_id=model_id,
        role=Role.CONTRIBUTOR,
        system_prompt="你是一个专业的知识分析师。请认真、准确、有深度地回答问题。",
        messages=[{"role": "user", "content": question}],
        timeout_seconds=120,
    )

    start = time.monotonic()
    response = await adapter.call(role_call)
    elapsed = int((time.monotonic() - start) * 1000)

    return {
        "source": f"baseline_{model_id}",
        "answer": response.content if response.success else f"ERROR: {response.error}",
        "latency_ms": elapsed,
        "success": response.success,
    }


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="L1 Validation Benchmark")
    parser.add_argument("--mode", default="light", choices=["light", "deep", "research"])
    parser.add_argument("--questions", type=int, default=5, help="Number of questions to test (1-5)")
    parser.add_argument("--baseline", default="claude_opus", help="Baseline model ID")
    parser.add_argument("--output", default=None, help="Output JSON path")
    args = parser.parse_args()

    questions = STANDARD_QUESTIONS[:args.questions]
    output_path = args.output or f"data/l1_validation_{args.mode}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    print("=" * 70)
    print(f"  L1 Validation — {len(questions)} questions")
    print(f"  Mode: {args.mode} | Baseline: {args.baseline}")
    print("=" * 70)
    print()

    results = []

    for i, q in enumerate(questions, 1):
        print(f"[{i}/{len(questions)}] {q['category']}: {q['question'][:60]}...")
        print()

        # Run orchestrated
        print(f"  Running orchestrated ({args.mode})...", end=" ", flush=True)
        try:
            orch_result = await run_orchestrated(q["question"], args.mode)
            print(f"✅ {orch_result['latency_ms']}ms, conf={orch_result.get('confidence', '?')}")
        except Exception as e:
            print(f"❌ {e}")
            orch_result = {"source": "orchestrated", "error": str(e), "answer": ""}

        # Run baseline
        print(f"  Running baseline ({args.baseline})...", end=" ", flush=True)
        try:
            base_result = await run_baseline(q["question"], args.baseline)
            print(f"✅ {base_result['latency_ms']}ms")
        except Exception as e:
            print(f"❌ {e}")
            base_result = {"source": "baseline", "error": str(e), "answer": ""}

        results.append({
            "question_id": q["id"],
            "category": q["category"],
            "question": q["question"],
            "evaluation_criteria": q["evaluation_criteria"],
            "orchestrated": orch_result,
            "baseline": base_result,
        })

        print()

    # Save results
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    report = {
        "timestamp": datetime.now().isoformat(),
        "mode": args.mode,
        "baseline_model": args.baseline,
        "questions_count": len(questions),
        "results": results,
    }

    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("=" * 70)
    print(f"  Results saved to: {output_path}")
    print("=" * 70)

    # Quick summary
    orch_latencies = [r["orchestrated"].get("latency_ms", 0) for r in results if "error" not in r["orchestrated"]]
    base_latencies = [r["baseline"].get("latency_ms", 0) for r in results if "error" not in r["baseline"]]

    if orch_latencies:
        print(f"  Orchestrated avg latency: {sum(orch_latencies) // len(orch_latencies)}ms")
    if base_latencies:
        print(f"  Baseline avg latency: {sum(base_latencies) // len(base_latencies)}ms")
    print()
    print("  Next: Review results and score each answer for quality comparison.")
    print()


if __name__ == "__main__":
    asyncio.run(main())
