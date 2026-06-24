#!/usr/bin/env python3
"""
Synthora 回归验证工具 (Regression Check)

每次代码/prompt/config 变动后跑一次，快速确认没有退步。

用法:
  # 跑当前代码 vs 已有 baseline (快速: 1次运行)
  python3 scripts/regression_check.py --mode deep --baseline data/benchmark/baseline_deep_v2.json

  # 只跑指定题目做快速检查
  python3 scripts/regression_check.py --mode deep --baseline data/benchmark/baseline_deep_v2.json -q factual_01,reasoning_01

  # 使用 v2 题库
  python3 scripts/regression_check.py --mode deep --baseline data/benchmark/baseline_deep_v2.json --questions-file scripts/test_questions_v2.json

退出码:
  0 = 无退步 (或无 baseline)
  1 = 检测到退步 (聚合均分下降 > 1 分 或 胜率下降 > 10%)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

DIMENSIONS = ["accuracy", "completeness", "nuance", "clarity", "balance"]

# Regression thresholds
SCORE_DROP_THRESHOLD = 1.0   # 聚合均分下降超过此值 → 红色警告
WINRATE_DROP_THRESHOLD = 10  # 胜率下降超过此百分点 → 红色警告


def _format_duration(seconds: float) -> str:
    s = int(seconds)
    if s >= 60:
        m, sec = divmod(s, 60)
        return f"{m}分{sec}秒"
    return f"{s}秒"


async def run_current_benchmark(mode: str, questions_file: str | None, concurrency: int, question_ids: list[str] | None) -> dict:
    """Run benchmark with current code."""
    from benchmark_quality import QualityBenchmark

    benchmark = QualityBenchmark(
        mode=mode,
        concurrency=concurrency,
        evaluator_model_id="claude_sonnet",
    )

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

    if question_ids:
        benchmark.questions = [q for q in benchmark.questions if q["id"] in question_ids]

    return await benchmark.run()


def compare_with_baseline(current: dict, baseline: dict) -> dict:
    """Compare current results with baseline, return regression report."""
    c_summary = current.get("summary", {})
    b_summary = baseline.get("summary", {})

    c_ens_avg = c_summary.get("ensemble_average_total", 0)
    b_ens_avg = b_summary.get("ensemble_average_total", 0)
    score_delta = round(c_ens_avg - b_ens_avg, 2)

    c_valid = c_summary.get("quality_valid_questions", 0)
    b_valid = b_summary.get("quality_valid_questions", 0)

    c_wins = c_summary.get("wins", 0)
    c_total_wt = c_wins + c_summary.get("ties", 0) + c_summary.get("losses", 0)
    b_wins = b_summary.get("wins", 0)
    b_total_wt = b_wins + b_summary.get("ties", 0) + b_summary.get("losses", 0)

    c_winrate = round(c_wins / c_total_wt * 100, 1) if c_total_wt > 0 else 0
    b_winrate = round(b_wins / b_total_wt * 100, 1) if b_total_wt > 0 else 0
    winrate_delta = round(c_winrate - b_winrate, 1)

    # Per-question diff
    b_questions = {q["id"]: q for q in baseline.get("questions", [])}
    c_questions = {q["id"]: q for q in current.get("questions", [])}

    per_question = []
    improved = []
    regressed = []
    for q_id in sorted(set(list(b_questions.keys()) + list(c_questions.keys()))):
        bq = b_questions.get(q_id)
        cq = c_questions.get(q_id)
        if not bq or not cq:
            continue
        b_total = sum(bq.get("ensemble_scores", {}).get(d, 0) for d in DIMENSIONS)
        c_total = sum(cq.get("ensemble_scores", {}).get(d, 0) for d in DIMENSIONS)
        diff = round(c_total - b_total, 1)
        entry = {"id": q_id, "baseline": b_total, "current": c_total, "diff": diff}
        per_question.append(entry)
        if diff > 1:
            improved.append(entry)
        elif diff < -1:
            regressed.append(entry)

    # Regression verdict
    has_score_regression = score_delta < -SCORE_DROP_THRESHOLD
    has_winrate_regression = winrate_delta < -WINRATE_DROP_THRESHOLD
    has_regression = has_score_regression or has_winrate_regression

    return {
        "has_regression": has_regression,
        "score_delta": score_delta,
        "winrate_delta": winrate_delta,
        "current_ens_avg": c_ens_avg,
        "baseline_ens_avg": b_ens_avg,
        "current_winrate": c_winrate,
        "baseline_winrate": b_winrate,
        "improved_questions": improved,
        "regressed_questions": regressed,
        "per_question": per_question,
    }


def print_regression_report(report: dict, mode: str) -> None:
    """Print regression report to console."""
    print()
    print("=" * 70)
    print(f"  Synthora 回归验证报告 — {mode.upper()} 模式")
    print("=" * 70)

    score_delta = report["score_delta"]
    winrate_delta = report["winrate_delta"]

    score_icon = "🟢" if score_delta >= 0 else ("🔴" if score_delta < -SCORE_DROP_THRESHOLD else "🟡")
    winrate_icon = "🟢" if winrate_delta >= 0 else ("🔴" if winrate_delta < -WINRATE_DROP_THRESHOLD else "🟡")

    print(f"\n  聚合均分: {report['baseline_ens_avg']:.1f} → {report['current_ens_avg']:.1f}  ({score_delta:+.1f}) {score_icon}")
    print(f"  胜率:     {report['baseline_winrate']:.0f}% → {report['current_winrate']:.0f}%  ({winrate_delta:+.1f}pp) {winrate_icon}")

    if report["improved_questions"]:
        print(f"\n  📈 提升的题目 ({len(report['improved_questions'])}):")
        for q in report["improved_questions"]:
            print(f"    {q['id']:<22} {q['baseline']:.0f} → {q['current']:.0f}  ({q['diff']:+.1f})")

    if report["regressed_questions"]:
        print(f"\n  📉 退步的题目 ({len(report['regressed_questions'])}):")
        for q in report["regressed_questions"]:
            print(f"    {q['id']:<22} {q['baseline']:.0f} → {q['current']:.0f}  ({q['diff']:+.1f})")

    stable = [q for q in report["per_question"] if abs(q["diff"]) <= 1]
    if stable:
        print(f"\n  ➖ 稳定的题目 ({len(stable)}): ", end="")
        print(", ".join(q["id"] for q in stable))

    print()
    if report["has_regression"]:
        print("  ╔══════════════════════════════════════════╗")
        print("  ║  🔴 检测到退步！请检查最近的改动        ║")
        print("  ╚══════════════════════════════════════════╝")
    else:
        print("  ╔══════════════════════════════════════════╗")
        print("  ║  🟢 无退步，改动安全                     ║")
        print("  ╚══════════════════════════════════════════╝")
    print()


async def main():
    parser = argparse.ArgumentParser(description="Synthora 回归验证")
    parser.add_argument("--mode", choices=["light", "deep", "research"], required=True)
    parser.add_argument("--baseline", "-b", required=True, help="基线文件路径")
    parser.add_argument("--concurrency", "-c", type=int, default=3)
    parser.add_argument("--questions", "-q", help="只跑指定题目 (逗号分隔)")
    parser.add_argument("--questions-file", help="题库文件路径")
    parser.add_argument("--output", "-o", help="保存回归报告的路径")

    args = parser.parse_args()

    baseline_path = Path(args.baseline)
    if not baseline_path.exists():
        print(f"  错误: 基线文件不存在: {baseline_path}")
        sys.exit(1)

    with open(baseline_path, "r", encoding="utf-8") as f:
        baseline = json.load(f)

    question_ids = [q.strip() for q in args.questions.split(",")] if args.questions else None

    print(f"\n  Synthora 回归验证")
    print(f"  模式: {args.mode}")
    print(f"  基线: {args.baseline}")
    if question_ids:
        print(f"  指定题目: {question_ids}")

    start = time.monotonic()
    current = await run_current_benchmark(args.mode, args.questions_file, args.concurrency, question_ids)
    elapsed = time.monotonic() - start

    print(f"\n  当前代码 benchmark 完成，耗时 {_format_duration(elapsed)}")

    report = compare_with_baseline(current, baseline)
    print_regression_report(report, args.mode)

    # Save report if requested
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        full_report = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "mode": args.mode,
            "baseline_path": str(baseline_path),
            "regression": report,
            "current_summary": current.get("summary", {}),
            "baseline_summary": baseline.get("summary", {}),
        }
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(full_report, f, ensure_ascii=False, indent=2)
        print(f"  报告已保存: {output_path}")

    sys.exit(1 if report["has_regression"] else 0)


if __name__ == "__main__":
    asyncio.run(main())
