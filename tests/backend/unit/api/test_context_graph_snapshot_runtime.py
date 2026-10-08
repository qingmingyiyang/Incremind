from dataclasses import replace

import pytest

from backend.api.context_graph_snapshot_runtime import (
    ContextGraphSnapshotConflict,
    ContextGraphSnapshotRepository,
    ContextGraphSnapshotRepositoryError,
)
from core.context_graph import (
    ContextPermissionGrant,
    ContextGraphNode,
    ContextGraphSnapshot,
    ContextProvenance,
)
from core.storage_provider import SQLiteStructuredRecordStore


def _snapshot(project="project-a", graph="graph-a", revision="r1"):
    return ContextGraphSnapshot(
        schema_version="1.0.0", graph_id=graph, graph_revision=revision,
        project_id=project, source_type="fixture", source_revision="source-r1",
        created_at="2026-08-30T00:00:00Z", nodes=(ContextGraphNode(
            node_id="node-a", node_type="note", title="Note", content_ref="ref://note",
            content_revision="content-r1", source_refs=("source://note",), trust="verified",
            created_at="2026-08-30T00:00:00Z", updated_at="2026-08-30T00:00:00Z",
        ),), edges=(), selected_outputs=("node-a",), token_estimate=1,
        provenance=ContextProvenance("fixture", "source-r1", "2026-08-30T00:00:00Z", "test", "1", "ref://source"),
    )


def _repository(tmp_path):
    return ContextGraphSnapshotRepository(SQLiteStructuredRecordStore(tmp_path / "snapshots.sqlite3"))


def _authority(project="project-a", *, allowed=("ref://note",), evidence=("evidence://permission-1",)):
    return {
        "capability_id": "thought-graph-context",
        "capability_revision": "cap-1",
        "permission_grant": ContextPermissionGrant(project, "permission-1", frozenset(allowed)),
        "permission_evidence_refs": evidence,
    }


def _append(repository, snapshot, predecessor):
    return repository.append(snapshot, predecessor, **_authority(snapshot.project_id))


def test_first_append_current_and_immutable_revision_resolution(tmp_path):
    repository = _repository(tmp_path)
    first = _append(repository, _snapshot(), None)
    second = _append(repository, _snapshot(revision="r2"), "r1")

    assert first.predecessor is None
    assert first.store_revision == 1
    assert repository.current(project_id="project-a", graph_id="graph-a") == second
    assert repository.revision(project_id="project-a", graph_id="graph-a", graph_revision="r1") == first
    assert second.predecessor == "r1"
    assert repository.previous("project-a", "graph-a", "r2") == first
    assert second.capability_revision == "cap-1"


def test_append_requires_the_exact_current_predecessor_and_rejects_revision_collisions(tmp_path):
    repository = _repository(tmp_path)
    _append(repository, _snapshot(), None)
    with pytest.raises(ContextGraphSnapshotConflict):
        _append(repository, _snapshot(revision="r2"), None)
    with pytest.raises(ContextGraphSnapshotConflict):
        _append(repository, _snapshot(revision="r2"), "not-r1")
    with pytest.raises(ContextGraphSnapshotConflict):
        _append(repository, _snapshot(), "r1")
    assert repository.current(project_id="project-a", graph_id="graph-a").graph_revision == "r1"


def test_project_scope_and_long_legal_graph_ids_are_never_encoded_as_storage_segments(tmp_path):
    repository = _repository(tmp_path)
    graph = "图" * 256
    _append(repository, _snapshot(project="project-a", graph=graph), None)
    _append(repository, _snapshot(project="project-b", graph=graph), None)

    assert repository.current(project_id="project-a", graph_id=graph).project_id == "project-a"
    assert repository.current(project_id="project-b", graph_id=graph).project_id == "project-b"
    assert repository.revision(project_id="project-c", graph_id=graph, graph_revision="r1") is None


def test_failed_transaction_never_advances_the_head(tmp_path, monkeypatch):
    repository = _repository(tmp_path)
    first = _append(repository, _snapshot(), None)
    from core.storage_provider import SQLiteStructuredRecordUnitOfWork

    original_put = SQLiteStructuredRecordUnitOfWork.put

    def fail_head(self, collection, *args, **kwargs):
        if collection == "context_graph_snapshot_heads":
            raise RuntimeError("injected write failure")
        return original_put(self, collection, *args, **kwargs)

    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, "put", fail_head)
    with pytest.raises(RuntimeError, match="injected"):
        _append(repository, _snapshot(revision="r2"), "r1")
    assert repository.current(project_id="project-a", graph_id="graph-a") == first
    assert repository.revision(project_id="project-a", graph_id="graph-a", graph_revision="r2") is None


def test_sequential_cas_race_and_future_or_invalid_payloads_fail_closed(tmp_path):
    repository = _repository(tmp_path)
    _append(repository, _snapshot(), None)
    winner = _append(repository, _snapshot(revision="r2"), "r1")
    with pytest.raises(ContextGraphSnapshotConflict):
        _append(repository, _snapshot(revision="r3"), "r1")
    assert repository.current(project_id="project-a", graph_id="graph-a") == winner

    records = repository._records
    with records.begin() as uow:
        uow.put("context_graph_snapshot_revisions", "bad-payload", {
            "schema_version": "2.0.0", "project_id": "project-a", "graph_id": "graph-a",
            "graph_revision": "future", "predecessor": "r2", "snapshot": {},
        }, expected_revision=0)
        uow.commit()
    with pytest.raises(ContextGraphSnapshotRepositoryError):
        repository.revision(project_id="project-a", graph_id="graph-a", graph_revision="future")

    with pytest.raises(ContextGraphSnapshotRepositoryError):
        _append(repository, replace(_snapshot(revision="r4"), token_estimate=-1), "r2")

    with pytest.raises(ContextGraphSnapshotRepositoryError):
        repository.append(_snapshot(revision="r4"), "r2", **_authority("project-b"))
    with pytest.raises(ContextGraphSnapshotRepositoryError):
        repository.append(_snapshot(revision="r4"), "r2", **_authority(allowed=()))
    with pytest.raises(ContextGraphSnapshotRepositoryError):
        repository.append(_snapshot(revision="r4"), "r2", **_authority(evidence=()))

    persisted = next(
        item for item in records.list("context_graph_snapshot_revisions")
        if item.payload.get("graph_revision") == "r1"
    )
    tampered = dict(persisted.payload)
    tampered["capability_revision"] = ""
    with records.begin() as uow:
        uow.put("context_graph_snapshot_revisions", persisted.object_id, tampered, expected_revision=persisted.revision)
        uow.commit()
    with pytest.raises(ContextGraphSnapshotRepositoryError):
        repository.revision("project-a", "graph-a", "r1")
