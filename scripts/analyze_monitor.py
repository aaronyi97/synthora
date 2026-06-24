#!/usr/bin/env python3
"""
Layer 3 离线复盘脚本 — 分析 query_monitor.jsonl 数据

用法:
  python3 scripts/analyze_monitor.py                          # 分析所有数据
  python3 scripts/analyze_monitor.py --mode deep              # 只看 deep 模式
  python3 scripts/analyze_monitor.py --last 7                 # 最近 7 天
  python3 scripts/analyze_monitor.py --export report.md       # 导出 Markdown 报告
  python3 scripts/analyze_monitor.py --reeval --model claude_opus_thinking  # 用强模型重新评估历史数据

输出指标:
  1. 聚合胜率 — 聚合答案优于最佳单模型的比例
  2. 模型贡献度排名 — 哪个模型被 Judge 评分最高
  3. 成本效率 — 每个模型的评分/成本比
  4. 质量门分布 — SYNTHESIZED vs BEST_SINGLE 比例
  5. 模型失败率 — 超时/错误频率
  6. 置信度趋势 — 随时间变化
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = PROJECT_ROOT / "data/logs/query_monitor.jsonl"


def load_records(mode_filter: str = "", last_days: int = 0) -> list[dict]:
    if not LOG_PATH.exists():
        print(f"❌ 日志文件不存在: {LOG_PATH}")
        print("   请先运行几次查询，系统会自动生成监控数据。")
        sys.exit(0)

    records = []
    cutoff = datetime.now() - timedelta(days=last_days) if last_days > 0 else None

    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if mode_filter and rec.get("mode") != mode_filter:
                    continue
                if cutoff:
                    ts = datetime.fromisoformat(rec.get("ts", "2000-01-01"))
                    if ts < cutoff:
                        continue
                records.append(rec)
            except Exception:
                continue

    return records


def analyze(records: list[dict]) -> dict:
    if not records:
        return {}

    total = len(records)
    result: dict = {"total_queries": total}

    # ── 1. 质量门分布 ──
    gate_counts: dict[str, int] = defaultdict(int)
    for r in records:
        gate_counts[r.get("gate_result", "unknown")] += 1
    result["gate_distribution"] = dict(gate_counts)
    synth = gate_counts.get("SYNTHESIZED", 0)
    result["synthesis_rate"] = round(synth / total * 100, 1) if total else 0

    # ── 2. 聚合胜率 (Layer 2 数据) ──
    # v1.1: 使用 winner_normalized 确保 A/B 交换后统计一致
    l2_records = [r for r in records if r.get("layer2_eval")]
    if l2_records:
        def _resolve_winner(ev: dict) -> str:
            """Resolve winner to canonical label, handling both old (A/B) and new (Alpha/Beta) formats."""
            if ev.get("winner_normalized"):
                return ev["winner_normalized"]
            # Fallback for legacy records written before v3.7 fix
            raw = (ev.get("winner") or "tie").strip().lower()
            swapped = ev.get("swapped", False)
            if raw == "tie":
                return "tie"
            elif raw in ("alpha", "a"):
                return "best_single" if swapped else "aggregated"
            elif raw in ("beta", "b"):
                return "aggregated" if swapped else "best_single"
            return "tie"

        winners_norm = [_resolve_winner(r["layer2_eval"]) for r in l2_records]
        agg_wins = winners_norm.count("aggregated")
        bs_wins = winners_norm.count("best_single")
        ties = winners_norm.count("tie")
        result["layer2_sample_count"] = len(l2_records)
        result["aggregation_win_rate"] = round(agg_wins / len(l2_records) * 100, 1)
        result["best_single_win_rate"] = round(bs_wins / len(l2_records) * 100, 1)
        result["tie_rate"] = round(ties / len(l2_records) * 100, 1)
        result["layer2_evaluator_models"] = list({
            r["layer2_eval"].get("evaluator_model", "unknown") for r in l2_records
        })
        # 平均分数: 统一用 a_/b_ key，小心处理交换后 key 可能不同
        agg_scores, bs_scores = [], []
        for r in l2_records:
            ev = r["layer2_eval"]
            swapped = ev.get("swapped", False)
            if swapped:
                agg_scores.append((ev.get("b_accuracy", 0) + ev.get("b_completeness", 0) + ev.get("b_nuance", 0)) / 3)
                bs_scores.append((ev.get("a_accuracy", 0) + ev.get("a_completeness", 0) + ev.get("a_nuance", 0)) / 3)
            else:
                agg_scores.append((ev.get("a_accuracy", 0) + ev.get("a_completeness", 0) + ev.get("a_nuance", 0)) / 3)
                bs_scores.append((ev.get("b_accuracy", 0) + ev.get("b_completeness", 0) + ev.get("b_nuance", 0)) / 3)
        result["layer2_avg_scores"] = {
            "aggregated_avg": round(sum(agg_scores) / len(agg_scores), 2) if agg_scores else 0,
            "best_single_avg": round(sum(bs_scores) / len(bs_scores), 2) if bs_scores else 0,
        }

    # ── 3. 模型贡献度排名 (Judge 评分) ──
    model_scores: dict[str, list[float]] = defaultdict(list)
    model_appearances: dict[str, int] = defaultdict(int)
    for r in records:
        evals = r.get("model_evaluations", {})
        for model_id, scores in evals.items():
            model_appearances[model_id] += 1
            if isinstance(scores, dict):
                avg = sum(scores.values()) / len(scores) if scores else 0
                model_scores[model_id].append(avg)

    model_ranking = []
    for model_id, scores in model_scores.items():
        avg_score = sum(scores) / len(scores) if scores else 0
        model_ranking.append({
            "model_id": model_id,
            "avg_judge_score": round(avg_score, 2),
            "appearances": model_appearances[model_id],
            "appearance_rate": round(model_appearances[model_id] / total * 100, 1),
        })
    model_ranking.sort(key=lambda x: x["avg_judge_score"], reverse=True)
    result["model_contribution_ranking"] = model_ranking

    # ── 4. 最佳单模型分布 ──
    best_single_counts: dict[str, int] = defaultdict(int)
    for r in records:
        bsm = r.get("best_single_model", "")
        if bsm:
            best_single_counts[bsm] += 1
    result["best_single_distribution"] = dict(
        sorted(best_single_counts.items(), key=lambda x: x[1], reverse=True)
    )

    # ── 5. 成本统计 ──
    costs_usd = [r.get("estimated_cost_usd", 0) for r in records]
    costs_rmb = [r.get("estimated_cost_rmb", 0) for r in records]
    result["cost_stats"] = {
        "total_usd": round(sum(costs_usd), 4),
        "total_rmb": round(sum(costs_rmb), 2),
        "avg_per_query_usd": round(sum(costs_usd) / total, 4) if total else 0,
        "avg_per_query_rmb": round(sum(costs_rmb) / total, 2) if total else 0,
        "max_query_rmb": round(max(costs_rmb), 2) if costs_rmb else 0,
        "min_query_rmb": round(min(costs_rmb), 2) if costs_rmb else 0,
    }

    # ── 6. 延迟统计 ──
    latencies = [r.get("latency_ms", 0) for r in records]
    result["latency_stats"] = {
        "avg_ms": round(sum(latencies) / total) if total else 0,
        "max_ms": max(latencies) if latencies else 0,
        "min_ms": min(latencies) if latencies else 0,
        "p90_ms": sorted(latencies)[int(len(latencies) * 0.9)] if latencies else 0,
    }

    # ── 7. 置信度统计 ──
    confidences = [r.get("confidence", 0) for r in records]
    result["confidence_stats"] = {
        "avg": round(sum(confidences) / total, 3) if total else 0,
        "low_confidence_rate": round(
            sum(1 for c in confidences if c < 0.6) / total * 100, 1
        ) if total else 0,
    }

    # ── 8. 模式分布 ──
    mode_counts: dict[str, int] = defaultdict(int)
    for r in records:
        mode_counts[r.get("mode", "unknown")] += 1
    result["mode_distribution"] = dict(mode_counts)

    return result


def print_report(stats: dict, records: list[dict]) -> str:
    lines = []
    a = lines.append

    a("=" * 65)
    a("  Synthora 模型质量监控报告")
    a(f"  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    a("=" * 65)

    a(f"\n📊 总查询数: {stats['total_queries']}")

    # 模式分布
    if "mode_distribution" in stats:
        a("\n── 模式分布 ──")
        for mode, cnt in stats["mode_distribution"].items():
            pct = round(cnt / stats["total_queries"] * 100, 1)
            a(f"  {mode:<12} {cnt:>4} 次  ({pct}%)")

    # 质量门
    a(f"\n── 质量门结果 ──")
    a(f"  综合率 (SYNTHESIZED): {stats.get('synthesis_rate', 0)}%")
    for gate, cnt in stats.get("gate_distribution", {}).items():
        pct = round(cnt / stats["total_queries"] * 100, 1)
        a(f"  {gate:<25} {cnt:>4} 次  ({pct}%)")

    # 聚合胜率
    if "aggregation_win_rate" in stats:
        a(f"\n── 聚合 vs 最佳单模型 (Layer 2, n={stats['layer2_sample_count']}) ──")
        a(f"  聚合答案胜出:    {stats['aggregation_win_rate']}%  ← 设计初心验证")
        a(f"  最佳单模型胜出:  {stats['best_single_win_rate']}%")
        a(f"  平局:            {stats['tie_rate']}%")
        l2s = stats.get("layer2_avg_scores", {})
        a(f"  聚合平均分: {l2s.get('aggregated_avg', 0)}/10")
        a(f"  单模型平均分: {l2s.get('best_single_avg', 0)}/10")
        if stats["aggregation_win_rate"] >= 60:
            a("  ✅ 聚合架构价值验证通过 (胜率 ≥ 60%)")
        else:
            a("  ⚠️  聚合架构胜率低于 60%，建议检查 Judge 配置")

    # 模型贡献度
    a(f"\n── 模型贡献度排名 (Judge 评分) ──")
    for i, m in enumerate(stats.get("model_contribution_ranking", []), 1):
        bar = "█" * int(m["avg_judge_score"])
        a(f"  {i}. {m['model_id']:<28} {m['avg_judge_score']:>4}/10  {bar}")
        a(f"     出现 {m['appearances']} 次 ({m['appearance_rate']}%)")

    # 最佳单模型分布
    a(f"\n── 最佳单模型分布 ──")
    for model, cnt in list(stats.get("best_single_distribution", {}).items())[:5]:
        pct = round(cnt / stats["total_queries"] * 100, 1)
        a(f"  {model:<28} {cnt:>4} 次  ({pct}%)")

    # 成本
    cost = stats.get("cost_stats", {})
    a(f"\n── 成本统计 ──")
    a(f"  总成本:         ¥{cost.get('total_rmb', 0)} (${cost.get('total_usd', 0)})")
    a(f"  平均每次查询:   ¥{cost.get('avg_per_query_rmb', 0)}")
    a(f"  最贵单次:       ¥{cost.get('max_query_rmb', 0)}")
    a(f"  最便宜单次:     ¥{cost.get('min_query_rmb', 0)}")

    # 延迟
    lat = stats.get("latency_stats", {})
    a(f"\n── 延迟统计 ──")
    a(f"  平均: {lat.get('avg_ms', 0)}ms  P90: {lat.get('p90_ms', 0)}ms  最慢: {lat.get('max_ms', 0)}ms")

    # 置信度
    conf = stats.get("confidence_stats", {})
    a(f"\n── 置信度 ──")
    a(f"  平均置信度: {conf.get('avg', 0)}")
    a(f"  低置信度率 (<0.6): {conf.get('low_confidence_rate', 0)}%")

    a("\n" + "=" * 65)

    report = "\n".join(lines)
    print(report)
    return report


def reeval_with_strong_model(
    records: list[dict],
    model_id: str,
    sample: int = 0,
) -> None:
    """用强模型重新评估历史数据中缺少 Layer 2 的记录。

    用法:
      python3 scripts/analyze_monitor.py --reeval --model claude_opus_thinking
      python3 scripts/analyze_monitor.py --reeval --model claude_opus_thinking --sample 20

    这是你说的「后期自己做复盘」的入口。
    """
    import asyncio
    import sys
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

    try:
        from agoracle.config.loader import load_config
        from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter
        from agoracle.services.query_monitor import run_layer2_async
    except ImportError as e:
        print(f"❌ 无法导入后端模块: {e}")
        print("   请确保在项目根目录运行此脚本")
        return

    targets = [r for r in records if not r.get("layer2_eval") and r.get("final_answer") and r.get("best_single_content")]
    if sample > 0:
        import random
        targets = random.sample(targets, min(sample, len(targets)))

    if not targets:
        print("没有需要重评的记录（所有记录已有 Layer 2 评估）")
        return

    print(f"将用 [{model_id}] 重新评估 {len(targets)} 条记录...")

    config = load_config()
    adapter = OpenAIModelAdapter(config)

    import os
    os.environ["MONITOR_EVALUATOR_MODEL"] = model_id
    os.environ["MONITOR_SAMPLE_RATE"] = "1.0"  # 重评时不抽样

    async def run_all():
        tasks = [
            run_layer2_async(
                query_id=r["query_id"],
                question=r["question"],
                final_answer=r["final_answer"],
                best_single_content=r["best_single_content"],
                best_single_model=r.get("best_single_model", ""),
                adapter=adapter,
            )
            for r in targets
        ]
        for i, coro in enumerate(asyncio.as_completed(tasks), 1):
            await coro
            print(f"  [{i}/{len(targets)}] 完成")

    asyncio.run(run_all())
    print(f"\n✅ 重评完成，结果已写入 {LOG_PATH}")


def main():
    parser = argparse.ArgumentParser(description="Synthora 模型质量监控复盘")
    parser.add_argument("--mode", default="", help="过滤模式 (light/deep/research/socratic)")
    parser.add_argument("--last", type=int, default=0, help="最近 N 天")
    parser.add_argument("--export", default="", help="导出 Markdown 报告到文件")
    parser.add_argument("--raw", action="store_true", help="输出原始 JSON 统计")
    parser.add_argument("--reeval", action="store_true", help="用强模型重新评估历史数据")
    parser.add_argument("--model", default="claude_opus_thinking", help="重评模型 ID (默认 claude_opus_thinking)")
    parser.add_argument("--sample", type=int, default=0, help="重评样本数（0=全部）")
    args = parser.parse_args()

    records = load_records(mode_filter=args.mode, last_days=args.last)
    if not records:
        print(f"没有找到匹配的记录 (mode={args.mode or '全部'}, last={args.last or '全部'}天)")
        return

    if args.reeval:
        reeval_with_strong_model(records, model_id=args.model, sample=args.sample)
        return

    stats = analyze(records)

    if args.raw:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return

    report = print_report(stats, records)

    if args.export:
        export_path = Path(args.export)
        export_path.write_text(f"# Synthora 监控报告\n\n```\n{report}\n```\n", encoding="utf-8")
        print(f"\n✅ 报告已导出: {export_path}")


if __name__ == "__main__":
    main()
