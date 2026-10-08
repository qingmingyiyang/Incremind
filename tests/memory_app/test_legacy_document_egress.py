"""Legacy review provenance through real isolated repositories; no app bootstrap."""

from copy import deepcopy

import pytest

from backend.memory_app.document_recognition import extract_document_candidate
from backend.memory_app.legacy_intake_review import LegacyIntakeReview
from backend.memory_app.source_egress import SourceEgressService
from backend.recognition import RecognitionConflict, RecognitionError, RecognitionService, WorkScope
from core.document_engine import DocumentDraft, SQLiteDocumentRepository
from core.job_runner.runtime import InMemoryJobRepository
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore


@pytest.fixture(params=[None, "text:0:25", "video:00:00:01-00:00:05"],
                ids=["created-document", "existing-text", "existing-video"])
def legacy_document(tmp_path, request):
    root = tmp_path / "runtime"
    objects = JsonObjectStore(root / ".rebuild-data", legacy_root=root / "library", namespace_id="default")
    objects.write("sources", "source-one", {
        "id": "source-one", "project_id": "alpha", "title": "Legacy evidence", "type": "text",
        "metadata": {"content_snapshot": "Original source evidence."},
        "created_at": "2026-09-24T00:00:00Z",
    }, expected_revision=0)
    records = SQLiteStructuredRecordStore(root / ".rebuild-data" / "structured-records.sqlite3")
    documents = SQLiteDocumentRepository(records)
    if request.param is not None:
        refs = [{"source_id": "source-one", "locator": request.param}]
        if request.param.startswith("video:"):
            refs.append({"source_id": "source-one", "locator": "video:00:00:10-00:00:15"})
        documents.create(DocumentDraft(
            title="Legacy evidence", document_type="legacy_text", markdown="Machine draft",
            source_refs=tuple(refs), project_id="alpha",
        ))
    with records.begin() as tx:
        tx.put("workspace_review_intents", "review-source-one", {
            "schema_version": "1.0.0", "id": "review-source-one", "source_id": "source-one",
            "project_id": "alpha", "job_id": "capture-one", "state": "pending", "source_revision": 1,
        }, expected_revision=0)
        tx.commit()
    jobs = InMemoryJobRepository()
    jobs.save({"id": "capture-one", "status": "completed", "source_id": "source-one"})
    review = LegacyIntakeReview(root, records, documents, object_store=objects, jobs=jobs)
    current = review.get("source-one", "alpha")
    current = review.save_draft(
        "source-one", "alpha", "Human reviewed evidence.", expected_revision=current["revision"],
        expected_document_basis=current["document_basis"],
    )
    confirmed = review.confirm(
        "source-one", "alpha", expected_revision=current["revision"],
        expected_document_basis=current["document_basis"], expected_markdown=current["draft_markdown"],
    )
    assert confirmed["status"] == "confirmed"
    service = RecognitionService(records)
    scope = WorkScope("local-user", "alpha")
    extracted = extract_document_candidate(documents, service, "alpha", confirmed["document_id"])
    recognition = service.publish(
        scope=scope, candidate_id=extracted["candidate_id"], expected_revision=1, reviewer="local-user",
    )
    return {
        "records": records, "documents": documents, "objects": objects, "service": service,
        "scope": scope, "confirmed": confirmed, "extracted": extracted, "recognition": recognition,
        "egress": SourceEgressService(records),
    }


def _snapshot(env, *, scope=None, recognition=None):
    recognition = recognition or env["recognition"]
    return env["egress"].snapshot(scope or env["scope"], [
        {"type": "recognition", "id": recognition.id, "revision": recognition.revision},
    ])


def _authorize(env, experience_id=None):
    return env["egress"].set_policy(
        env["scope"], "experience", experience_id or env["extracted"]["experience_id"], 1, 0,
        ["generation", "embedding", "rerank"],
    )


def _change(env, collection, identity, **changes):
    with env["records"].begin() as tx:
        row = tx.read(collection, identity)
        tx.put(collection, identity, {**row.payload, **changes}, expected_revision=row.revision)
        tx.commit()


def _delete(env, collection, identity):
    with env["records"].begin() as tx:
        row = tx.read(collection, identity)
        tx.delete(collection, identity, expected_revision=row.revision)
        tx.commit()


def test_confirmed_legacy_document_can_authorize_and_validate_recognition(legacy_document):
    env = legacy_document
    assert env["records"].list("workspace_items") == ()
    # Both absent policies and explicit non-private policies allow every purpose.
    env["egress"].require(_snapshot(env), "generation")
    _authorize(env)
    snapshot = _snapshot(env)
    env["egress"].require(snapshot, "generation")
    env["egress"].validate_snapshot(env["scope"], snapshot)
    assert env["records"].list("workspace_items") == ()
    for purpose in ("embedding", "rerank"):
        env["egress"].require(snapshot, purpose)
    env["egress"].set_policy(env["scope"], "experience", env["extracted"]["experience_id"], 1, 1, [])
    with pytest.raises(RecognitionError, match="cannot broaden"):
        env["egress"].set_policy(env["scope"], "recognition", env["recognition"].id, 1, 0,
                                 ["generation", "embedding", "rerank"])


@pytest.mark.parametrize("changes", [
    {"id": "review-other"}, {"source_id": "other"}, {"project_id": "beta"},
    {"document_id": "other"}, {"state": "pending"}, {"state": "confirming"},
    {"document_revision": True}, {"document_revision": 0}, {"document_revision": 99},
    {"expected_document_id": "other"}, {"expected_document_revision": True},
    {"confirmed_markdown": "not the confirmed evidence"},
])
def test_confirmation_corruption_rejects_frozen_and_new_snapshots(legacy_document, changes):
    env = legacy_document
    _authorize(env)
    snapshot = _snapshot(env)
    _change(env, "workspace_review_intents", "review-source-one", **changes)
    with pytest.raises(RecognitionConflict):
        env["egress"].validate_snapshot(env["scope"], snapshot)
    with pytest.raises(RecognitionConflict):
        _snapshot(env)


@pytest.mark.parametrize("collection", ["workspace_review_intents", "documents",
                                       "document_revisions", "document_markdown"])
def test_missing_authority_or_historical_evidence_is_rejected(legacy_document, collection):
    env = legacy_document
    _authorize(env)
    snapshot = _snapshot(env)
    doc = env["extracted"]["document_id"]
    identity = ("review-source-one" if collection == "workspace_review_intents" else doc
                if collection == "documents" else f"{doc}~r{env['extracted']['document_revision']}")
    _delete(env, collection, identity)
    with pytest.raises(RecognitionConflict):
        env["egress"].validate_snapshot(env["scope"], snapshot)
    with pytest.raises(RecognitionConflict):
        _snapshot(env)


@pytest.mark.parametrize("refs", [
    [], [{"locator": "text:0:1"}], [{"source_id": "source-one"}],
    [{"source_id": "other", "locator": "text:0:1"}],
    [{"source_id": "source-one", "locator": "source://other"}],
    [{"source_id": "source-one", "locator": "text:0:1"},
     {"source_id": "other", "locator": "video:0:1"}],
    [{"source_id": "source-one", "locator": "workspace://source-one"}],
    [{"source_id": "source-one", "locator": "workspace://other"}],
    [{"source_id": "source-one", "locator": "workspace://source-one"}] * 2,
])
def test_missing_ambiguous_or_workspace_refs_cannot_use_legacy_confirmation(legacy_document, refs):
    env = legacy_document
    _authorize(env)
    snapshot = _snapshot(env)
    key = f"{env['extracted']['document_id']}~r{env['extracted']['document_revision']}"
    record = env["records"].read("document_revisions", key)
    _change(env, "document_revisions", key,
            source_snapshot={**record.payload["source_snapshot"], "source_refs": refs})
    with pytest.raises(RecognitionConflict):
        env["egress"].validate_snapshot(env["scope"], snapshot)
    with pytest.raises(RecognitionConflict):
        _snapshot(env)


def test_edits_and_archive_preserve_frozen_evidence_and_new_reviewed_revision(legacy_document):
    env = legacy_document
    source_before = deepcopy(env["objects"].read("sources", "source-one"))
    source_revision = env["objects"].revision("sources", "source-one")
    _authorize(env)
    old = _snapshot(env)
    doc = env["extracted"]["document_id"]
    edited = env["documents"].save_user_edit(
        doc, markdown="Later user edit", expected_revision=env["extracted"]["document_revision"],
    )
    extracted = extract_document_candidate(env["documents"], env["service"], "alpha", doc)
    recognition = env["service"].publish(scope=env["scope"], candidate_id=extracted["candidate_id"],
                                         expected_revision=1, reviewer="local-user")
    _authorize(env, extracted["experience_id"])
    new = _snapshot(env, recognition=recognition)
    env["documents"].archive(doc, expected_revision=edited["revision"])
    for snapshot in (old, new):
        env["egress"].validate_snapshot(env["scope"], snapshot)
        env["egress"].require(snapshot, "generation")
    assert env["documents"].markdown(doc, revision=env["extracted"]["document_revision"]) == "Human reviewed evidence."
    assert env["objects"].read("sources", "source-one") == source_before
    assert env["objects"].revision("sources", "source-one") == source_revision


def test_changed_source_binding_in_later_revision_needs_new_confirmation(legacy_document):
    env = legacy_document
    doc = env["extracted"]["document_id"]
    env["documents"].save_user_edit(doc, markdown="Different evidence",
        expected_revision=env["extracted"]["document_revision"],
        source_refs=({"source_id": "source-one", "locator": "text:100:200"},))
    extracted = extract_document_candidate(env["documents"], env["service"], "alpha", doc)
    with pytest.raises(RecognitionConflict, match="confirmation evidence"):
        _authorize(env, extracted["experience_id"])


def test_project_isolation_and_policy_revocation(legacy_document):
    env = legacy_document
    _authorize(env)
    snapshot = _snapshot(env)
    with pytest.raises(RecognitionConflict):
        _snapshot(env, scope=WorkScope("local-user", "beta"))
    with pytest.raises(RecognitionConflict):
        _snapshot(env, scope=WorkScope("other-user", "alpha"))
    env["egress"].set_policy(env["scope"], "experience", env["extracted"]["experience_id"], 1, 1, [])
    with pytest.raises(RecognitionConflict):
        env["egress"].validate_snapshot(env["scope"], snapshot)
    with pytest.raises(RecognitionConflict, match="not authorized"):
        env["egress"].require(_snapshot(env), "generation")


def test_experience_revocation_invalidates_published_recognition(legacy_document):
    env = legacy_document
    _authorize(env)
    snapshot = _snapshot(env)
    env["service"].revoke_experience(scope=env["scope"], experience_id=env["extracted"]["experience_id"],
                                     expected_revision=1)
    with pytest.raises(RecognitionConflict):
        env["egress"].validate_snapshot(env["scope"], snapshot)
    assert env["service"].retrieval_entries(scope=env["scope"]) == ()


def test_document_project_change_rejects_historical_snapshot(legacy_document):
    env = legacy_document
    _authorize(env)
    snapshot = _snapshot(env)
    _change(env, "documents", env["extracted"]["document_id"], project_id="beta")
    with pytest.raises(RecognitionConflict):
        env["egress"].validate_snapshot(env["scope"], snapshot)


@pytest.mark.parametrize("legacy_document", ["text:0:25", "video:00:00:01-00:00:05"], indirect=True)
def test_preconfirmation_revision_cannot_borrow_later_approval(legacy_document):
    env = legacy_document
    identity = env["extracted"]["experience_id"]
    experience = env["records"].read("recognition_experiences", identity)
    provenance = deepcopy(experience.payload["provenance"])
    provenance["source_refs"][0]["revision"] = 1
    _change(env, "recognition_experiences", identity, provenance=provenance)
    with pytest.raises(RecognitionConflict, match="confirmation revision"):
        env["egress"].snapshot(env["scope"], [{"type": "experience", "id": identity, "revision": 2}])


@pytest.mark.parametrize("collection", ["document_revisions", "document_markdown"])
def test_later_revision_still_requires_confirmation_history(legacy_document, collection):
    env = legacy_document
    doc = env["extracted"]["document_id"]
    confirmed_revision = env["extracted"]["document_revision"]
    env["documents"].save_user_edit(doc, markdown="Later user edit", expected_revision=confirmed_revision)
    extracted = extract_document_candidate(env["documents"], env["service"], "alpha", doc)
    _authorize(env, extracted["experience_id"])
    roots = [{"type": "experience", "id": extracted["experience_id"], "revision": 1}]
    snapshot = env["egress"].snapshot(env["scope"], roots)
    _delete(env, collection, f"{doc}~r{confirmed_revision}")
    with pytest.raises(RecognitionConflict):
        env["egress"].validate_snapshot(env["scope"], snapshot)
    with pytest.raises(RecognitionConflict):
        env["egress"].snapshot(env["scope"], roots)


def test_workspace_confirmation_keeps_its_own_strict_authority(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "workspace.sqlite3")
    documents = SQLiteDocumentRepository(records)
    document = documents.create(DocumentDraft(title="Workspace evidence", document_type="workspace",
        markdown="Reviewed workspace content", project_id="alpha",
        source_refs=({"source_id": "workspace-one", "locator": "workspace://workspace-one"},)))
    with records.begin() as tx:
        tx.put("workspace_items", "workspace-one", {
            "id": "workspace-one", "project_id": "alpha", "status": "confirmed",
            "document_id": document["id"],
        }, expected_revision=0)
        tx.commit()
    service = RecognitionService(records)
    scope = WorkScope("local-user", "alpha")
    extracted = extract_document_candidate(documents, service, "alpha", document["id"])
    authority = SourceEgressService(records)
    authority.set_policy(scope, "experience", extracted["experience_id"], 1, 0, ["generation", "embedding", "rerank"])
    roots = [{"type": "experience", "id": extracted["experience_id"], "revision": 1}]
    snapshot = authority.snapshot(scope, roots)
    authority.require(snapshot, "generation")
    authority.validate_snapshot(scope, snapshot)
    assert snapshot["nodes"][0]["dependency_revisions"]["workspace_item_revision"] == 1
    _change({"records": records}, "workspace_items", "workspace-one", status="ready")
    with pytest.raises(RecognitionConflict, match="source egress snapshot conflicted"):
        authority.validate_snapshot(scope, snapshot)
    with pytest.raises(RecognitionConflict, match="workspace document source"):
        authority.snapshot(scope, roots)


def test_legacy_recognition_is_consumed_by_workspace_http_question(legacy_document, tmp_path):
    import json
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.memory_app.workspace import install_workspace_routes
    from backend.memory_app.model_config import ModelConfiguration
    from backend.security.secrets import InMemorySecretStore

    env = legacy_document
    _authorize(env)
    sent = []

    def completion(**kwargs):
        sent.append(kwargs["messages"])
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(
            {"answer": "Evidence retained.", "citations": [1]})}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}}

    models = ModelConfiguration(env["records"], tmp_path / "runtime", InMemorySecretStore(),
        completion_fn=completion)
    models.update("generation", {"base_url": "https://example.invalid/v1", "model": "synthetic-model",
        "api_key": "synthetic-only", "allow_remote": True, "expected_revision": 0})

    app = FastAPI()
    install_workspace_routes(app, runtime_root=tmp_path / "runtime", records=env["records"],
                             models=models, documents=env["documents"], service=env["service"])
    with TestClient(app) as http:
        request = {"project_id": "alpha", "question": "Human reviewed evidence"}
        preview = http.post("/api/workspace/v1/ask/preview", json=request)
        assert preview.status_code == 200, preview.text
        sources = preview.json()["sources"]
        assert sources[0]["type"] == "recognition"
        assert sources[0]["id"] == env["recognition"].id
        response = http.post("/api/workspace/v1/ask", json={**request, "preview_id": preview.json()["preview_id"]})
        assert response.status_code == 200, response.text
        assert response.json()["sources"][0]["id"] == env["recognition"].id
        assert sources[0]["excerpt"] in sent[0][-1]["content"]
    assert len(sent) == 1
