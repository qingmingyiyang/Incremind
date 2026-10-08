"""E2E test for X-Worker-Secret auth middleware.

Usage: CHRIPTMAS_ROOT=/path/to/repo runtime/python.exe test_auth_e2e.py

Starts the backend on port 8002 with a known worker secret,
then runs 6 test scenarios against it.
"""
import asyncio
import os
import sys
import time
import threading

import httpx
import uvicorn

# Add src/ to path so "backend.api.app" resolves correctly
SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # src/
sys.path.insert(0, SRC_DIR)

from backend.api.app import app

TEST_SECRET = "test-e2e-worker-secret-chriptmas"
TEST_PORT = 8002
BASE_URL = f"http://127.0.0.1:{TEST_PORT}"


def start_backend():
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    uvicorn.run(app, host="127.0.0.1", port=TEST_PORT, log_level="critical")


def test():
    os.environ["CHRIPTMAS_WORKER_SECRET"] = TEST_SECRET

    # Start backend in a thread
    t = threading.Thread(target=start_backend, daemon=True)
    t.start()
    time.sleep(3)

    results = []

    # Test 1: Health endpoint (exempt from auth) — should pass
    try:
        r = httpx.get(f"{BASE_URL}/api/health", timeout=5)
        ok = r.status_code == 200
        results.append(("Health (exempt)", "200", str(r.status_code), ok))
    except Exception as e:
        results.append(("Health (exempt)", "200", str(e), False))

    # Test 2: Non-health endpoint WITHOUT auth header — should get 403
    try:
        r = httpx.get(f"{BASE_URL}/api/series/default", timeout=5)
        ok = r.status_code == 403
        results.append(("GET no auth", "403", str(r.status_code), ok))
    except Exception as e:
        results.append(("GET no auth", "403", str(e), False))

    # Test 3: Non-health endpoint WITH WRONG auth header — should get 403
    try:
        r = httpx.get(
            f"{BASE_URL}/api/series/default",
            headers={"X-Worker-Secret": "wrong-secret"},
            timeout=5,
        )
        ok = r.status_code == 403
        results.append(("GET wrong auth", "403", str(r.status_code), ok))
    except Exception as e:
        results.append(("GET wrong auth", "403", str(e), False))

    # Test 4: Non-health endpoint WITH CORRECT auth header — should get 200
    try:
        r = httpx.get(
            f"{BASE_URL}/api/series/default",
            headers={"X-Worker-Secret": TEST_SECRET},
            timeout=5,
        )
        ok = r.status_code == 200
        results.append(("GET correct auth", "200", str(r.status_code), ok))
    except Exception as e:
        results.append(("GET correct auth", "200", str(e), False))

    # Test 5: POST without auth — should get 403
    try:
        r = httpx.post(
            f"{BASE_URL}/api/series/default/intake",
            json={"title": "test_e2e", "body": "test body"},
            timeout=5,
        )
        ok = r.status_code == 403
        results.append(("POST no auth", "403", str(r.status_code), ok))
    except Exception as e:
        results.append(("POST no auth", "403", str(e), False))

    # Test 6: POST with correct auth — should get 201/200
    try:
        r = httpx.post(
            f"{BASE_URL}/api/series/default/intake",
            json={"title": "test_e2e", "body": "test body"},
            headers={"X-Worker-Secret": TEST_SECRET},
            timeout=5,
        )
        ok = r.status_code in (200, 201)
        results.append(("POST correct auth", "200/201", str(r.status_code), ok))
    except Exception as e:
        results.append(("POST correct auth", "200/201", str(e), False))

    # Print results
    print(f"\n{'Test':<25} {'Expected':<12} {'Actual':<12} {'Result':<8}")
    print("-" * 57)
    passed = 0
    for name, expected, actual, ok in results:
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        print(f"{name:<25} {expected:<12} {actual:<12} {status:<8}")

    print(f"\n{passed}/{len(results)} tests passed")
    return passed == len(results)


if __name__ == "__main__":
    success = test()
    sys.exit(0 if success else 1)
