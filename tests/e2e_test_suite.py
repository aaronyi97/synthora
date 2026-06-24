"""
Synthora E2E Test Suite — 真实API请求验证
用法: export $(grep -v '^#' .env | grep -v '^$' | xargs) && python3 tests/e2e_test_suite.py

测试分层:
  L1: 管道可用性 — 每个模式返回非空答案
  L2: 质量基线 — 答案切题、结构完整
  L4: 边界压力 — 空输入、超长、特殊字符
"""

import json
import time
import sys
import urllib.request
import urllib.error

API_BASE = "https://api.example.com/api"
COOKIE = None  # Will be set after login

# ============================================================
# Test Questions — 覆盖各模式和场景
# ============================================================

L1_TESTS = [
    # (name, question, mode, web_search, min_answer_len)
    ("light_basic", "什么是量子计算？用三句话解释", "light", False, 50),
    ("light_math", "计算 17 * 23 + 45 / 9", "light", False, 10),
    ("light_chinese", "鲁迅的《狂人日记》讲了什么？", "light", False, 50),
    ("auto_route", "如何学好Python编程？", "auto", False, 50),
    ("deep_compare", "比较 React 和 Vue 的优缺点", "deep", False, 100),
    ("deep_analysis", "为什么中国房价近年来持续下跌？深层原因是什么", "deep", False, 100),
]

L2_QUALITY_CHECKS = [
    # (name, question, mode, must_contain_any, must_not_contain)
    ("factual_accuracy", "地球到月球的平均距离是多少？", "light",
     ["384", "38万", "38.4万"], []),
    ("structured_answer", "比较TCP和UDP的区别", "light",
     ["TCP", "UDP", "可靠", "连接"], []),
    ("reasoning", "一个房间有3盏灯，门外有3个开关，你只能进房间一次，怎么确定哪个开关对应哪盏灯？", "deep",
     ["热", "温", "摸"], []),
]

L4_BOUNDARY_TESTS = [
    # (name, question, mode, expect_error)
    ("empty_input", "", "light", True),
    ("whitespace_only", "   ", "light", True),
    ("single_char", "?", "light", False),  # should work, maybe short answer
    ("very_long", "请解释" * 500, "light", False),  # 1500 chars, should work
    ("special_chars", "如何处理 <script>alert('xss')</script> 攻击？", "light", False),
    ("mixed_lang", "Explain the difference between は and が in Japanese", "light", False),
]


def _set_cookie(value):
    global COOKIE
    COOKIE = value


def _request(method, path, data=None, timeout=120):
    """Make HTTP request to API."""
    url = f"{API_BASE}{path}"
    headers = {"Content-Type": "application/json"}
    if COOKIE:
        headers["Cookie"] = COOKIE

    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            cookie_header = resp.getheader("Set-Cookie")
            if cookie_header:
                # Extract session cookie
                for part in cookie_header.split(","):
                    if "session=" in part:
                        _set_cookie(part.split(";")[0].strip())
            return json.loads(resp.read().decode()), resp.status
    except urllib.error.HTTPError as e:
        body_text = e.read().decode() if e.fp else ""
        try:
            return json.loads(body_text), e.code
        except Exception:
            return {"error": body_text}, e.code
    except Exception as e:
        return {"error": str(e)}, 0


def setup_auth():
    """Register + login test user."""
    global COOKIE
    # Try login first
    data, code = _request("POST", "/auth/login",
                          {"username": "e2e_test_user", "password": "test1234"})
    if code == 200 and data.get("status") == "ok":
        print(f"  ✅ Login OK (user: e2e_test_user)")
        return True

    # Register
    data, code = _request("POST", "/auth/register",
                          {"username": "e2e_test_user", "password": "test1234",
                           "display_name": "E2E Test"})
    if code == 200:
        # Login after register
        data, code = _request("POST", "/auth/login",
                              {"username": "e2e_test_user", "password": "test1234"})
        if code == 200:
            print(f"  ✅ Register + Login OK")
            return True

    print(f"  ❌ Auth failed: {data}")
    return False


def run_l1():
    """L1: Pipeline availability — each mode returns non-empty answer."""
    print("\n═══ L1: 管道可用性 ═══")
    results = []
    for name, question, mode, web_search, min_len in L1_TESTS:
        start = time.time()
        data, code = _request("POST", "/ask", {
            "question": question, "mode": mode, "web_search": web_search
        })
        elapsed = time.time() - start
        answer = data.get("final_answer", "")
        confidence = data.get("confidence", 0)
        latency = data.get("latency_ms", 0)

        passed = (
            code == 200
            and len(answer) >= min_len
            and confidence > 0
            and "系统错误" not in answer
        )
        status = "✅" if passed else "❌"
        print(f"  {status} {name}: mode={mode}, len={len(answer)}, "
              f"conf={confidence}, latency={latency}ms, http={elapsed:.1f}s")
        if not passed:
            print(f"     answer: {answer[:200]}")
        results.append((name, passed, answer))
    return results


def run_l2():
    """L2: Quality baseline — answers are on-topic and structured."""
    print("\n═══ L2: 质量基线 ═══")
    results = []
    for name, question, mode, must_contain, must_not in L2_QUALITY_CHECKS:
        data, code = _request("POST", "/ask", {
            "question": question, "mode": mode, "web_search": False
        })
        answer = data.get("final_answer", "")

        contains_ok = any(kw in answer for kw in must_contain) if must_contain else True
        not_contains_ok = all(kw not in answer for kw in must_not) if must_not else True
        passed = code == 200 and contains_ok and not_contains_ok and len(answer) > 20

        status = "✅" if passed else "❌"
        print(f"  {status} {name}: contains_check={contains_ok}, len={len(answer)}")
        if not passed:
            print(f"     answer: {answer[:300]}")
        results.append((name, passed, answer))
    return results


def run_l4():
    """L4: Boundary and stress — bad inputs don't crash the system."""
    print("\n═══ L4: 边界压力 ═══")
    results = []
    for name, question, mode, expect_error in L4_BOUNDARY_TESTS:
        data, code = _request("POST", "/ask", {
            "question": question, "mode": mode, "web_search": False
        }, timeout=30)

        if expect_error:
            # Should return an error, not crash (4xx, not 5xx)
            passed = 400 <= code < 500 or (code == 200 and data.get("confidence", 1) == 0)
        else:
            answer = data.get("final_answer", "")
            passed = code == 200 and len(answer) > 0

        status = "✅" if passed else "❌"
        print(f"  {status} {name}: http={code}, expect_error={expect_error}")
        if not passed:
            print(f"     response: {json.dumps(data, ensure_ascii=False)[:200]}")
        results.append((name, passed))
    return results


def main():
    print("Synthora E2E Test Suite")
    print(f"Target: {API_BASE}")
    print()

    # Health check
    print("═══ Health Check ═══")
    data, code = _request("GET", "/health")
    if code != 200 or data.get("status") != "ok":
        print(f"  ❌ Backend unreachable: {data}")
        sys.exit(1)
    print(f"  ✅ Backend OK: v{data.get('version')}, "
          f"{data.get('models_available')}/{data.get('models_total')} models")

    # Auth
    print("\n═══ Auth ═══")
    if not setup_auth():
        sys.exit(1)

    # Run test levels
    l1 = run_l1()
    l2 = run_l2()
    l4 = run_l4()

    # Summary
    print("\n═══ SUMMARY ═══")
    all_results = [(n, p) for n, p, *_ in l1 + l2] + l4
    passed = sum(1 for _, p in all_results if p)
    total = len(all_results)
    failed = [n for n, p in all_results if not p]
    print(f"  {passed}/{total} passed")
    if failed:
        print(f"  Failed: {', '.join(failed)}")
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
