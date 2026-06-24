"""
老数据价值挖掘脚本 (v1.0)

P0-1: 导出 low_confidence 查询 → benchmark 候选
P0-2: 导出"聚合明显赢"案例 → 产品价值实证
P1:   分析 best_single（聚合输）的题型分布 → 找系统弱点

输出目录: data/analysis/
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
MONITOR_LOG = ROOT / "data/logs/query_monitor.jsonl"
OUTPUT_DIR = ROOT / "data/analysis"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


_TEST_QUESTIONS = {"test", "测试", "hello", "hi", "你好", "test.", "test。"}


def load_monitor() -> list[dict]:
    records = []
    with open(MONITOR_LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def filter_real(records: list[dict]) -> list[dict]:
    """过滤测试请求，只保留真实用户查询。"""
    return [
        r for r in records
        if r.get("question", "").strip().lower() not in _TEST_QUESTIONS
        and len(r.get("question", "").strip()) > 10
    ]


def export_low_confidence(records: list[dict]) -> None:
    """P0-1: low_confidence 查询 → benchmark 候选"""
    candidates = [
        r for r in records
        if r.get("gate_result") == "low_confidence"
        and r.get("question")
        and len(r.get("question", "")) > 10
    ]

    # 按 confidence 升序（最难的排前面）
    candidates.sort(key=lambda r: r.get("confidence", 1.0))

    out = []
    for r in candidates:
        out.append({
            "query_id": r.get("query_id", ""),
            "mode": r.get("mode", ""),
            "question": r.get("question", ""),
            "confidence": r.get("confidence", 0),
            "gate_result": r.get("gate_result", ""),
            "best_single_score_gap": r.get("best_single_score_gap", 0),
            "question_type": r.get("question_type", "unknown"),
            "final_answer_preview": r.get("final_answer", "")[:300],
            "contributor_count": r.get("contributor_count", 0),
            "ts": r.get("ts", ""),
        })

    path = OUTPUT_DIR / "low_confidence_candidates.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for item in out:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    print(f"\n[P0-1] low_confidence 候选题目")
    print(f"  总计: {len(out)} 条")
    print(f"  confidence < 0.3: {sum(1 for r in out if r['confidence'] < 0.3)} 条 ← 最优先加入benchmark")
    print(f"  confidence 0.3-0.5: {sum(1 for r in out if 0.3 <= r['confidence'] < 0.5)} 条")

    # 题型分布
    qtypes = Counter(r["question_type"] for r in out)
    print(f"  题型分布: {dict(qtypes.most_common(6))}")
    print(f"  模式分布: {dict(Counter(r['mode'] for r in out).most_common())}")
    print(f"  → 已导出到: {path}")

    # 预览最难的5道题
    print(f"\n  最难的5道题（置信度最低）:")
    for i, r in enumerate(out[:5], 1):
        print(f"  {i}. [{r['mode']}][conf={r['confidence']:.2f}] {r['question'][:80]}")


def export_aggregation_wins(records: list[dict]) -> None:
    """P0-2: 聚合明显优于最佳单模型 → 产品价值实证"""
    wins = [
        r for r in records
        if r.get("best_single_score_gap", 0) > 0.3
        and r.get("question")
        and r.get("gate_result") == "synthesized"
    ]
    wins.sort(key=lambda r: r.get("best_single_score_gap", 0), reverse=True)

    out = []
    for r in wins:
        out.append({
            "query_id": r.get("query_id", ""),
            "mode": r.get("mode", ""),
            "question": r.get("question", ""),
            "best_single_score_gap": r.get("best_single_score_gap", 0),
            "confidence": r.get("confidence", 0),
            "question_type": r.get("question_type", "unknown"),
            "best_single_model": r.get("best_single_model", ""),
            "final_answer_preview": r.get("final_answer", "")[:300],
            "ts": r.get("ts", ""),
        })

    path = OUTPUT_DIR / "aggregation_wins.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for item in out:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    print(f"\n[P0-2] 聚合明显赢（gap > 0.3）案例")
    print(f"  总计: {len(out)} 条")
    print(f"  gap > 0.5: {sum(1 for r in out if r['best_single_score_gap'] > 0.5)} 条 ← 最强演示案例")
    qtypes = Counter(r["question_type"] for r in out)
    print(f"  题型分布: {dict(qtypes.most_common(6))}")
    print(f"  → 已导出到: {path}")

    print(f"\n  最强的5个演示案例（gap最大）:")
    for i, r in enumerate(out[:5], 1):
        print(f"  {i}. [gap={r['best_single_score_gap']:.2f}][{r['question_type']}] {r['question'][:80]}")


def analyze_aggregation_losses(records: list[dict]) -> None:
    """P1: best_single（聚合输）的题型分布 → 找弱点"""
    losses = [
        r for r in records
        if r.get("gate_result") == "best_single"
        and r.get("question")
    ]

    print(f"\n[P1] 聚合未赢（best_single）分析")
    print(f"  总计: {len(losses)} 条 ({len(losses)/len(records)*100:.1f}%)")

    qtypes = Counter(r.get("question_type", "unknown") for r in losses)
    modes = Counter(r.get("mode", "?") for r in losses)
    print(f"  题型分布: {dict(qtypes.most_common(8))}")
    print(f"  模式分布: {dict(modes.most_common())}")

    # 和 synthesized 的题型对比，找相对弱项
    wins = [r for r in records if r.get("gate_result") == "synthesized"]
    win_types = Counter(r.get("question_type", "unknown") for r in wins)

    print(f"\n  聚合胜率（按题型）:")
    all_types = set(list(qtypes.keys()) + list(win_types.keys()))
    type_stats = []
    for t in all_types:
        w = win_types.get(t, 0)
        l = qtypes.get(t, 0)
        total = w + l
        if total >= 10:
            rate = w / total * 100
            type_stats.append((t, rate, w, l, total))
    type_stats.sort(key=lambda x: x[1])
    for t, rate, w, l, total in type_stats:
        bar = "█" * int(rate / 5)
        flag = " ← 弱项" if rate < 70 else ""
        print(f"  {t:20s}: {rate:5.1f}% {bar}{flag} (赢{w}/输{l})")


def main() -> None:
    print("=" * 60)
    print(f"老数据价值挖掘分析 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    print(f"\n加载 query_monitor.jsonl ...")
    all_records = load_monitor()
    records = filter_real(all_records)
    test_count = len(all_records) - len(records)
    print(f"  总记录: {len(all_records)} 条 | 测试请求过滤: {test_count} 条 | 真实查询: {len(records)} 条")

    export_low_confidence(records)
    export_aggregation_wins(records)
    analyze_aggregation_losses(records)

    print(f"\n{'=' * 60}")
    print(f"输出目录: {OUTPUT_DIR}")
    print(f"  - low_confidence_candidates.jsonl  → benchmark 扩充候选")
    print(f"  - aggregation_wins.jsonl           → 产品价值实证案例")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
