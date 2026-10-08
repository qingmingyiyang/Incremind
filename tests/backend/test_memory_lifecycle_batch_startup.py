from __future__ import annotations

from fastapi import FastAPI

from backend.api.memory_lifecycle_batch_startup import recover_memory_lifecycle_batches
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.product_core.memory_lifecycle import GovernedMemoryLifecycle, MemoryBatchItem


NOW = "2026-08-30T10:00:00+00:00"


def _store(tmp_path):
    return build_rebuild_object_store(tmp_path)[0]


def _seed(store, object_id: str) -> None:
    store.write("memory_atoms", object_id, {
        "schema_version": "1.0.0", "id": object_id, "project_id": "project-a",
        "revision": 1, "content": object_id, "trust_status": "user_confirmed",
    }, expected_revision=0)


def _interrupted_operation(store, object_id: str) -> str:
    preview = GovernedMemoryLifecycle(store).preview_batch_soft_redact(
        project_id="project-a", items=(MemoryBatchItem("atom", object_id, 1, 1),),
        reason="confirmed cleanup", occurred_at=NOW,
    )
    operation_id = f"memory-batch-{preview.preview_token[:20]}"
    store.write("memory_lifecycle_batches", operation_id, {
        "schema_version": "1.0.0", "id": operation_id, "action": "soft_redact",
        "project_id": preview.project_id, "preview_token": preview.preview_token,
        "reason": preview.reason, "occurred_at": preview.occurred_at,
        "status": "applying", "completed": [],
        "items": [{
            "layer": "atom", "object_id": object_id,
            "expected_revision": 1, "expected_storage_revision": 1,
        }],
    }, expected_revision=0)
    return operation_id


def test_startup_resumes_confirmed_lifecycle_batch(tmp_path):
    store = _store(tmp_path)
    _seed(store, "memory-a")
    operation_id = _interrupted_operation(store, "memory-a")
    application = FastAPI()

    report = recover_memory_lifecycle_batches(application, tmp_path)

    assert report.scanned == 1 and report.resumed == 1 and report.failed == 0
    assert report.operation_ids == (operation_id,)
    assert store.read("memory_lifecycle_batches", operation_id)["status"] == "completed"
    assert store.read("memory_atoms", "memory-a")["lifecycle_status"] == "redacted"
    assert application.state.memory_lifecycle_batch_startup_recovery is report


def test_startup_resumes_interrupted_batch_undo(tmp_path):
    store = _store(tmp_path)
    _seed(store, "memory-a")
    operation_id = _interrupted_operation(store, "memory-a")
    lifecycle = GovernedMemoryLifecycle(store)
    lifecycle.resume_batch_soft_redact(operation_id=operation_id, confirm=True)
    operation = dict(store.read("memory_lifecycle_batches", operation_id))
    operation["status"] = "undoing"
    operation["undo_started_at"] = NOW
    store.write(
        "memory_lifecycle_batches", operation_id, operation,
        expected_revision=store.revision("memory_lifecycle_batches", operation_id),
    )

    report = recover_memory_lifecycle_batches(FastAPI(), tmp_path)

    assert report.resumed == 1
    assert store.read("memory_lifecycle_batches", operation_id)["status"] == "undone"
    assert store.read("memory_atoms", "memory-a")["lifecycle_status"] == "active"


def test_startup_recovery_is_bounded_and_ignores_terminal_batches(tmp_path):
    store = _store(tmp_path)
    for object_id in ("memory-a", "memory-b"):
        _seed(store, object_id)
        _interrupted_operation(store, object_id)
    application = FastAPI()

    report = recover_memory_lifecycle_batches(application, tmp_path, limit=1)

    assert report.scanned == 2 and report.resumed == 1 and report.deferred == 1
    second = recover_memory_lifecycle_batches(application, tmp_path)
    assert second.scanned == 1 and second.resumed == 1
    assert recover_memory_lifecycle_batches(application, tmp_path).scanned == 0
