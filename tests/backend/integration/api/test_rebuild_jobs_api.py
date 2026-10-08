from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.routes.product import jobs as product_jobs
from core.job_runner import ObjectStoreJobRepository, SQLiteJobStore
from core.storage_provider import JsonObjectStore


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def _store(tmp_path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _job(
    job_id: str = "job-api-1",
    *,
    job_type: str = "workbench_auto_intake",
    status: str = "failed",
    source_id: str = "source-api-1",
    attempt: int = 1,
    steps=None,
    checkpoint=None,
) -> dict[str, object]:
    if steps is None:
        steps = [
            {"name": "step_a", "status": "completed", "error": None},
            {"name": "step_b", "status": "failed", "error": {"code": "provider_missing", "message": "ASR 未启用"}},
        ]
    return {
        "schema_version": "1.0.0",
        "id": job_id,
        "source_id": source_id,
        "job_type": job_type,
        "idempotency_key": f"intake-{job_id}",
        "status": status,
        "attempt": attempt,
        "max_attempts": 3,
        "lease": None,
        "progress": {"current": 1, "total": 2, "percent": 50, "message": None},
        "steps": steps,
        "error": {"code": "provider_missing", "message": "ASR 未启用", "retryable": True, "failed_step": "step_b", "details": {}},
        "checkpoint": checkpoint,
        "staged_outputs": [{"kind": "source", "uri": f"crp://ns/sources/{source_id}", "object_id": source_id, "published": True}],
        "published_outputs": [],
        "log_refs": [f"crp://ns/logs/jobs/{job_id}/orchestrate.jsonl"],
        "created_at": "2026-07-04T09:00:00+08:00",
        "updated_at": "2026-07-04T09:00:00+08:00",
    }


def _checkpoint() -> dict[str, object]:
    return {
        "resume_step": "step_b",
        "checkpoint_uri": "crp://ns/jobs/job-api-1/checkpoint",
        "state_hash": "sha256:" + "a" * 64,
        "updated_at": "2026-07-04T09:30:00+08:00",
    }


def _seed_jobs(store: JsonObjectStore) -> None:
    repo = ObjectStoreJobRepository(store)
    repo.save(_job("job-api-1", job_type="workbench_auto_intake", status="failed", source_id="source-a"))
    repo.save(_job("job-api-2", job_type="workbench_auto_intake", status="completed", source_id="source-b"))
    repo.save(_job("job-api-3", job_type="capture", status="failed", source_id="source-a"))


# ── GET /api/rebuild/jobs/{job_id} ──

def test_job_detail_returns_record(tmp_path) -> None:
    _seed_jobs(_store(tmp_path))
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/jobs/job-api-1")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "job-api-1"
    assert body["status"] == "failed"
    assert body["job_type"] == "workbench_auto_intake"
    assert len(body["steps"]) == 2
    assert body["execution"]["schema_version"] == 1
    assert body["execution"]["authority"] == "legacy_history"
    assert body["execution"]["nodes"] == []
    assert body["execution_version"] == "legacy-v1-readonly"
    assert body["execution_action"] == {
        "kind": "none", "enabled": False, "reason": "legacy_history",
    }


def test_job_detail_keeps_flat_projection_when_effect_tree_read_fails(tmp_path, monkeypatch) -> None:
    _seed_jobs(_store(tmp_path))

    def unavailable(*_args, **_kwargs):
        raise OSError("test-only unavailable Effect database")

    monkeypatch.setattr(product_jobs, "load_effect_execution_projection", unavailable)
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/jobs/job-api-1")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "job-api-1"
    assert body["status"] == "failed"
    assert body["execution"] == {
        "schema_version": 1,
        "authority": "core_effect_log",
        "available": False,
        "nodes": [],
    }


def test_job_detail_returns_404_when_not_found(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/jobs/missing")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


# ── GET /api/rebuild/jobs ──

def test_job_list_returns_all_by_default(tmp_path) -> None:
    _seed_jobs(_store(tmp_path))
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/jobs")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert len(body["jobs"]) == 3


def test_job_list_filters_by_job_type(tmp_path) -> None:
    _seed_jobs(_store(tmp_path))
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/jobs?job_type=capture")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["jobs"][0]["job_type"] == "capture"


def test_job_list_filters_by_status(tmp_path) -> None:
    _seed_jobs(_store(tmp_path))
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/jobs?status=failed")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert all(job["status"] == "failed" for job in body["jobs"])


def test_job_list_filters_legacy_unknown_without_mutating_authority(tmp_path) -> None:
    ObjectStoreJobRepository(_store(tmp_path)).save(
        _job("job-api-legacy-pending", status="pending")
    )
    database = tmp_path / ".rebuild-data" / "jobs.sqlite3"

    with _client(tmp_path) as client:
        with sqlite3.connect(database) as monitor:
            before = monitor.execute("PRAGMA data_version").fetchone()[0]
            response = client.get("/api/rebuild/jobs?status=legacy_unknown")
            after = monitor.execute("PRAGMA data_version").fetchone()[0]

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["jobs"][0]["id"] == "job-api-legacy-pending"
    assert body["jobs"][0]["status"] == "legacy_unknown"
    assert body["jobs"][0]["legacy_status"] == "pending"
    assert body["jobs"][0]["history_source"] == "legacy_job_history"
    assert before == after


def test_job_list_filters_by_source_id(tmp_path) -> None:
    _seed_jobs(_store(tmp_path))
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/jobs?source_id=source-a")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert all(job["source_id"] == "source-a" for job in body["jobs"])


def test_job_list_rejects_invalid_status(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/jobs?status=invalid")
    assert response.status_code == 400
    assert "invalid status" in response.json()["detail"]


def test_job_list_clamps_limit(tmp_path) -> None:
    _seed_jobs(_store(tmp_path))
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/jobs?limit=2")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["filters"]["limit"] == 2


def test_job_detail_and_list_are_readonly_after_explicit_startup_import(tmp_path) -> None:
    _seed_jobs(_store(tmp_path))
    database = tmp_path / ".rebuild-data" / "jobs.sqlite3"
    with _client(tmp_path) as client:
        with sqlite3.connect(database) as monitor:
            before = monitor.execute("PRAGMA data_version").fetchone()[0]
            detail = client.get("/api/rebuild/jobs/job-api-1")
            listed = client.get("/api/rebuild/jobs")
            after = monitor.execute("PRAGMA data_version").fetchone()[0]

    assert detail.status_code == listed.status_code == 200
    assert before == after


def test_job_list_rejects_invalid_limit(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/jobs?limit=abc")
    assert response.status_code == 400
    assert "invalid limit" in response.json()["detail"]


@pytest.mark.parametrize("action", ("retry", "resume", "cancel"))
def test_legacy_job_write_routes_are_not_mounted(tmp_path, action: str) -> None:
    store = _store(tmp_path)
    _seed_jobs(store)

    with _client(tmp_path) as client:
        sqlite = SQLiteJobStore(tmp_path / ".rebuild-data" / "jobs.sqlite3")
        before = sqlite.read("job-api-1")
        response = client.post(
            f"/api/rebuild/jobs/job-api-1/{action}",
            json={"request_id": "retired-command"},
        )
        after = sqlite.read("job-api-1")

    assert response.status_code == 404
    assert before is not None and after is not None
    assert before.revision == after.revision
    assert before.payload == after.payload
