#!/usr/bin/env python3
"""
Single-query pipeline test with per-model real-time monitoring.

Features:
  - Shows each model's response time as it completes
  - Circuit breaker: if pipeline exceeds hard timeout, abort and report stuck models
  - Step-by-step pipeline visibility (fan-out → judge → quality gate)

Usage:
  python3 scripts/test_single_query.py research
  python3 scripts/test_single_query.py deep
  python3 scripts/test_single_query.py light
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# Enable INFO logging to see per-model timing from adapter
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
# Quiet down noisy libs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from agoracle.config.loader import load_config
from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter
from agoracle.adapters.judge.llm_judge import LLMJudge
from agoracle.adapters.judge.metadata_extractor import LLMMetadataExtractor
from agoracle.services.orchestrator import Orchestrator, ProgressReporter
from agoracle.services.prompt_loader import PromptLoader
from agoracle.domain.types import Mode, QueryContext

# ── Circuit breaker config ───────────────────────────────
PIPELINE_HARD_TIMEOUT = {
    "light": 60,      # 1 min
    "deep": 180,      # 3 min
    "research": 240,  # 4 min
}


class LiveReporter(ProgressReporter):
    """Real-time progress reporter that prints each step."""

    def __init__(self):
        self.stage_starts: dict[str, float] = {}
        self.start = time.monotonic()

    def _elapsed(self) -> str:
        return f"{time.monotonic() - self.start:.1f}s"

    async def on_stage_start(self, stage: str, detail: str = "") -> None:
        self.stage_starts[stage] = time.monotonic()
        print(f"  [{self._elapsed()}] ▶ {stage}: {detail}", flush=True)

    async def on_contributor_done(
        self, model_id: str, success: bool, latency_ms: int
    ) -> None:
        status = "✅" if success else "❌"
        print(
            f"  [{self._elapsed()}]   {status} {model_id}: {latency_ms}ms "
            f"({latency_ms/1000:.1f}s)",
            flush=True,
        )

    async def on_judge_token(self, token: str) -> None:
        pass  # Don't print individual tokens

    async def on_stage_complete(self, stage: str, detail: str = "") -> None:
        start = self.stage_starts.get(stage, self.start)
        stage_ms = int((time.monotonic() - start) * 1000)
        print(
            f"  [{self._elapsed()}] ■ {stage} done: {detail} ({stage_ms}ms)",
            flush=True,
        )


async def run_single(mode_str: str, question: str):
    config = load_config()
    adapter = OpenAIModelAdapter(config)
    prompts = PromptLoader(PROJECT_ROOT / "prompts")
    judge = LLMJudge(adapter, prompts)
    extractor = LLMMetadataExtractor(adapter, prompts)
    orch = Orchestrator(config, adapter, judge, extractor, prompts)

    mode_config = config.modes.get(mode_str)
    contributors = mode_config.contributors if mode_config else []
    n_of_m = mode_config.n_of_m if mode_config else 0
    hard_timeout = PIPELINE_HARD_TIMEOUT.get(mode_str, 180)

    mode = Mode(mode_str)
    ctx = QueryContext(
        question=question,
        mode=mode,
        resolved_mode=mode,
        web_search_enabled=False,
        critique_enabled=True,
    )

    print(f"\n{'='*60}")
    print(f"  Mode: {mode_str}")
    print(f"  Contributors: {contributors}")
    print(f"  N-of-M: {n_of_m} (0=wait all)")
    print(f"  Hard timeout: {hard_timeout}s")
    print(f"  Question: {question[:60]}...")
    print(f"{'='*60}\n")

    reporter = LiveReporter()
    start = time.monotonic()

    try:
        result = await asyncio.wait_for(
            orch.execute(ctx, progress=reporter),
            timeout=hard_timeout,
        )
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        print(f"\n{'='*60}")
        print(f"  ⛔ CIRCUIT BREAKER: pipeline exceeded {hard_timeout}s")
        print(f"  Wall time: {elapsed:.1f}s")
        print(f"  STOP — check which model/step is stuck before continuing")
        print(f"{'='*60}\n")
        return elapsed

    elapsed = time.monotonic() - start

    print(f"\n{'='*60}")
    print(f"  ✅ Result:")
    print(f"  Latency: {elapsed:.1f}s ({result.latency_ms}ms)")
    print(f"  Contributors: {result.contributor_count}")
    print(f"  Confidence: {result.confidence:.2f}")
    print(f"  Quality Gate: {result.quality_gate_result}")
    print(f"  Answer length: {len(result.final_answer)} chars")
    print(f"{'='*60}\n")

    print(f"  Answer preview:\n  {result.final_answer[:300]}...")
    return elapsed


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "research"
    question = sys.argv[2] if len(sys.argv) > 2 else "AI 是否应该拥有法律人格？请从支持和反对两方面分析。"

    elapsed = asyncio.run(run_single(mode, question))
    print(f"\n  Total wall time: {elapsed:.1f}s")
