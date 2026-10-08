import json

import pytest

from backend.recognition import RecognitionService, WorkScope
from core.document_engine import DocumentDraft, SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.fixture
def env(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "recognitions.sqlite3")
    return records, RecognitionService(records), WorkScope("user", "project")


def publish(service, scope, *, experiences=(), recognitions=(), content="采用此结论"):
    candidate = service.propose(scope=scope, content=content,
        source_experience_ids=experiences, source_recognition_ids=recognitions)
    return service.publish(scope=scope, candidate_id=candidate.id,
        expected_revision=candidate.revision, reviewer="user")


def retain_artifact(records, service, scope, key, roots):
    documents = SQLiteDocumentRepository(records, namespace_id="recognition")
    document = documents.create(DocumentDraft(title="合成模型成果 " + key, document_type="agent-result",
        markdown="尚未核验的模型正文 " + key, project_id=scope.project_id,
        source_refs=({"source_id": key, "locator": "task://" + key},)))
    with records.begin() as tx:
        packet = tx.put("recognition_context_packets", "packet-" + key,
            {"id": "packet-" + key, "project_id": scope.project_id, "kind": "context",
             "state": "consumed", "task_id": key,
             "items": [{"id": item.id, "revision": item.revision} for item in roots]}, expected_revision=0)
        task = tx.put("recognition_tasks", key,
            {"id": key, "project_id": scope.project_id, "state": "completed",
             "document_id": document["id"], "context_packet_id": packet.object_id}, expected_revision=0)
        tx.commit()
    return service.stage_experience(scope=scope, content="保留的模型正文", experience_id="artifact-" + key,
        provenance={"kind": "model_generated_artifact", "actor": "agent", "source_refs": [
            {"type": "task", "id": key, "revision": task.revision},
            {"type": "document", "id": document["id"], "revision": document["revision"]},
            {"type": "context_packet", "id": packet.object_id, "revision": packet.revision},
        ]})


@pytest.mark.parametrize("kind,status", [
    ("user_statement", "user_asserted"),
    ("workspace_confirmed_document", "unverified"),
    (None, "unknown"),
])
def test_published_projection_keeps_source_identity_without_copying_body_or_actor(env, kind, status):
    records, service, scope = env
    provenance = {"kind": kind, "actor": "private-actor", "occurred_at": "2019-01-01T08:00:00+08:00"} if kind else None
    source = service.stage_experience(scope=scope, content="PRIVATE_SOURCE_BODY", provenance=provenance)
    recognition = publish(service, scope, experiences=[source])

    projection = service.get_recognition(scope=scope, recognition_id=recognition.id).retrieval_projection()

    assert projection["source_evidence_complete"] is True
    assert projection["source_evidence_reason"] is None
    assert projection["source_evidence"] == [{
        "type": "experience", "id": source, "revision": 1,
        "kind": kind or "legacy_unspecified", "epistemic_status": status,
        "recorded_at": records.read("recognition_experiences", source).payload["created_at"],
        "occurred_at": "2019-01-01T08:00:00+08:00" if kind else None,
        "artifact_status": None, "outcome_status": None,
    }]
    assert "PRIVATE_SOURCE_BODY" not in json.dumps(projection)
    assert "private-actor" not in json.dumps(projection)


def test_indirect_recognitions_and_nested_artifacts_keep_unique_exact_sources(env):
    records, service, scope = env
    source = service.stage_experience(scope=scope, content="原始用户陈述",
        provenance={"kind": "user_statement", "occurred_at": "2020-01-01T00:00:00Z"})
    first = publish(service, scope, experiences=[source])
    artifact_a = retain_artifact(records, service, scope, "task-a", [first])
    second = publish(service, scope, experiences=[artifact_a])
    artifact_b = retain_artifact(records, service, scope, "task-b", [second])
    third = publish(service, scope, experiences=[artifact_b])
    last = publish(service, scope, recognitions=[first.id, third.id])

    result = service.get_recognition(scope=scope, recognition_id=last.id).retrieval_projection()

    evidence = result["source_evidence"]
    assert result["source_evidence_complete"] is True
    assert [(item["id"], item["revision"]) for item in evidence] == sorted([(source, 1), (artifact_a, 1), (artifact_b, 1)])
    assert len(evidence) == 3
    assert {item["id"] for item in evidence if item["kind"] == "model_generated_artifact"} == {artifact_a, artifact_b}
    assert all(item["epistemic_status"] == "unverified" and item["outcome_status"] == "unknown"
        and item["artifact_status"] == "committed" for item in evidence if item["id"] != source)
    from backend.memory_app.context_adapter import ContextAdapter
    packet = ContextAdapter().compile_selected(scope.project_id, [result], [last.id], "成果是否已验证？", 1)
    assert packet["items"][0]["source_evidence"] == evidence
    wire = packet["messages"][1]["content"]
    assert '"kind":"model_generated_artifact"' in wire
    assert '"epistemic_status":"unverified"' in wire and '"outcome_status":"unknown"' in wire
    assert source in wire and artifact_a in wire and artifact_b in wire
    assert "PRIVATE_SOURCE_BODY" not in wire and "保留的模型正文" not in wire


def test_parent_lineage_and_unrelated_records_do_not_become_supporting_evidence(env):
    records, service, scope = env
    source = service.stage_experience(scope=scope, content="实际来源")
    other = service.stage_experience(scope=scope, content="无关来源")
    unrelated = publish(service, scope, experiences=[other])
    actual = publish(service, scope, experiences=[source])
    with records.begin() as tx:
        row = tx.read("recognitions", actual.id)
        tx.put("recognitions", actual.id, {**row.payload, "parent_ids": [unrelated.id]}, expected_revision=row.revision)
        tx.commit()
    result = service.get_recognition(scope=scope, recognition_id=actual.id).retrieval_projection()
    assert [item["id"] for item in result["source_evidence"]] == [source]


def test_source_revision_and_recording_time_are_not_replaced_by_recognition_revision(env):
    records, service, scope = env
    source = service.stage_experience(scope=scope, content="用户原始陈述", provenance={
        "kind": "user_statement", "occurred_at": "2018-01-01T00:00:00Z"})
    with records.begin() as tx:
        row = tx.read("recognition_experiences", source)
        recorded_at = row.payload["created_at"]
        tx.put("recognition_experiences", source, {**row.payload, "content": "修订后的来源内容",
            "updated_at": "2030-01-01T00:00:00Z"}, expected_revision=row.revision)
        tx.commit()
    recognition = publish(service, scope, experiences=[source])
    revised = service.revise(scope=scope, recognition_id=recognition.id,
        expected_revision=recognition.revision, content="只修订认识表述")
    source_item = revised.retrieval_projection()["source_evidence"][0]
    assert source_item["revision"] == 2
    assert source_item["recorded_at"] == recorded_at
    assert source_item["occurred_at"] == "2018-01-01T00:00:00Z"


def test_read_projection_does_not_change_history_exports_or_write_records(env, monkeypatch):
    records, service, scope = env
    source = service.stage_experience(scope=scope, content="原始资料")
    recognition = publish(service, scope, experiences=[source])
    before_markdown = service.export_markdown(scope=scope, recognition_id=recognition.id)
    collections = ("recognitions", "recognition_experiences", "recognition_candidates", "recognition_versions")
    before = {name: records.list(name) for name in collections}
    from core.storage_provider.sqlite_uow import SQLiteStructuredRecordUnitOfWork
    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, "put", lambda *_a, **_kw: pytest.fail("read wrote a record"))

    assert service.list_recognitions(scope=scope)[0].retrieval_projection()["source_evidence_complete"]
    assert service.retrieval_entries(scope=scope)[0]["source_evidence"]
    assert service.get_recognition(scope=scope, recognition_id=recognition.id).authorized
    assert service.export_markdown(scope=scope, recognition_id=recognition.id) == before_markdown
    assert {name: records.list(name) for name in collections} == before


def test_metadata_count_overflow_preserves_lifecycle_but_cannot_claim_completeness(env, monkeypatch):
    records, service, scope = env
    sources = [service.stage_experience(scope=scope, content=f"来源{i}") for i in range(3)]
    recognition = publish(service, scope, experiences=sources)
    import backend.recognition.service as owner
    monkeypatch.setattr(owner, "MAX_SOURCE_EVIDENCE_ITEMS", 2, raising=False)
    result = service.get_recognition(scope=scope, recognition_id=recognition.id).retrieval_projection()
    assert result["source_evidence_complete"] is False
    assert result["source_evidence_reason"] == "source_evidence_item_limit"
    assert result["source_evidence"] == []
    assert result["authorized"] is True and result["recorded_state"] == "active"
    assert records.read("recognitions", recognition.id).revision == recognition.revision


def test_metadata_byte_overflow_is_explicit_and_does_not_truncate_time(env, monkeypatch):
    _, service, scope = env
    source = service.stage_experience(scope=scope, content="资料", provenance={
        "kind": "user_statement", "occurred_at": "2026-01-01T00:00:00." + "1" * 800 + "Z"})
    recognition = publish(service, scope, experiences=[source])
    import backend.recognition.service as owner
    monkeypatch.setattr(owner, "MAX_SOURCE_EVIDENCE_BYTES", 512, raising=False)
    result = service.get_recognition(scope=scope, recognition_id=recognition.id).retrieval_projection()
    assert result["source_evidence_complete"] is False
    assert result["source_evidence_reason"] == "source_evidence_byte_limit"
    assert result["source_evidence"] == []
    assert result["authorized"] is True


def test_revoked_or_changed_source_does_not_expose_partial_evidence_as_complete(env):
    _, service, scope = env
    sources = [service.stage_experience(scope=scope, content=f"资料{i}") for i in range(2)]
    recognition = publish(service, scope, experiences=sources)
    service.revoke_experience(scope=scope, experience_id=sources[1], expected_revision=1)
    result = service.get_recognition(scope=scope, recognition_id=recognition.id).retrieval_projection()
    assert result["source_evidence_complete"] is False
    assert result["source_evidence"] == []
    assert result["authorized"] is False


def test_malformed_stored_time_makes_projection_incomplete_without_new_publication_rules(env):
    records, service, scope = env
    source = service.stage_experience(scope=scope, content="旧资料")
    with records.begin() as tx:
        row = tx.read("recognition_experiences", source)
        tx.put("recognition_experiences", source, {**row.payload, "created_at": "invalid-time"},
            expected_revision=row.revision)
        tx.commit()
    # Existing eligibility concerns live source identity/revision, not fact verification.
    published = publish(service, scope, experiences=[source])
    result = published.retrieval_projection()
    assert published.state == "active" and published.authorized
    assert result["source_evidence_complete"] is False and result["source_evidence"] == []
    assert result["source_evidence_reason"] == "source_evidence_invalid_metadata"
    from backend.memory_app.context_adapter import ContextSelectionError, format_recognition_content
    with pytest.raises(ContextSelectionError, match="recognition_source_evidence_incomplete"):
        format_recognition_content(result)
