#!/usr/bin/env python3
"""
Model connectivity test — verify each model API is reachable and responds correctly.

Usage:
  python scripts/test_connectivity.py
"""

import asyncio
import os
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agoracle.config.loader import load_config


async def test_model(model_id: str, model_config, test_prompt: str = "Say 'hello' in one word.") -> dict:
    """Test a single model's connectivity."""
    import httpx

    # Support both api_key_env (single) and api_key_env_list (rotation)
    api_key = ""
    key_env_used = model_config.api_key_env
    if getattr(model_config, "api_key_env_list", None):
        for env_name in model_config.api_key_env_list:
            val = os.getenv(env_name, "")
            if val:
                api_key = val
                key_env_used = env_name
                break
    if not api_key:
        api_key = os.getenv(model_config.api_key_env, "")
    base_url = os.getenv(model_config.base_url_env, "")

    if not api_key:
        return {
            "model_id": model_id,
            "status": "SKIP",
            "reason": f"No API key ({key_env_used} not set)",
            "latency_ms": 0,
        }

    if not base_url:
        return {
            "model_id": model_id,
            "status": "SKIP",
            "reason": f"No base URL ({model_config.base_url_env} not set)",
            "latency_ms": 0,
        }

    # Build request
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # Per-model temperature (e.g. Kimi K2.5 requires exactly 1.0)
    temperature = getattr(model_config, "temperature", None)
    if temperature is None:
        temperature = 0

    payload = {
        "model": model_config.model_name,
        "messages": [{"role": "user", "content": test_prompt}],
        "max_tokens": 50,
        "temperature": temperature,
    }

    # Kimi K2.5 thinking mode may require extra_body
    if getattr(model_config, "thinking_enabled", False):
        payload["thinking"] = {"type": "enabled"}

    # Use model's configured timeout (thinking models need 60-120s), min 30s
    model_timeout = max(30.0, getattr(model_config, "timeout_seconds", 30))
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=model_timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            latency = int((time.monotonic() - start) * 1000)

            if resp.status_code == 200:
                data = resp.json()
                content = ""
                # Extract response content
                if "choices" in data and data["choices"]:
                    msg = data["choices"][0].get("message", {})
                    content = msg.get("content", "")[:100]
                
                actual_model = data.get("model", "unknown")
                
                return {
                    "model_id": model_id,
                    "status": "OK",
                    "response": content,
                    "actual_model": actual_model,
                    "latency_ms": latency,
                }
            else:
                error_text = resp.text[:200]
                return {
                    "model_id": model_id,
                    "status": "ERROR",
                    "reason": f"HTTP {resp.status_code}: {error_text}",
                    "latency_ms": latency,
                }

    except httpx.TimeoutException:
        latency = int((time.monotonic() - start) * 1000)
        return {
            "model_id": model_id,
            "status": "TIMEOUT",
            "reason": f"Timed out after {latency}ms",
            "latency_ms": latency,
        }
    except Exception as e:
        latency = int((time.monotonic() - start) * 1000)
        return {
            "model_id": model_id,
            "status": "ERROR",
            "reason": str(e)[:200],
            "latency_ms": latency,
        }


async def main():
    print("=" * 70)
    print("  Agoracle — Model Connectivity Test")
    print("=" * 70)
    print()

    config = load_config()
    results = []

    # Test each model sequentially (to get clear output)
    for model_id, model_config in config.models.items():
        print(f"Testing {model_id} ({model_config.name})...", end=" ", flush=True)
        result = await test_model(model_id, model_config)
        results.append(result)

        if result["status"] == "OK":
            print(f"✅ OK ({result['latency_ms']}ms)")
            print(f"   Model: {result.get('actual_model', '?')}")
            print(f"   Response: {result.get('response', '')[:60]}")
        elif result["status"] == "SKIP":
            print(f"⏭️  SKIP — {result['reason']}")
        elif result["status"] == "TIMEOUT":
            print(f"⏳ TIMEOUT — {result['reason']}")
        else:
            print(f"❌ ERROR — {result['reason'][:80]}")
        print()

    # Summary
    print("=" * 70)
    print("  Summary")
    print("=" * 70)
    ok = sum(1 for r in results if r["status"] == "OK")
    skip = sum(1 for r in results if r["status"] == "SKIP")
    fail = sum(1 for r in results if r["status"] in ("ERROR", "TIMEOUT"))
    print(f"  ✅ OK: {ok}  |  ⏭️ SKIP: {skip}  |  ❌ FAIL: {fail}  |  Total: {len(results)}")

    if fail > 0:
        print("\n  Failed models:")
        for r in results:
            if r["status"] in ("ERROR", "TIMEOUT"):
                print(f"    - {r['model_id']}: {r.get('reason', '')[:60]}")

    if skip > 0:
        print("\n  Skipped models (no API key):")
        for r in results:
            if r["status"] == "SKIP":
                print(f"    - {r['model_id']}: {r.get('reason', '')[:60]}")

    print()


if __name__ == "__main__":
    asyncio.run(main())
