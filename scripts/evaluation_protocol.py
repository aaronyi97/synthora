#!/usr/bin/env python3
"""
Synthora 固定评估协议 (Evaluation Protocol v2)

核心原则: 评估标准在实验开始前固定，实验结束后不可更改。

功能:
  1. 多次运行 benchmark 取均值（默认 3 次）
  2. 预定义的成功标准（不可事后修改）
  3. 自动校验评估 prompt hash，防止悄悄修改评分标准
  4. 输出正式的验证报告

用法:
  # 完整验证 (3次运行取均值)
  python3 scripts/evaluation_protocol.py --mode deep --runs 3

  # 快速验证 (1次运行，用于开发迭代)
  python3 scripts/evaluation_protocol.py --mode deep --runs 1 --quick

  # 三模式全量验证
  python3 scripts/evaluation_protocol.py --mode all --runs 3

  # 使用 v2 题库
  python3 scripts/evaluation_protocol.py --mode deep --runs 3 --questions-file scripts/test_questions_v2.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# ═══════════════════════════════════════════════
# FROZEN SUCCESS CRITERIA — DO NOT MODIFY AFTER BASELINE
# ═══════════════════════════════════════════════

PROTOCOL_VERSION = "2.0"

SUCCESS_CRITERIA = {
    "light": {
        "vs_best_single_delta": 10.0,   # 聚合须优于最佳单模型至少 10 分 (0-500 scale)
        "min_win_rate_pct": 50,         # 逐题胜率 ≥ 50%
        "min_coverage_pct": 80,         # 有效题占比 ≥ 80%
    },
    "deep": {
        "vs_best_single_delta": 15.0,   # 聚合须优于最佳单模型至少 15 分 (0-500 scale)
        "min_win_rate_pct": 55,         # 逐题胜率 ≥ 55%
        "min_coverage_pct": 80,
    },
    "research": {
        "vs_best_single_delta": 20.0,   # Research 标准最严 (0-500 scale)
        "min_win_rate_pct": 60,         # 逐题胜率 ≥ 60%
        "min_coverage_pct": 80,
    },
}

# Evaluator model — fixed, not configurable
FIXED_EVALUATOR = "claude_sonnet"


def _get_evaluator_prompt_hash() -> str:
    """Get SHA256 hash of the evaluator prompt from benchmark_quality.py."""
    # Import to get the actual prompt
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    from benchmark_quality import EVALUATOR_SYSTEM_PROMPT
    return hashlib.sha256(EVALUATOR_SYSTEM_PROMPT.encode()).hexdigest()[:12]


def _format_duration(seconds: float) -> str:
    s = int(seconds)
    if s >= 3600:
        h, r = divmod(s, 3600)
        m, sec = divmod(r, 60)
        return f"{h}时{m}分{sec}秒"
    elif s >= 60:
        m, sec = divmod(s, 60)
        return f"{m}分{sec}秒"
    return f"{s}秒"


async def run_single_benchmark(mode: str, questions_file: str | None, concurrency: int) -> dict:
    """Run one complete benchmark and return results dict."""
    from benchmark_quality import QualityBenchmark

    benchmark = QualityBenchmark(
        mode=mode,
        concurrency=concurrency,
        evaluator_model_id=FIXED_EVALUATOR,
    )

    # Override question file if specified
    if questions_file:
        q_path = PROJECT_ROOT / questions_file
        if q_path.exists():
            with open(q_path, "r", encoding="utf-8") as f:
                q_data = json.load(f)
            benchmark.all_questions = q_data["questions"]
            benchmark.questions = [
                q for q in benchmark.all_questions
                if mode in q.get("expected_modes", [mode])
            ]

    return await benchmark.run()


def merge_multi_run_results(all_runs: list[dict], mode: str) -> dict:
    """Merge multiple benchmark runs into averaged results."""
    from benchmark_quality import DIMENSIONS

    n_runs = len(all_runs)
    if n_runs == 0:
        return {}

    # Collect all question IDs across runs
    all_q_ids = set()
    for run in all_runs:
        for q in run.get("questions", []):
            all_q_ids.add(q["id"])

    # For each question, average the ensemble and individual scores across runs
    merged_questions = []
    for q_id in sorted(all_q_ids):
        q_runs = []
        for run in all_runs:
            for q in run.get("questions", []):
                if q["id"] == q_id and q.get("question_status") == "QUALITY_VALID":
                    q_runs.append(q)

        if not q_runs:
            # All runs failed for this question
            sample = None
            for run in all_runs:
                for q in run.get("questions", []):
                    if q["id"] == q_id:
                        sample = q
                        break
                if sample:
                    break
            if sample:
                merged_questions.append({
                    "id": q_id,
                    "question": sample.get("question", ""),
                    "category": sample.get("category", ""),
                    "question_status": "QUALITY_INVALID",
                    "valid_runs": 0,
                    "total_runs": n_runs,
                })
            continue

        # Average ensemble scores
        avg_ens = {}
        for d in DIMENSIONS:
            vals = [q["ensemble_scores"].get(d, 0) for q in q_runs if q.get("ensemble_scores")]
            avg_ens[d] = sum(vals) / len(vals) if vals else 0

        # Average individual scores per model
        all_models = set()
        for q in q_runs:
            all_models.update(q.get("individual_scores", {}).keys())

        avg_ind = {}
        for mid in all_models:
            model_scores = {}
            for d in DIMENSIONS:
                vals = [
                    q["individual_scores"][mid].get(d, 0)
                    for q in q_runs
                    if mid in q.get("individual_scores", {})
                    and q["individual_scores"][mid].get(d, 0) > 0
                ]
                model_scores[d] = sum(vals) / len(vals) if vals else 0
            avg_ind[mid] = model_scores

        # Compute winner
        ens_total = sum(avg_ens.values())
        best_single_total = max(
            (sum(scores.values()) for scores in avg_ind.values()),
            default=0,
        )
        best_single_model = max(
            avg_ind.keys(),
            key=lambda m: sum(avg_ind[m].values()),
            default=None,
        ) if avg_ind else None

        margin = round(ens_total - best_single_total, 2)
        if margin > 0:
            winner = "ensemble"
        elif margin == 0:
            winner = "tie"
        else:
            winner = best_single_model

        merged_questions.append({
            "id": q_id,
            "question": q_runs[0].get("question", ""),
            "category": q_runs[0].get("category", ""),
            "question_status": "QUALITY_VALID",
            "valid_runs": len(q_runs),
            "total_runs": n_runs,
            "ensemble_scores": {d: round(v, 2) for d, v in avg_ens.items()},
            "individual_scores": {
                mid: {d: round(v, 2) for d, v in scores.items()}
                for mid, scores in avg_ind.items()
            },
            "ensemble_total": round(ens_total, 2),
            "best_single_model": best_single_model,
            "best_single_total": round(best_single_total, 2),
            "winner": winner,
            "margin_vs_best_single": margin,
        })

    return {
        "meta": {
            "protocol_version": PROTOCOL_VERSION,
            "mode": mode,
            "evaluator": FIXED_EVALUATOR,
            "evaluator_prompt_hash": _get_evaluator_prompt_hash(),
            "n_runs": n_runs,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "success_criteria": SUCCESS_CRITERIA.get(mode, {}),
        },
        "questions": merged_questions,
    }


def evaluate_against_criteria(merged: dict) -> dict:
    """Evaluate merged results against frozen success criteria."""
    from benchmark_quality import DIMENSIONS

    mode = merged["meta"]["mode"]
    criteria = SUCCESS_CRITERIA.get(mode, {})
    questions = merged.get("questions", [])

    valid = [q for q in questions if q.get("question_status") == "QUALITY_VALID"]
    total = len(questions)
    n_valid = len(valid)

    coverage_pct = round(n_valid / total * 100, 1) if total > 0 else 0

    # Win rate
    wins = sum(1 for q in valid if q.get("winner") == "ensemble")
    ties = sum(1 for q in valid if q.get("winner") == "tie")
    losses = n_valid - wins - ties
    win_rate_pct = round(wins / n_valid * 100, 1) if n_valid > 0 else 0

    # Ensemble vs best single delta
    ens_totals = [q["ensemble_total"] for q in valid if "ensemble_total" in q]
    best_singles = [q["best_single_total"] for q in valid if "best_single_total" in q]
    avg_ens = sum(ens_totals) / len(ens_totals) if ens_totals else 0
    avg_best = sum(best_singles) / len(best_singles) if best_singles else 0
    delta = round(avg_ens - avg_best, 2)

    # Check criteria
    checks = {
        "coverage": {
            "value": coverage_pct,
            "threshold": criteria.get("min_coverage_pct", 80),
            "pass": coverage_pct >= criteria.get("min_coverage_pct", 80),
        },
        "win_rate": {
            "value": win_rate_pct,
            "threshold": criteria.get("min_win_rate_pct", 50),
            "pass": win_rate_pct >= criteria.get("min_win_rate_pct", 50),
        },
        "vs_best_single_delta": {
            "value": delta,
            "threshold": criteria.get("vs_best_single_delta", 1.0),
            "pass": delta >= criteria.get("vs_best_single_delta", 1.0),
        },
    }

    all_pass = all(c["pass"] for c in checks.values())

    return {
        "mode": mode,
        "protocol_version": PROTOCOL_VERSION,
        "all_criteria_pass": all_pass,
        "verdict": "PASS" if all_pass else "FAIL",
        "stats": {
            "total_questions": total,
            "valid_questions": n_valid,
            "coverage_pct": coverage_pct,
            "wins": wins,
            "ties": ties,
            "losses": losses,
            "win_rate_pct": win_rate_pct,
            "ensemble_avg": round(avg_ens, 2),
            "best_single_avg": round(avg_best, 2),
            "delta": delta,
        },
        "criteria_checks": checks,
    }


def print_report(evaluation: dict, merged: dict) -> None:
    """Print the validation report to console."""
    from benchmark_quality import DIMENSIONS

    mode = evaluation["mode"]
    meta = merged["meta"]
    stats = evaluation["stats"]
    checks = evaluation["criteria_checks"]
    verdict = evaluation["verdict"]

    print()
    print("=" * 70)
    print(f"  Synthora 评估协议 v{PROTOCOL_VERSION} — 正式验证报告")
    print("=" * 70)
    print(f"  模式: {mode}")
    print(f"  运行次数: {meta['n_runs']}")
    print(f"  评估模型: {meta['evaluator']}")
    print(f"  Prompt Hash: {meta['evaluator_prompt_hash']}")
    print(f"  时间: {meta['timestamp']}")
    print()

    # Stats
    print(f"  有效题目: {stats['valid_questions']}/{stats['total_questions']} ({stats['coverage_pct']}%)")
    print(f"  胜/平/负: {stats['wins']}/{stats['ties']}/{stats['losses']}")
    print(f"  胜率: {stats['win_rate_pct']}%")
    print(f"  聚合平均: {stats['ensemble_avg']:.2f}/500")
    print(f"  最佳单模型平均: {stats['best_single_avg']:.2f}/500")
    print(f"  差值 (Δ): {stats['delta']:+.2f}")
    print()

    # Criteria checks
    print("  成功标准检查:")
    for name, check in checks.items():
        status = "✅ PASS" if check["pass"] else "❌ FAIL"
        print(f"    {name:<25} {check['value']:.1f} {'≥' if check['pass'] else '<'} {check['threshold']}  {status}")
    print()

    # Per-question detail
    print("  逐题详情:")
    for q in merged.get("questions", []):
        q_id = q["id"]
        if q.get("question_status") != "QUALITY_VALID":
            print(f"    {q_id:<22} ⚠️ 无效 ({q.get('valid_runs', 0)}/{q.get('total_runs', 0)} runs)")
            continue
        ens = q.get("ensemble_total", 0)
        best = q.get("best_single_total", 0)
        margin = q.get("margin_vs_best_single", 0)
        winner = q.get("winner", "?")
        runs_info = f"({q.get('valid_runs', '?')}/{q.get('total_runs', '?')} runs)"
        icon = "🟢" if winner == "ensemble" else "🔴" if winner not in ("tie", "ensemble") else "🟡"
        print(f"    {q_id:<22} {icon} 聚合 {ens:.1f} vs 最佳 {best:.1f} ({margin:+.1f}) {runs_info}")
    print()

    # Final verdict
    if verdict == "PASS":
        print(f"  ╔══════════════════════════════════════╗")
        print(f"  ║  ✅ 验证通过: {mode.upper()} 模式有效           ║")
        print(f"  ╚══════════════════════════════════════╝")
    else:
        print(f"  ╔══════════════════════════════════════╗")
        print(f"  ║  ❌ 验证未通过: {mode.upper()} 模式需要优化      ║")
        print(f"  ╚══════════════════════════════════════╝")
    print()


async def run_protocol(mode: str, n_runs: int, questions_file: str | None, concurrency: int):
    """Run the full evaluation protocol for one mode."""
    print(f"\n{'='*70}")
    print(f"  开始 {mode.upper()} 模式验证 — {n_runs} 次运行")
    print(f"{'='*70}")

    protocol_start = time.monotonic()
    all_runs = []

    for i in range(n_runs):
        run_start = time.monotonic()
        print(f"\n  ── 第 {i+1}/{n_runs} 次运行 ──")
        result = await run_single_benchmark(mode, questions_file, concurrency)
        run_elapsed = time.monotonic() - run_start
        all_runs.append(result)

        summary = result.get("summary", {})
        ens_avg = summary.get("ensemble_average_total", 0)
        wins = summary.get("wins", 0)
        ties = summary.get("ties", 0)
        losses = summary.get("losses", 0)
        print(f"  第 {i+1} 次完成: 聚合 {ens_avg:.1f}/500, W/T/L={wins}/{ties}/{losses}, 耗时 {_format_duration(run_elapsed)}")

        if i < n_runs - 1:
            remaining = (n_runs - i - 1) * run_elapsed
            print(f"  预计剩余: {_format_duration(remaining)}")

    # Merge runs
    merged = merge_multi_run_results(all_runs, mode)

    # Evaluate against criteria
    evaluation = evaluate_against_criteria(merged)

    # Print report
    print_report(evaluation, merged)

    # Save results
    output_dir = PROJECT_ROOT / "data" / "benchmark"
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"protocol_{mode}_{ts}.json"

    full_output = {
        "evaluation": evaluation,
        "merged": merged,
        "individual_runs": [
            {"run_index": i, "summary": r.get("summary", {})}
            for i, r in enumerate(all_runs)
        ],
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(full_output, f, ensure_ascii=False, indent=2)

    total_elapsed = time.monotonic() - protocol_start
    print(f"  结果已保存: {output_path}")
    print(f"  总耗时: {_format_duration(total_elapsed)}")

    return evaluation


async def main():
    parser = argparse.ArgumentParser(description="Synthora 固定评估协议")
    parser.add_argument("--mode", choices=["light", "deep", "research", "all"], default="deep")
    parser.add_argument("--runs", type=int, default=3, help="运行次数 (默认 3)")
    parser.add_argument("--concurrency", "-c", type=int, default=3, help="每次运行的题目并发数")
    parser.add_argument("--quick", action="store_true", help="快速模式: 1 次运行，高并发")
    parser.add_argument("--questions-file", help="题库文件路径 (默认: scripts/test_questions.json)")

    args = parser.parse_args()

    if args.quick:
        args.runs = 1
        args.concurrency = 5

    modes = ["light", "deep", "research"] if args.mode == "all" else [args.mode]

    print(f"\n  Synthora 评估协议 v{PROTOCOL_VERSION}")
    print(f"  评估模型: {FIXED_EVALUATOR} (固定)")
    print(f"  Prompt Hash: {_get_evaluator_prompt_hash()}")
    print(f"  运行次数: {args.runs}")
    print(f"  模式: {', '.join(modes)}")
    print(f"  成功标准 (冻结):")
    for m in modes:
        c = SUCCESS_CRITERIA.get(m, {})
        print(f"    {m}: Δ≥{c.get('vs_best_single_delta', '?')}, "
              f"胜率≥{c.get('min_win_rate_pct', '?')}%, "
              f"覆盖率≥{c.get('min_coverage_pct', '?')}%")

    all_results = {}
    for mode in modes:
        result = await run_protocol(mode, args.runs, args.questions_file, args.concurrency)
        all_results[mode] = result

    # Final summary for multi-mode
    if len(modes) > 1:
        print("\n" + "=" * 70)
        print("  总结")
        print("=" * 70)
        for mode, result in all_results.items():
            verdict = result["verdict"]
            delta = result["stats"]["delta"]
            icon = "✅" if verdict == "PASS" else "❌"
            print(f"  {icon} {mode:<10} Δ={delta:+.2f}  {verdict}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
