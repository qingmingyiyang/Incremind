from __future__ import annotations

from types import SimpleNamespace
import time

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.job_runtime import configured_sqlite_job_types
from backend.api.routes.product.job_lifecycle import recover_rebuild_job_lifecycle
from core.aggregate_repository_factory import STRUCTURED_DATABASE_NAME
from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.effect_log import EffectLog, EffectState
from core.job_runner import (
    ObjectStoreJobRepository,
    SQLiteJobStore,
    job_execution_operation_id,
)
from core.product_core import (
    CandidateMemoryJobInput,
    ReadSourceTextContent,
    build_candidate_memory_job,
)
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def _job(job_id: str, *, status: str = "failed") -> dict[str, object]:
    return {
        "id": job_id,
        "job_type": "extract_memory",
        "status": status,
        "attempt": 1,
        "steps": [{"name": "publish_atom", "status": status, "error": None}],
        "lease": None,
        "checkpoint": {"resume_step": "publish_atom"},
        "staged_outputs": [],
        "published_outputs": [],
        "error": None,
        "updated_at": "2026-07-11T00:00:00Z",
    }


def _wait_for_status(client: TestClient, job_id: str, status: str) -> dict[str, object] | None:
    deadline = time.monotonic() + 3.0
    current = None
    while time.monotonic() < deadline:
        current = client.get(f"/api/rebuild/jobs/{job_id}").json()
        if current.get("status") == status:
            return current
        time.sleep(0.02)
    return current


def test_empty_migration_allowlist_preserves_empty_job_api_with_durable_sqlite_authority(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CHRIPTMAS_SQLITE_JOB_TYPES", raising=False)
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/jobs")

    assert response.status_code == 200
    assert response.json()["jobs"] == []
    assert (tmp_path / ".rebuild-data" / "jobs.sqlite3").is_file()


def test_missing_media_hands_job_returns_json_404_without_legacy_probe(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CHRIPTMAS_SQLITE_JOB_TYPES", raising=False)
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/jobs/media_hands:xhs-source:analyze_source")

    assert response.status_code == 404
    assert response.json() == {"detail": "job not found: media_hands:xhs-source:analyze_source"}


def test_legacy_job_allowlist_environment_cannot_change_effect_authority(monkeypatch) -> None:
    monkeypatch.setenv("CHRIPTMAS_SQLITE_JOB_TYPES", "media_hands")
    assert configured_sqlite_job_types() == frozenset({
        "extract_memory",
        "extract_memory_candidate",
    })







def test_sidecar_shutdown_stops_shared_sqlite_job_lifecycle(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CHRIPTMAS_SQLITE_JOB_TYPES", "extract_memory_candidate")
    client = _client(tmp_path)

    with client:
        assert getattr(client.app.state, "rebuild_job_lifecycle", None) is None

    assert getattr(client.app.state, "rebuild_job_lifecycle", None) is None




def test_auto_intake_candidate_requires_review_and_second_confirmation_before_publication(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CHRIPTMAS_SQLITE_JOB_TYPES", "extract_memory_candidate")

    with _client(tmp_path) as client:
        intake = client.post(
            "/api/rebuild/workbench/auto-intake",
            json={"content": "候选记忆必须经过人工确认和二次发布确认。", "add_to_knowledge_base": True},
        )
        assert intake.status_code == 201, intake.text
        parent_id = intake.json()["job_id"]
        deadline = time.monotonic() + 3.0
        child = None
        while time.monotonic() < deadline:
            parent = client.get(f"/api/rebuild/jobs/{parent_id}").json()
            children = parent.get("child_jobs", [])
            if children and children[0].get("status") == "completed":
                child = children[0]
                break
            time.sleep(0.02)
        assert child is not None, parent
        candidate_output = child["staged_outputs"][0]
        candidate_id = candidate_output["object_id"]

        detail = client.get(f"/api/rebuild/memory-candidates/{candidate_id}/review")
        promoted = client.post(
            f"/api/rebuild/memory-candidates/{candidate_id}/review",
            json={"action": "promote_to_atom", "reason": "用户确认进入草稿记忆。"},
        )
        repeated_review = client.post(
            f"/api/rebuild/memory-candidates/{candidate_id}/review",
            json={"action": "promote_to_atom", "reason": "不得重复确认。"},
        )
        staging_id = promoted.json().get("promoted_object_id")
        missing_confirmation = client.post(
            f"/api/rebuild/staging-atoms/{staging_id}/publication",
            json={"confirm": False, "reason": "未完成二次确认。"},
        )
        store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
        assert store.list("memory_atoms") == ()
        published = client.post(
            f"/api/rebuild/staging-atoms/{staging_id}/publication",
            json={"confirm": True, "reason": "用户二次确认发布长期记忆。"},
        )
        repeated_publish = client.post(
            f"/api/rebuild/staging-atoms/{staging_id}/publication",
            json={"confirm": True, "reason": "不得重复发布。"},
        )

    assert detail.status_code == 200
    assert detail.json()["candidate_status"] == "pending_review"
    assert detail.json()["available_actions"] == ["reject", "promote_to_atom", "withdraw"]
    assert promoted.status_code == 200
    assert promoted.json()["memory_publication_state"] == "staging_atom_created_not_published"
    assert repeated_review.status_code == 200
    assert missing_confirmation.status_code == 400
    assert published.status_code == 200
    assert published.json()["memory_publication_state"] == "published_with_rollback_ref"
    assert published.json()["rollback_ref"]
    assert repeated_publish.status_code == 200
    assert repeated_publish.json()["publication_id"] == published.json()["publication_id"]
    records = SQLiteStructuredRecordStore(
        tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    )
    assert len(records.list("memory_atoms")) == 1
    assert store.list("memory_atoms") == ()
    assert store.read("memory_candidates", candidate_id)["status"] == "promoted"
