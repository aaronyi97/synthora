#!/usr/bin/env python3
"""分析每个题型中，哪个模型最常获胜，判断双模型竞争该选哪几个"""
import json, glob
from collections import defaultdict

files = sorted(glob.glob('data/benchmark/deep_*.json'))

# 统计每个题型中，每个模型获胜的次数和平均分
cat_model_wins = defaultdict(lambda: defaultdict(int))
cat_model_scores = defaultdict(lambda: defaultdict(list))

for f in files:
    with open(f, 'r', encoding='utf-8') as fh:
        d = json.load(fh)
    judge = d.get('meta', {}).get('judge', '')
    for q in d.get('questions', []):
        cat = q.get('category', 'unknown')
        best = q.get('best_single_model', '')
        if best:
            cat_model_wins[cat][best] += 1
        ind = q.get('individual_scores', {})
        for m, scores in ind.items():
            if isinstance(scores, dict):
                total = sum(scores.values())
            else:
                total = scores
            cat_model_scores[cat][m].append(total)

# 需要双模型竞争的题型
race_cats = ['factual', 'controversial', 'cultural']

print("=" * 80)
print("不适合聚合的题型：各模型表现排名")
print("=" * 80)

all_model_wins_in_race = defaultdict(int)
all_model_scores_in_race = defaultdict(list)

for cat in race_cats:
    print(f"\n### {cat} ###")
    
    # 按平均分排名
    model_avgs = {}
    for m, scores in cat_model_scores[cat].items():
        if scores:
            model_avgs[m] = sum(scores) / len(scores)
    
    sorted_models = sorted(model_avgs.items(), key=lambda x: -x[1])
    
    print("  按平均分排名:")
    for m, avg in sorted_models:
        win_count = cat_model_wins[cat].get(m, 0)
        short = m.replace('claude_opus_thinking', 'opus').replace('gpt52_thinking', 'gpt52').replace('deepseek_reasoner', 'deepseek').replace('gemini_31_pro_thinking', 'gemini').replace('gpt53_codex', 'codex')
        print(f"    {short:15s}  avg={avg:.1f}  wins={win_count}")
        all_model_wins_in_race[m] += win_count
        all_model_scores_in_race[m].extend(scores)
    
    print(f"  获胜最多: {max(cat_model_wins[cat].items(), key=lambda x: x[1]) if cat_model_wins[cat] else 'N/A'}")

print("\n" + "=" * 80)
print("跨题型汇总（factual + controversial + cultural）")
print("=" * 80)

print("\n  模型总获胜次数:")
for m, w in sorted(all_model_wins_in_race.items(), key=lambda x: -x[1]):
    short = m.replace('claude_opus_thinking', 'opus').replace('gpt52_thinking', 'gpt52').replace('deepseek_reasoner', 'deepseek').replace('gemini_31_pro_thinking', 'gemini').replace('gpt53_codex', 'codex').replace('kimi', 'kimi')
    avg = sum(all_model_scores_in_race[m]) / len(all_model_scores_in_race[m]) if all_model_scores_in_race[m] else 0
    print(f"    {short:15s}  wins={w}  avg={avg:.1f}")

print("\n建议:")
top2 = sorted(all_model_wins_in_race.items(), key=lambda x: -x[1])[:3]
print(f"  Top 3 模型: {[t[0].replace('claude_opus_thinking','opus').replace('gpt52_thinking','gpt52').replace('kimi','kimi') for t in top2]}")
