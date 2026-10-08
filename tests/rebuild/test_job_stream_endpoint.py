"""GET /api/rebuild/jobs/{job_id}/stream SSE 端点测试。

验证 SSE 端点已挂载、返回 text/event-stream、推送 job_updated 事件、终态后关闭。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import sys
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from backend.api.app import create_app  # noqa: E402
from backend.api.routes.product.jobs import _stream_job_progress  # noqa: E402
from core.job_runner import (  # noqa: E402
    JobStepResult,
    ObjectStoreJobRepository,
    RoutedJobRepository,
    SQLiteJobRuntimeLifecycle,
    SQLiteJobStore,
)
from core.storage_provider import JsonObjectStore  # noqa: E402


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def _store(tmp_path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _seed_completed_job(tmp_path, job_id="job-sse-1", *, status: str = "completed") -> str:
    store = _store(tmp_path)
    store.write("jobs", job_id, {
        "job_id": job_id,
        "job_type": "workbench_auto_intake",
        "status": status,
        "source_id": "src-1",
        "attempt": 1,
        "max_attempts": 3,
        "steps": [
            {"name": "classify", "status": "completed", "progress": 100},
            {"name": "structure", "status": "completed", "progress": 100},
        ],
        "created_at": "2026-07-04T00:00:00Z",
        "updated_at": "2026-07-04T00:01:00Z",
    }, expected_revision=0)
    return job_id


def test_job_stream_returns_event_stream_for_existing_job(tmp_path) -> None:
    """已存在的 job 应返回 text/event-stream 并推送 job_updated 事件。"""
    job_id = _seed_completed_job(tmp_path)
    client = _client(tmp_path)
    with client.stream("GET", f"/api/rebuild/jobs/{job_id}/stream") as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")
        assert response.headers.get("cache-control") == "no-store"
        # 收集前几个字节确认有 event: job_updated
        chunks = []
        for chunk in response.iter_text():
            chunks.append(chunk)
            if len(chunks) >= 2:
                break
        body = "".join(chunks)
        assert "event: job_updated" in body
        assert '"status":"completed"' in body or '"status": "completed"' in body


def test_job_stream_returns_404_for_missing_job(tmp_path) -> None:
    """不存在的 job 应返回 404。"""
    client = _client(tmp_path)
    response = client.get("/api/rebuild/jobs/nonexistent-job/stream")
    assert response.status_code == 404


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_job_stream_closes_after_terminal_status(tmp_path, status: str) -> None:
    """普通终态 job 的 SSE 流应在推送最终事件后直接关闭。"""
    job_id = _seed_completed_job(tmp_path, status=status)
    client = _client(tmp_path)
    with client.stream("GET", f"/api/rebuild/jobs/{job_id}/stream") as response:
        assert response.status_code == 200
        # 终态流应自然结束（iter_text 耗尽）
        body = "".join(response.iter_text())
        assert "event: job_updated" in body
        assert "event: manual_resolution_required" not in body


class _SlowStreamHandler:
    job_type = "extract_memory"

    def run_step(self, step_name, job):
        time.sleep(0.7)
        return JobStepResult(staged_outputs=(f"crp://default/jobs/{job['id']}/{step_name}",))


def _sse_payload(event: str) -> dict[str, object]:
    data_line = next(line for line in event.splitlines() if line.startswith("data: "))
    return json.loads(data_line.removeprefix("data: "))


def _sse_name(event: str) -> str:
    event_line = next(line for line in event.splitlines() if line.startswith("event: "))
    return event_line.removeprefix("event: ")


class _FrozenLegacyRepository:
    def __init__(self, *, legacy_status: str, with_candidate_child: bool = False) -> None:
        self._job = {
            "id": f"job-legacy-{legacy_status}",
            "job_type": "extract_memory",
            "status": "legacy_unknown",
            "legacy_status": legacy_status,
            "execution_version": "legacy-v1-readonly",
            "execution_action": {
                "kind": "none",
                "enabled": False,
                "reason": "legacy_history",
            },
            "events": [],
            "steps": [],
            "updated_at": "2026-07-11T00:00:00Z",
        }
        self._child: dict[str, object] | None = None
        if with_candidate_child:
            child_id = f"job-legacy-{legacy_status}-candidate"
            self._job["staged_outputs"] = [{"kind": "candidate_job", "object_id": child_id}]
            self._child = {
                "id": child_id,
                "parent_job_id": self._job["id"],
                "job_type": "extract_memory_candidate",
                "status": "running",
                "events": [],
                "steps": [],
                "updated_at": "2026-07-11T00:00:01Z",
            }

    def get(self, job_id: str) -> dict[str, object] | None:
        if self._child is not None and job_id == self._child["id"]:
            return dict(self._child)
        if job_id != self._job["id"]:
            return None
        return dict(self._job)


@pytest.mark.parametrize("legacy_status", ["pending", "running"])
def test_legacy_unknown_stream_emits_manual_resolution_then_closes(legacy_status: str) -> None:
    repository = _FrozenLegacyRepository(legacy_status=legacy_status)
    job_id = f"job-legacy-{legacy_status}"

    async def observe() -> tuple[str, str]:
        stream = _stream_job_progress(repository=repository, job_id=job_id)
        first = await anext(stream)
        second = await anext(stream)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        return first, second

    first, second = asyncio.run(observe())

    assert _sse_name(first) == "job_updated"
    assert _sse_payload(first)["status"] == "legacy_unknown"
    assert _sse_name(second) == "manual_resolution_required"
    assert _sse_payload(second) == {
        "job_id": job_id,
        "status": "legacy_unknown",
        "legacy_status": legacy_status,
        "reason": "legacy_execution_result_unknown",
        "action": {
            "kind": "review_history",
            "mode": "read_only",
            "enabled": True,
        },
    }


def test_legacy_unknown_parent_ignores_child_aggregate_for_manual_resolution() -> None:
    repository = _FrozenLegacyRepository(legacy_status="running", with_candidate_child=True)
    job_id = "job-legacy-running"

    async def observe() -> tuple[str, str]:
        stream = _stream_job_progress(repository=repository, job_id=job_id)
        first = await anext(stream)
        second = await anext(stream)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        return first, second

    first, second = asyncio.run(observe())

    assert _sse_payload(first)["aggregate_status"] == "running"
    assert _sse_name(second) == "manual_resolution_required"
    assert _sse_payload(second)["legacy_status"] == "running"

