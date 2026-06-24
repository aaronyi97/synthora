#!/usr/bin/env python3
"""
Synthora 质量评估基准 (Quality Benchmark)

核心功能:
  1. 对每道基准题，分别获取「聚合回答」和「各模型单独回答」
  2. 用独立的评估模型对所有回答打分（5 维度 × 1-100 分）
  3. 输出 JSON 基线文件 + 控制台汇总
  4. 支持 --compare 模式，对比两份基线文件

用法:
  # 跑基准 (light 模式)
  python3 scripts/benchmark_quality.py --mode light

  # 跑基准 (deep 模式)，默认用 gemini_3_flash 评估（与 Judge 分离）。请在前台运行以便直接看到进度与预计剩余时间。
  python3 scripts/benchmark_quality.py --mode deep

  # 指定评估模型
  python3 scripts/benchmark_quality.py --mode deep --evaluator claude_opus_thinking

  # 对比两份基线
  python3 scripts/benchmark_quality.py --compare data/benchmark/baseline_v1.json data/benchmark/candidate.json

评分维度:
  - accuracy     事实准确性 (1-10)
  - completeness 完整性 (1-10)
  - nuance       多角度/辩证性 (1-10)
  - clarity      表达清晰度 (1-10)
  - balance      不偏颇/客观性 (1-10)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

# Setup path
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
from agoracle.services.orchestrator import Orchestrator
from agoracle.services.prompt_loader import PromptLoader

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("benchmark")
logger.setLevel(logging.INFO)

# ═══════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════

DIMENSIONS = ["accuracy", "completeness", "nuance", "clarity", "balance"]

EVALUATOR_SYSTEM_PROMPT = """你是一位极其严格的答案质量评审专家。你必须像顶级学术期刊的审稿人一样苛刻地评分。

## 重要评分原则（必须遵守）

1. **使用完整刻度**：分数范围 1-100，你必须使用 20-90 区间，而非扎堆在 70-95。一般性正确的 AI 回答应在 55-70 区间。
2. **强制区分**：你的 5 个维度分数中，最高分和最低分之间的差距必须 ≥ 15 分。如果所有维度都接近，说明你不够仔细。
3. **扣分必须有据**：每扣分都要有具体理由。同样，高于 80 分也必须有充分理由。
4. **找弱点**：每个回答必须至少找到 1 个具体弱点并在 brief_comment 中说明。完美回答不存在。

## 评分维度（每项 1-100 分）

1. **accuracy（事实准确性）**
   - 90-100: 专家级精确，所有事实可验证，无任何含糊之处
   - 75-89: 核心事实正确，少量细节不够精确但不影响结论
   - 55-74: 基本方向正确，有明显简化或个别事实错误
   - 35-54: 有重要事实错误，影响论述可信度
   - 1-34: 核心论述就是错的

2. **completeness（完整性）**
   - 90-100: 教科书级覆盖，包括边缘情况、例外和最新发展
   - 75-89: 覆盖主要方面，遗漏了部分有价值的补充视角
   - 55-74: 覆盖基本方面，缺少深入展开
   - 35-54: 有重要方面被完全遗漏
   - 1-34: 严重不完整

3. **nuance（多角度/辩证性）**
   - 90-100: 专家级多维思考，主动识别局限性、反例和开放问题
   - 75-89: 考虑了主要不同视角，有一定深度
   - 55-74: 承认复杂性但未深入，缺少反面论证
   - 35-54: 观点单一，缺乏辩证思维
   - 1-34: 完全一面之词

4. **clarity（表达清晰度）**
   - 90-100: 结构精巧，逻辑链条完整，专业且易懂
   - 75-89: 结构清楚，表达流畅，偶有冗余
   - 55-74: 能理解但组织一般，有些啰嗦或跳跃
   - 35-54: 结构混乱，需要反复阅读
   - 1-34: 难以理解

5. **balance（不偏颇/客观性）**
   - 90-100: 对各方立场给予公正且有深度的呈现，明确区分事实与观点
   - 75-89: 基本平衡，轻微倾向但不影响整体
   - 55-74: 有可感知的倾向性
   - 35-54: 明显偏向一方
   - 1-34: 完全偏向一方

## 典型评分参考（校准锚点）

- 一个短但正确的 AI 回答: accuracy=65, completeness=40, nuance=35, clarity=70, balance=65 → 总分 275/500
- 一个详细全面的好 AI 回答: accuracy=76, completeness=74, nuance=68, clarity=78, balance=75 → 总分 371/500
- 一个真正卓越的专家级回答: accuracy=88, completeness=85, nuance=82, clarity=86, balance=84 → 总分 425/500

## 输出格式

严格按以下 JSON 格式输出，不要输出任何其他内容：

```json
{
  "accuracy": <1-100>,
  "completeness": <1-100>,
  "nuance": <1-100>,
  "clarity": <1-100>,
  "balance": <1-100>,
  "brief_comment": "<一句话点评，指出最主要的优点和一个具体弱点>"
}
```
"""


# ═══════════════════════════════════════════════
# Core Benchmark Logic
# ═══════════════════════════════════════════════

class QualityBenchmark:
    """质量评估基准引擎。"""

    # Default lite questions: 1 factual + 1 technical + 1 controversial (representative spread)
    LITE_QUESTIONS = ["fact_01", "tech_02", "controversy_01"]

    # Smoke test: 8 fixed core + 2 rotating slots (total 10 per run)
    # Fixed core covers: factual/controversial/technical/reasoning/hallucination paths
    # Rotating pool covers: analytical/cultural/meta_cognition (anti-overfitting)
    # - factual_01: BEST_SINGLE trigger (clear correct answer)
    # - controversial_01: multi_perspective strategy (MoA should be suppressed)
    # - technical_01: debate strategy (MoA + refinement)
    # - reasoning_01: exclude_search_contributors (pure logic)
    # - search_factual_01: Tavily search path (requires_search=true)
    # - hallucination_trap_01: premise error detection
    # - hallucination_trap_03: attribution verification (top error type 22.6%)
    # - hallucination_trap_04: numerical computation (2nd error type 19.4%)
    SMOKE_QUESTIONS = [
        "factual_01", "controversial_01", "technical_01",
        "reasoning_01", "search_factual_01", "hallucination_trap_01",
        "hallucination_trap_03", "hallucination_trap_04",
    ]
    # Rotating pool: 2 questions sampled per run (seeded by run_id for reproducibility)
    # Covers categories missing from fixed core: analytical/cultural/meta_cognition
    SMOKE_ROTATION_POOL = [
        "analytical_v3_01", "analytical_v3_02",   # v4.29 regression detection
        "cultural_01", "cultural_02",              # cross-cultural analysis
        "meta_cognition_01", "meta_cognition_02",  # deep synthesis
    ]
    SMOKE_ROTATION_SLOTS = 2  # number of questions sampled from pool per run

    # Research smoke: 6 fixed core + 2 rotating slots (total 8 per run)
    # Covers research-specific paths: 7-model ensemble, deep synthesis, search, meta-cognition
    # - technical_01: 7-model ensemble on hard technical (Transformer math) — stress test
    # - controversial_02: multi-perspective synthesis (nuclear energy) — MoA quality
    # - search_factual_01: requires_search=true (real-time data injection)
    # - meta_cognition_01: deep synthesis (Munger mental models) — research strength
    # - reasoning_05: pure logic (folk theorem / Nash equilibrium) — no search needed
    # - factual_01: physics precision (quantum entanglement) — CONFIDENCE_WITHOUT_BASIS risk
    RESEARCH_SMOKE_QUESTIONS = [
        "technical_01", "controversial_02", "search_factual_01",
        "meta_cognition_01", "reasoning_05", "factual_01",
    ]
    RESEARCH_SMOKE_ROTATION_POOL = [
        "search_factual_02", "search_controversial_01",  # more search coverage
        "analytical_v3_01", "analytical_v3_03",          # v4.29 regression
        "cultural_01", "hallucination_trap_01",          # missing categories
    ]
    RESEARCH_SMOKE_ROTATION_SLOTS = 2

    def __init__(
        self,
        mode: str = "light",
        concurrency: int = 3,
        show_answers: bool = False,
        evaluator_model_id: str | None = None,
        lite: bool = False,
        stop_on_error: bool = False,
        question_timeout: int = 0,
        use_router: bool = True,
        content_critique: bool = False,
    ):
        self.mode = mode
        self.concurrency = concurrency
        self.show_answers = show_answers
        self.lite = lite
        self.stop_on_error = stop_on_error
        self.question_timeout = question_timeout  # 0 = no per-question timeout
        self.use_router = use_router              # v2.4.3: default True — enables question_type attribution for root cause analysis
        self.content_critique = content_critique  # v4.23: auto content critique (Layer ③)
        # Semaphore 限制同时进行的题目数，防止 API 限流
        self._question_sem = asyncio.Semaphore(concurrency)
        # v3.10: 断线检测 — 连续 ConnectionError 超过阈值时自动暂停
        self._consecutive_conn_errors = 0
        self._conn_error_threshold = 3  # 连续3题全部 ConnectionError 则暂停
        # v4.1: 模型异常强制停止 — QUOTA_EXHAUSTED/模型持续失败时立即终止
        # key=model_id, value=连续失败题数
        self._model_fail_counts: dict[str, int] = {}
        self._model_fail_threshold = 2  # 同一模型连续2题失败 → 强制停止
        self._force_stop_event = asyncio.Event()  # 设置后所有待运行题目跳过
        self._force_stop_reason: str = ""           # 记录停止原因，写入 summary
        # v4.8 铁律: 评估器失败计数 — 连续2题评估器失败 → 强制停止（污染数据无价值）
        self._evaluator_fail_counts: dict[str, int] = {}
        self._evaluator_fail_threshold = 2
        # v4.8 铁律: 成本异常阈值 — 单题超过此值即停（默认 $2.00，异常才会触发）
        self._cost_abort_per_question = 2.00
        self.config = load_config()

        # ── Lite mode: override config to eliminate opus calls ──
        # Judge → sonnet_thinking, remove opus from contributors, disable refine
        # Result: 0 opus API calls → fast + cheap iteration testing
        if lite:
            mc = self.config.modes.get(mode)
            if mc:
                mc.judge = "claude_sonnet_thinking"
                mc.answer_critic = ""            # skip Answer Critic
                mc.max_refinement_rounds = 0     # skip refine
                if "claude_opus_thinking" in mc.contributors:
                    mc.contributors = [c for c in mc.contributors if c != "claude_opus_thinking"]
                    # Adjust n_of_m: keep skip-slowest-1 behavior
                    mc.n_of_m = max(1, len(mc.contributors) - 1)

        self.adapter = OpenAIModelAdapter(self.config)
        self.prompt_loader = PromptLoader(PROJECT_ROOT / "prompts")
        self.judge = LLMJudge(self.adapter, self.prompt_loader)
        self.extractor = LLMMetadataExtractor(self.adapter, self.prompt_loader)

        # SearchService: wired but inactive during benchmark (web_search_enabled=False)
        from agoracle.services.search_service import SearchService
        sc = self.config.search
        self.search_service = SearchService(
            api_key_env=sc.api_key_env,
            max_results=sc.max_results,
            search_depth=sc.search_depth,
            include_answer=sc.include_answer,
            timeout_seconds=sc.timeout_seconds,
        ) if sc.enabled else None

        self.orchestrator = Orchestrator(
            config=self.config,
            model_adapter=self.adapter,
            judge=self.judge,
            extractor=self.extractor,
            prompt_loader=self.prompt_loader,
            search_service=self.search_service,
        )

        # Load mode config to get contributor list
        self.mode_config = self.config.modes.get(mode)
        if not self.mode_config:
            raise ValueError(f"Mode '{mode}' not found in config")

        # Evaluator: must be independent from BOTH Judge AND contributors.
        # claude_sonnet is NOT a Deep contributor, NOT the Judge, follows JSON format
        # perfectly, fast (~3s), and gives discriminating scores (not ceiling effect).
        self.evaluator_model_id = evaluator_model_id or "claude_sonnet"

        # Load questions (v2: 30题6类，统计置信度更高)
        q_path = PROJECT_ROOT / "scripts" / "test_questions_v2.json"
        with open(q_path, "r", encoding="utf-8") as f:
            q_data = json.load(f)
        self.all_questions = q_data["questions"]

        # Filter questions suitable for this mode
        self.questions = [
            q for q in self.all_questions
            if mode in q.get("expected_modes", [mode])
        ]

        logger.info(
            f"Benchmark initialized: mode={mode}, concurrency={concurrency}, "
            f"questions={len(self.questions)}/{len(self.all_questions)}, "
            f"contributors={self.mode_config.contributors}"
        )

    async def run(self, output_path: Path | None = None) -> dict:
        """Run the complete benchmark and return results dict.

        Args:
            output_path: If provided, results are saved incrementally after each
                         question completes. This prevents data loss on kill/crash
                         and enables seamless --resume.
        """
        timestamp = datetime.now().isoformat(timespec="seconds")

        # ── Traceability metadata ──
        try:
            commit_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=str(PROJECT_ROOT),
                stderr=subprocess.DEVNULL,
            ).decode().strip()
        except Exception:
            commit_sha = "unknown"
            logger.warning("Could not determine git commit SHA — traceability degraded")
        evaluator_prompt_hash = hashlib.sha256(
            EVALUATOR_SYSTEM_PROMPT.encode()
        ).hexdigest()[:12]

        results = {
            "meta": {
                "timestamp": timestamp,
                "mode": self.mode,
                "evaluator": self.evaluator_model_id,
                "contributors": self.mode_config.contributors,
                "judge": self.mode_config.judge,
                "question_count": len(self.questions),
                "commit_sha": commit_sha,
                "evaluator_prompt_hash": evaluator_prompt_hash,
                "use_router": self.use_router,
                "config_snapshot": {
                    mid: self.config.models[mid].model_name
                    for mid in self.mode_config.contributors
                    if mid in self.config.models
                },
                # v4.7 (变更5): question_set traceability — prevents compare-different-sets confusion
                "question_set_id": getattr(self, '_smoke_question_set_id', 'full_suite'),
                "question_ids": [q["id"] for q in self.questions],
                "rotation_ids": getattr(self, '_smoke_rotation_ids', []),
            },
            "questions": [],
        }

        # ── Progress / ETA tracking ──
        total = len(self.questions)
        self._progress_total = total
        self._progress_done = 0
        self._progress_start = time.monotonic()
        self._progress_question_times: list[float] = []  # seconds per question
        self._progress_lock = asyncio.Lock()

        # Initial save so the file exists immediately (enables early --resume)
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            self._save_results(results, output_path)

        print(f"\n  多题并发: {total} 题, 最多同时 {self.concurrency} 题")
        if output_path:
            print(f"  增量保存: {output_path}（每题完成自动写盘，支持 Ctrl+C 后 --resume 续跑）")
        print(f"{'='*60}")
        self._print_progress_bar()

        async def _run_with_sem(q):
            async with self._question_sem:
                q_start = time.monotonic()
                if self.question_timeout > 0:
                    out = await asyncio.wait_for(
                        self._benchmark_one_question(q),
                        timeout=self.question_timeout,
                    )
                else:
                    out = await self._benchmark_one_question(q)
                q_elapsed = time.monotonic() - q_start
                q_id = q.get("id", "?")

                async with self._progress_lock:
                    self._progress_done += 1
                    self._progress_question_times.append(q_elapsed)

                    # Append result and save incrementally
                    results["questions"].append(out)
                    ens = out.get("ensemble_scores", {})
                    ens_total = sum(ens.get(d, 0) for d in DIMENSIONS)
                    winner = out.get("winner", "?")
                    margin = out.get("margin_vs_best_single")
                    margin_str = f" ({'+' if margin and margin > 0 else ''}{margin:.0f})" if margin is not None else ""
                    print(f"  [{q_id}] 完成 聚合 {ens_total}/500 | {winner}{margin_str}")

                    if output_path:
                        self._save_results(results, output_path)

                    # ── v4.8 铁律检测 ──────────────────────────────────────────
                    # 铁律1: AUTH失败 (401) — 第1题即停，key无效继续跑没有意义
                    ind_answers = out.get("individual_answers", {})
                    for mid, ans_data in ind_answers.items():
                        ans_text = ans_data.get("answer", "") if isinstance(ans_data, dict) else str(ans_data)
                        # v2.8.9: _classify_error() returns "AuthenticationError: model call failed"
                        # (no "401" string), so also match the error class name directly.
                        is_auth_fail = (
                            ("401" in ans_text and ("invalid" in ans_text.lower() or "无效" in ans_text))
                            or "AuthenticationError" in ans_text
                        )
                        is_quota_fail = "QUOTA_EXHAUSTED" in ans_text or "quota" in ans_text.lower()
                        is_model_fail = not ans_text or ans_text.startswith("[错误") or ans_text.startswith("系统错误:")
                        if is_auth_fail:
                            # AUTH失败：第1次就停止，key失效无法自愈
                            stop_msg = f"🔴 [铁律1] AUTH失败: {mid} 返回 401 invalid token，benchmark 立即终止。\n  根因: API Key 失效或路由错误，继续跑产生的数据全部无效。\n  修复步骤: 检查并替换 {mid} 的 API Key，然后重新运行（不要 --resume，数据已污染）。"
                            print(f"\n  {stop_msg}")
                            self._force_stop_reason = stop_msg
                            self._force_stop_event.set()
                        elif is_quota_fail or is_model_fail:
                            self._model_fail_counts[mid] = self._model_fail_counts.get(mid, 0) + 1
                            fail_count = self._model_fail_counts[mid]
                            reason = "QUOTA_EXHAUSTED" if is_quota_fail else "持续失败"
                            print(f"  ⚠️  [{q_id}] {mid} {reason} (连续第{fail_count}次)")
                            if fail_count >= self._model_fail_threshold:
                                stop_msg = f"🔴 [铁律1] {mid} 连续 {fail_count} 题 {reason}，benchmark 终止。\n  修复后重新运行（不要 --resume）。"
                                print(f"\n  {stop_msg}")
                                self._force_stop_reason = stop_msg
                                self._force_stop_event.set()
                        else:
                            self._model_fail_counts[mid] = 0

                    # 铁律2: 评估器失败 — 评分为0视为评估器故障，连续2题停止
                    # 评估器故障会导致 winner/margin 全部无意义，数据不可用
                    eval_statuses = out.get("individual_eval_statuses", {})
                    ens_eval = out.get("ensemble_eval_status", "")
                    eval_failed_count = sum(
                        1 for st in list(eval_statuses.values()) + [ens_eval]
                        if st in ("PARSE_ERROR", "INFRA_FAILED")
                    )
                    total_evals = len(eval_statuses) + 1  # +1 for ensemble
                    eval_fail_rate = eval_failed_count / total_evals if total_evals > 0 else 0
                    if eval_fail_rate >= 0.5:  # 超过50%的评估失败
                        self._evaluator_fail_counts["evaluator"] = self._evaluator_fail_counts.get("evaluator", 0) + 1
                        ev_fail = self._evaluator_fail_counts["evaluator"]
                        print(f"  ⚠️  [{q_id}] 评估器故障率 {eval_fail_rate:.0%} ({eval_failed_count}/{total_evals}) (连续第{ev_fail}次)")
                        if ev_fail >= self._evaluator_fail_threshold:
                            stop_msg = f"🔴 [铁律2] 评估器连续 {ev_fail} 题故障率 ≥50%，评分数据无效，benchmark 终止。\n  根因: 评估模型 ({self.evaluator_model_id}) API 不稳定。\n  修复步骤: 等待 API 恢复或切换评估模型 (--evaluator)，然后重新运行（不要 --resume）。"
                            print(f"\n  {stop_msg}")
                            self._force_stop_reason = stop_msg
                            self._force_stop_event.set()
                    else:
                        self._evaluator_fail_counts["evaluator"] = 0

                    # 铁律3: 成本异常 — 单题超过阈值立即停止
                    q_cost = out.get("total_cost_usd_accurate", 0.0)
                    if q_cost > self._cost_abort_per_question:
                        stop_msg = f"🔴 [铁律3] 成本异常: [{q_id}] 单题消耗 ${q_cost:.4f}，超过阈值 ${self._cost_abort_per_question:.2f}，benchmark 终止。\n  根因: 可能存在无限循环/超长响应/计费错误。\n  修复步骤: 检查该题的模型响应长度和 API 计费记录，确认原因后重新运行。"
                        print(f"\n  {stop_msg}")
                        self._force_stop_reason = stop_msg
                        self._force_stop_event.set()

                self._print_progress_bar()
                return out

        async def _run_with_sem_safe(q):
            q_id = q.get("id", f"q?")
            # v4.1: 强制停止检测 — 已触发则跳过剩余题目
            if self._force_stop_event.is_set():
                return {
                    "id": q_id, "question": q.get("question", ""),
                    "category": q.get("category", "unknown"),
                    "error": "跳过（模型异常强制停止）",
                    "question_status": "SKIPPED",
                    "ensemble_scores": {}, "individual_scores": {},
                }
            try:
                return await _run_with_sem(q)
            except asyncio.TimeoutError:
                error_msg = f"超时 (>{self.question_timeout}s)"
                logger.error(f"题目 {q_id} {error_msg}")
                print(f"\n  ⏰ [{q_id}] {error_msg}")
                failed = {
                    "id": q_id,
                    "question": q.get("question", ""),
                    "category": q.get("category", "unknown"),
                    "error": error_msg,
                    "question_status": "TIMEOUT",
                    "ensemble_scores": {},
                    "individual_scores": {},
                }
                async with self._progress_lock:
                    self._progress_done += 1
                    results["questions"].append(failed)
                    if output_path:
                        self._save_results(results, output_path)
                self._print_progress_bar()
                if self.stop_on_error:
                    print(f"  ❌ 已停止。修复后用 --resume 继续：")
                    print(f"     python3 scripts/benchmark_quality.py --mode {self.mode} -c 1 -s -t {self.question_timeout} --resume {output_path}")
                    raise
                return failed
            except Exception as exc:
                logger.exception(f"题目 {q_id} 失败: {exc}")
                is_conn_error = "ConnectionError" in type(exc).__name__ or "Connection error" in str(exc)
                failed = {
                    "id": q_id,
                    "question": q.get("question", ""),
                    "category": q.get("category", "unknown"),
                    "error": str(exc),
                    "question_status": "INFRA_FAILED",
                    "ensemble_scores": {},
                    "individual_scores": {},
                }
                async with self._progress_lock:
                    self._progress_done += 1
                    results["questions"].append(failed)
                    if output_path:
                        self._save_results(results, output_path)
                    # v3.10: 断线检测
                    if is_conn_error:
                        self._consecutive_conn_errors += 1
                    else:
                        self._consecutive_conn_errors = 0
                    if self._consecutive_conn_errors >= self._conn_error_threshold:
                        print(f"\n  🔴 连续 {self._consecutive_conn_errors} 题 ConnectionError，网络可能已断线，自动暂停。")
                        print(f"  恢复后用 --resume 继续：")
                        if output_path:
                            print(f"     python3 scripts/benchmark_quality.py --mode {self.mode} -c 1 --resume {output_path}")
                        raise RuntimeError(f"网络断线检测：连续 {self._consecutive_conn_errors} 次 ConnectionError") from exc
                self._print_progress_bar()
                if self.stop_on_error:
                    print(f"\n  ❌ [{q_id}] 失败: {exc}")
                    print(f"  已停止。修复后用 --resume 继续：")
                    print(f"     python3 scripts/benchmark_quality.py --mode {self.mode} -c 1 -s -t {self.question_timeout} --resume {output_path}")
                    raise
                print(f"  [{q_id}] 失败，跳过: {exc}")
                return failed

        try:
            task_list = [_run_with_sem_safe(q) for q in self.questions]
            await asyncio.gather(*task_list)
        except (asyncio.TimeoutError, Exception) as exc:
            # stop_on_error: save progress and return partial results
            if self.stop_on_error:
                results["summary"] = self._compute_summary(results["questions"])
                results["summary"]["early_stop"] = True
                results["summary"]["stop_reason"] = str(exc)
                if output_path:
                    self._save_results(results, output_path)
                return results
            raise

        # Final progress line (100%)
        self._print_progress_bar(final=True)

        # Aggregate
        results["summary"] = self._compute_summary(results["questions"])

        # v4.8: propagate iron-law stop reason into summary
        if self._force_stop_reason:
            results["summary"]["early_stop"] = True
            results["summary"]["stop_reason"] = self._force_stop_reason
            results["summary"]["verdict"] = "ABORTED_IRON_LAW"
            print(f"\n  {'='*60}")
            print(f"  ⛔ 铁律触发，benchmark 已中止。数据不可用。")
            print(f"  停止原因: {self._force_stop_reason.splitlines()[0]}")
            print(f"  {'='*60}")
            if output_path:
                self._save_results(results, output_path)
            return results

        # v4.7 (Change 2): auto pairwise reeval on smoke runs
        # pairwise A/B is more robust than absolute scores for winner determination
        # Only runs if this is a smoke run (has _smoke_question_set_id attribute)
        if getattr(self, '_smoke_question_set_id', None):
            try:
                print(f"\n  \u21bb Pairwise \u9a8c\u8bc1\u5c45\u540c\u9053\u800c\u884c\uff0c\u8bf7\u7a0d\u5019...", flush=True)
                pairwise_summary = await _run_inline_pairwise(
                    results["questions"], self.adapter, self.evaluator_model_id
                )
                results["summary"]["pairwise_win_rate_pct"] = pairwise_summary["win_rate_pct"]
                results["summary"]["pairwise_detail"] = pairwise_summary["detail"]
                print(f"  \u2705 Pairwise\u80dc\u7387: {pairwise_summary['win_rate_pct']}% ({pairwise_summary['ensemble_wins']}/{pairwise_summary['total']} \u9898)", flush=True)
            except Exception as e:
                results["summary"]["pairwise_win_rate_pct"] = None
                results["summary"]["pairwise_error"] = str(e)
                print(f"  \u26a0\ufe0f Pairwise \u8fd0\u884c\u5931\u8d25 (\u4e0d\u5f71\u54cd\u4e3b\u7ed3\u679c): {e}", flush=True)

        # Final save with summary
        if output_path:
            self._save_results(results, output_path)

        return results

    @staticmethod
    def _save_results(results: dict, path: Path) -> None:
        """Atomically write results to JSON file."""
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        tmp.rename(path)  # atomic on same filesystem

    def _print_progress_bar(self, final: bool = False) -> None:
        """Print a progress bar with ETA to the console (overwrites current line)."""
        done = self._progress_done
        total = self._progress_total
        elapsed = time.monotonic() - self._progress_start
        pct = done / total if total > 0 else 1.0

        # ETA calculation
        if done > 0 and not final and self._progress_question_times:
            avg_per_q = sum(self._progress_question_times) / len(self._progress_question_times)
            # Account for concurrency: remaining batches
            remaining_questions = total - done
            effective_conc = min(self.concurrency, remaining_questions) if remaining_questions > 0 else 1
            remaining_batches = remaining_questions / effective_conc if effective_conc > 0 else 0
            eta_seconds = remaining_batches * avg_per_q
            eta_str = self._format_duration(eta_seconds)
        elif final:
            eta_str = "完成!"
        else:
            eta_str = "估算中..."

        # Progress bar
        bar_width = 30
        filled = int(bar_width * pct)
        bar = "█" * filled + "░" * (bar_width - filled)
        elapsed_str = self._format_duration(elapsed)

        line = (
            f"\r  进度 [{bar}] {done}/{total} ({pct:.0%}) "
            f"| 已用 {elapsed_str} | 预计剩余 {eta_str}   "
        )
        if final:
            print(line, flush=True)
        else:
            print(line, end="", flush=True)

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Format seconds into human-readable M分S秒 or S秒."""
        s = int(seconds)
        if s >= 3600:
            h, remainder = divmod(s, 3600)
            m, sec = divmod(remainder, 60)
            return f"{h}时{m}分{sec}秒"
        elif s >= 60:
            m, sec = divmod(s, 60)
            return f"{m}分{sec}秒"
        else:
            return f"{s}秒"

    async def _benchmark_one_question(self, question_data: dict) -> dict:
        """Benchmark one question: ensemble + each individual model. 多题并发时输出带 [q_id] 前缀."""
        question = question_data["question"]
        q_id = question_data["id"]
        p = f"  [{q_id}]"  # 多题并发时区分输出

        result = {
            "id": q_id,
            "question": question,
            "category": question_data.get("category", "unknown"),
        }

        # v4.2: Reset adapter cost tracker at question start for accurate per-question cost
        self.adapter.reset_cost_tracker()

        # ── 1. 聚合管道（串行）──
        individual_answers: dict[str, dict] = {}
        model_ids = [
            mid for mid in self.mode_config.contributors
            if self.adapter.supports_model(mid)
        ]
        print(f"{p} 步骤1: 聚合管道...", flush=True)
        ensemble_cost = 0.0
        routed_question_type = "unknown"
        ensemble_gate_result = "unknown"
        # v4.22e: per-question search — requires_search=true enables Tavily for this question
        q_search = question_data.get("requires_search", False)
        try:
            ensemble_answer, ensemble_latency, ensemble_cost, routed_question_type, ensemble_gate_result = await self._run_ensemble(question, web_search_enabled=q_search)
            qt_tag = f" [{routed_question_type}]" if self.use_router else ""
            search_tag = " 🔍" if q_search else ""
            print(f"{p} ✅ 聚合完成 {ensemble_latency}ms {len(ensemble_answer)}字 ${ensemble_cost:.4f} gate={ensemble_gate_result}{qt_tag}{search_tag}", flush=True)
        except Exception as e:
            ensemble_answer, ensemble_latency = f"[错误: {e}]", 0
            print(f"{p} ❌ 聚合失败: {e}", flush=True)
        result["ensemble_answer"] = ensemble_answer
        result["ensemble_latency_ms"] = ensemble_latency
        result["ensemble_cost_usd"] = ensemble_cost  # v4.2: This is now accurate (from adapter tracker)
        result["question_type"] = routed_question_type
        result["gate_result"] = ensemble_gate_result

        # ── 2. 各单模型并行执行 ──
        print(f"{p} 步骤2: 单模型并行回答 ({len(model_ids)}个)...", flush=True)

        async def _run_one_individual(mid: str) -> tuple[str, str, int]:
            try:
                ans, lat = await self._run_individual(mid, question)
                print(f"{p}   ✅ {mid}: {lat}ms {len(ans)}字", flush=True)
                return mid, ans, lat
            except Exception as e:
                print(f"{p}   ❌ {mid}: 失败 {e}", flush=True)
                return mid, f"[错误: {e}]", 0

        ind_results = await asyncio.gather(*[_run_one_individual(m) for m in model_ids])
        for mid, ans, lat in ind_results:
            individual_answers[mid] = {"answer": ans, "latency_ms": lat}
        result["individual_answers"] = individual_answers

        # ── 3. 评估：并行打分 ──
        print(f"{p} 步骤3: 并行评估打分 (1+{len(individual_answers)}个)...", flush=True)

        async def _eval_one(label: str, answer_text: str, mid: str | None = None) -> tuple[str, dict, str]:
            try:
                scores, raw = await self._evaluate_answer(question, answer_text, mid)
                total = sum(scores.get(d, 0) for d in DIMENSIONS)
                eval_tag = f" [cross-eval:{self._CROSS_FAMILY_EVALUATOR[mid]}]" if mid and mid in self._CROSS_FAMILY_EVALUATOR else ""
                print(f"{p}   ✅ {label} 评分: {total}/500{eval_tag}", flush=True)
                return label, scores, raw
            except Exception as e:
                logger.warning(f"{q_id} {label} 评估失败: {e}")
                print(f"{p}   ❌ {label} 评估失败: {e}", flush=True)
                return label, {d: 0 for d in DIMENSIONS}, str(e)

        # v4.22e: pass model_id="ensemble" so cross-family evaluator routes ensemble
        # to non-Anthropic evaluator (Judge=opus produces ensemble → Gemini evaluates)
        eval_tasks = [_eval_one("聚合", ensemble_answer, "ensemble")]
        for mid in individual_answers:
            eval_tasks.append(_eval_one(mid, individual_answers[mid]["answer"], mid))

        eval_results = await asyncio.gather(*eval_tasks)

        # Unpack results
        ens_scores = {d: 0 for d in DIMENSIONS}
        ind_scores = {}
        eval_raw_map = {}
        for label, scores, raw in eval_results:
            if label == "聚合":
                ens_scores = scores
                result["ensemble_eval_raw"] = raw
            else:
                ind_scores[label] = scores
                eval_raw_map[label] = raw
        result["ensemble_scores"] = ens_scores
        result["individual_scores"] = ind_scores
        result["eval_raw"] = eval_raw_map

        # ── 4. Per-question enrichment: eval_status, winner, margin ──
        ensemble_total = sum(ens_scores.get(d, 0) for d in DIMENSIONS)

        # Eval status for ensemble
        # v3.10: 同时识别 benchmark 本地异常 "[错误" 和 orchestrator 系统错误 "系统错误:"
        _is_error_answer = lambda a: not a or a.startswith("[错误") or a.startswith("系统错误:")
        if _is_error_answer(ensemble_answer):
            ens_eval_status = "INFRA_FAILED"
        elif ensemble_total == 0:
            ens_eval_status = "PARSE_ERROR"
        else:
            ens_eval_status = "OK"

        # Eval status for each individual
        ind_eval_statuses = {}
        for model_id in individual_answers:
            ind_total = sum(ind_scores.get(model_id, {}).get(d, 0) for d in DIMENSIONS)
            ans = individual_answers[model_id].get("answer", "")
            if not ans or ans.startswith("[错误"):
                ind_eval_statuses[model_id] = "INFRA_FAILED"
            elif ind_total == 0:
                ind_eval_statuses[model_id] = "PARSE_ERROR"
            else:
                ind_eval_statuses[model_id] = "OK"

        # Best single model (only from OK evaluations)
        valid_singles = {
            mid: sum(ind_scores[mid].get(d, 0) for d in DIMENSIONS)
            for mid, st in ind_eval_statuses.items()
            if st == "OK"
        }
        if valid_singles:
            best_single_model = max(valid_singles, key=valid_singles.get)
            best_single_total = valid_singles[best_single_model]
        else:
            best_single_model = None
            best_single_total = 0

        # Question status
        if ens_eval_status == "OK" and valid_singles:
            question_status = "QUALITY_VALID"
        elif any(
            st in ("API_ERROR", "INFRA_FAILED")
            for st in [ens_eval_status] + list(ind_eval_statuses.values())
        ):
            question_status = "INFRA_FAILED"
        else:
            question_status = "PARSE_FAILED"

        # Winner + margin
        # v4.1 目标修正: 聚合 >= 最佳单模型即为成功（不差于最强就是胜利）
        # 旧逻辑: margin > 0 才算胜，平局算输——这要求太高，且与核心目标矛盾
        # 新逻辑: margin >= 0 都算聚合胜（包括平局），只有 margin < 0 才算输
        if question_status == "QUALITY_VALID":
            margin = round(ensemble_total - best_single_total, 2)
            if margin >= 0:
                winner = "ensemble"
            else:
                winner = best_single_model
        else:
            margin = None
            winner = "not_comparable"

        result["question_status"] = question_status
        result["ensemble_eval_status"] = ens_eval_status
        result["individual_eval_statuses"] = ind_eval_statuses
        result["ensemble_total"] = ensemble_total
        result["best_single_model"] = best_single_model
        result["best_single_total"] = best_single_total
        result["winner"] = winner
        result["margin_vs_best_single"] = margin

        # v4.2: Read adapter cost tracker for accurate total cost (pipeline + individual + eval)
        _tracker_data = self.adapter.get_cost_tracker()
        total_cost_accurate = sum(cost for _, _, _, cost in _tracker_data)
        result["total_cost_usd_accurate"] = round(total_cost_accurate, 6)

        print(f"{p} 聚合 {ensemble_total}/500 | {winner}" +
              (f" ({'+' if margin > 0 else ''}{margin})" if margin is not None else ""),
              flush=True)

        # ── 5. 自动内容质检 (Layer ③) ──
        if self.content_critique and question_status == "QUALITY_VALID":
            accuracy = ens_scores.get("accuracy", 0)
            has_warnings = bool(result.get("fact_warnings"))
            is_trap = question_data.get("hallucination_trap", False)
            # Tiered strategy: always critique traps/low-accuracy/warned; skip high-accuracy clean answers
            if is_trap or accuracy < 80 or has_warnings:
                deep_critique = (accuracy < 60 or has_warnings or is_trap)
                print(f"{p} 步骤5: 内容质检 ({'深度' if deep_critique else '标准'})...", flush=True)
                try:
                    critique = await self._auto_critique(question, ensemble_answer, deep=deep_critique)
                    result["content_critique"] = critique
                    n_issues = len(critique.get("issues", []))
                    n_high = sum(1 for i in critique.get("issues", []) if i.get("severity") == "HIGH")
                    status = "🟢 clean" if critique.get("clean") else f"🔴 {n_high}H/{n_issues - n_high}M"
                    print(f"{p}   ✅ 质检完成: {status}", flush=True)
                except Exception as e:
                    result["content_critique"] = {"issues": [], "clean": True, "skipped": True, "reason": str(e)}
                    print(f"{p}   ❌ 质检失败: {e}", flush=True)
            else:
                result["content_critique"] = {"issues": [], "clean": True, "skipped": True, "reason": "accuracy >= 80, no warnings"}

        if self.show_answers:
            self._print_question_answers(q_id, question, result)

        return result

    def _print_question_answers(self, q_id: str, question: str, result: dict) -> None:
        """打印单题的完整问题、聚合答案与各模型答案（便于终端完整查看）。"""
        sep = "─" * 72
        print(f"\n{sep}")
        print(f"【问题】[{q_id}] {question}")
        print(sep)
        ens = result.get("ensemble_answer", "")
        ens_scores = result.get("ensemble_scores", {})
        ens_total = sum(ens_scores.get(d, 0) for d in DIMENSIONS)
        print(f"\n>>> 聚合答案 (总分 {ens_total}/500, {result.get('ensemble_latency_ms', 0)}ms)")
        print(ens)
        print()
        for model_id, data in result.get("individual_answers", {}).items():
            scores = result.get("individual_scores", {}).get(model_id, {})
            total = sum(scores.get(d, 0) for d in DIMENSIONS)
            print(f">>> {model_id} (总分 {total}/500, {data.get('latency_ms', 0)}ms)")
            print(data.get("answer", ""))
            print()
        print(sep + "\n")

    async def _run_ensemble(self, question: str, web_search_enabled: bool = False) -> tuple[str, int, float, str, str]:
        """Run the full Orchestrator pipeline, return (answer, latency_ms, estimated_cost_usd, question_type, gate_result).

        v4.22e: web_search_enabled can be set per-question to test Tavily search path.
        """
        from agoracle.domain.types import QuestionType
        mode_enum = Mode(self.mode)
        query_id = f"bench-{uuid.uuid4().hex[:8]}"

        # v2.4.2: optionally run through router to get question_type
        # This enables validating smart_routing pipeline adjustments in benchmark
        question_type = QuestionType.UNKNOWN
        if self.use_router:
            from agoracle.domain.router import route
            decision = route(question, query_id=query_id)
            question_type = decision.question_type

        context = QueryContext(
            query_id=query_id,
            question=question,
            mode=mode_enum,
            resolved_mode=mode_enum,
            web_search_enabled=web_search_enabled,
            critique_enabled=self.mode_config.critique_always_on,
            output_depth=OutputDepth.LEVEL_1,
            question_type=question_type,
        )

        start = time.monotonic()
        result = await self.orchestrator.execute(context)
        latency = int((time.monotonic() - start) * 1000)

        return result.final_answer, latency, result.estimated_cost_usd, question_type.value, result.quality_gate_result

    async def _run_individual(
        self, model_id: str, question: str
    ) -> tuple[str, int]:
        """Call one model directly (bypass orchestrator), return (answer, latency_ms).

        Mirrors fan_out.py build_contributor_calls prompt selection per mode:
          - research: contributor_research_{model_id} → contributor_deep_{model_id} → contributor
          - deep/light: contributor_deep_{model_id} → contributor_deep → contributor
        This ensures a fair individual baseline — not artificially suppressed by a generic prompt,
        and not inflated/deflated by using the wrong mode's prompt for Research.
        """
        # Mirror fan_out.py build_contributor_calls mode-specific prompt selection
        if self.mode == "research":
            system_prompt = (
                self.prompt_loader.render(
                    f"contributor_research_{model_id}",
                    profile_section="",
                    rag_section="",
                    session_section="",
                    web_search_instruction="",
                )
                or self.prompt_loader.render(
                    f"contributor_deep_{model_id}",
                    profile_section="",
                    rag_section="",
                    session_section="",
                    web_search_instruction="",
                )
                or self.prompt_loader.render(
                    "contributor",
                    profile_section="",
                    rag_section="",
                    session_section="",
                )
            )
        else:
            system_prompt = (
                self.prompt_loader.render(
                    f"contributor_deep_{model_id}",
                    profile_section="",
                    rag_section="",
                    session_section="",
                    web_search_instruction="",
                )
                or self.prompt_loader.render(
                    "contributor_deep",
                    profile_section="",
                    rag_section="",
                    session_section="",
                )
                or self.prompt_loader.render(
                    "contributor",
                    profile_section="",
                    rag_section="",
                    session_section="",
                )
            )

        role_call = RoleCall(
            call_id=f"bench-{model_id}-{uuid.uuid4().hex[:6]}",
            model_id=model_id,
            role=Role.CONTRIBUTOR,
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": question}],
            timeout_seconds=120,
            web_search=False,
        )

        start = time.monotonic()
        response = await self.adapter.call(role_call)
        latency = int((time.monotonic() - start) * 1000)

        # v2.8.6: benchmark retry guard — 发生重试说明 API 不稳定，立即暂停
        if response.retry_count > 0:
            raise RuntimeError(
                f"Benchmark 暂停：模型 '{response.model_id}' 发生了 {response.retry_count} 次重试，"
                f"说明 API 当前不稳定。请检查 API 状态后用 --resume 继续。"
            )

        if response.success:
            return response.content, latency
        return f"[错误: {response.error}]", latency

    # v4.6: Cross-family evaluator map — prevent same-family preference bias
    # Anthropic (claude_*) → evaluated by gemini_31_pro_thinking (Google family, strong enough)
    # NOT gemini_3_flash: flash may under-score nuance/depth in complex reasoning answers
    # gemini_31_pro_thinking: thinking model, understands deep reasoning, no Anthropic bias
    # Others → evaluated by default claude_sonnet (Anthropic family)
    # Rationale: Panickssery et al. (2024) 10-25% self-preference premium
    # v4.22e: Expanded cross-family evaluator map
    # Key insight from external audit: ensemble answers are produced by Judge (opus/Anthropic),
    # so evaluating ensemble with claude_sonnet = same-family bias.
    # "ensemble" is a virtual model_id used when evaluating aggregated answers.
    _CROSS_FAMILY_EVALUATOR: dict[str, str] = {
        # Anthropic family → Google evaluates
        "claude_opus_thinking": "gemini_31_pro_thinking",
        "claude_sonnet_thinking": "gemini_31_pro_thinking",
        "claude_sonnet": "gemini_31_pro_thinking",
        "claude_opus": "gemini_31_pro_thinking",
        # Ensemble (produced by Anthropic Judge) → Google evaluates
        "ensemble": "gemini_31_pro_thinking",
    }

    async def _evaluate_answer(
        self, question: str, answer: str, model_id: str | None = None
    ) -> tuple[dict[str, float], str]:
        """Use the evaluator model to score an answer on 5 dimensions.

        v4.6: model_id triggers cross-family evaluator selection to reduce
        same-family preference bias (Panickssery et al. 2024).

        Returns:
            (scores_dict, raw_evaluator_output) — raw output stored for debugging.
        """
        if not answer or answer.startswith("[错误"):
            return {d: 0 for d in DIMENSIONS}, ""

        # v4.6: Use cross-family evaluator for Anthropic models to reduce bias
        eval_model = (
            self._CROSS_FAMILY_EVALUATOR.get(model_id, self.evaluator_model_id)
            if model_id
            else self.evaluator_model_id
        )

        user_message = (
            f"## 问题\n{question}\n\n"
            f"## 待评估的回答\n{answer}\n\n"
            f"请按照系统提示中的评分标准，严格打分并输出 JSON。"
        )

        # v4.29: 评估超时提升至 180s（Gemini thinking 模型需要更长时间）
        # 同时加一次超时重试，避免偶发 TimeoutError 导致 PARSE_ERROR
        for _attempt in range(2):
            role_call = RoleCall(
                call_id=f"eval-{uuid.uuid4().hex[:6]}",
                model_id=eval_model,
                role=Role.JUDGE,  # reuse judge role for evaluation
                system_prompt=EVALUATOR_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
                timeout_seconds=180,
            )

            response = await self.adapter.call(role_call)

            # v2.8.6: benchmark retry guard — 评估阶段发生重试同样立即暂停
            if response.retry_count > 0:
                raise RuntimeError(
                    f"Benchmark 暂停：评估模型 '{response.model_id}' 发生了 {response.retry_count} 次重试，"
                    f"说明 API 当前不稳定。请检查 API 状态后用 --resume 继续。"
                )

            if not response.success:
                if _attempt == 0 and "Timeout" in (response.error or ""):
                    logger.warning(f"Evaluator timeout (attempt 1), retrying: {response.error}")
                    continue
                logger.warning(f"Evaluator failed: {response.error}")
                return {d: 0 for d in DIMENSIONS}, f"[API error: {response.error}]"

            raw = response.content or ""
            return self._parse_evaluation(raw), raw

        return {d: 0 for d in DIMENSIONS}, "[API error: exhausted retries]"

    # ═══════════════════════════════════════════════
    # Auto Content Critique (Layer ③)
    # ═══════════════════════════════════════════════
    # v4.23: Automated content quality checking — finds specific factual errors
    # in answers, complementing Layer ② score monitoring.
    # Audit model: deepseek_reasoner (non-Anthropic/Google/GPT, 推理稳定, 幻觉识别不被击穿)
    # Cross-validation: gpt53_codex for HIGH findings (GPT family, 跨家族 DeepSeek×GPT)
    _CRITIQUE_MODEL = "deepseek_reasoner"
    _CRITIQUE_CROSS_MODEL = "gpt53_codex"

    async def _auto_critique(
        self,
        question: str,
        answer: str,
        deep: bool = False,
    ) -> dict:
        """Run automated content critique on an answer.

        Args:
            question: The original question.
            answer: The answer to critique.
            deep: If True, cross-validate HIGH findings with a second model.

        Returns:
            dict with keys: issues (list), clean (bool), model, cross_validation (optional).
        """
        if not answer or answer.startswith("[错误") or answer.startswith("系统错误:"):
            return {"issues": [], "clean": True, "skipped": True, "reason": "error_answer"}

        critique_prompt = self.prompt_loader.render("auto_critique") or ""
        if not critique_prompt:
            # Fallback: read directly from file
            prompt_path = PROJECT_ROOT / "prompts" / "auto_critique.md"
            if prompt_path.exists():
                critique_prompt = prompt_path.read_text(encoding="utf-8")
            else:
                return {"issues": [], "clean": True, "skipped": True, "reason": "no_prompt"}

        user_message = (
            f"## 问题\n{question}\n\n"
            f"## 待审查的回答\n{answer}\n\n"
            f"请按照系统提示中的要求审查这份回答，输出严格 JSON。"
        )

        role_call = RoleCall(
            call_id=f"critique-{uuid.uuid4().hex[:6]}",
            model_id=self._CRITIQUE_MODEL,
            role=Role.JUDGE,
            system_prompt=critique_prompt,
            messages=[{"role": "user", "content": user_message}],
            timeout_seconds=120,
        )

        try:
            response = await self.adapter.call(role_call)
            if not response.success:
                return {"issues": [], "clean": True, "skipped": True, "reason": f"api_error: {response.error}"}
            result = self._parse_critique(response.content or "")
            result["model"] = self._CRITIQUE_MODEL
        except Exception as e:
            return {"issues": [], "clean": True, "skipped": True, "reason": f"exception: {e}"}

        # Cross-validate HIGH findings with a second model (different family)
        high_issues = [i for i in result.get("issues", []) if i.get("severity") == "HIGH"]
        if deep and high_issues:
            try:
                cross_call = RoleCall(
                    call_id=f"critique-cross-{uuid.uuid4().hex[:6]}",
                    model_id=self._CRITIQUE_CROSS_MODEL,
                    role=Role.JUDGE,
                    system_prompt=critique_prompt,
                    messages=[{"role": "user", "content": user_message}],
                    timeout_seconds=120,
                )
                cross_response = await self.adapter.call(cross_call)
                if cross_response.success:
                    cross_result = self._parse_critique(cross_response.content or "")
                    cross_result["model"] = self._CRITIQUE_CROSS_MODEL
                    result["cross_validation"] = cross_result

                    # Confirm HIGH: both models must flag HIGH for the same type
                    cross_high_types = {i.get("type") for i in cross_result.get("issues", []) if i.get("severity") == "HIGH"}
                    for issue in result["issues"]:
                        if issue.get("severity") == "HIGH":
                            if issue.get("type") in cross_high_types:
                                issue["confirmed"] = True
                            else:
                                issue["confirmed"] = False
                                issue["severity"] = "MEDIUM"  # downgrade unconfirmed
            except Exception as e:
                result["cross_validation_error"] = str(e)

        return result

    @staticmethod
    def _parse_critique(raw: str) -> dict:
        """Parse auto-critique JSON output. Tolerant of markdown fences and thinking tags."""
        import re
        text = raw.strip()

        # Strip thinking tags
        if "<thinking>" in text:
            text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL).strip()

        # Extract JSON from markdown fences
        fence_match = re.search(r"```(?:json)?\s*\n?({.*?})\s*```", text, re.DOTALL)
        if fence_match:
            text = fence_match.group(1).strip()
        elif "{" in text:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end > start:
                text = text[start:end + 1]

        # Fix common format issues
        text = text.replace("\u201c", '"').replace("\u201d", '"')
        text = text.replace("\u2018", "'").replace("\u2019", "'")
        text = re.sub(r",\s*}", "}", text)
        text = re.sub(r",\s*]", "]", text)

        try:
            data = json.loads(text)
            issues = data.get("issues", [])
            clean = data.get("clean", len(issues) == 0)
            # Validate issue structure
            valid_types = {"NUMERICAL_ERROR", "INTERNAL_CONTRADICTION", "FABRICATED_CLAIM", "CONFIDENCE_WITHOUT_BASIS"}
            validated = []
            for issue in issues:
                if isinstance(issue, dict) and issue.get("type") in valid_types:
                    validated.append(issue)
            return {"issues": validated, "clean": clean and len(validated) == 0}
        except (json.JSONDecodeError, TypeError, ValueError):
            return {"issues": [], "clean": True, "parse_error": True, "raw": text[:500]}

    @staticmethod
    def _parse_evaluation(raw: str) -> dict[str, float]:
        """
        Parse evaluator JSON output into dimension scores.

        Multi-layer fallback:
          1. Strip thinking tags / markdown fences
          2. Extract JSON block (```json or first {…})
          3. Fix common format issues (Chinese quotes, trailing commas)
          4. Standard json.loads
          5. Last resort: regex per-dimension extraction
        """
        import re

        text = raw.strip()

        # ── Layer 1: Remove thinking tags ─────────────────────
        if "<thinking>" in text:
            text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL)
            text = text.strip()

        # ── Layer 2: Extract JSON block ─────────────────────
        # P2 fix: use regex to reliably extract ```json ... ``` or ``` ... ``` blocks
        fence_match = re.search(r"```(?:json)?\s*\n?({.*?})\s*```", text, re.DOTALL)
        if fence_match:
            text = fence_match.group(1).strip()
        elif "{" in text:
            # Try to find the first { … } block
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end > start:
                text = text[start:end + 1]

        # ── Layer 3: Fix common format issues ─────────────────
        # Chinese quotes → ASCII quotes
        text = text.replace("\u201c", '"').replace("\u201d", '"')
        text = text.replace("\u2018", "'").replace("\u2019", "'")
        text = text.replace("\uff1a", ":")  # fullwidth colon
        # Trailing commas before } or ]
        text = re.sub(r",\s*}", "}", text)
        text = re.sub(r",\s*]", "]", text)
        # Remove BOM or invisible chars
        text = text.strip("\ufeff\u200b")

        # ── Layer 4: Standard JSON parse ──────────────────────
        try:
            data = json.loads(text)

            # 4a: Ideal format — all 5 dimensions present
            if all(d in data for d in DIMENSIONS):
                scores = {}
                for d in DIMENSIONS:
                    scores[d] = max(0, min(100, float(data[d])))
                return scores

            # 4b: Fallback — model returned {score: N} (e.g. gpt53_codex)
            #     Convert single score → distribute across dimensions
            if "score" in data:
                raw_score = float(data["score"])
                # Normalize: if ≤ 10, assume old 0-10 scale → map to 0-100
                if raw_score <= 10:
                    per_dim = max(0, min(100, raw_score * 10))
                else:
                    per_dim = max(0, min(100, raw_score))
                logger.info(
                    f"  Fallback: {{score}} format detected (score={raw_score}) → {per_dim:.1f}/dim"
                )
                return {d: per_dim for d in DIMENSIONS}

            # 4c: Some dimensions present, fill missing with 0
            scores = {}
            for d in DIMENSIONS:
                val = data.get(d, 0)
                scores[d] = max(0, min(100, float(val)))
            if sum(scores.values()) > 0:
                return scores

        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass  # Fall through to regex extraction

        # ── Layer 5: Regex per-dimension extraction ───────────
        logger.warning(f"JSON parse failed, attempting regex extraction. Raw: {raw[:300]}")
        scores = {}
        for d in DIMENSIONS:
            # Match patterns like "accuracy": 8, "accuracy" : 7.5, accuracy: 9
            match = re.search(
                rf'["\']?{d}["\']?\s*[:：]\s*(\d+(?:\.\d+)?)', raw, re.IGNORECASE
            )
            if match:
                scores[d] = max(0, min(100, float(match.group(1))))
            else:
                scores[d] = 0
                logger.warning(f"  Dimension '{d}' not found in raw output")

        extracted_total = sum(scores.values())
        if extracted_total > 0:
            logger.info(f"  Regex extraction recovered {extracted_total}/500")

        # ── Layer 6: Last resort — regex for "score": N ──────
        if extracted_total == 0:
            match = re.search(r'["\']?score["\']?\s*[:：]\s*(\d+(?:\.\d+)?)', raw, re.IGNORECASE)
            if match:
                raw_score = float(match.group(1))
                per_dim = max(0, min(100, raw_score if raw_score > 10 else raw_score * 10))
                logger.info(f"  Last-resort score extraction: {raw_score} → {per_dim:.1f}/dim")
                return {d: per_dim for d in DIMENSIONS}

        return scores

    @staticmethod
    def _compute_summary(question_results: list[dict]) -> dict:
        """Compute aggregated summary with quality/infra separation, W/T/L, and validity gate.

        Quality metrics (ensemble_average_total, delta, W/T/L) are computed
        ONLY over QUALITY_VALID questions — infra/parse failures never pollute
        the quality averages.
        """
        total_questions = len(question_results)

        # ── Classify questions by status ──
        quality_valid: list[dict] = []
        infra_failed_count = 0
        parse_failed_count = 0

        for qr in question_results:
            status = qr.get("question_status")
            if status == "QUALITY_VALID":
                quality_valid.append(qr)
            elif status in ("INFRA_FAILED", "API_ERROR"):
                infra_failed_count += 1
            elif status in ("PARSE_FAILED", "PARSE_ERROR"):
                parse_failed_count += 1
            elif qr.get("error"):
                # Exception-level failures from run()
                infra_failed_count += 1
            else:
                # Legacy format (no question_status) — infer from scores
                ens = qr.get("ensemble_scores", {})
                if ens and sum(ens.get(d, 0) for d in DIMENSIONS) > 0:
                    quality_valid.append(qr)
                else:
                    infra_failed_count += 1

        n = len(quality_valid)
        coverage_rate = round(n / total_questions * 100, 1) if total_questions > 0 else 0

        if n == 0:
            return {
                "total_questions": total_questions,
                "quality_valid_questions": 0,
                "infra_failed_questions": infra_failed_count,
                "parse_failed_questions": parse_failed_count,
                "coverage_rate": coverage_rate,
                "wins": 0, "ties": 0, "losses": 0,
                "run_valid": False,
                "run_validity_reason": "No quality-valid questions",
                "verdict": "INVALID_RUN",
            }

        # ── W / L （v4.1: 平局并入胜利） ──
        # 核心目标：聚合 >= 最佳单模型即为成功，不需要必须超越
        wins = 0
        ties = 0   # 保留字段兼容旧数据，但新逻辑下 winner=="ensemble" 包含了旧的 tie
        losses = 0
        for qr in quality_valid:
            if "winner" in qr:
                w = qr["winner"]
                if w == "ensemble":
                    wins += 1
                elif w == "tie":  # 历史数据兼容：旧版本平局也算胜
                    wins += 1
                    ties += 1  # 单独记录供分析
                elif w not in ("not_comparable", None):
                    losses += 1
            else:
                # Legacy: compute inline from scores
                et = sum(qr.get("ensemble_scores", {}).get(d, 0) for d in DIMENSIONS)
                ind_totals = [
                    sum(s.get(d, 0) for d in DIMENSIONS)
                    for s in qr.get("individual_scores", {}).values()
                ]
                bi = max((t for t in ind_totals if t > 0), default=0)
                m = et - bi
                if m >= 0:
                    wins += 1
                else:
                    losses += 1

        # ── Ensemble averages (quality_valid only) ──
        ens_totals = []
        ens_dim_sums = {d: 0.0 for d in DIMENSIONS}
        ens_latencies = []
        for qr in quality_valid:
            scores = qr.get("ensemble_scores", {})
            total = sum(scores.get(d, 0) for d in DIMENSIONS)
            ens_totals.append(total)
            for d in DIMENSIONS:
                ens_dim_sums[d] += scores.get(d, 0)
            if qr.get("ensemble_latency_ms"):
                ens_latencies.append(qr["ensemble_latency_ms"])

        ensemble_avg = sum(ens_totals) / n
        ensemble_dim_avg = {d: ens_dim_sums[d] / n for d in DIMENSIONS}

        # ── Individual averages (quality_valid, skip 0-score entries) ──
        all_model_ids: set[str] = set()
        for qr in quality_valid:
            all_model_ids.update(qr.get("individual_scores", {}).keys())

        individual_avgs: dict[str, dict] = {}
        for mid in sorted(all_model_ids):
            totals = []
            dim_sums = {d: 0.0 for d in DIMENSIONS}
            count = 0
            for qr in quality_valid:
                scores = qr.get("individual_scores", {}).get(mid, {})
                if scores:
                    total = sum(scores.get(d, 0) for d in DIMENSIONS)
                    if total > 0:  # skip eval-failed 0-score entries
                        totals.append(total)
                        for d in DIMENSIONS:
                            dim_sums[d] += scores.get(d, 0)
                        count += 1
            if count > 0:
                individual_avgs[mid] = {
                    "average_total": sum(totals) / count,
                    "dimensions": {d: dim_sums[d] / count for d in DIMENSIONS},
                    "question_count": count,
                }

        # Best individual
        best_ind_avg = max(
            (v["average_total"] for v in individual_avgs.values()),
            default=0,
        )

        # Delta: ensemble vs best individual
        delta = ensemble_avg - best_ind_avg

        # ── Per-category W/T/L ──
        category_stats: dict[str, dict] = {}
        for qr in quality_valid:
            cat = qr.get("category", "unknown")
            if cat not in category_stats:
                category_stats[cat] = {"wins": 0, "ties": 0, "losses": 0, "count": 0,
                                        "ens_total": 0.0, "best_total": 0.0,
                                        "gate_results": {}}
            cs = category_stats[cat]
            cs["count"] += 1
            w = qr.get("winner")
            if w == "ensemble":
                cs["wins"] += 1
            elif w == "tie":
                cs["ties"] += 1
            elif w not in ("not_comparable", None):
                cs["losses"] += 1
            cs["ens_total"] += qr.get("ensemble_total", 0)
            cs["best_total"] += qr.get("best_single_total", 0)
            # Accumulate gate_result counts
            gate = qr.get("gate_result", "unknown")
            cs["gate_results"][gate] = cs["gate_results"].get(gate, 0) + 1

        category_summary: dict[str, dict] = {}
        for cat, cs in sorted(category_stats.items()):
            cnt = cs["count"]
            avg_ens = round(cs["ens_total"] / cnt, 1) if cnt else 0
            avg_best = round(cs["best_total"] / cnt, 1) if cnt else 0
            category_summary[cat] = {
                "wins": cs["wins"],
                "ties": cs["ties"],
                "losses": cs["losses"],
                "count": cnt,
                "avg_ensemble": avg_ens,
                "avg_best_single": avg_best,
                "avg_delta": round(avg_ens - avg_best, 1),
                "gate_results": cs["gate_results"],
            }

        # ── Validity gate ──
        run_valid = coverage_rate >= 80
        # v4.1: 胜率 = wins / n（wins 包含平局）
        win_rate = round(wins / n * 100, 1) if n > 0 else 0
        if not run_valid:
            run_validity_reason = f"Coverage {coverage_rate}% < 80% threshold"
            verdict = "INVALID_RUN"
        else:
            # v4.1 verdict 逻辑：以胜率为主要指标
            # 目标：大多数情况下聚合不差于最强单模型（胜率 >= 60%）
            # 旧 delta 阈值保留供参考，但不再是主要 verdict 依据
            threshold = 500 * 0.02  # 10 points = 2%
            if win_rate >= 70:
                verdict = "ENSEMBLE_DOMINANT"   # 聚合在大多数题目不差于最强单模型
            elif win_rate >= 50:
                verdict = "ENSEMBLE_ACCEPTABLE" # 超过半数题目不差
            elif delta > -threshold:
                verdict = "COMPARABLE"           # 平均分接近，但胜率不足
            else:
                verdict = "INDIVIDUAL_BETTER"    # 聚合明显差于单模型
            run_validity_reason = "OK"

        # v4.7 (Change 3): gate_result split stats
        # Separates "routing correctly chose best_single" from "ensemble truly lost"
        synthesized_qs = [qr for qr in quality_valid if qr.get("gate_result") == "synthesized"]
        best_single_qs = [qr for qr in quality_valid if qr.get("gate_result") == "best_single"]
        synthesized_wins = sum(1 for qr in synthesized_qs if qr.get("winner") == "ensemble")
        best_single_wins = sum(1 for qr in best_single_qs if qr.get("winner") == "ensemble")
        routing_correct = sum(
            1 for qr in best_single_qs
            if qr.get("winner") != "ensemble"  # system routed to best_single and it was indeed better
        )
        synthesized_win_rate = round(synthesized_wins / len(synthesized_qs) * 100, 1) if synthesized_qs else None
        routing_accuracy = round(routing_correct / len(best_single_qs) * 100, 1) if best_single_qs else None

        # v4.7 (Change 4): quality_floor observation (no verdict impact, monitoring only)
        floor_breaches = []
        for qr in quality_valid:
            qfloor = qr.get("quality_floor")
            ens_total_qr = qr.get("ensemble_total", 0)
            if qfloor and ens_total_qr > 0 and ens_total_qr < qfloor * 5:
                floor_breaches.append({
                    "id": qr.get("id", "?"),
                    "ensemble_total": ens_total_qr,
                    "floor_threshold": qfloor * 5,
                    "deficit": round(qfloor * 5 - ens_total_qr, 1),
                })

        return {
            "total_questions": total_questions,
            "quality_valid_questions": n,
            "infra_failed_questions": infra_failed_count,
            "parse_failed_questions": parse_failed_count,
            "coverage_rate": coverage_rate,
            "wins": wins,
            "ties": ties,
            "losses": losses,
            "run_valid": run_valid,
            "run_validity_reason": run_validity_reason,
            "ensemble_average_total": round(ensemble_avg, 2),
            "ensemble_dimension_averages": {
                d: round(v, 2) for d, v in ensemble_dim_avg.items()
            },
            "average_ensemble_latency_ms": (
                round(sum(ens_latencies) / len(ens_latencies))
                if ens_latencies else None
            ),
            "individual_averages": {
                mid: {
                    "average_total": round(v["average_total"], 2),
                    "dimensions": {d: round(s, 2) for d, s in v["dimensions"].items()},
                }
                for mid, v in individual_avgs.items()
            },
            "best_individual_average": round(best_ind_avg, 2),
            "ensemble_vs_best_individual_delta": round(delta, 2),
            "win_rate_pct": win_rate,
            # v4.7 (Change 3): gate_result split metrics
            "synthesized_win_rate_pct": synthesized_win_rate,
            "synthesized_questions_count": len(synthesized_qs),
            "best_single_questions_count": len(best_single_qs),
            "routing_accuracy_pct": routing_accuracy,
            # v4.7 (Change 4): quality_floor breach monitoring
            "floor_breach_count": len(floor_breaches),
            "floor_breach_questions": floor_breaches,
            "verdict": verdict,
            "evaluator_bias_note": (
                "v4.7: cross-family evaluation applied + gate_result split metrics added. "
                "synthesized_win_rate_pct is primary quality signal for ensemble value. "
                "win_rate_pct includes best_single routing decisions (expected low). "
                "floor_breach_count monitors absolute quality floor (observation only)."
            ),
            "category_breakdown": category_summary,
            "total_cost_usd": round(sum(
                qr.get("ensemble_cost_usd", 0.0) for qr in question_results
            ), 6),
            "total_cost_usd_accurate": round(sum(
                qr.get("total_cost_usd_accurate", 0.0) for qr in question_results
            ), 6),
        }


# ═══════════════════════════════════════════════
# v4.7: Inline pairwise helper for smoke runs
# ═══════════════════════════════════════════════

async def _run_inline_pairwise(
    question_results: list[dict],
    adapter: "OpenAIModelAdapter",
    evaluator_model_id: str,
) -> dict:
    """Run pairwise A/B comparison on completed smoke results.

    For each QUALITY_VALID question: ensemble vs best_single, 3 rounds with
    position swap, majority vote. Returns win_rate_pct + per-question detail.
    Cost: ~$0.003/question x 3 rounds = ~$0.03 for 10 questions.
    """
    valid_qs = [
        qr for qr in question_results
        if qr.get("question_status") == "QUALITY_VALID"
        and qr.get("ensemble_answer")
        and qr.get("individual_answers")
    ]
    if not valid_qs:
        return {"win_rate_pct": None, "ensemble_wins": 0, "total": 0, "detail": []}

    ensemble_wins = 0
    detail = []

    for qr in valid_qs:
        q_id = qr.get("id", "?")
        question_text = qr.get("question", "")
        ensemble_ans = qr.get("ensemble_answer", "")
        ind_scores = qr.get("individual_scores", {})
        ind_answers = qr.get("individual_answers", {})
        valid_singles = {
            mid: sum(ind_scores.get(mid, {}).get(d, 0) for d in DIMENSIONS)
            for mid in ind_answers
            if ind_scores.get(mid) and sum(ind_scores.get(mid, {}).get(d, 0) for d in DIMENSIONS) > 0
        }
        if not valid_singles or not ensemble_ans:
            continue
        best_model = max(valid_singles, key=valid_singles.get)
        best_ans = ind_answers[best_model].get("answer", "") if isinstance(ind_answers[best_model], dict) else ""
        if not best_ans:
            continue

        tasks = [
            _pairwise_judge_one(adapter, evaluator_model_id, question_text,
                                ensemble_ans, best_ans, f"ipw-{q_id}-r1"),
            _pairwise_judge_one(adapter, evaluator_model_id, question_text,
                                best_ans, ensemble_ans, f"ipw-{q_id}-r2"),
            _pairwise_judge_one(adapter, evaluator_model_id, question_text,
                                ensemble_ans, best_ans, f"ipw-{q_id}-r3"),
        ]
        verdicts_raw = await asyncio.gather(*tasks)
        ens_votes = (
            (1 if verdicts_raw[0] == "A" else 0) +
            (1 if verdicts_raw[1] == "B" else 0) +  # swapped: B = ensemble
            (1 if verdicts_raw[2] == "A" else 0)
        )
        ens_wins_q = ens_votes >= 2
        if ens_wins_q:
            ensemble_wins += 1
        detail.append({
            "id": q_id,
            "pairwise_winner": "ensemble" if ens_wins_q else best_model,
            "ensemble_votes": ens_votes,
            "gate_result": qr.get("gate_result", "unknown"),
        })

    total = len(detail)
    win_rate = round(ensemble_wins / total * 100, 1) if total > 0 else None
    return {"win_rate_pct": win_rate, "ensemble_wins": ensemble_wins, "total": total, "detail": detail}


# ═══════════════════════════════════════════════
# Write full answers to text file
# ═══════════════════════════════════════════════

def _write_answers_txt(results: dict, path: Path) -> None:
    """将每题的完整问题、聚合答案与各模型答案写入文本文件。"""
    lines = []
    meta = results.get("meta", {})
    lines.append("=" * 72)
    lines.append("Synthora 质量基准 — 完整答案")
    lines.append(f"时间: {meta.get('timestamp', '')}  模式: {meta.get('mode', '')}")
    lines.append(f"贡献者: {meta.get('contributors', [])}")
    lines.append("=" * 72)
    lines.append("")

    for q in results.get("questions", []):
        if q.get("error"):
            lines.append(f"[{q.get('id', '?')}] 失败: {q['error']}")
            lines.append("")
            continue
        q_id = q.get("id", "?")
        question = q.get("question", "")
        lines.append("─" * 72)
        lines.append(f"【问题】[{q_id}] {question}")
        lines.append("─" * 72)

        ens = q.get("ensemble_answer", "")
        ens_scores = q.get("ensemble_scores", {})
        ens_total = sum(ens_scores.get(d, 0) for d in DIMENSIONS)
        lines.append(f"\n>>> 聚合答案 (总分 {ens_total}/500, {q.get('ensemble_latency_ms', 0)}ms)")
        lines.append(ens)
        lines.append("")

        for model_id, data in q.get("individual_answers", {}).items():
            scores = q.get("individual_scores", {}).get(model_id, {})
            total = sum(scores.get(d, 0) for d in DIMENSIONS)
            lines.append(f">>> {model_id} (总分 {total}/500, {data.get('latency_ms', 0)}ms)")
            lines.append(data.get("answer", ""))
            lines.append("")

        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ═══════════════════════════════════════════════
# Pairwise Re-evaluation Mode (P0 fix: lower variance)
# ═══════════════════════════════════════════════

PAIRWISE_SYSTEM_PROMPT = """你是一个严格的回答质量评判者。
以下是同一问题的两个回答，判断哪个对用户更有价值。
只输出 'A' 或 'B'，不要任何其他文字。
如果两个回答质量完全相同，输出 'A'。"""


async def _pairwise_judge_one(
    adapter,
    evaluator_model_id: str,
    question: str,
    answer_a: str,
    answer_b: str,
    call_id: str,
) -> str:
    """Ask evaluator to pick A or B. Returns 'A', 'B', or 'ERROR'."""
    user_msg = (
        f"问题：{question[:400]}\n\n"
        f"回答A：\n{answer_a[:3000]}\n\n"
        f"回答B：\n{answer_b[:3000]}"
    )
    rc = RoleCall(
        call_id=call_id,
        model_id=evaluator_model_id,
        role=Role.JUDGE,
        system_prompt=PAIRWISE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
        timeout_seconds=30,
    )
    resp = await adapter.call(rc)
    if resp.success and resp.content:
        v = resp.content.strip().upper()[:1]
        return v if v in ("A", "B") else "ERROR"
    return "ERROR"


async def pairwise_reeval(benchmark_path: str, evaluator_model_id: str = "claude_sonnet") -> None:
    """Re-evaluate an existing benchmark JSON using pairwise A/B comparison.

    For each question: compare ensemble_answer vs best_single_answer.
    Repeat 3 times with A/B order swapped to reduce position bias.
    Report win rate = fraction of questions where ensemble wins majority vote.

    Does NOT re-run any contributor or judge models — only calls evaluator.
    Cost: ~$0.003/question × 3 calls = ~$0.10 for 11 questions.
    """
    config = load_config()
    adapter = OpenAIModelAdapter(config)

    with open(benchmark_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    questions = [
        q for q in data.get("questions", [])
        if not q.get("error")
        and q.get("ensemble_answer")
        and q.get("individual_answers")
    ]

    if not questions:
        print("  没有可评估的题目。")
        return

    print("=" * 60)
    print(f"  Pairwise 重评估")
    print(f"  文件: {benchmark_path}")
    print(f"  评估模型: {evaluator_model_id}")
    print(f"  题目数: {len(questions)}")
    print(f"  每题 3 次 A/B 对比（含顺序互换），消除位置偏差")
    print("=" * 60)

    ensemble_wins = 0
    results_detail = []

    for q in questions:
        q_id = q.get("id", "?")
        question_text = q.get("question", "")
        ensemble_ans = q.get("ensemble_answer", "")

        # Find best single model answer
        ind_scores = q.get("individual_scores", {})
        ind_answers = q.get("individual_answers", {})
        valid = {
            mid: sum(ind_scores.get(mid, {}).get(d, 0) for d in DIMENSIONS)
            for mid in ind_answers
            if ind_scores.get(mid)
        }
        if not valid:
            print(f"  [{q_id}] 跳过（无有效单模型答案）")
            continue
        best_model = max(valid, key=valid.get)
        best_ans = ind_answers[best_model].get("answer", "")

        if not ensemble_ans or not best_ans:
            print(f"  [{q_id}] 跳过（答案为空）")
            continue

        # 3 rounds: AB, BA, AB — majority vote
        tasks = [
            _pairwise_judge_one(adapter, evaluator_model_id, question_text,
                                ensemble_ans, best_ans, f"pw-{q_id}-r1"),
            _pairwise_judge_one(adapter, evaluator_model_id, question_text,
                                best_ans, ensemble_ans, f"pw-{q_id}-r2"),  # swapped
            _pairwise_judge_one(adapter, evaluator_model_id, question_text,
                                ensemble_ans, best_ans, f"pw-{q_id}-r3"),
        ]
        verdicts_raw = await asyncio.gather(*tasks)

        # Normalize: r2 is swapped, so 'A' means best_single won
        # r1,r3: A=ensemble wins; r2: B=ensemble wins
        ens_votes = (
            (1 if verdicts_raw[0] == "A" else 0) +
            (1 if verdicts_raw[1] == "B" else 0) +  # swapped
            (1 if verdicts_raw[2] == "A" else 0)
        )
        ens_win = ens_votes >= 2  # majority
        if ens_win:
            ensemble_wins += 1

        old_margin = q.get("margin_vs_best_single", 0) or 0
        symbol = "✅" if ens_win else "❌"
        print(f"  [{q_id}] {symbol} ensemble={'WIN' if ens_win else 'LOSE'} "
              f"votes={ens_votes}/3 verdicts={verdicts_raw} "
              f"best={best_model} old_margin={old_margin:+.0f}")
        results_detail.append({
            "id": q_id,
            "ensemble_win": ens_win,
            "votes": ens_votes,
            "verdicts_raw": list(verdicts_raw),
            "best_model": best_model,
            "old_margin": old_margin,
        })

    total = len(results_detail)
    if total == 0:
        print("  无有效结果。")
        return

    win_rate = ensemble_wins / total * 100
    old_rr = sum(1 for r in results_detail if r["old_margin"] < 0) / total * 100
    new_rr = (total - ensemble_wins) / total * 100

    print()
    print("=" * 60)
    print(f"  Pairwise 结果汇总 ({total} 题)")
    print(f"  聚合胜率:          {ensemble_wins}/{total} = {win_rate:.1f}%")
    print(f"  route_regret (pairwise): {total - ensemble_wins}/{total} = {new_rr:.1f}%")
    print(f"  route_regret (旧打分法): {new_rr:.0f}% → 旧={old_rr:.1f}%")
    print(f"  评估模型: {evaluator_model_id}")
    print("=" * 60)
    print()
    print("  解读：pairwise 方差比绝对打分低 3-5x，结果更可信。")
    print("  建议：对同一文件跑 2 次 pairwise，若两次胜率差 <5% 则结果稳定。")
    print()


# ═══════════════════════════════════════════════
# Compare Mode
# ═══════════════════════════════════════════════

def compare_results(baseline_path: str, candidate_path: str) -> None:
    """Compare two benchmark result files and print regression analysis."""
    with open(baseline_path, "r", encoding="utf-8") as f:
        baseline = json.load(f)
    with open(candidate_path, "r", encoding="utf-8") as f:
        candidate = json.load(f)

    # B2: Validate traceability fields
    for label, data, path in [
        ("baseline", baseline, baseline_path),
        ("candidate", candidate, candidate_path),
    ]:
        meta = data.get("meta", {})
        if not meta.get("commit_sha") or meta.get("commit_sha") == "unknown":
            print(f"  ⚠️  WARNING: {label} ({path}) missing valid commit_sha — results not reproducible")
        if not meta.get("evaluator_prompt_hash"):
            print(f"  ⚠️  WARNING: {label} ({path}) missing evaluator_prompt_hash — prompt drift undetectable")

    b_summary = baseline.get("summary", {})
    c_summary = candidate.get("summary", {})

    print("=" * 70)
    print("  质量基准对比报告")
    print("=" * 70)
    print(f"  基线: {baseline_path}")
    print(f"    时间: {baseline['meta']['timestamp']}")
    print(f"    模式: {baseline['meta']['mode']}")
    print(f"  候选: {candidate_path}")
    print(f"    时间: {candidate['meta']['timestamp']}")
    print(f"    模式: {candidate['meta']['mode']}")
    print()

    # Overall comparison
    b_ens = b_summary.get("ensemble_average_total", 0)
    c_ens = c_summary.get("ensemble_average_total", 0)
    delta = c_ens - b_ens
    direction = "↑ 提升" if delta > 0 else "↓ 下降" if delta < 0 else "= 持平"

    print(f"  聚合总分:  基线 {b_ens:.1f}  →  候选 {c_ens:.1f}  ({direction} {abs(delta):.1f})")
    print()

    # Dimension comparison
    print("  各维度对比:")
    b_dims = b_summary.get("ensemble_dimension_averages", {})
    c_dims = c_summary.get("ensemble_dimension_averages", {})
    for d in DIMENSIONS:
        b_val = b_dims.get(d, 0)
        c_val = c_dims.get(d, 0)
        dd = c_val - b_val
        arrow = "↑" if dd > 0 else "↓" if dd < 0 else "="
        print(f"    {d:<15} {b_val:.1f} → {c_val:.1f}  {arrow} {abs(dd):.1f}")
    print()

    # Per-question comparison
    print("  各题目对比:")
    b_questions = {q["id"]: q for q in baseline.get("questions", [])}
    c_questions = {q["id"]: q for q in candidate.get("questions", [])}

    for q_id in b_questions:
        if q_id not in c_questions:
            continue
        bq = b_questions[q_id]
        cq = c_questions[q_id]
        b_total = sum(bq.get("ensemble_scores", {}).get(d, 0) for d in DIMENSIONS)
        c_total = sum(cq.get("ensemble_scores", {}).get(d, 0) for d in DIMENSIONS)
        dd = c_total - b_total
        arrow = "↑" if dd > 0 else "↓" if dd < 0 else "="
        print(f"    {q_id:<20} {b_total:.0f} → {c_total:.0f}  {arrow} {abs(dd):.1f}")
    print()

    # Model changes
    b_models = baseline["meta"].get("config_snapshot", {})
    c_models = candidate["meta"].get("config_snapshot", {})
    changes = []
    for mid in set(list(b_models.keys()) + list(c_models.keys())):
        b_name = b_models.get(mid, "(未配置)")
        c_name = c_models.get(mid, "(未配置)")
        if b_name != c_name:
            changes.append(f"    {mid}: {b_name} → {c_name}")
    if changes:
        print("  模型变更:")
        for c in changes:
            print(c)
    else:
        print("  模型变更: 无")
    print()

    # Verdict — threshold aligned with _compute_summary: ±2% of 500 = ±10 points
    # (old hardcoded ±2 was only ±0.4% on the 0-500 scale, far too sensitive)
    _cmp_threshold = 500 * 0.02  # 10 points
    if delta > _cmp_threshold:
        print("  结论: ✅ 候选配置显著优于基线，建议采纳。")
    elif delta > 0:
        print("  结论: ✅ 候选配置略优于基线，可以采纳。")
    elif delta > -_cmp_threshold:
        print("  结论: ⚠️ 候选配置与基线基本持平，差异不显著。")
    else:
        print("  结论: ❌ 候选配置劣于基线，建议回滚。")
    print()


# ═══════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════

async def main():
    parser = argparse.ArgumentParser(
        description="Synthora 质量评估基准",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 scripts/benchmark_quality.py --mode light
  python3 scripts/benchmark_quality.py --mode deep
  python3 scripts/benchmark_quality.py --mode deep -c 2          # 最多 2 题并发 (省 API)
  python3 scripts/benchmark_quality.py --mode deep -c 0          # 全部题目同时并发
  python3 scripts/benchmark_quality.py --mode deep -c 1          # 串行 (最安全)
  python3 scripts/benchmark_quality.py --mode deep --resume data/benchmark/deep_xxx.json  # 续跑未完成
  python3 scripts/benchmark_quality.py --compare baseline.json candidate.json
  python3 scripts/benchmark_quality.py --mode light --questions fact_01,reasoning_01
        """,
    )
    parser.add_argument(
        "--mode", choices=["light", "deep", "research"],
        default="light", help="要评测的模式 (default: light)",
    )
    parser.add_argument(
        "--compare", nargs=2, metavar=("BASELINE", "CANDIDATE"),
        help="对比两份基线文件",
    )
    parser.add_argument(
        "--output", "-o",
        help="输出文件路径 (默认: data/benchmark/<mode>_<timestamp>.json)",
    )
    parser.add_argument(
        "--questions", "-q",
        help="只跑指定题目 (逗号分隔的 ID 列表, 例如: fact_01,reasoning_01)",
    )
    parser.add_argument(
        "--concurrency", "-c", type=int, default=3,
        help="最多同时跑几道题 (default: 3, 设 1=串行, 设 0=全部同时)",
    )
    parser.add_argument(
        "--show-answers", "-a", action="store_true",
        help="每题完成后在终端打印完整聚合与各模型答案，并另存为 _answers.txt 便于阅读",
    )
    parser.add_argument(
        "--resume", "-r",
        help="从已有结果文件续跑：跳过已完成的题目，只跑未完成的，结果合并回该文件",
    )
    parser.add_argument(
        "--evaluator", "-e",
        help="评估模型 ID，用于对答案打分（默认: gemini_3_flash，快速/格式好/与 Judge&Contributor 独立）",
    )
    parser.add_argument(
        "--lite", "-l", action="store_true",
        help="轻量模式: Judge→sonnet_thinking, 移除opus, 跳过精炼, 默认3题. 0次opus调用, 省钱省时间",
    )
    parser.add_argument(
        "--stop-on-error", "-s", action="store_true",
        help="遇到任何错误(异常/超时)立即停止，保存进度后用 --resume 继续",
    )
    parser.add_argument(
        "--question-timeout", "-t", type=int, default=0,
        help="每题最大耗时(秒)，超时视为错误 (0=不限制，推荐 deep: 300， research: 600)",
    )
    parser.add_argument(
        "--no-router", action="store_true",
        help="禁用 Router 的 question_type 分类，强制 question_type=unknown（兼容旧基线对比）。"
             "默认开启 Router（v2.4.3 起）。",
    )
    parser.add_argument(
        "--smoke", action="store_true",
        help="烟雾测试模式: 6题最小信息集，覆盖全部关键路径（搜索/幻觉/策略/门控）。"
             "预算 <=$0.50，耗时 <=10min。requires_search 题自动开启 Tavily。",
    )
    parser.add_argument(
        "--content-critique", action="store_true",
        help="启用自动内容质检 (Layer ③): benchmark 完成评分后，自动用外部模型审查每道答案的事实性硬伤。"
             "审查模型: deepseek_reasoner (非Anthropic/Google/GPT)。HIGH 发现自动交叉验证 (gpt53_codex)。"
             "额外成本: ~$0.06/36题。",
    )
    parser.add_argument(
        "--pairwise-reeval", metavar="BENCHMARK_JSON",
        help="对已有 benchmark JSON 做 pairwise 重评估（不重跑模型）。"
             "每题 3 次 A/B 对比，输出聚合胜率。用法: --pairwise-reeval data/benchmark/deep_xxx.json",
    )
    parser.add_argument(
        "--pairwise-evaluator", default="claude_sonnet",
        help="pairwise 重评估使用的模型 (default: claude_sonnet)",
    )

    args = parser.parse_args()

    # Pairwise re-evaluation mode
    if args.pairwise_reeval:
        await pairwise_reeval(args.pairwise_reeval, args.pairwise_evaluator)
        return

    # Compare mode
    if args.compare:
        compare_results(args.compare[0], args.compare[1])
        return

    # Benchmark mode
    conc = args.concurrency if args.concurrency > 0 else 999
    benchmark = QualityBenchmark(
        mode=args.mode,
        concurrency=conc,
        show_answers=args.show_answers,
        evaluator_model_id=args.evaluator,
        lite=getattr(args, 'lite', False),
        stop_on_error=getattr(args, 'stop_on_error', False),
        question_timeout=getattr(args, 'question_timeout', 0),
        use_router=not getattr(args, 'no_router', False),
        content_critique=getattr(args, 'content_critique', False),
    )

    # Smoke mode: fixed core + rotating slots (mode-aware, seeded by run_id for reproducibility)
    if getattr(args, 'smoke', False) and not args.questions:
        import random as _random
        _run_seed = hash(datetime.now().strftime("%Y%m%d%H")) & 0xFFFFFF  # changes hourly
        if benchmark.mode == "research":
            _core_ids = list(QualityBenchmark.RESEARCH_SMOKE_QUESTIONS)
            _pool = [qid for qid in QualityBenchmark.RESEARCH_SMOKE_ROTATION_POOL if qid not in _core_ids]
            _slots = QualityBenchmark.RESEARCH_SMOKE_ROTATION_SLOTS
        else:
            _core_ids = list(QualityBenchmark.SMOKE_QUESTIONS)
            _pool = [qid for qid in QualityBenchmark.SMOKE_ROTATION_POOL if qid not in _core_ids]
            _slots = QualityBenchmark.SMOKE_ROTATION_SLOTS
        # Sample rotation slots deterministically (reproducible within same hour)
        _rng = _random.Random(_run_seed)
        _rotation_ids = _rng.sample(_pool, min(_slots, len(_pool)))
        smoke_ids = _core_ids + _rotation_ids
        benchmark.questions = [
            q for q in benchmark.all_questions if q["id"] in smoke_ids
            and benchmark.mode in q.get("expected_modes", [benchmark.mode])
        ]
        if not benchmark.questions:
            print(f"警告: smoke 默认题目不在当前模式中，回退到全部题目")
            benchmark.questions = [
                q for q in benchmark.all_questions
                if benchmark.mode in q.get("expected_modes", [benchmark.mode])
            ]
        # Attach question_set metadata for traceability (变更5)
        benchmark._smoke_question_set_id = f"smoke_{benchmark.mode}_{_run_seed:06x}"
        benchmark._smoke_core_ids = _core_ids
        benchmark._smoke_rotation_ids = _rotation_ids
        print(f"  🚨 SMOKE MODE — {len(benchmark.questions)} 题快速回归 (core={len(_core_ids)}, rotation={_rotation_ids})")
        print(f"  📋 question_set_id: {benchmark._smoke_question_set_id}")

    # Lite mode: default to 3 representative questions unless --questions overrides
    if args.lite and not args.questions:
        lite_ids = QualityBenchmark.LITE_QUESTIONS
        benchmark.questions = [
            q for q in benchmark.questions if q["id"] in lite_ids
        ]
        if not benchmark.questions:
            print(f"警告: lite 默认题目不在当前模式中，回退到全部题目")
            benchmark.questions = [
                q for q in benchmark.all_questions
                if benchmark.mode in q.get("expected_modes", [benchmark.mode])
            ]

    # Filter specific questions if requested
    if args.questions:
        q_ids = [q.strip() for q in args.questions.split(",")]
        benchmark.questions = [
            q for q in benchmark.questions if q["id"] in q_ids
        ]
        if not benchmark.questions:
            print(f"错误: 没有匹配的题目。可用 ID: {[q['id'] for q in benchmark.all_questions]}")
            return

    # Resume: skip completed, run only remaining, merge back
    resume_path = None
    if args.resume:
        resume_path = Path(args.resume)
        if not resume_path.exists():
            print(f"错误: --resume 文件不存在: {resume_path}")
            return
        with open(resume_path, "r", encoding="utf-8") as f:
            existing = json.load(f)
        # v2.9: Safety guard — refuse to resume an iron-law-aborted run.
        # ABORTED_IRON_LAW means the data was explicitly flagged as invalid;
        # resuming would mix clean and polluted questions into the same file.
        _existing_summary = existing.get("summary", {})
        if _existing_summary.get("verdict") == "ABORTED_IRON_LAW":
            _stop_reason = _existing_summary.get("stop_reason", "(no reason recorded)")
            print(f"""\n  ⛔ 拒绝续跑: 该文件因铁律触发而中止，数据已污染。
  停止原因: {_stop_reason.splitlines()[0]}
  请重新运行（不要 --resume），或用 --questions 指定干净的题目子集。
  如果你确认要强制续跑，请手动将文件中的 verdict 字段改为空字符串后再执行。""")
            return
        completed_ids = set()
        completed_latencies = []
        for q in existing.get("questions", []):
            if q.get("error"):
                continue
            es = q.get("ensemble_scores", {})
            if es and sum(es.get(d, 0) for d in DIMENSIONS) > 0:
                completed_ids.add(q["id"])
                if q.get("ensemble_latency_ms"):
                    completed_latencies.append(q["ensemble_latency_ms"])
        benchmark.questions = [q for q in benchmark.questions if q["id"] not in completed_ids]
        if not benchmark.questions:
            print(f"  [--resume] 所有题目均已在 {resume_path} 中完成，无需续跑。")
            return
        avg_latency_ms = sum(completed_latencies) / len(completed_latencies) if completed_latencies else 120_000
        effective_conc = min(conc, len(benchmark.questions))
        estimated_sec = (len(benchmark.questions) / effective_conc) * (avg_latency_ms / 1000)
        print(f"  [--resume] 已完成 {len(completed_ids)} 题，剩余 {len(benchmark.questions)} 题")
        print(f"  预计剩余时间约 {int(estimated_sec // 60)} 分 {int(estimated_sec % 60)} 秒（基于历史耗时 {int(avg_latency_ms/1000)}s/题）")
        print()

    print("=" * 60)
    if args.lite:
        print(f"  ⚡ LITE MODE — 快速迭代测试 (0 opus 调用)")
    print(f"  Synthora 质量评估基准")
    print(f"  模式: {args.mode}")
    print(f"  题目数: {len(benchmark.questions)}")
    print(f"  并发: 最多 {benchmark.concurrency} 题同时")
    print(f"  评估模型: {benchmark.evaluator_model_id}")
    print(f"  贡献者: {benchmark.mode_config.contributors}")
    print(f"  评判者: {benchmark.mode_config.judge}")
    if args.lite:
        print(f"  精炼: 已禁用 (省成本)")
    if args.stop_on_error:
        print(f"  错误策略: 遇错立停停止 (--stop-on-error)")
    if args.question_timeout > 0:
        print(f"  每题超时: {args.question_timeout}秒")
    if not getattr(args, 'no_router', False):
        print(f"  Router: 启用 (question_type 影响 smart_routing)")
    if getattr(args, 'content_critique', False):
        print(f"  内容质检: 启用 (Layer ③, 审查={QualityBenchmark._CRITIQUE_MODEL}, 交叉={QualityBenchmark._CRITIQUE_CROSS_MODEL})")
    print("=" * 60)

    # Determine output path before run() so incremental save works
    output_dir = PROJECT_ROOT / "data" / "benchmark"
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.resume:
        output_path = Path(args.resume)
    elif args.output:
        output_path = Path(args.output)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = output_dir / f"{args.mode}_{ts}.json"

    # v3.9: 倒序跑题目 — 避免每次都从同一批题目开始，覆盖更多题型组合
    benchmark.questions = list(reversed(benchmark.questions))
    print(f"  题目顺序: 倒序 (共 {len(benchmark.questions)} 题)")

    results = await benchmark.run(output_path=output_path)

    # Resume 模式：合并已有结果，写回 resume 文件
    if args.resume:
        resume_path = Path(args.resume)
        with open(resume_path, "r", encoding="utf-8") as f:
            existing = json.load(f)
        existing_by_id = {q["id"]: q for q in existing.get("questions", [])}
        for q in results.get("questions", []):
            existing_by_id[q["id"]] = q
        merged_questions = list(existing_by_id.values())
        # 保持与题库一致顺序
        all_ids = [q["id"] for q in benchmark.all_questions if benchmark.mode in q.get("expected_modes", [benchmark.mode])]
        id_to_idx = {i: idx for idx, i in enumerate(all_ids)}
        merged_questions.sort(key=lambda x: id_to_idx.get(x["id"], 999))
        results = {
            "meta": {
                **existing["meta"],
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "question_count": len(merged_questions),
            },
            "questions": merged_questions,
        }
        results["summary"] = benchmark._compute_summary(merged_questions)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n{'='*60}")
        print(f"  已合并写回: {output_path}（共 {len(merged_questions)} 题）")
        print(f"{'='*60}")
    else:
        print(f"\n{'='*60}")
        print(f"  结果已保存: {output_path}")
        print(f"{'='*60}")

    # v3.9: 始终写入完整回答文件（不依赖 --show-answers 参数）
    if results.get("questions"):
        answers_path = output_path.with_stem(output_path.stem + "_answers").with_suffix(".txt")
        _write_answers_txt(results, answers_path)
        print(f"  完整答案已写入: {answers_path}")
    elif args.show_answers and results.get("questions"):
        answers_path = output_path.with_stem(output_path.stem + "_answers").with_suffix(".txt")
        _write_answers_txt(results, answers_path)
        print(f"  完整答案已写入: {answers_path}")

    # Print summary
    summary = results.get("summary", {})
    qv = summary.get("quality_valid_questions", 0)
    total_q = summary.get("total_questions", len(results.get("questions", [])))

    if qv == 0:
        print("\n  全部题目失败，无有效汇总。")
        infra = summary.get("infra_failed_questions", 0)
        parse = summary.get("parse_failed_questions", 0)
        if infra or parse:
            print(f"  基础设施失败: {infra}  解析失败: {parse}")
        print()
        return

    # ── Validity & coverage ──
    run_valid = summary.get("run_valid", True)
    coverage = summary.get("coverage_rate", 100)
    validity_mark = "VALID" if run_valid else f"INVALID ({summary.get('run_validity_reason', '')})"
    wins = summary.get("wins", 0)
    ties = summary.get("ties", 0)
    losses = summary.get("losses", 0)
    win_rate = summary.get("win_rate_pct", 0)

    print(f"\n  有效题目: {qv}/{total_q} ({coverage}%)")
    infra = summary.get("infra_failed_questions", 0)
    parse = summary.get("parse_failed_questions", 0)
    if infra or parse:
        print(f"  排除: 基础设施失败 {infra}, 解析失败 {parse}")
    # v4.1: 核心指标 — 聚合不差于最强单模型的题目占比
    print(f"  聚合胜率 (>=最强单模型): {wins}/{qv} = {win_rate}%")
    if ties > 0:
        print(f"    其中平局(=最强): {ties} 题")
    print(f"  聚合输给单模型: {losses}/{qv} 题")
    print(f"  运行有效性: {validity_mark}")

    # ── Scores ──
    avg_latency = summary.get("average_ensemble_latency_ms")
    latency_str = f"  (平均延迟: {avg_latency}ms)" if avg_latency else ""
    print(f"\n  聚合平均总分: {summary.get('ensemble_average_total', 0):.1f}/500{latency_str}")
    print(f"  最佳单模型平均: {summary.get('best_individual_average', 0):.1f}/500")
    delta = summary.get("ensemble_vs_best_individual_delta", 0)
    direction = "优于" if delta > 0 else "劣于" if delta < 0 else "等于"
    print(f"  平均分聚合 {direction} 最佳单模型: {abs(delta):.1f} 分")
    verdict = summary.get('verdict', 'N/A')
    verdict_desc = {
        "ENSEMBLE_DOMINANT":   "✅ 聚合在大多数题目不差于最强单模型 (>=70%)",
        "ENSEMBLE_ACCEPTABLE": "✅ 聚合在超过半数题目不差于最强单模型 (>=50%)",
        "COMPARABLE":          "⚠️  平均分接近，但胜率不足50%",
        "INDIVIDUAL_BETTER":   "❌ 聚合明显差于单模型",
        "INVALID_RUN":         "❌ 运行无效（覆盖率不足）",
    }.get(verdict, verdict)
    print(f"  结论: {verdict_desc}")
    # v4.2: Display accurate total cost (pipeline + individual + eval)
    total_cost_accurate = summary.get("total_cost_usd_accurate", 0.0)
    total_cost_old = summary.get("total_cost_usd", 0.0)
    print(f"  真实资金消耗: ${total_cost_accurate:.4f} USD")
    if total_cost_old > 0 and abs(total_cost_accurate - total_cost_old) > 0.01:
        print(f"    (旧估算: ${total_cost_old:.4f}, 低估 {total_cost_accurate/total_cost_old:.1f}x)" if total_cost_accurate > total_cost_old else "")

    print(f"\n  各维度聚合平均:")
    for d in DIMENSIONS:
        val = summary.get("ensemble_dimension_averages", {}).get(d, 0)
        print(f"    {d:<15} {val:.1f}/100")

    print(f"\n  各模型单独平均:")
    for mid, data in summary.get("individual_averages", {}).items():
        print(f"    {mid:<25} {data['average_total']:.1f}/500")

    # v4.23: Content critique summary (Layer ③)
    if getattr(args, 'content_critique', False):
        questions = results.get("questions", [])
        critiqued = [q for q in questions if "content_critique" in q and not q["content_critique"].get("skipped")]
        skipped = [q for q in questions if "content_critique" in q and q["content_critique"].get("skipped")]
        all_issues = []
        for q in critiqued:
            all_issues.extend(q["content_critique"].get("issues", []))
        n_high = sum(1 for i in all_issues if i.get("severity") == "HIGH")
        n_med = sum(1 for i in all_issues if i.get("severity") == "MEDIUM")
        n_confirmed = sum(1 for i in all_issues if i.get("confirmed"))
        print(f"\n  ── 内容质检 (Layer ③) ──")
        print(f"  审查: {len(critiqued)} 题 | 跳过: {len(skipped)} 题")
        print(f"  发现: {n_high} HIGH + {n_med} MEDIUM = {len(all_issues)} 条")
        if n_confirmed:
            print(f"  交叉验证确认: {n_confirmed} 条 HIGH")
        if all_issues:
            # Type distribution
            from collections import Counter
            type_counts = Counter(i.get("type", "UNKNOWN") for i in all_issues)
            for t, c in type_counts.most_common():
                print(f"    {t}: {c}")

    print()


if __name__ == "__main__":
    asyncio.run(main())
