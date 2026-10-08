"""GET /api/rebuild/developer-studio/logs 端点测试。

验证 3 类日志（HTTP access / Job execution / LLM call）的聚合、过滤、分页、脱敏。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import sys
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from backend.api.app import create_app  # noqa: E402
from core.storage_provider import JsonObjectStore  # noqa: E402


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def _store(tmp_path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _seed_job(tmp_path, *, job_id: str, status: str, job_type: str = "workbench_auto_intake", error: str = "") -> None:
    store = _store(tmp_path)
    store.write("jobs", job_id, {
        "id": job_id,
        "job_id": job_id,
        "job_type": job_type,
        "status": status,
        "source_id": "src-1",
        "attempt": 1,
        "max_attempts": 3,
        "steps": [],
        "created_at": "2026-07-03T10:00:00+00:00",
        "updated_at": "2026-07-03T10:01:00+00:00",
        "error": error,
    }, expected_revision=None)


def _seed_model_result(
    tmp_path,
    *,
    result_id: str,
    status: str = "completed",
    model_name: str = "deepseek-chat",
    elapsed_ms: int = 1200,
    error_message: str = "",
    completed_at: str = "2026-07-03T14:32:01+00:00",
) -> None:
    store = _store(tmp_path)
    # 先写一个 model_request 以满足 model_result 验证（实际聚合只读 model_results）
    request_id = f"req-{result_id}"
    store.write("model_requests", request_id, {
        "id": request_id,
        "project_id": "default",
        "capability": "text_generation",
        "provider_preference": {"mode": "local_only", "allow_remote": False},
        "payload": {
            "kind": "answer",
            "recall_result_id": "recall-1",
            "input_refs": [{"kind": "recall_result", "id": "recall-1"}],
            "source_refs": [{"kind": "source", "id": "src-1"}],
        },
        "privacy": {"scope": "local_only", "allow_remote": False},
        "response_schema": {"type": "text"},
    }, expected_revision=None)

    error_block = None
    if status != "completed":
        error_block = {
            "code": status,
            "message": error_message,
            "retryable": False,
        }

    store.write("model_results", result_id, {
        "schema_version": "1.0.0",
        "id": result_id,
        "request_id": request_id,
        "status": status,
        "provider": {
            "provider_id": "deepseek" if status == "completed" else None,
            "mode": "local" if status == "completed" else "none",
            "remote": False,
            "config_version": 1 if status == "completed" else None,
        },
        "model": {
            "name": model_name if status == "completed" else None,
            "version": "2026-06-30" if status == "completed" else None,
            "capability": "text_generation" if status == "completed" else "none",
        },
        "output": {
            "kind": "text" if status == "completed" else "none",
            "content": "lorem ipsum" if status == "completed" else None,
            "structured": None,
            "output_refs": [],
        },
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "cost_usd": 0,
            "currency": "none",
        },
        "latency": {
            "started_at": completed_at,
            "completed_at": completed_at,
            "elapsed_ms": elapsed_ms,
            "timed_out": False,
        },
        "error": error_block,
        "safety": {
            "blocked": status != "completed",
            "categories": ["privacy"] if status == "privacy_blocked" else (["policy"] if status == "safety_blocked" else []),
            "redaction_applied": False,
            "output_truncated": False,
        },
        "created_at": completed_at,
    }, expected_revision=None)


# ─── 基础返回结构 ───

def test_logs_returns_200_with_empty_store(tmp_path) -> None:
    """空数据时也应返回 200，logs=[] 且 total=0。"""
    client = _client(tmp_path)
    response = client.get("/api/rebuild/developer-studio/logs")
    assert response.status_code == 200
    payload = response.json()
    assert payload["logs"] == []
    assert payload["total"] == 0
    assert payload["filters"]["component"] == "all"
    assert payload["filters"]["status"] == "all"
    assert payload["filters"]["limit"] == 50
    assert payload["filters"]["offset"] == 0


def test_logs_no_cache(tmp_path) -> None:
    """响应应携带 no-store。"""
    client = _client(tmp_path)
    response = client.get("/api/rebuild/developer-studio/logs")
    assert response.headers.get("Cache-Control") == "no-store"


def test_logs_rejects_post(tmp_path) -> None:
    """端点只支持 GET，POST 应返回 405。"""
    client = _client(tmp_path)
    response = client.post("/api/rebuild/developer-studio/logs")
    assert response.status_code == 405


# ─── LLM 日志聚合 ───

def test_logs_includes_llm_completed_result(tmp_path) -> None:
    """已完成的 model_result 应聚合并标记为 ok。"""
    _seed_model_result(tmp_path, result_id="mr-1", status="completed", model_name="deepseek-chat", elapsed_ms=1200)
    client = _client(tmp_path)
    response = client.get("/api/rebuild/developer-studio/logs")
    assert response.status_code == 200
    logs = response.json()["logs"]
    assert any(log["component"] == "llm" and log["status"] == "ok" for log in logs)
    llm_log = next(log for log in logs if log["component"] == "llm")
    assert llm_log["model"] == "deepseek-chat"
    assert llm_log["duration"] == "1.2s"
    assert llm_log["request_id"] == "req-mr-1"


def test_logs_includes_llm_failed_result(tmp_path) -> None:
    """privacy_blocked/safety_blocked 的 model_result 应标记为 fail。"""
    _seed_model_result(
        tmp_path,
        result_id="mr-fail-1",
        status="privacy_blocked",
        error_message="隐私策略阻止本次模型调用。",
    )
    client = _client(tmp_path)
    response = client.get("/api/rebuild/developer-studio/logs")
    assert response.status_code == 200
    logs = response.json()["logs"]
    failed = [log for log in logs if log["component"] == "llm" and log["status"] == "fail"]
    assert len(failed) == 1
    assert "隐私策略" in failed[0]["error"]


# ─── Job 日志聚合 ───

def test_logs_includes_job_completed(tmp_path) -> None:
    """completed job 应标记为 ok。"""
    _seed_job(tmp_path, job_id="job-ok-1", status="completed", job_type="workbench_auto_intake")
    client = _client(tmp_path)
    response = client.get("/api/rebuild/developer-studio/logs")
    assert response.status_code == 200
    logs = response.json()["logs"]
    job_logs = [log for log in logs if log["component"] == "job"]
    assert len(job_logs) == 1
    assert job_logs[0]["status"] == "ok"
    assert "workbench_auto_intake" in job_logs[0]["stage"]


def test_logs_includes_job_failed(tmp_path) -> None:
    """failed job 应标记为 fail，error 文本应保留。"""
    _seed_job(tmp_path, job_id="job-fail-1", status="failed", error="下载超时")
    client = _client(tmp_path)
    response = client.get("/api/rebuild/developer-studio/logs")
    assert response.status_code == 200
    logs = response.json()["logs"]
    failed = [log for log in logs if log["component"] == "job" and log["status"] == "fail"]
    assert len(failed) == 1
    assert "下载超时" in failed[0]["error"]


# ─── HTTP access 日志聚合 ───

def test_logs_includes_http_access_entries(tmp_path) -> None:
    """触发一次 GET 请求后，access log 应包含该请求。"""
    client = _client(tmp_path)
    # 先发一次任意 GET 请求以触发 access log 采集
    client.get("/api/rebuild/developer-studio/logs")
    # 再读 logs 看 access log 是否被记录
    response = client.get("/api/rebuild/developer-studio/logs?component=http")
    assert response.status_code == 200
    logs = response.json()["logs"]
    http_logs = [log for log in logs if log["component"] == "http"]
    assert len(http_logs) >= 1
    # 应包含 GET /api/rebuild/developer-studio/logs 的记录
    assert any("developer-studio/logs" in log["stage"] for log in http_logs)


def test_logs_skip_health_check_path(tmp_path) -> None:
    """/api/health 不应被记录。"""
    client = _client(tmp_path)
    # 触发一次 health 请求
    try:
        client.get("/api/health")
    except Exception:
        pass  # health 端点可能不存在，仅用于触发中间件
    response = client.get("/api/rebuild/developer-studio/logs?component=http")
    logs = response.json()["logs"]
    assert not any("/api/health" in log["stage"] for log in logs)


# ─── 过滤 ───

def test_logs_filter_by_component_llm(tmp_path) -> None:
    """component=llm 应只返回 LLM 日志。"""
    _seed_model_result(tmp_path, result_id="mr-1", status="completed")
    _seed_job(tmp_path, job_id="job-1", status="completed")
    client = _client(tmp_path)
    response = client.get("/api/rebuild/developer-studio/logs?component=llm")
    assert response.status_code == 200
    logs = response.json()["logs"]
    assert all(log["component"] == "llm" for log in logs)
    assert len(logs) == 1


def test_logs_filter_by_component_job(tmp_path) -> None:
    """component=job 应只返回 Job 日志。"""
    _seed_model_result(tmp_path, result_id="mr-1", status="completed")
    _seed_job(tmp_path, job_id="job-1", status="completed")
    client = _client(tmp_path)
    response = client.get("/api/rebuild/developer-studio/logs?component=job")
    assert response.status_code == 200
    logs = response.json()["logs"]
    # 注意：access log 中也可能有 component=job 的请求，但 job 集合只有 1 条
    assert all(log["component"] == "job" for log in logs)
    # 至少 1 条来自 jobs 集合
    assert any("workbench_auto_intake" in log["stage"] for log in logs)


def test_logs_filter_by_status_fail(tmp_path) -> None:
    """status=fail 应只返回失败日志。"""
    _seed_model_result(tmp_path, result_id="mr-ok", status="completed")
    _seed_model_result(tmp_path, result_id="mr-fail", status="privacy_blocked", error_message="blocked")
    _seed_job(tmp_path, job_id="job-ok", status="completed")
    _seed_job(tmp_path, job_id="job-fail", status="failed", error="err")
    client = _client(tmp_path)
    response = client.get("/api/rebuild/developer-studio/logs?status=fail")
    assert response.status_code == 200
    logs = response.json()["logs"]
    assert all(log["status"] == "fail" for log in logs)
    # 至少包含 LLM fail 和 Job fail
    components = {log["component"] for log in logs}
    assert "llm" in components
    assert "job" in components


# ─── 分页 ───

def test_logs_pagination_limit_and_offset(tmp_path) -> None:
    """limit + offset 分页。"""
    # 写 3 条 LLM 日志
    for i in range(3):
        _seed_model_result(
            tmp_path,
            result_id=f"mr-{i}",
            status="completed",
            completed_at=f"2026-07-0{i+1}T10:00:00+00:00",
        )
    client = _client(tmp_path)
    # 第一页 limit=2
    response = client.get("/api/rebuild/developer-studio/logs?component=llm&limit=2&offset=0")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert len(payload["logs"]) == 2
    # 第二页 offset=2
    response = client.get("/api/rebuild/developer-studio/logs?component=llm&limit=2&offset=2")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert len(payload["logs"]) == 1


def test_logs_invalid_component_returns_400(tmp_path) -> None:
    """非法 component 应返回 400。"""
    client = _client(tmp_path)
    response = client.get("/api/rebuild/developer-studio/logs?component=invalid")
    assert response.status_code == 400


def test_logs_invalid_limit_returns_400(tmp_path) -> None:
    """非法 limit 应返回 400。"""
    client = _client(tmp_path)
    response = client.get("/api/rebuild/developer-studio/logs?limit=abc")
    assert response.status_code == 400


def test_logs_invalid_status_returns_400(tmp_path) -> None:
    """非法 status 应返回 400。"""
    client = _client(tmp_path)
    response = client.get("/api/rebuild/developer-studio/logs?status=warn")
    assert response.status_code == 400


# ─── 脱敏 ───

def test_logs_sanitize_api_key_in_error(tmp_path) -> None:
    """error 文本中的 api_key 应被遮蔽。"""
    sensitive_error = "请求失败，api_key=sk-1234567890abcdef 被拒绝"
    _seed_model_result(
        tmp_path,
        result_id="mr-sensitive",
        status="privacy_blocked",
        error_message=sensitive_error,
    )
    client = _client(tmp_path)
    response = client.get("/api/rebuild/developer-studio/logs?component=llm")
    assert response.status_code == 200
    logs = response.json()["logs"]
    assert len(logs) == 1
    error_text = logs[0]["error"]
    # 原始 api_key 值不应出现
    assert "sk-1234567890abcdef" not in error_text
    # 应包含 REDACTED 标记
    assert "REDACTED" in error_text


def test_logs_sanitize_cookie_in_job_error(tmp_path) -> None:
    """job error 中的 cookie 应被遮蔽。"""
    sensitive_error = "下载失败，cookie: SESSDATA=abc123 失效"
    _seed_job(tmp_path, job_id="job-sensitive", status="failed", error=sensitive_error)
    client = _client(tmp_path)
    response = client.get("/api/rebuild/developer-studio/logs?component=job")
    assert response.status_code == 200
    logs = response.json()["logs"]
    assert len(logs) == 1
    error_text = logs[0]["error"]
    assert "abc123" not in error_text
    assert "REDACTED" in error_text


def test_logs_no_request_body_or_headers_captured(tmp_path) -> None:
    """access log 不应包含请求体或请求头内容。"""
    client = _client(tmp_path)
    # 发一个带"敏感"头的请求
    client.get(
        "/api/rebuild/developer-studio/logs",
        headers={"X-Worker-Secret": "should-not-appear"},
    )
    response = client.get("/api/rebuild/developer-studio/logs?component=http")
    logs = response.json()["logs"]
    # 不应包含 secret 值
    for log in logs:
        assert "should-not-appear" not in log["stage"]
        assert "should-not-appear" not in log.get("error", "")
