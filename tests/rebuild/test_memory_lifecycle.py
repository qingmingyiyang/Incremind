from __future__ import annotations

import json

import pytest

from core.memory_core import ObjectStoreMemoryStore
from core.product_core.memory_lifecycle import (
    GovernedMemoryLifecycle,
    MemoryBatchItem,
    MemoryLifecycleConflict,
    MemoryLifecycleError,
)
from core.storage_provider import JsonObjectStore


NOW = "2026-08-28T10:00:00+00:00"


def _store(tmp_path):
    store = JsonObjectStore(tmp_path / "vault", legacy_root=tmp_path / "legacy")
    atom = {
        "schema_version": "1.0.0", "id": "memory-a", "project_id": "project-a",
        "revision": 1, "content": "old governed fact", "trust_status": "user_confirmed",
        "source_refs": [{"source_id": "source-a", "locator": "line:1"}],
        "created_at": NOW, "updated_at": NOW,
    }
    store.write("memory_atoms", "memory-a", atom, expected_revision=0)
    return store


def test_supersede_preserves_history_and_invalidates_reverse_lineage(tmp_path):
    store = _store(tmp_path)
    changes: list[dict[str, object]] = []
    lifecycle = GovernedMemoryLifecycle(store, change_sink=lambda event: changes.append(dict(event)))
    old_ref = "crp://default/memory/atom/memory-a@r1"
    for kind, ref in (
        ("compaction", "crp://compactions/project-a/summary-a"),
        ("document", "crp://documents/project-a/document-a"),
        ("external_context", "agent-session-a"),
    ):
        lifecycle.register_lineage(
            project_id="project-a", memory_ref=old_ref, consumer_kind=kind,
            consumer_ref=ref, recorded_at=NOW,
        )

    result = lifecycle.supersede(
        layer="atom", object_id="memory-a", project_id="project-a",
        expected_revision=1, expected_storage_revision=1,
        changes={"content": "corrected governed fact"}, reason="user corrected the fact",
        occurred_at="2026-08-01T09:00:00+00:00", recorded_at=NOW, confirm=True,
    )

    assert result.revision == 2
    assert result.invalidated_refs == (old_ref,)
    assert result.affected_consumers == (
        "agent-session-a", "crp://compactions/project-a/summary-a",
        "crp://documents/project-a/document-a",
    )
    assert ObjectStoreMemoryStore(store).get("atom", "memory-a")["content"] == "corrected governed fact"
    history = store.list("memory_revision_history")
    assert len(history) == 1 and history[0]["payload"]["content"] == "old governed fact"
    assert {item["status"] for item in store.list("memory_lineage_refs")} == {"stale"}
    assert changes[0]["change_type"] == "memory.invalidated"
    assert changes[0]["object_revision"] == "r2"


def test_soft_redact_disappears_from_recall_and_can_be_restored(tmp_path):
    store = _store(tmp_path)
    lifecycle = GovernedMemoryLifecycle(store)

    redacted = lifecycle.redact(
        layer="atom", object_id="memory-a", project_id="project-a",
        expected_revision=1, expected_storage_revision=1, reason="user no longer wants this recalled",
        mode="soft", occurred_at=NOW, confirm=True,
    )
    assert redacted.status == "redacted"
    reader = ObjectStoreMemoryStore(store)
    assert reader.get("atom", "memory-a") is None
    assert reader.list("atom") == ()

    restored = lifecycle.restore_soft_redaction(
        layer="atom", object_id="memory-a", project_id="project-a",
        expected_revision=2, expected_storage_revision=2, occurred_at=NOW, confirm=True,
    )
    assert restored.status == "active" and restored.revision == 3
    assert reader.get("atom", "memory-a")["content"] == "old governed fact"


def test_hard_redact_removes_payload_and_requires_exact_confirmation(tmp_path):
    store = _store(tmp_path)
    lifecycle = GovernedMemoryLifecycle(store)
    secret = "private-value-that-must-disappear"
    lifecycle.supersede(
        layer="atom", object_id="memory-a", project_id="project-a",
        expected_revision=1, expected_storage_revision=1, changes={"content": secret},
        reason="prepare sensitive fixture", occurred_at=NOW, recorded_at=NOW, confirm=True,
    )

    with pytest.raises(MemoryLifecycleError, match="confirmation"):
        lifecycle.redact(
            layer="atom", object_id="memory-a", project_id="project-a",
            expected_revision=2, expected_storage_revision=2, reason="privacy removal",
            mode="hard", occurred_at=NOW, confirm=True, hard_confirmation="DELETE",
        )

    result = lifecycle.redact(
        layer="atom", object_id="memory-a", project_id="project-a",
        expected_revision=2, expected_storage_revision=2, reason="privacy removal",
        mode="hard", occurred_at=NOW, confirm=True, hard_confirmation="DELETE memory-a",
    )
    assert result.status == "redacted"
    tombstone = store.read("memory_atoms", "memory-a")
    assert tombstone is not None and tombstone["redact_mode"] == "hard"
    assert "content" not in tombstone
    assert store.list("memory_revision_history") == ()
    serialized = json.dumps(
        {name: store.list(name) for name in store.collection_names()}, ensure_ascii=False,
    )
    assert secret not in serialized


def test_revision_and_project_drift_fail_closed(tmp_path):
    store = _store(tmp_path)
    lifecycle = GovernedMemoryLifecycle(store)
    with pytest.raises(MemoryLifecycleConflict, match="project"):
        lifecycle.supersede(
            layer="atom", object_id="memory-a", project_id="project-b",
            expected_revision=1, expected_storage_revision=1, changes={"content": "wrong"},
            reason="wrong project", occurred_at=NOW, recorded_at=NOW, confirm=True,
        )
    with pytest.raises(MemoryLifecycleConflict, match="revision"):
        lifecycle.supersede(
            layer="atom", object_id="memory-a", project_id="project-a",
            expected_revision=2, expected_storage_revision=1, changes={"content": "stale"},
            reason="stale edit", occurred_at=NOW, recorded_at=NOW, confirm=True,
        )


def test_batch_soft_redact_preview_confirm_replay_and_undo(tmp_path):
    store = _store(tmp_path)
    second = {
        **dict(store.read("memory_atoms", "memory-a")),
        "id": "memory-b", "content": "second fact",
    }
    store.write("memory_atoms", "memory-b", second, expected_revision=0)
    lifecycle = GovernedMemoryLifecycle(store)
    items = (
        MemoryBatchItem("atom", "memory-a", 1, 1),
        MemoryBatchItem("atom", "memory-b", 1, 1),
    )

    preview = lifecycle.preview_batch_soft_redact(
        project_id="project-a", items=items, reason="user clears selected memories",
        occurred_at=NOW,
    )
    assert store.list("memory_lifecycle_batches") == ()
    applied = lifecycle.confirm_batch_soft_redact(
        preview=preview, expected_preview_token=preview.preview_token, confirm=True,
    )
    assert applied["status"] == "completed"
    assert len(applied["completed"]) == 2
    assert lifecycle.confirm_batch_soft_redact(
        preview=preview, expected_preview_token=preview.preview_token, confirm=True,
    )["status"] == "completed"
    assert ObjectStoreMemoryStore(store).list("atom") == ()

    undone = lifecycle.undo_batch_soft_redact(
        operation_id=str(applied["id"]), occurred_at=NOW, confirm=True,
    )
    assert undone["status"] == "undone"
    assert {item["id"] for item in ObjectStoreMemoryStore(store).list("atom")} == {
        "memory-a", "memory-b",
    }
    assert lifecycle.undo_batch_soft_redact(
        operation_id=str(applied["id"]), occurred_at=NOW, confirm=True,
    )["status"] == "undone"


def test_batch_preview_rejects_duplicate_and_revision_drift(tmp_path):
    lifecycle = GovernedMemoryLifecycle(_store(tmp_path))
    duplicate = MemoryBatchItem("atom", "memory-a", 1, 1)
    with pytest.raises(MemoryLifecycleError, match="duplicate"):
        lifecycle.preview_batch_soft_redact(
            project_id="project-a", items=(duplicate, duplicate), reason="duplicate selection",
            occurred_at=NOW,
        )
    with pytest.raises(MemoryLifecycleConflict, match="revision"):
        lifecycle.preview_batch_soft_redact(
            project_id="project-a", items=(MemoryBatchItem("atom", "memory-a", 2, 1),),
            reason="stale selection", occurred_at=NOW,
        )


def test_batch_resume_recognizes_item_applied_before_progress_checkpoint(tmp_path):
    store = _store(tmp_path)
    lifecycle = GovernedMemoryLifecycle(store)
    item = MemoryBatchItem("atom", "memory-a", 1, 1)
    preview = lifecycle.preview_batch_soft_redact(
        project_id="project-a", items=(item,), reason="recover interrupted batch",
        occurred_at=NOW,
    )
    operation_id = f"memory-batch-{preview.preview_token[:20]}"
    store.write(
        "memory_lifecycle_batches", operation_id,
        {
            "schema_version": "1.0.0", "id": operation_id, "action": "soft_redact",
            "project_id": "project-a", "preview_token": preview.preview_token,
            "reason": preview.reason, "occurred_at": NOW, "status": "applying",
            "completed": [],
            "items": [{
                "layer": "atom", "object_id": "memory-a", "expected_revision": 1,
                "expected_storage_revision": 1,
            }],
        },
        expected_revision=0,
    )
    lifecycle.redact(
        layer="atom", object_id="memory-a", project_id="project-a",
        expected_revision=1, expected_storage_revision=1, reason=preview.reason,
        mode="soft", occurred_at=NOW, confirm=True,
    )

    resumed = lifecycle.confirm_batch_soft_redact(
        preview=preview, expected_preview_token=preview.preview_token, confirm=True,
    )
    assert resumed["status"] == "completed"
    assert resumed["completed"] == ["atom:memory-a"]
