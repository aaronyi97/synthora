#!/usr/bin/env python3
"""
单模型性能测试 — 测试新替换模型的响应速度和质量
重点对比: GPT-5.2 Thinking vs Gemini 3.1 Pro Thinking vs Claude Opus 4.6 Thinking
"""
from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agoracle.config.loader import load_config
from agoracle.adapters.models.openai_adapter import OpenAIModelAdapter
from agoracle.domain.types import Role, RoleCall

TEST_PROMPT = "请用200字以内解释量子纠缠的核心原理，并举一个实际应用例子。"

MODELS_TO_TEST = [
    ("gpt52_pro",            "GPT-5.2 Thinking        "),
    ("gpt52_all",            "GPT-5.2 All             "),
    ("gemini_31_pro_thinking", "Gemini 3.1 Pro Thinking "),
    ("claude_opus_thinking",  "Claude Opus 4.6 Thinking"),
    ("claude_sonnet",         "Claude Sonnet 4.6       "),
    ("claude_sonnet_thinking","Claude Sonnet 4.6 Think "),
]

async def test_model(adapter: OpenAIModelAdapter, model_id: str, label: str) -> dict:
    call = RoleCall(
        call_id=f"perf-{model_id}-{uuid.uuid4().hex[:6]}",
        model_id=model_id,
        role=Role.CONTRIBUTOR,
        system_prompt="你是一个简洁准确的AI助手，用中文回答。",
        messages=[{"role": "user", "content": TEST_PROMPT}],
        timeout_seconds=120,
    )
    t0 = time.monotonic()
    try:
        resp = await adapter.call(call)
        elapsed = time.monotonic() - t0
        ok = resp.success and bool(resp.content)
        tokens = len(resp.content.split()) if resp.content else 0
        preview = (resp.content[:80] + "…") if resp.content and len(resp.content) > 80 else (resp.content or "")
        return {
            "label": label,
            "model_id": model_id,
            "success": ok,
            "latency_s": round(elapsed, 2),
            "word_count": tokens,
            "preview": preview,
            "error": resp.error if not ok else None,
        }
    except Exception as e:
        elapsed = time.monotonic() - t0
        return {
            "label": label,
            "model_id": model_id,
            "success": False,
            "latency_s": round(elapsed, 2),
            "word_count": 0,
            "preview": "",
            "error": str(e),
        }

async def main():
    cfg = load_config(PROJECT_ROOT / "config.yaml")
    adapter = OpenAIModelAdapter(cfg)

    print(f"\n{'='*70}")
    print(f"  模型性能测试 — 测试问题: {TEST_PROMPT[:50]}...")
    print(f"{'='*70}\n")

    # Run all models concurrently
    tasks = [test_model(adapter, mid, lbl) for mid, lbl in MODELS_TO_TEST]
    results = await asyncio.gather(*tasks)

    # Sort by latency
    results.sort(key=lambda r: r["latency_s"] if r["success"] else 9999)

    print(f"{'模型':<28} {'状态':<6} {'延迟(s)':<10} {'字数':<8} {'预览'}")
    print(f"{'-'*28} {'-'*6} {'-'*10} {'-'*8} {'-'*30}")
    for r in results:
        status = "✅ OK" if r["success"] else "❌ ERR"
        preview = r["preview"] if r["success"] else f"ERROR: {r['error']}"
        print(f"{r['label']} {status:<6} {r['latency_s']:<10} {r['word_count']:<8} {preview}")

    print(f"\n{'='*70}")
    print("  Thinking 模型横向对比 (GPT-5.2 T / Gemini 3.1 T / Opus 4.6 T)")
    print(f"{'='*70}")
    thinking = [r for r in results if "Thinking" in r["label"] or "Think" in r["label"]]
    if len(thinking) >= 2:
        fastest = min(thinking, key=lambda r: r["latency_s"] if r["success"] else 9999)
        slowest = max(thinking, key=lambda r: r["latency_s"] if r["success"] else 0)
        print(f"  最快: {fastest['label'].strip()} ({fastest['latency_s']}s)")
        print(f"  最慢: {slowest['label'].strip()} ({slowest['latency_s']}s)")
        if fastest["latency_s"] > 0:
            ratio = slowest["latency_s"] / fastest["latency_s"]
            print(f"  速度比: {ratio:.1f}x")
    print()

if __name__ == "__main__":
    asyncio.run(main())
