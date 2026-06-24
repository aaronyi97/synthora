#!/usr/bin/env python3
"""分析不同题型的聚合效果，找出哪些题型适合聚合，哪些适合直接用单模型"""

import json
import glob
from collections import defaultdict

# 按题型统计聚合表现
category_stats = defaultdict(lambda: {'deltas': [], 'wins': 0, 'losses': 0, 'ties': 0, 'count': 0, 'questions': []})

files = sorted(glob.glob('data/benchmark/deep_*.json'))

for f in files:
    with open(f, 'r', encoding='utf-8') as fh:
        d = json.load(fh)
    
    questions = d.get('questions', [])
    judge = d.get('meta', {}).get('judge', '')
    ts = d.get('meta', {}).get('timestamp', '')[:10]
    
    # 只看 opus 当 Judge 的测试（质量可靠）
    if 'opus' not in judge:
        continue
    
    for q in questions:
        cat = q.get('category', 'unknown')
        et = q.get('ensemble_total')
        bt = q.get('best_single_total')
        best_model = q.get('best_single_model', '')
        qid = q.get('id', '')
        
        if et is None or bt is None:
            continue
        
        delta = et - bt
        winner = q.get('winner', '')
        
        category_stats[cat]['deltas'].append(delta)
        category_stats[cat]['count'] += 1
        category_stats[cat]['questions'].append({
            'id': qid,
            'delta': delta,
            'ensemble': et,
            'best_single': bt,
            'best_model': best_model,
            'date': ts
        })
        
        if winner == 'ensemble':
            category_stats[cat]['wins'] += 1
        elif winner == 'tie':
            category_stats[cat]['ties'] += 1
        else:
            category_stats[cat]['losses'] += 1

print('='*80)
print('题型聚合效果分析（仅 opus 当 Judge 的测试）')
print('='*80)

# 按平均 Delta 排序
sorted_cats = sorted(category_stats.items(), key=lambda x: sum(x[1]['deltas'])/len(x[1]['deltas']), reverse=True)

for cat, stats in sorted_cats:
    avg_delta = sum(stats['deltas']) / len(stats['deltas'])
    win_rate = stats['wins'] / stats['count'] * 100
    
    print(f"\n{cat}:")
    print(f"  样本数: {stats['count']}")
    print(f"  平均 Delta: {avg_delta:+.1f}")
    print(f"  胜/平/负: {stats['wins']}/{stats['ties']}/{stats['losses']}")
    print(f"  胜率: {win_rate:.0f}%")
    
    # 判断是否适合聚合
    if avg_delta > 3 and win_rate > 60:
        verdict = "✅ 适合聚合（显著提升）"
    elif avg_delta > 0 and win_rate > 50:
        verdict = "🟡 聚合有小幅提升"
    else:
        verdict = "❌ 不适合聚合（单模型更好）"
    
    print(f"  结论: {verdict}")
    
    # 显示具体题目
    if stats['count'] <= 5:
        print("  具体题目:")
        for q in stats['questions']:
            print(f"    {q['id']}: {q['delta']:+.0f} (聚合{q['ensemble']} vs {q['best_model'][:15]} {q['best_single']})")

print('\n' + '='*80)
print('建议的路由策略')
print('='*80)

# 生成路由建议
suitable_for_ensemble = []
suitable_for_single = []

for cat, stats in category_stats.items():
    avg_delta = sum(stats['deltas']) / len(stats['deltas'])
    win_rate = stats['wins'] / stats['count'] * 100
    
    if avg_delta > 2 and win_rate > 55:
        suitable_for_ensemble.append(cat)
    elif avg_delta < -5 or win_rate < 40:
        suitable_for_single.append(cat)

print("\n适合聚合的题型（走完整 Deep 流程）:")
for cat in suitable_for_ensemble:
    print(f"  - {cat}")

print("\n适合单模型的题型（直接用最强 2 个模型竞争）:")
for cat in suitable_for_single:
    print(f"  - {cat}")

print("\n其他题型: 根据问题复杂度动态决策")
