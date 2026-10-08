import pytest

from backend.memory_app.document_recognition import DocumentRecognitionError, extract_document_candidate
from backend.recognition import RecognitionService, WorkScope
from core.document_engine import SQLiteDocumentRepository
from core.document_engine.ports import DocumentDraft
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.fixture
def extract(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    documents = SQLiteDocumentRepository(records, namespace_id="recognition")
    document = documents.create(DocumentDraft(title="source", document_type="legacy-material",
        markdown="original evidence", project_id="alpha", source_refs=({"source_id": "source-1", "locator": "text:0:17"},)))
    service = RecognitionService(records)
    def run(**kwargs):
        return extract_document_candidate(documents, service, "alpha", document["id"], **kwargs)
    return run, documents, service, document


def test_old_workspace_ids_are_preserved_only_for_matching_revision(extract):
    run, documents, service, document = extract
    old = {"experience_id": "experience-workspace-old", "candidate_id": "candidate-workspace-old"}
    scope = WorkScope("local-user", "alpha")
    service.stage_experience(scope=scope, content="original evidence", experience_id=old["experience_id"],
        provenance={"kind": "workspace_confirmed_document", "actor": "local-user", "source_refs": [
            {"type": "document", "id": document["id"], "revision": 1}]})
    # Recover a stopped extraction after the experience was stored.
    first = run(previous=old)
    assert all(first[k] == v for k, v in old.items())
    service.publish(scope=scope, candidate_id=old["candidate_id"], expected_revision=1, reviewer="local-user")
    assert run(previous=old) == first
    documents.save_user_edit(document["id"], markdown="updated evidence", expected_revision=1)
    second = run(previous=old)
    assert second["document_revision"] == 2
    assert second["candidate_id"] != first["candidate_id"]
    assert service.records.read("recognition_candidates", old["candidate_id"]).payload["state"] == "published"
    # Legacy and workspace entry points converge on the new revision.
    assert run() == second


def test_partial_candidate_creation_can_resume(extract, monkeypatch):
    run, _, service, _ = extract
    propose = service.propose
    def fail(**kwargs):
        raise RuntimeError("synthetic stop")
    monkeypatch.setattr(service, "propose", fail)
    with pytest.raises(RuntimeError, match="synthetic stop"):
        run()
    assert len(service.records.list("recognition_experiences")) == 1
    monkeypatch.setattr(service, "propose", propose)
    first = run()
    assert run() == first
    assert len(service.records.list("recognition_experiences")) == 1
    assert len(service.records.list("recognition_candidates")) == 1


def test_conflicting_revision_identity_is_not_overwritten(extract):
    run, _, service, document = extract
    first = run()
    row = service.records.read("recognition_experiences", first["experience_id"])
    with service.records.begin() as tx:
        tx.put("recognition_experiences", row.object_id, {**row.payload, "content": "conflict"}, expected_revision=row.revision)
        tx.commit()
    with pytest.raises(DocumentRecognitionError, match="recognition_source_conflict"):
        run()
    assert service.records.read("recognition_experiences", row.object_id).payload["content"] == "conflict"


@pytest.mark.parametrize("length", [1284, 1801, 100_000])
def test_document_candidate_preserves_tail_restriction_for_human_review(extract, length):
    run, documents, service, document = extract
    heading = "可以执行清理命令。\n"
    restriction = "\n仅限隔离演示环境，禁止在生产环境运行上述命令。"
    markdown = heading + "背" * (length - len(heading) - len(restriction)) + restriction
    documents.save_user_edit(document["id"], markdown=markdown, expected_revision=1)

    result = run()
    candidate = service.records.read("recognition_candidates", result["candidate_id"])
    assert candidate.payload["content"] == markdown
    assert candidate.payload["state"] == "pending"
    assert candidate.payload["source_experience_ids"] == [result["experience_id"]]
    assert service.records.read("recognition_experiences", result["experience_id"]).payload["content"] == markdown


def test_reextract_keeps_existing_candidate_edit_and_conditions(extract):
    run, _, service, _ = extract
    result = run()
    scope = WorkScope("local-user", "alpha")
    service.edit_candidate(scope=scope, candidate_id=result["candidate_id"], expected_revision=1,
                           content="人工缩写且保留关键限制", conditions=["仅适用隔离演示"])
    assert run() == result
    candidate = service.records.read("recognition_candidates", result["candidate_id"])
    assert candidate.payload["content"] == "人工缩写且保留关键限制"
    assert candidate.payload["conditions"] == ["仅适用隔离演示"]
    assert candidate.revision == 2


def test_revision_retry_accepts_the_services_existing_outer_whitespace_normalization(extract):
    run, documents, service, document = extract
    markdown = "\n\n  完整正文。\n仅限隔离演示。  \n"
    documents.save_user_edit(document["id"], markdown=markdown, expected_revision=1)
    first = run()
    assert run(previous=first) == first
    assert service.records.read("recognition_candidates", first["candidate_id"]).payload["content"] == markdown.strip()
    assert documents.markdown(document["id"], revision=2) == markdown
