#!/usr/bin/env python3
"""
Synthora Quality Gate 决策审计工具

从 benchmark JSON 中提取 gate_result 决策，计算 regret（反事实损失），
识别系统性误判模式。

指标定义:
  - gate_regret: oracle_score - chosen_score
    oracle = max(best_single_score, synthesized_score)
    chosen = 实际走 BEST_SINGLE 或 SYNTHESIZED 的得分
  - 错误 SYNTHESIZED: 走了 SYNTHESIZED 但 best_single 得分更高（margin < -5）
  - 错误 BEST_SINGLE: 走了 BEST_SINGLE 但对照 benchmark 中 SYNTHESIZED 可能更好（需要 A/B）

用法:
  python3 scripts/audit_gate_decisions.py data/benchmark/deep_xxx.json
  python3 scripts/audit_gate_decisions.py --all              # 分析全部 deep benchmark
  python3 scripts/audit_gate_decisions.py --all --mode research
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BENCHMARK_DIR = PROJECT_ROOT / "data" / "benchmark"

DIMENSIONS = ["accuracy", "completeness", "nuance", "clarity", "balance"]


def analyze_one_benchmark(path: Path) -> dict | None:
    """Analyze gate decisions in a single benchmark file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        print(f"  ⚠️ {path.name}: {e}", file=sys.stderr)
        return None

    meta = data.get("meta", {})
    questions = data.get("questions", [])

    results = []
    for q in questions:
        if q.get("error"):
            continue

        q_id = q.get("id", "?")
        gate = q.get("gate_result", "unknown")
        ens_scores = q.get("ensemble_scores", {})
        ind_scores = q.get("individual_scores", {})
        ens_total = sum(ens_scores.get(d, 0) for d in DIMENSIONS)

        if not ind_scores or ens_total == 0:
            continue

        # Find best single model
        best_model = None
        best_total = 0
        for mid, scores in ind_scores.items():
            total = sum(scores.get(d, 0) for d in DIMENSIONS)
            if total > best_total:
                best_total = total
                best_model = mid

        margin = ens_total - best_total
        question_type = q.get("question_type", "unknown")
        category = q.get("category", "unknown")

        # Gate regret analysis
        if gate == "best_single":
            # Chose best_single — regret = 0 if best_single was indeed better
            # But we can't know synthesized score (wasn't generated)
            # We only know that BEST_SINGLE was triggered
            regret_type = "optimal" if margin >= 0 else "unknown"
            regret = 0  # Can't compute without counterfactual
        elif gate == "synthesized":
            # Chose synthesized — regret = max(0, best_single - ensemble)
            if margin < 0:
                regret = abs(margin)
                regret_type = "wrong_synthesized" if margin < -5 else "minor_loss"
            else:
                regret = 0
                regret_type = "optimal"
        else:
            regret = 0
            regret_type = "other"

        results.append({
            "q_id": q_id,
            "category": category,
            "question_type": question_type,
            "gate": gate,
            "ensemble_total": ens_total,
            "best_single_total": best_total,
            "best_single_model": best_model,
            "margin": margin,
            "regret": regret,
            "regret_type": regret_type,
        })

    if not results:
        return None

    return {
        "file": path.name,
        "timestamp": meta.get("timestamp", ""),
        "commit": meta.get("commit_sha", "unknown")[:8],
        "questions": results,
    }


def print_gate_audit(analyses: list[dict]) -> None:
    """Print comprehensive gate audit report."""
    all_questions = []
    for a in analyses:
        all_questions.extend(a["questions"])

    if not all_questions:
        print("  无有效数据。")
        return

    total = len(all_questions)

    # Gate distribution
    gate_counts: dict[str, int] = {}
    for q in all_questions:
        gate_counts[q["gate"]] = gate_counts.get(q["gate"], 0) + 1

    print(f"\n## Gate 分布 ({total} 题)")
    for gate, count in sorted(gate_counts.items(), key=lambda x: -x[1]):
        pct = count / total * 100
        print(f"  {gate:<20} {count:>4} ({pct:.0f}%)")

    # Regret analysis
    wrong_synth = [q for q in all_questions if q["regret_type"] == "wrong_synthesized"]
    minor_loss = [q for q in all_questions if q["regret_type"] == "minor_loss"]
    optimal = [q for q in all_questions if q["regret_type"] == "optimal"]
    synth_questions = [q for q in all_questions if q["gate"] == "synthesized"]

    print(f"\n## Regret 分析 (SYNTHESIZED 路径: {len(synth_questions)} 题)")
    if synth_questions:
        print(f"  ✅ 最优 (聚合 ≥ 最佳单模型): {len(optimal)}/{len(synth_questions)} "
              f"({len(optimal)/len(synth_questions)*100:.0f}%)")
        print(f"  ⚠️ 小损失 (margin -1~-5):      {len(minor_loss)}/{len(synth_questions)} "
              f"({len(minor_loss)/len(synth_questions)*100:.0f}%)")
        print(f"  🔴 错误 SYNTH (margin < -5):    {len(wrong_synth)}/{len(synth_questions)} "
              f"({len(wrong_synth)/len(synth_questions)*100:.0f}%)")

        avg_margin = sum(q["margin"] for q in synth_questions) / len(synth_questions)
        print(f"  平均 margin: {avg_margin:+.1f}")

        if wrong_synth:
            print(f"\n  错误 SYNTHESIZED 详情:")
            for q in sorted(wrong_synth, key=lambda x: x["margin"]):
                print(f"    [{q['q_id']}] margin={q['margin']:+.0f} "
                      f"best={q['best_single_model']} "
                      f"type={q['question_type']} cat={q['category']}")

    # Category breakdown
    print(f"\n## 按类别分析")
    cat_data: dict[str, list] = {}
    for q in all_questions:
        cat_data.setdefault(q["category"], []).append(q)

    print(f"  {'类别':<20} {'题数':>4} {'胜率':>6} {'平均margin':>10} {'错误SYNTH':>10}")
    print("  " + "─" * 55)
    for cat, qs in sorted(cat_data.items()):
        wins = sum(1 for q in qs if q["margin"] >= 0)
        avg_m = sum(q["margin"] for q in qs) / len(qs)
        ws = sum(1 for q in qs if q["regret_type"] == "wrong_synthesized")
        print(f"  {cat:<20} {len(qs):>4} {wins/len(qs)*100:>5.0f}% {avg_m:>+10.1f} {ws:>10}")

    # Question type breakdown
    print(f"\n## 按题型分析")
    qt_data: dict[str, list] = {}
    for q in all_questions:
        qt_data.setdefault(q["question_type"], []).append(q)

    print(f"  {'题型':<20} {'题数':>4} {'胜率':>6} {'平均margin':>10} {'SYNTH率':>8}")
    print("  " + "─" * 55)
    for qt, qs in sorted(qt_data.items()):
        wins = sum(1 for q in qs if q["margin"] >= 0)
        avg_m = sum(q["margin"] for q in qs) / len(qs)
        synth_rate = sum(1 for q in qs if q["gate"] == "synthesized") / len(qs) * 100
        print(f"  {qt:<20} {len(qs):>4} {wins/len(qs)*100:>5.0f}% {avg_m:>+10.1f} {synth_rate:>7.0f}%")

    # Route regret summary
    all_margins = [q["margin"] for q in all_questions]
    negative_margins = [m for m in all_margins if m < 0]
    print(f"\n## Route Regret 汇总")
    print(f"  聚合劣于最佳单模型: {len(negative_margins)}/{total} ({len(negative_margins)/total*100:.0f}%)")
    if negative_margins:
        print(f"  劣势时平均损失: {sum(negative_margins)/len(negative_margins):.1f} 分")
        print(f"  最大单次损失: {min(negative_margins):.0f} 分")
    positive_margins = [m for m in all_margins if m > 0]
    if positive_margins:
        print(f"  优势时平均增益: +{sum(positive_margins)/len(positive_margins):.1f} 分")
        print(f"  最大单次增益: +{max(positive_margins):.0f} 分")

    # Actionable recommendation
    if len(wrong_synth) / max(len(synth_questions), 1) > 0.3:
        print(f"\n  ⚡ 建议: 错误 SYNTHESIZED 率 > 30%，需提高 best_single_gap_threshold")
    elif len(negative_margins) / total > 0.55:
        print(f"\n  ⚡ 建议: 聚合劣势率 > 55%，需审查 Judge prompt 是否在和稀泥")


def main():
    parser = argparse.ArgumentParser(description="Quality Gate 决策审计")
    parser.add_argument("files", nargs="*", help="Benchmark JSON 文件路径")
    parser.add_argument("--all", action="store_true", help="分析 data/benchmark/ 中全部文件")
    parser.add_argument("--mode", default="deep", help="--all 模式下筛选的模式 (default: deep)")
    parser.add_argument("--last", type=int, default=0, help="只分析最近 N 份")
    args = parser.parse_args()

    print("=" * 70)
    print("  Synthora Quality Gate 决策审计")
    print("=" * 70)

    paths: list[Path] = []
    if args.all:
        paths = sorted(BENCHMARK_DIR.glob(f"{args.mode}_*.json"))
        if args.last > 0:
            paths = paths[-args.last:]
    elif args.files:
        paths = [Path(f) for f in args.files]
    else:
        print("  用法: python3 scripts/audit_gate_decisions.py --all")
        print("  或:   python3 scripts/audit_gate_decisions.py data/benchmark/deep_xxx.json")
        return

    analyses = []
    for p in paths:
        result = analyze_one_benchmark(p)
        if result:
            analyses.append(result)

    if not analyses:
        print("  无有效数据。")
        return

    total_qs = sum(len(a["questions"]) for a in analyses)
    print(f"\n  分析 {len(analyses)} 份 benchmark，共 {total_qs} 个有效题目")

    print_gate_audit(analyses)
    print()


if __name__ == "__main__":
    main()
