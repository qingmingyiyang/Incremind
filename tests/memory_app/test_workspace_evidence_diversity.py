"""Identical derivations must not consume the whole question evidence budget."""

import asyncio
import json

import pytest
from fastapi import FastAPI, HTTPException

from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.workspace import install_workspace_routes
from backend.security.secrets import InMemorySecretStore
from backend.recognition import RecognitionService, WorkScope
from core.document_engine import SQLiteDocumentRepository
from core.document_engine.ports import DocumentDraft
from core.storage_provider import SQLiteStructuredRecordStore


class CaptureModel:
    def __init__(self):
        self.messages = []

    def __call__(self, **kwargs):
        self.messages = kwargs["messages"]
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(
            {"answer": "The evidence disagrees.", "citations": [1, 2]})}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}}


def product(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    documents = SQLiteDocumentRepository(records, namespace_id="test")
    service = RecognitionService(records)
    model = CaptureModel()
    models = ModelConfiguration(records, tmp_path, InMemorySecretStore(), completion_fn=model)
    models.update("generation", {"base_url": "https://example.invalid/v1", "model": "synthetic-model",
        "api_key": "synthetic-only", "allow_remote": True, "expected_revision": 0})
    domains = install_workspace_routes(FastAPI(), runtime_root=tmp_path, records=records,
        documents=documents, service=service, models=models)
    query = domains.query
    return records, documents, service, query, model


SCOPE = WorkScope("local-user", "alpha")
BODY = "alphaomega uses the old rule."


def experience(records, service):
    identity = service.stage_experience(scope=SCOPE, content="Synthetic evidence")
    SourceEgressService(records).set_policy(SCOPE, "experience", identity, 1, 0, ["generation", "embedding", "rerank"])
    return identity


def publish(service, identity, content=BODY, conditions=None):
    candidate = service.propose(scope=SCOPE, content=content, source_experience_ids=[identity],
                                conditions=conditions or [])
    return service.publish(scope=SCOPE, candidate_id=candidate.candidate_id,
                           expected_revision=candidate.revision, reviewer="local-user")


def test_exact_derivations_leave_room_for_conflicting_document_and_keep_exclusion_trail(tmp_path):
    records, documents, service, query, model = product(tmp_path)
    identity = experience(records, service)
    duplicate_ids = {publish(service, identity).id for _ in range(6)}
    document = documents.create(DocumentDraft(title="Correction", document_type="legacy-material",
        markdown="alphaomega now uses the corrected rule; the old rule is superseded.",
        source_refs=({"source_id": "synthetic-correction", "locator": "text:0:72"},), project_id="alpha"))
    plan = query.prepare_ask("alpha", "alphaomega corrected?")
    preview = query.public_ask_preview("fixture", plan)
    assert [source["type"] for source in preview["sources"]] == ["recognition", "document"]
    assert preview["sources"][1]["id"] == document["id"]
    representative = preview["sources"][0]
    omitted = preview["excluded_sources"]
    assert {entry["id"] for entry in omitted} == duplicate_ids - {representative["id"]}
    assert all(entry["reason"] == "duplicate_recognition_evidence" and
               entry["duplicate_of"] == {"id": representative["id"], "revision": 1} for entry in omitted)
    assert all("content" not in entry for entry in omitted)
    preview_id = query.store_ask_preview(plan)
    result = asyncio.run(query.execute_ask(preview_id, "alpha", "alphaomega corrected?", False))
    assert "corrected rule" in model.messages[-1]["content"]
    assert model.messages[-1]["content"].count(BODY) == 1
    assert result["excluded_sources"] == omitted
    assert len(service.list_recognitions(scope=SCOPE)) == 6  # No storage merge or deletion.


def test_independent_evidence_stays_stored_but_recall_skips_duplicate_text(tmp_path):
    records, _, service, query, _ = product(tmp_path)
    ids = {publish(service, experience(records, service)).id for _ in range(2)}
    collected = query.collect_candidates("alpha", "alphaomega")
    assert {entry["id"] for entry in collected["candidates"]} == ids
    before = {row.object_id: (row.revision, row.payload) for row in records.list("recognitions")}
    plan = query.prepare_ask("alpha", "alphaomega", collected=collected)
    assert len(plan["chosen"]) == 1
    assert {entry["id"] for entry in plan["chosen"]} <= ids
    assert sum(row.get("skipped_duplicate", 0) for row in plan["trace"]) == 1
    assert plan["excluded_sources"] == []
    assert {row.object_id: (row.revision, row.payload) for row in records.list("recognitions")} == before
    assert {entry.id for entry in service.list_recognitions(scope=SCOPE)} == ids


@pytest.mark.parametrize("other,conditions,selected,skipped", [
    ("alphaomega does not use the old rule.", [], 1, 1),
    ("alphaomega uses the old rule for 90 days.", [], 2, 0),
    (BODY, ["Only for prototypes."], 1, 1),
])
def test_distinct_statements_stay_candidates_under_recall_dedup(tmp_path, other, conditions, selected, skipped):
    records, _, service, query, _ = product(tmp_path)
    identity = experience(records, service)
    ids = {publish(service, identity).id, publish(service, identity, other, conditions).id}
    collected = query.collect_candidates("alpha", "alphaomega")
    assert {entry["id"] for entry in collected["candidates"]} == ids
    before = {row.object_id: (row.revision, row.payload) for row in records.list("recognitions")}
    plan = query.prepare_ask("alpha", "alphaomega", collected=collected)
    assert len(plan["chosen"]) == selected
    assert {entry["id"] for entry in plan["chosen"]} <= ids
    assert sum(row.get("skipped_duplicate", 0) for row in plan["trace"]) == skipped
    assert plan["excluded_sources"] == []
    assert {row.object_id: (row.revision, row.payload) for row in records.list("recognitions")} == before
    assert {entry.id for entry in service.list_recognitions(scope=SCOPE)} == ids



def test_confirmed_refutes_pair_keeps_both_near_duplicate_statements(tmp_path):
    from backend.memory_app.v2.links import InsightLinks

    records, _, service, query, _ = product(tmp_path)
    identity = experience(records, service)
    first = publish(service, identity)
    other = publish(service, identity, "alphaomega does not use the old rule.")
    ids = {first.id, other.id}
    collected = query.collect_candidates("alpha", "alphaomega")
    assert {entry["id"] for entry in collected["candidates"]} == ids
    unlinked = query.prepare_ask("alpha", "alphaomega", collected=collected)
    assert len(unlinked["chosen"]) == 1
    assert sum(row.get("skipped_duplicate", 0) for row in unlinked["trace"]) == 1
    links = InsightLinks(records, service)
    proposal = links.propose("alpha", first.id, other.id, "refutes", "Synthetic contradictory evidence")
    links.review("alpha", proposal["id"], proposal["revision"], True)
    before = {row.object_id: (row.revision, row.payload) for row in records.list("recognitions")}
    plan = query.prepare_ask("alpha", "alphaomega")
    assert {entry["id"] for entry in plan["chosen"]} == ids
    assert sum(row.get("skipped_duplicate", 0) for row in plan["trace"]) == 0
    assert plan["excluded_sources"] == []
    query.validate_ask_plan(plan)
    assert {row.object_id: (row.revision, row.payload) for row in records.list("recognitions")} == before
    assert {entry.id for entry in service.list_recognitions(scope=SCOPE)} == ids


def test_selected_representative_still_requires_frozen_revision_validation(tmp_path):
    records, _, service, query, model = product(tmp_path)
    identity = experience(records, service)
    for _ in range(2):
        publish(service, identity)
    plan = query.prepare_ask("alpha", "alphaomega")
    assert len(plan["chosen"]) == 1
    preview_id = query.store_ask_preview(plan)
    SourceEgressService(records).set_policy(SCOPE, "experience", identity, 1, 1, [])
    with pytest.raises(HTTPException) as failure:
        asyncio.run(query.execute_ask(preview_id, "alpha", "alphaomega", False))
    assert failure.value.detail == "source_changed_retry"
    assert model.messages == []


@pytest.mark.parametrize("metadata", [
    {"source_evidence": [], "source_evidence_complete": False, "source_evidence_reason": "source_evidence_item_limit"},
    {"source_evidence": [{"type": "experience", "revision": True}], "source_evidence_complete": True},
])
def test_incomplete_or_invalid_source_identity_is_excluded_as_a_whole(tmp_path, monkeypatch, metadata):
    records, _, service, query, model = product(tmp_path)
    identity = experience(records, service)
    recognition = publish(service, identity)
    entry = {**service.retrieval_entries(scope=SCOPE)[0], **metadata}
    monkeypatch.setattr(service, "retrieval_entries", lambda **kwargs: (entry,))
    plan = query.prepare_ask("alpha", "alphaomega")
    preview = query.public_ask_preview("fixture", plan)
    assert preview["no_match"] is True
    assert preview["sources"] == []
    assert preview["excluded_sources"] == [{"type": "recognition", "id": recognition.id,
        "revision": 1, "reason": "recognition_source_evidence_incomplete"}]
    assert model.messages == []
