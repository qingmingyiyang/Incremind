from __future__ import annotations

from backend.memory_app.erasure import ErasureConflict, ErasureService
from backend.memory_app.relations import RelationProposalService
from backend.recognition import RecognitionService, WorkScope
from backend.recognition import RecognitionConflict
import pytest
from backend.recognition_retrieval import SQLiteEmbeddingCache
from core.document_engine import DocumentDraft, SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore


def _put(records, collection, object_id, payload):
    with records.begin() as uow:
        uow.put(collection, object_id, payload, expected_revision=0)
        uow.commit()


def test_erasure_includes_source_policies_and_rejects_changed_policy_preview(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "recognitions.sqlite3")
    scope = _scope()
    _put(records, "recognitions", "root", _recognition("root", scope))
    policy = {"source_type": "recognition", "source_id": "root", "source_revision": 1,
              "scope": {"user_id": scope.user_id, "project_id": scope.project_id},
              "allowed_purposes": ["generation"]}
    _put(records, "source_egress_recognition_policies", "root", policy)
    _put(records, "source_egress_recognition_policies", "other", {**policy,
        "scope": {"user_id": scope.user_id, "project_id": "other-project"}})
    service = ErasureService(records, tmp_path)
    preview = service.preview(scope=scope, recognition_id="root", expected_revision=1)
    assert preview.counts["source_egress_recognition_policies"] == 1
    with records.begin() as tx:
        tx.put("source_egress_recognition_policies", "root", {**policy, "allowed_purposes": []}, expected_revision=1)
        tx.commit()
    with pytest.raises(ErasureConflict, match="scope changed"):
        service.erase(scope=scope, recognition_id="root", expected_revision=1, expected_plan=preview.revisions)
    assert records.read("recognitions", "root") is not None
    preview = service.preview(scope=scope, recognition_id="root", expected_revision=1)
    service.erase(scope=scope, recognition_id="root", expected_revision=1, expected_plan=preview.revisions)
    assert records.read("source_egress_recognition_policies", "root") is None
    assert records.read("source_egress_recognition_policies", "other") is not None


def test_erasure_removes_restructure_snapshots_but_preserves_other_scope(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "recognitions.sqlite3")
    scope = _scope()
    _put(records, "recognitions", "root", _recognition("root", scope))
    collection = "recognition_restructure_proposals"
    payload = {"scope": {"user_id": scope.user_id, "project_id": scope.project_id},
        "project_id": scope.project_id, "input_recognition_ids": ["root"],
        "snapshot": {"content": "secret recognition"}}
    _put(records, collection, "proposal", payload)
    _put(records, collection, "output-proposal", {**payload,
        "input_recognition_ids": [], "output_recognition_ids": ["root"]})
    _put(records, collection, "other", {**payload,
        "scope": {"user_id": scope.user_id, "project_id": "project-b"}, "project_id": "project-b"})
    erasure = ErasureService(records, tmp_path)
    preview = erasure.preview(scope=scope, recognition_id="root", expected_revision=1)
    assert preview.counts[collection] == 2
    erasure.erase(scope=scope, recognition_id="root", expected_revision=1, expected_plan=preview.revisions)
    assert records.read(collection, "proposal") is None
    assert records.read(collection, "output-proposal") is None
    assert records.read(collection, "other") is not None


def test_erasure_rejects_a_preview_after_restructure_proposal_changes(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "recognitions.sqlite3")
    scope = _scope()
    _put(records, "recognitions", "root", _recognition("root", scope))
    erasure = ErasureService(records, tmp_path)
    preview = erasure.preview(scope=scope, recognition_id="root", expected_revision=1)
    _put(records, "recognition_restructure_proposals", "proposal", {
        "scope": {"user_id": scope.user_id, "project_id": scope.project_id},
        "project_id": scope.project_id, "input_recognition_ids": ["root"],
        "snapshot": {"content": "secret recognition"}})
    with pytest.raises(ErasureConflict, match="scope changed"):
        erasure.erase(scope=scope, recognition_id="root", expected_revision=1, expected_plan=preview.revisions)
    assert records.read("recognitions", "root") is not None
    assert records.read("recognition_restructure_proposals", "proposal") is not None


def test_erasure_clears_restructure_packets_tasks_and_internal_results(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "recognitions.sqlite3")
    scope = _scope()
    _put(records, "recognitions", "root", _recognition("root", scope))
    _put(records, "recognition_context_packets", "restructure-packet", {
        "kind": "restructure", "project_id": scope.project_id,
        "snapshot": {"recognitions": [{"id": "root", "payload": {"content": "secret"}}]},
        "messages": [{"role": "user", "content": "secret"}]})
    _put(records, "recognition_tasks", "restructure-task", {
        "kind": "restructure", "project_id": scope.project_id, "context_packet_id": "restructure-packet"})
    document = SQLiteDocumentRepository(records, namespace_id="recognition").create(DocumentDraft(
        title="internal", document_type="restructure-internal", markdown="secret model output",
        project_id=scope.project_id, source_refs=({"source_id": "restructure-task", "locator": "task://restructure-task"},)))
    erasure = ErasureService(records, tmp_path)
    preview = erasure.preview(scope=scope, recognition_id="root", expected_revision=1)
    assert preview.counts["recognition_context_packets"] == 1
    assert preview.counts["recognition_tasks"] == 1
    assert preview.counts["documents"] == 1
    erasure.erase(scope=scope, recognition_id="root", expected_revision=1, expected_plan=preview.revisions)
    assert records.read("recognition_context_packets", "restructure-packet") is None
    assert records.read("recognition_tasks", "restructure-task") is None
    assert SQLiteDocumentRepository(records, namespace_id="recognition").read(document["id"]) is None


def test_erasure_includes_layout_references_and_rechecks_layout_revision(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "recognitions.sqlite3")
    scope = _scope()
    _put(records, "recognitions", "root", _recognition("root", scope))
    payload = {"scope": {"user_id": scope.user_id, "project_id": scope.project_id},
               "project_id": scope.project_id, "node_ids": ["root"]}
    _put(records, "recognition_graph_views", "view", payload)
    _put(records, "recognition_graph_views", "other", {**payload,
         "scope": {"user_id": "other-user", "project_id": scope.project_id}})
    erasure = ErasureService(records, tmp_path)
    preview = erasure.preview(scope=scope, recognition_id="root", expected_revision=1)
    assert preview.counts["recognition_graph_views"] == 1
    with records.begin() as tx:
        tx.put("recognition_graph_views", "view", {**payload, "hidden_ids": ["root"]}, expected_revision=1)
        tx.commit()
    with pytest.raises(ErasureConflict):
        erasure.erase(scope=scope, recognition_id="root", expected_revision=1, expected_plan=preview.revisions)
    preview = erasure.preview(scope=scope, recognition_id="root", expected_revision=1)
    erasure.erase(scope=scope, recognition_id="root", expected_revision=1, expected_plan=preview.revisions)
    assert records.read("recognition_graph_views", "view") is None
    assert records.read("recognition_graph_views", "other") is not None


def _scope(project_id="project-a"):
    return WorkScope("local-user", project_id)


def _recognition(identifier, scope, *, sources=(), parents=(), content="secret recognition"):
    return {
        "id": identifier, "scope": {"user_id": scope.user_id, "project_id": scope.project_id},
        "project_id": scope.project_id, "content": content, "state": "active", "version": 1,
        "source_experience_ids": [], "source_recognition_ids": list(sources), "parent_ids": list(parents),
        "source_experience_revisions": {}, "source_recognition_revisions": {}, "conditions": [],
    }


def test_erasure_removes_history_derived_state_and_task_documents(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "recognitions.sqlite3")
    scope = _scope()
    recognition_service = RecognitionService(records)
    initial_experience = recognition_service.stage_experience(scope=scope, content="initial evidence")
    root_candidate = recognition_service.propose(scope=scope, content="secret recognition", source_experience_ids=[initial_experience])
    root = recognition_service.publish(scope=scope, candidate_id=root_candidate.id, expected_revision=root_candidate.revision, reviewer="local-user", recognition_id="root")
    _put(records, "recognition_context_packets", "packet", {"id": "packet", "project_id": "project-a", "items": [{"id": root.id, "revision": root.revision, "content": "secret recognition"}], "messages": [{"role": "user", "content": "secret recognition"}]})
    _put(records, "recognition_tasks", "task", {"id": "task", "project_id": "project-a", "context_packet_id": "packet", "input": "secret task"})
    document = SQLiteDocumentRepository(records, namespace_id="recognition").create(DocumentDraft(title="task result", document_type="agent-result", markdown="secret output", project_id="project-a", source_refs=({"source_id": "task", "locator": "task://task"},)))
    retained = recognition_service.stage_experience(scope=scope, experience_id=f"experience-task-r{document['revision']}", content=f"task://task document://{document['id']}\nsecret output")
    child_candidate = recognition_service.propose(scope=scope, content="derived secret", source_experience_ids=[retained])
    child = recognition_service.publish(scope=scope, candidate_id=child_candidate.id, expected_revision=child_candidate.revision, reviewer="local-user", recognition_id="child")
    recognition_service.upsert_question(scope=scope, question_id="question", question="what", content="question secret", recognition_ids=[child.id], source_revisions={child.id: child.revision}, expected_revision=0)
    RelationProposalService(records).propose(scope, root.id, child.id, "derived_from", "secret evidence")
    cache = SQLiteEmbeddingCache(str(tmp_path / "recognition-vectors.sqlite3"))
    cache._connection.execute("INSERT INTO recognition_embedding_cache VALUES(?,?,?,?,?)", ("project-a", "root", 1, "test", "[1.0]"))
    cache._connection.commit()
    cache.close()

    service = ErasureService(records, tmp_path)
    preview = service.preview(scope=scope, recognition_id="root", expected_revision=1)
    receipt = service.erase(scope=scope, recognition_id="root", expected_revision=1)

    assert preview.counts["recognitions"] == 2
    assert receipt.erased is True and receipt.cache_state == "cleared"
    cache = SQLiteEmbeddingCache(str(tmp_path / "recognition-vectors.sqlite3"))
    assert cache.delete_recognition(project_id="project-a", recognition_id="root") == 0
    cache.close()
    for collection in ("recognitions", "recognition_versions", "recognition_relations", "recognition_relation_proposals", "recognition_candidates", "recognition_questions", "recognition_context_packets", "recognition_tasks", "documents", "document_revisions", "document_markdown"):
        assert records.list(collection) == ()
    assert records.read("recognition_experiences", initial_experience) is not None
    assert records.read("recognition_experiences", retained) is None
    assert "content" not in records.read("recognition_tombstones", "root").payload
    assert records.read("recognition_tombstones", "child") is not None
    assert records.read("recognition_tombstones", retained) is not None
    assert "secret" not in str(records.read("recognition_erasure_receipts", "root").payload)


def test_erasure_preserves_another_project_and_replays_same_id(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "recognitions.sqlite3")
    target_scope, other_scope = _scope(), _scope("project-b")
    _put(records, "recognitions", "root", _recognition("root", target_scope))
    _put(records, "recognitions", "other", _recognition("other", other_scope, content="other content"))
    _put(records, "recognition_context_packets", "other-packet", {"id": "other-packet", "project_id": "project-b", "items": [{"id": "root", "content": "foreign copy"}]})
    service = ErasureService(records, tmp_path)

    first = service.erase(scope=target_scope, recognition_id="root", expected_revision=1)
    replay = service.erase(scope=target_scope, recognition_id="root", expected_revision=999)

    assert first.erased and replay.idempotent
    assert records.read("recognitions", "other").payload["content"] == "other content"
    assert records.read("recognition_context_packets", "other-packet") is not None


def test_erasure_rejects_stale_revision_without_partial_delete(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "recognitions.sqlite3")
    scope = _scope()
    _put(records, "recognitions", "root", _recognition("root", scope))
    service = ErasureService(records, tmp_path)

    try:
        service.erase(scope=scope, recognition_id="root", expected_revision=2)
    except ErasureConflict:
        pass
    else:
        raise AssertionError("stale revision must be rejected")

    assert records.read("recognitions", "root") is not None
    assert records.read("recognition_tombstones", "root") is None


def test_erasure_rejects_changed_preview_and_tombstone_prevents_recreation(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "recognitions.sqlite3")
    service = RecognitionService(records)
    scope = _scope()
    experience = service.stage_experience(scope=scope, content="保留原始来源")
    candidate = service.propose(scope=scope, content="要删除的认识", source_experience_ids=[experience])
    root = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer="local-user", recognition_id="root")
    erasure = ErasureService(records, tmp_path)
    preview = erasure.preview(scope=scope, recognition_id=root.id, expected_revision=1)
    derived = service.propose(scope=scope, content="新派生候选", source_experience_ids=[experience], source_recognition_ids=[root.id])
    with pytest.raises(ErasureConflict, match="scope changed"):
        erasure.erase(scope=scope, recognition_id=root.id, expected_revision=1, expected_plan=preview.revisions)
    assert records.read("recognition_candidates", derived.id) is not None
    erasure.erase(scope=scope, recognition_id=root.id, expected_revision=1)
    candidate = service.propose(scope=scope, content="新的人工候选", source_experience_ids=[experience])
    with pytest.raises(RecognitionConflict, match="erased"):
        service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer="local-user", recognition_id="root")


def test_erasure_cache_failure_is_recorded_and_retried_without_recreating_content(tmp_path, monkeypatch):
    records = SQLiteStructuredRecordStore(tmp_path / "recognitions.sqlite3")
    _put(records, "recognitions", "root", _recognition("root", _scope()))
    erasure = ErasureService(records, tmp_path)
    monkeypatch.setattr(erasure, "_clear_cache", lambda *args: "pending")
    receipt = erasure.erase(scope=_scope(), recognition_id="root", expected_revision=1)
    assert receipt.cache_state == "pending" and records.read("recognitions", "root") is None
    restarted = ErasureService(records, tmp_path)
    assert restarted.recover_pending() == 1
    assert records.read("recognition_erasure_receipts", "root").payload["cache_state"] == "cleared"
    assert records.read("recognitions", "root") is None


def test_erasure_wal_checkpoint_pending_is_recovered_after_restart(tmp_path, monkeypatch):
    records = SQLiteStructuredRecordStore(tmp_path / "recognitions.sqlite3")
    _put(records, "recognitions", "root", _recognition("root", _scope()))
    erasure = ErasureService(records, tmp_path)
    monkeypatch.setattr(erasure, "_checkpoint", lambda: "wal_checkpoint_pending")
    receipt = erasure.erase(scope=_scope(), recognition_id="root", expected_revision=1)
    assert receipt.storage_boundary.endswith("wal_checkpoint_pending")
    ErasureService(records, tmp_path).recover_pending()
    assert records.read("recognition_erasure_receipts", "root").payload["storage_boundary"].endswith("wal_truncated")
    assert records.read("recognitions", "root") is None
