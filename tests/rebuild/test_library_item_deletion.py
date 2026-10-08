from __future__ import annotations

from pathlib import Path
from datetime import UTC, datetime, timedelta

from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.job_runner import ObjectStoreJobRepository
from core.product_core import (
    DeleteLibraryItem,
    LibraryItemDeletionResult,
    UndoLibraryItemDeletion,
    serialize_library_item_deletion_result,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _register_source(store: JsonObjectStore) -> str:
    """注册一个 text source，返回实际生成的 source_id。"""
    ObjectStoreSourceRegistrar(store).register(
        SourceSubmission(
            kind="text",
            title="Deletion fixture",
            content="Content for deletion test.",
        )
    )
    sources = store.list("sources")
    assert len(sources) == 1
    return str(sources[0].get("id"))


def _write_derived_records(store: JsonObjectStore, source_id: str) -> None:
    """写入关联该 source 的衍生数据：content_read / structure / series_assignment / job / source_output / tag_index。"""
    store.write(
        "source_content_reads",
        f"content-read-{source_id}",
        {"id": f"content-read-{source_id}", "source_id": source_id, "status": "completed"},
        expected_revision=None,
    )
    store.write(
        "source_structures",
        f"structure-{source_id}",
        {"id": f"structure-{source_id}", "source_id": source_id, "tags": ["test"]},
        expected_revision=None,
    )
    store.write(
        "source_series_assignments",
        f"series-assignment-{source_id}",
        {"id": f"series-assignment-{source_id}", "source_id": source_id, "series_name": "test"},
        expected_revision=None,
    )
    ObjectStoreJobRepository(store).save({
        "schema_version": "1.0.0",
        "id": f"job-intake-{source_id}",
        "source_id": source_id,
        "job_type": "workbench_auto_intake",
        "idempotency_key": f"workbench-auto-intake-{source_id}",
        "status": "completed",
        "attempt": 1,
        "max_attempts": 1,
        "lease": None,
        "progress": {"current": 1, "total": 1, "percent": 100, "message": "done"},
        "steps": [],
        "error": None,
        "checkpoint": None,
        "staged_outputs": [],
        "published_outputs": [],
        "log_refs": [],
        "created_at": "2026-07-05T10:00:00+08:00",
        "updated_at": "2026-07-05T10:00:00+08:00",
    })
    store.write(
        "source_outputs",
        f"output-{source_id}-1",
        {"id": f"output-{source_id}-1", "source_id": source_id, "kind": "text_content"},
        expected_revision=None,
    )
    store.write(
        "tag_index",
        f"tag-{source_id}-1",
        {"id": f"tag-{source_id}-1", "source_id": source_id, "tag": "test"},
        expected_revision=None,
    )


def test_delete_source_tombstones_main_record_and_preserves_derivatives(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source_id = _register_source(store)
    _write_derived_records(store, source_id)
    # 无关记录，验证不会误删
    store.write(
        "source_outputs",
        "output-other-source",
        {"id": "output-other-source", "source_id": "source-other-xxx", "kind": "text_content"},
        expected_revision=None,
    )

    now = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)
    result = DeleteLibraryItem(
        store, clock=lambda: now, operation_id_factory=lambda: "library-delete-test",
    ).execute(item_type="source", item_id=source_id)

    assert result.status == "deleted"
    assert result.item_id == source_id
    assert "sources" in result.deleted_collections
    assert result.operation_id == "library-delete-test"
    assert result.revision == 2
    assert result.undo_expires_at == "2026-07-21T04:00:00Z"
    # Source、原档引用和衍生记录全部保留，只有Source自身生命周期以CAS更新。
    assert store.read("sources", source_id) is None
    source = store.read_including_deleted("sources", source_id)
    assert source is not None
    assert source["library_lifecycle"]["status"] == "deleted"
    assert store.read("source_content_reads", f"content-read-{source_id}") is not None
    assert store.read("source_structures", f"structure-{source_id}") is not None
    assert store.read("source_series_assignments", f"series-assignment-{source_id}") is not None
    assert store.read("jobs", f"job-intake-{source_id}") is not None
    assert store.read("source_outputs", f"output-{source_id}-1") is not None
    assert store.read("tag_index", f"tag-{source_id}-1") is not None
    # 无关记录保留
    assert store.read("source_outputs", "output-other-source") is not None

    restored = UndoLibraryItemDeletion(store, clock=lambda: now + timedelta(hours=1)).execute(
        item_type="source", item_id=source_id, operation_id="library-delete-test", expected_revision=2,
    )
    assert restored.status == "restored"
    assert restored.revision == 3
    assert store.read("sources", source_id)["library_lifecycle"]["status"] == "active"


def test_source_delete_and_undo_are_replay_safe_and_stale_revision_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source_id = _register_source(store)
    now = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)
    deleter = DeleteLibraryItem(store, clock=lambda: now, operation_id_factory=lambda: "delete-op")
    first = deleter.execute(item_type="source", item_id=source_id)
    replay = deleter.execute(item_type="source", item_id=source_id)
    assert replay.operation_id == first.operation_id
    assert replay.revision == first.revision == 2

    undo = UndoLibraryItemDeletion(store, clock=lambda: now)
    stale = undo.execute(item_type="source", item_id=source_id, operation_id="delete-op", expected_revision=1)
    assert stale.status == "conflict"
    restored = undo.execute(item_type="source", item_id=source_id, operation_id="delete-op", expected_revision=2)
    assert restored.status == "restored"
    replay_restore = undo.execute(item_type="source", item_id=source_id, operation_id="delete-op", expected_revision=2)
    assert replay_restore.status == "restored"
    assert store.revision("sources", source_id) == 3


def test_source_undo_expires_without_mutating_source(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source_id = _register_source(store)
    now = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)
    DeleteLibraryItem(store, clock=lambda: now, operation_id_factory=lambda: "delete-expired").execute(
        item_type="source", item_id=source_id,
    )
    result = UndoLibraryItemDeletion(store, clock=lambda: now + timedelta(days=8)).execute(
        item_type="source", item_id=source_id, operation_id="delete-expired", expected_revision=2,
    )
    assert result.status == "expired"
    assert store.revision("sources", source_id) == 2
    assert store.read("sources", source_id) is None
    assert store.read_including_deleted("sources", source_id) is not None


def test_delete_nonexistent_source_returns_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path)

    result = DeleteLibraryItem(store).execute(
        item_type="source",
        item_id="source-does-not-exist",
    )

    assert result.status == "not_found"
    assert result.deleted_count == 0
    assert result.error is not None


def test_delete_unsupported_item_type_returns_unsupported(tmp_path: Path) -> None:
    store = _store(tmp_path)

    result = DeleteLibraryItem(store).execute(
        item_type="scenario",
        item_id="scenario-001",
    )

    assert result.status == "unsupported"
    assert result.deleted_count == 0
    assert "not supported" in (result.error or "")


def test_delete_rejects_empty_inputs(tmp_path: Path) -> None:
    store = _store(tmp_path)

    result = DeleteLibraryItem(store).execute(item_type="", item_id="")

    assert result.status == "rejected"
    assert result.deleted_count == 0


def test_serialize_deletion_result_round_trip() -> None:
    result = LibraryItemDeletionResult(
        status="deleted",
        item_type="source",
        item_id="source-test",
        deleted_collections=("sources", "source_content_reads"),
        deleted_count=2,
        error=None,
    )
    serialized = serialize_library_item_deletion_result(result)
    assert serialized["status"] == "deleted"
    assert serialized["item_id"] == "source-test"
    assert serialized["deleted_collections"] == ["sources", "source_content_reads"]
    assert serialized["deleted_count"] == 2
    assert serialized["error"] is None


# ── 非Source aggregate必须使用专用lifecycle，通用删除fail closed ──


def test_delete_document_is_unsupported_and_preserves_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("documents", "doc-001", {"id": "doc-001", "title": "测试文档"}, expected_revision=None)

    result = DeleteLibraryItem(store).execute(item_type="document", item_id="doc-001")

    assert result.status == "unsupported"
    assert result.deleted_count == 0
    assert store.read("documents", "doc-001") is not None


def test_delete_atom_is_unsupported_and_preserves_history(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("memory_atoms", "atom-001", {"id": "atom-001", "content": "测试 Atom"}, expected_revision=None)

    result = DeleteLibraryItem(store).execute(item_type="atom", item_id="atom-001")

    assert result.status == "unsupported"
    assert result.deleted_count == 0
    assert store.read("memory_atoms", "atom-001") is not None


def test_delete_memory_candidate_is_unsupported_and_preserves_review_fact(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("memory_candidates", "candidate-001", {"id": "candidate-001"}, expected_revision=None)

    result = DeleteLibraryItem(store).execute(item_type="memory_candidate", item_id="candidate-001")

    assert result.status == "unsupported"
    assert result.deleted_count == 0
    assert store.read("memory_candidates", "candidate-001") is not None


def test_delete_document_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path)

    result = DeleteLibraryItem(store).execute(item_type="document", item_id="nonexistent")

    assert result.status == "unsupported"
    assert result.deleted_count == 0
