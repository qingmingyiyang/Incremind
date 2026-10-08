import pytest

from backend.memory_app.packet_egress import capture_packet_egress
from backend.memory_app.source_egress import SourceEgressService
from backend.recognition import RecognitionConflict, RecognitionError, RecognitionService, WorkScope
from core.document_engine import DocumentDraft, SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.fixture
def env(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "memory.sqlite3")
    service = RecognitionService(records)
    return records, service, SourceEgressService(records), WorkScope("user", "project")


def _recognition(service, scope, source_id):
    candidate = service.propose(scope=scope, content="derived", source_experience_ids=[source_id])
    return service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer="user")


def _retained_artifact(records, service, scope, *, source=True, document_revision=1):
    source_id = service.stage_experience(scope=scope, content="source")
    if source:
        authority = SourceEgressService(records)
        authority.set_policy(scope, "experience", source_id, 1, 0, ["generation", "embedding", "rerank"])
        recognition = _recognition(service, scope, source_id)
        packet_payload = {
            "id": "packet", "kind": "context", "project_id": scope.project_id,
            "query": "task", "items": [{"id": recognition.id, "revision": recognition.revision}],
        }
    else:
        packet_payload = {"id": "packet", "kind": "context", "project_id": scope.project_id,
                          "query": "task", "items": []}
    packet_payload["source_egress"] = capture_packet_egress(service, scope, packet_payload)
    packet_payload.update({"state": "consumed", "task_id": "task"})
    repository = SQLiteDocumentRepository(records, namespace_id="recognition")
    document = repository.create(DocumentDraft(
        title="task output", document_type="agent-result", markdown="original output",
        project_id=scope.project_id,
        source_refs=({"source_id": "task", "locator": "task://task"},),
    ))
    with records.begin() as tx:
        packet = tx.put("recognition_context_packets", "packet", packet_payload, expected_revision=0)
        task = tx.put("recognition_tasks", "task", {
            "id": "task", "project_id": scope.project_id, "state": "completed",
            "document_id": document["id"], "context_packet_id": "packet",
        }, expected_revision=0)
        tx.commit()
    if document_revision == 2:
        document = repository.save_user_edit(document["id"], markdown="edited output", expected_revision=1)
    artifact_id = service.stage_experience(
        scope=scope, content="retained output", experience_id="artifact",
        provenance={"kind": "model_generated_artifact", "actor": "agent", "source_refs": [
            {"type": "task", "id": "task", "revision": task.revision},
            {"type": "document", "id": document["id"], "revision": document["revision"]},
            {"type": "context_packet", "id": "packet", "revision": packet.revision},
        ]},
    )
    return {"artifact": artifact_id, "source": source_id, "document": document, "repository": repository}


def _snapshot(egress, scope, artifact):
    return egress.snapshot(scope, [{"type": "experience", "id": artifact, "revision": 1}])


@pytest.mark.parametrize("frozen_purposes", [["generation"], []])
def test_retained_artifact_inherits_frozen_privacy_without_purpose_ceiling(env, frozen_purposes):
    records, service, egress, scope = env
    items = _retained_artifact(records, service, scope)
    # Keep exact packet evidence while exercising legacy and private captures.
    with records.begin() as tx:
        packet = tx.read("recognition_context_packets", "packet")
        frozen = {**packet.payload["source_egress"], "nodes": [
            {**node, "effective_purposes": frozen_purposes}
            for node in packet.payload["source_egress"]["nodes"]]}
        updated = tx.put("recognition_context_packets", "packet", {**packet.payload, "source_egress": frozen}, expected_revision=packet.revision)
        artifact = tx.read("recognition_experiences", items["artifact"])
        provenance = {**artifact.payload["provenance"], "source_refs": [
            {**ref, "revision": updated.revision} if ref["type"] == "context_packet" else ref
            for ref in artifact.payload["provenance"]["source_refs"]]}
        tx.put("recognition_experiences", items["artifact"], {**artifact.payload, "provenance": provenance}, expected_revision=artifact.revision)
        tx.commit()
    refs = [{"type": "experience", "id": items["artifact"], "revision": 2}]
    snapshot = egress.snapshot(scope, refs)
    for purpose in ("generation", "embedding", "rerank"):
        if frozen_purposes:
            egress.require(snapshot, purpose)
        else:
            with pytest.raises(RecognitionConflict):
                egress.require(snapshot, purpose)
    egress.set_policy(scope, "experience", items["source"], 1, 1, [])
    private = egress.snapshot(scope, refs)
    for purpose in ("generation", "embedding", "rerank"):
        with pytest.raises(RecognitionConflict):
            egress.require(private, purpose)
    with pytest.raises(RecognitionError, match="cannot broaden"):
        egress.set_policy(scope, "experience", items["artifact"], 2, 0, ["generation", "embedding", "rerank"])


def test_retained_artifact_rechecks_upstream_source_and_keeps_historical_document(env):
    records, service, egress, scope = env
    items = _retained_artifact(records, service, scope, document_revision=2)
    # A later user edit must not sever the r2 evidence that was retained.
    items["repository"].save_user_edit(items["document"]["id"], markdown="later edit", expected_revision=2)
    egress.require(_snapshot(egress, scope, items["artifact"]), "generation")
    service.revoke_experience(scope=scope, experience_id=items["source"], expected_revision=1)
    with pytest.raises(RecognitionConflict, match="unavailable"):
        _snapshot(egress, scope, items["artifact"])


def test_artifact_closure_does_not_include_an_unrelated_outer_snapshot_root(env):
    records, service, egress, scope = env
    items = _retained_artifact(records, service, scope)
    unrelated = service.stage_experience(scope=scope, content="unrelated")
    egress.set_policy(scope, "experience", unrelated, 1, 0, ["generation", "embedding", "rerank"])
    # The outer call shares its cache across roots.  The artifact's frozen
    # packet closure must still be compared only with its own source closure.
    snapshot = egress.snapshot(scope, [
        {"type": "experience", "id": items["artifact"], "revision": 1},
        {"type": "experience", "id": unrelated, "revision": 1},
    ])
    egress.require(snapshot, "generation")


def test_frozen_packet_rejects_a_missing_ancestor_even_with_matching_packet_revision(env):
    records, service, egress, scope = env
    items = _retained_artifact(records, service, scope)
    with records.begin() as tx:
        packet = tx.read("recognition_context_packets", "packet")
        frozen = dict(packet.payload["source_egress"])
        frozen["nodes"] = [node for node in frozen["nodes"] if node["type"] == "recognition"]
        updated_packet = tx.put("recognition_context_packets", "packet", {**packet.payload, "source_egress": frozen}, expected_revision=packet.revision)
        artifact = tx.read("recognition_experiences", items["artifact"])
        provenance = dict(artifact.payload["provenance"])
        provenance["source_refs"] = [
            {**ref, **({"revision": updated_packet.revision} if ref["type"] == "context_packet" else {})}
            for ref in provenance["source_refs"]
        ]
        tx.put("recognition_experiences", items["artifact"], {**artifact.payload, "provenance": provenance}, expected_revision=artifact.revision)
        tx.commit()
    with pytest.raises(RecognitionConflict, match="closure"):
        egress.snapshot(scope, [{"type": "experience", "id": items["artifact"], "revision": 2}])


def test_old_artifact_snapshot_detects_policy_revoke_then_regrant_with_same_effective_value(env):
    records, service, egress, scope = env
    items = _retained_artifact(records, service, scope)
    snapshot = _snapshot(egress, scope, items["artifact"])
    egress.set_policy(scope, "experience", items["source"], 1, 1, [])
    egress.set_policy(scope, "experience", items["source"], 1, 2, ["generation", "embedding", "rerank"])
    with pytest.raises(RecognitionConflict):
        egress.validate_snapshot(scope, snapshot)
    egress.require(_snapshot(egress, scope, items["artifact"]), "generation")


def test_nested_artifacts_inherit_then_fail_closed_when_the_original_source_is_revoked(env):
    records, service, egress, scope = env
    first = _retained_artifact(records, service, scope)
    recognition = _recognition(service, scope, first["artifact"])
    packet_payload = {
        "id": "packet-two", "kind": "context", "project_id": scope.project_id,
        "query": "second task", "items": [{"id": recognition.id, "revision": recognition.revision}],
    }
    packet_payload["source_egress"] = capture_packet_egress(service, scope, packet_payload)
    packet_payload.update({"state": "consumed", "task_id": "task-two"})
    repository = SQLiteDocumentRepository(records, namespace_id="recognition")
    document = repository.create(DocumentDraft(
        title="second output", document_type="agent-result", markdown="second result", project_id=scope.project_id,
        source_refs=({"source_id": "task-two", "locator": "task://task-two"},),
    ))
    with records.begin() as tx:
        packet = tx.put("recognition_context_packets", "packet-two", packet_payload, expected_revision=0)
        task = tx.put("recognition_tasks", "task-two", {
            "id": "task-two", "project_id": scope.project_id, "state": "completed",
            "document_id": document["id"], "context_packet_id": "packet-two",
        }, expected_revision=0)
        tx.commit()
    second = service.stage_experience(scope=scope, content="second retained", experience_id="artifact-two",
        provenance={"kind": "model_generated_artifact", "source_refs": [
            {"type": "task", "id": "task-two", "revision": task.revision},
            {"type": "document", "id": document["id"], "revision": document["revision"]},
            {"type": "context_packet", "id": "packet-two", "revision": packet.revision},
        ]})
    egress.require(_snapshot(egress, scope, second), "generation")
    # The second packet froze the first artifact's historical dependencies.
    # Replacing its stored historical revision cannot be relabelled as a new
    # safe source merely by taking another second-artifact snapshot.
    key = first["document"]["id"] + "~r1"
    with records.begin() as tx:
        markdown = tx.read("document_markdown", key)
        tx.put("document_markdown", key, dict(markdown.payload), expected_revision=markdown.revision)
        tx.commit()
    with pytest.raises(RecognitionConflict, match="closure"):
        _snapshot(egress, scope, second)
    service.revoke_experience(scope=scope, experience_id=first["source"], expected_revision=1)
    with pytest.raises(RecognitionConflict, match="unavailable"):
        _snapshot(egress, scope, second)


def test_artifact_snapshot_detects_mutated_historical_markdown_storage(env):
    records, service, egress, scope = env
    items = _retained_artifact(records, service, scope)
    snapshot = _snapshot(egress, scope, items["artifact"])
    key = items["document"]["id"] + "~r1"
    with records.begin() as tx:
        markdown = tx.read("document_markdown", key)
        tx.put("document_markdown", key, dict(markdown.payload), expected_revision=markdown.revision)
        tx.commit()
    with pytest.raises(RecognitionConflict):
        egress.validate_snapshot(scope, snapshot)


@pytest.mark.parametrize("change", ["task_revision", "packet_revision", "packet_scope", "packet_task"])
def test_retained_artifact_rejects_changed_or_wrong_packet_evidence(env, change):
    records, service, egress, scope = env
    items = _retained_artifact(records, service, scope)
    with records.begin() as tx:
        if change == "task_revision":
            task = tx.read("recognition_tasks", "task")
            tx.put("recognition_tasks", "task", dict(task.payload), expected_revision=task.revision)
        else:
            packet = tx.read("recognition_context_packets", "packet")
            payload = dict(packet.payload)
            if change == "packet_scope":
                payload["project_id"] = "other"
            if change == "packet_task":
                payload["task_id"] = "other-task"
            tx.put("recognition_context_packets", "packet", payload, expected_revision=packet.revision)
        tx.commit()
    with pytest.raises(RecognitionConflict):
        _snapshot(egress, scope, items["artifact"])


def test_no_source_model_artifact_is_allowed_leaf_unless_explicitly_private(env):
    records, service, egress, scope = env
    items = _retained_artifact(records, service, scope, source=False)
    snapshot = _snapshot(egress, scope, items["artifact"])
    for purpose in ("generation", "embedding", "rerank"):
        egress.require(snapshot, purpose)
    egress.set_policy(scope, "experience", items["artifact"], 1, 0, [])
    private = _snapshot(egress, scope, items["artifact"])
    for purpose in ("generation", "embedding", "rerank"):
        with pytest.raises(RecognitionConflict):
            egress.require(private, purpose)
    egress.set_policy(scope, "experience", items["artifact"], 1, 1, ["generation", "embedding", "rerank"])
    egress.require(_snapshot(egress, scope, items["artifact"]), "generation")


def test_legacy_or_incomplete_model_artifact_provenance_never_becomes_a_leaf(env):
    records, service, egress, scope = env
    artifact = service.stage_experience(
        scope=scope, content="legacy output", experience_id="artifact",
        provenance={"kind": "model_generated_artifact", "source_refs": [
            {"type": "task", "id": "task", "revision": 1},
            {"type": "document", "id": "document", "revision": 1},
        ]},
    )
    with pytest.raises(RecognitionConflict, match="incomplete"):
        _snapshot(egress, scope, artifact)
    with pytest.raises(RecognitionConflict):
        egress.set_policy(scope, "experience", artifact, 1, 0, ["generation", "embedding", "rerank"])


def test_backup_restore_preserves_artifact_authority_and_allows_local_revocation(env, tmp_path):
    from backend.memory_app.backup import backup_database

    records, service, egress, scope = env
    items = _retained_artifact(records, service, scope, document_revision=2)
    original = _snapshot(egress, scope, items["artifact"])
    backup = tmp_path / "portable.sqlite3"
    destination = tmp_path / "restored.sqlite3"
    backup_database(records.database_path, backup)
    backup_database(backup, destination)
    restored_records = SQLiteStructuredRecordStore(destination)
    restored = SourceEgressService(restored_records)
    restored.validate_snapshot(scope, original)
    assert _snapshot(restored, scope, items["artifact"]) == original
    repository = SQLiteDocumentRepository(restored_records, namespace_id="recognition")
    assert repository.markdown(items["document"]["id"], revision=2) == "edited output"
    restored.require(original, "generation")
    for purpose in ("embedding", "rerank"):
        restored.require(original, purpose)

    # Non-private policies no longer impose a purpose ceiling on retained artifacts.
    restored.set_policy(scope, "experience", items["source"], 1, 1, ["generation", "embedding", "rerank"])
    restored.set_policy(scope, "experience", items["artifact"], 1, 0, ["generation", "embedding", "rerank"])
    restored.set_policy(scope, "experience", items["source"], 1, 2, [])
    with pytest.raises(RecognitionConflict):
        restored.validate_snapshot(scope, original)
    with pytest.raises(RecognitionConflict):
        restored.require(_snapshot(restored, scope, items["artifact"]), "generation")
    # Revoking the restored copy does not mutate the original database.
    egress.validate_snapshot(scope, original)
    egress.require(_snapshot(egress, scope, items["artifact"]), "generation")
