#!/usr/bin/env python3
"""
Synthora 质量趋势分析工具

读取 data/benchmark/*.json，按时间排序，提取关键指标趋势。
自动检测微退化（连续 N 次某维度/类别下降）。

用法:
  python3 scripts/trend_analysis.py                    # 分析所有 deep benchmark
  python3 scripts/trend_analysis.py --mode research    # 分析 research benchmark
  python3 scripts/trend_analysis.py --last 10          # 只看最近 10 份
  python3 scripts/trend_analysis.py --alert-window 3   # 连续 3 次下降告警
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BENCHMARK_DIR = PROJECT_ROOT / "data" / "benchmark"

DIMENSIONS = ["accuracy", "completeness", "nuance", "clarity", "balance"]


def load_benchmarks(mode: str, last_n: int = 0) -> list[dict]:
    """Load and sort benchmark JSONs by timestamp."""
    pattern = f"{mode}_*.json"
    files = sorted(BENCHMARK_DIR.glob(pattern))

    results = []
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            meta = data.get("meta", {})
            summary = data.get("summary", {})
            if not summary:
                continue
            # Skip runs with 0 valid questions
            if summary.get("quality_valid_questions", 0) == 0:
                continue
            results.append({
                "file": f.name,
                "timestamp": meta.get("timestamp", ""),
                "commit": meta.get("commit_sha", "unknown")[:8],
                "n_questions": summary.get("quality_valid_questions", 0),
                "total_questions": summary.get("total_questions", 0),
                "ensemble_avg": summary.get("ensemble_average_total", 0),
                "best_ind_avg": summary.get("best_individual_average", 0),
                "delta": summary.get("ensemble_vs_best_individual_delta", 0),
                "win_rate": summary.get("win_rate_pct", 0),
                "wins": summary.get("wins", 0),
                "losses": summary.get("losses", 0),
                "dim_avgs": summary.get("ensemble_dimension_averages", {}),
                "category_breakdown": summary.get("category_breakdown", {}),
                "verdict": summary.get("verdict", "N/A"),
                "cost": summary.get("total_cost_usd_accurate", 0),
                "contributors": meta.get("contributors", []),
                "judge": meta.get("judge", ""),
            })
        except (json.JSONDecodeError, KeyError) as e:
            print(f"  ⚠️ 跳过 {f.name}: {e}", file=sys.stderr)

    if last_n > 0:
        results = results[-last_n:]
    return results


def detect_regressions(runs: list[dict], window: int = 3) -> list[str]:
    """Detect consecutive declines in dimensions or categories."""
    alerts = []
    if len(runs) < window:
        return alerts

    # Check dimension trends
    for dim in DIMENSIONS:
        streak = 0
        for i in range(1, len(runs)):
            prev = runs[i - 1]["dim_avgs"].get(dim, 0)
            curr = runs[i]["dim_avgs"].get(dim, 0)
            if curr < prev - 0.5:  # >0.5 point decline
                streak += 1
                if streak >= window - 1:
                    alerts.append(
                        f"🔴 维度 {dim} 连续 {streak + 1} 次下降 "
                        f"({runs[i - streak]['dim_avgs'].get(dim, 0):.1f} → {curr:.1f})"
                    )
            else:
                streak = 0

    # Check win_rate trend
    streak = 0
    for i in range(1, len(runs)):
        if runs[i]["win_rate"] < runs[i - 1]["win_rate"] - 1:
            streak += 1
            if streak >= window - 1:
                alerts.append(
                    f"🔴 win_rate 连续 {streak + 1} 次下降 "
                    f"({runs[i - streak]['win_rate']:.0f}% → {runs[i]['win_rate']:.0f}%)"
                )
        else:
            streak = 0

    # Check ensemble_avg trend
    streak = 0
    for i in range(1, len(runs)):
        if runs[i]["ensemble_avg"] < runs[i - 1]["ensemble_avg"] - 2:
            streak += 1
            if streak >= window - 1:
                alerts.append(
                    f"🔴 ensemble_avg 连续 {streak + 1} 次下降 "
                    f"({runs[i - streak]['ensemble_avg']:.1f} → {runs[i]['ensemble_avg']:.1f})"
                )
        else:
            streak = 0

    return alerts


def detect_category_weakness(runs: list[dict]) -> list[str]:
    """Find categories that consistently underperform."""
    if not runs:
        return []

    # Aggregate category win rates across all runs
    cat_wins: dict[str, list[float]] = {}
    for run in runs:
        for cat, info in run.get("category_breakdown", {}).items():
            if isinstance(info, dict):
                w = info.get("wins", 0)
                t = info.get("total", 0)
                if t > 0:
                    cat_wins.setdefault(cat, []).append(w / t * 100)

    alerts = []
    for cat, rates in cat_wins.items():
        avg_rate = sum(rates) / len(rates)
        if avg_rate < 40:
            alerts.append(f"🟡 类别 {cat} 平均胜率 {avg_rate:.0f}% — 系统性弱点")

    return alerts


def detect_model_drag(runs: list[dict]) -> list[str]:
    """Detect models that consistently lose as best_single (拖后腿检测)."""
    # This requires per-question data which summary doesn't have
    # Placeholder for future enhancement
    return []


def print_trend_table(runs: list[dict]) -> None:
    """Print a compact trend table."""
    if not runs:
        print("  无有效数据。")
        return

    # Header
    print(f"{'时间':<22} {'题数':>4} {'聚合':>6} {'最佳单':>6} {'Δ':>5} "
          f"{'胜率':>5} {'W/L':>5} {'判定':<20} {'commit':>8}")
    print("─" * 95)

    for r in runs:
        verdict_short = {
            "ENSEMBLE_DOMINANT": "✅ DOMINANT",
            "ENSEMBLE_ACCEPTABLE": "✅ ACCEPT",
            "COMPARABLE": "⚠️ COMPARABLE",
            "INDIVIDUAL_BETTER": "❌ IND_BETTER",
            "INVALID_RUN": "❌ INVALID",
        }.get(r["verdict"], r["verdict"][:15])

        delta_str = f"{r['delta']:+.0f}"
        print(
            f"{r['timestamp']:<22} {r['n_questions']:>4} "
            f"{r['ensemble_avg']:>6.1f} {r['best_ind_avg']:>6.1f} {delta_str:>5} "
            f"{r['win_rate']:>4.0f}% {r['wins']:>2}/{r['losses']:<2} "
            f"{verdict_short:<20} {r['commit']:>8}"
        )


def print_dimension_trend(runs: list[dict]) -> None:
    """Print dimension-level trends for last N runs."""
    if len(runs) < 2:
        return

    print(f"\n{'维度':<15}", end="")
    for r in runs[-5:]:  # Show last 5
        label = r["timestamp"][5:16] if len(r["timestamp"]) >= 16 else r["timestamp"][:11]
        print(f" {label:>12}", end="")
    print(f" {'趋势':>8}")
    print("─" * (15 + 13 * min(5, len(runs)) + 8))

    for dim in DIMENSIONS:
        print(f"{dim:<15}", end="")
        values = []
        for r in runs[-5:]:
            val = r["dim_avgs"].get(dim, 0)
            values.append(val)
            print(f" {val:>12.1f}", end="")

        # Trend arrow
        if len(values) >= 2:
            diff = values[-1] - values[0]
            if diff > 2:
                trend = "↑↑"
            elif diff > 0.5:
                trend = "↑"
            elif diff < -2:
                trend = "↓↓"
            elif diff < -0.5:
                trend = "↓"
            else:
                trend = "→"
        else:
            trend = "—"
        print(f" {trend:>8}")


def print_summary_stats(runs: list[dict]) -> None:
    """Print aggregate statistics."""
    if not runs:
        return

    win_rates = [r["win_rate"] for r in runs]
    deltas = [r["delta"] for r in runs]
    ens_avgs = [r["ensemble_avg"] for r in runs]

    print(f"\n  总运行数: {len(runs)}")
    print(f"  聚合平均分: {min(ens_avgs):.1f} ~ {max(ens_avgs):.1f} (均值 {sum(ens_avgs)/len(ens_avgs):.1f})")
    print(f"  胜率范围:   {min(win_rates):.0f}% ~ {max(win_rates):.0f}% (均值 {sum(win_rates)/len(win_rates):.0f}%)")
    print(f"  Delta范围:  {min(deltas):+.1f} ~ {max(deltas):+.1f} (均值 {sum(deltas)/len(deltas):+.1f})")

    # Route regret: percentage of runs where losses > wins
    regret_runs = sum(1 for r in runs if r["losses"] > r["wins"])
    print(f"  聚合净劣势运行: {regret_runs}/{len(runs)} ({regret_runs/len(runs)*100:.0f}%)")

    total_cost = sum(r["cost"] for r in runs)
    print(f"  总测试成本: ${total_cost:.2f}")


def main():
    parser = argparse.ArgumentParser(description="Synthora 质量趋势分析")
    parser.add_argument("--mode", default="deep", choices=["light", "deep", "research"])
    parser.add_argument("--last", type=int, default=0, help="只分析最近 N 份 benchmark")
    parser.add_argument("--alert-window", type=int, default=3, help="连续下降 N 次触发告警 (default: 3)")
    args = parser.parse_args()

    print("=" * 95)
    print(f"  Synthora 质量趋势分析 — {args.mode} 模式")
    print("=" * 95)

    runs = load_benchmarks(args.mode, args.last)
    if not runs:
        print(f"  data/benchmark/{args.mode}_*.json 中无有效数据。")
        return

    # 1. Trend table
    print(f"\n## 趋势表（{len(runs)} 份有效 benchmark）\n")
    print_trend_table(runs)

    # 2. Dimension trends
    print(f"\n## 维度趋势（最近 {min(5, len(runs))} 次）")
    print_dimension_trend(runs)

    # 3. Summary stats
    print(f"\n## 汇总统计")
    print_summary_stats(runs)

    # 4. Regression detection
    alerts = detect_regressions(runs, args.alert_window)
    cat_alerts = detect_category_weakness(runs)
    all_alerts = alerts + cat_alerts

    if all_alerts:
        print(f"\n## ⚠️ 告警 ({len(all_alerts)})")
        for a in all_alerts:
            print(f"  {a}")
    else:
        print(f"\n## ✅ 无告警（连续 {args.alert_window} 次下降检测通过）")

    print()


if __name__ == "__main__":
    main()
