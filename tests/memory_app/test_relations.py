from __future__ import annotations

import pytest

from backend.memory_app.relations import RelationProposalService
from backend.recognition import RecognitionConflict, RecognitionError, RecognitionService, WorkScope
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.fixture
def records(tmp_path):
    return SQLiteStructuredRecordStore(tmp_path / "recognitions.sqlite3")


@pytest.fixture
def service(records):
    return RecognitionService(records)


@pytest.fixture
def relations(records):
    return RelationProposalService(records)


@pytest.fixture
def scope():
    return WorkScope("user-1", "project-1")


def _published(service, scope, content):
    experience_id = service.stage_experience(scope=scope, content=f"evidence: {content}")
    candidate = service.propose(scope=scope, content=content, source_experience_ids=[experience_id])
    return service.publish(
        scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision, reviewer="user-1"
    )


def test_proposal_is_project_scoped_and_not_an_active_relation(relations, service, scope):
    source = _published(service, scope, "SQLite is authoritative")
    target = _published(service, scope, "The workbench uses SQLite")

    proposal = relations.propose(scope, source.id, target.id, "supports", "I verified both statements manually.")

    assert proposal["state"] == "pending"
    assert proposal["source"] == "manual"
    assert proposal["from_revision"] == source.revision
    assert relations.list(WorkScope("user-1", "project-2")) == ()
    with pytest.raises(RecognitionConflict, match="work scope"):
        relations.propose(
            WorkScope("user-1", "project-2"), source.id, target.id, "supports", "Cross-project proposal."
        )
    assert [item["id"] for item in service.retrieval_entries(scope=scope)] == sorted([source.id, target.id])
    assert service.records.list("recognition_relations") == ()


@pytest.mark.parametrize("changed_endpoint", ("source", "target"))
def test_review_requires_current_active_endpoints(relations, service, scope, changed_endpoint):
    source = _published(service, scope, "The API is local")
    target = _published(service, scope, "Local origin checks protect the API")
    proposal = relations.propose(scope, source.id, target.id, "supports", "Manual architecture review.")
    changed = source if changed_endpoint == "source" else target
    service.revise(
        scope=scope,
        recognition_id=changed.id,
        expected_revision=changed.revision,
        content=f"{changed.content} (revised)",
    )

    with pytest.raises(RecognitionConflict, match=f"{changed_endpoint} recognition revision changed"):
        relations.review(scope, proposal["id"], proposal["revision"], "approved")

    assert relations.list(scope)[0]["state"] == "pending"


def test_pending_or_rejected_proposals_never_become_edges(relations, service, scope):
    source = _published(service, scope, "Review is required")
    target = _published(service, scope, "Candidates remain isolated")
    pending = relations.propose(scope, source.id, target.id, "refutes", "Manual review note.")
    rejected = relations.review(scope, pending["id"], pending["revision"], "rejected")

    assert rejected["state"] == "rejected"
    assert service.records.list("recognition_relations") == ()


def test_stale_proposal_can_still_be_rejected(relations, service, scope):
    source = _published(service, scope, "A pending relation can expire")
    target = _published(service, scope, "An expired proposal still needs closure")
    proposal = relations.propose(scope, source.id, target.id, "supplements", "Manual review note.")
    service.revise(
        scope=scope,
        recognition_id=source.id,
        expected_revision=source.revision,
        content="The relation proposal endpoint changed",
    )

    rejected = relations.review(scope, proposal["id"], proposal["revision"], "rejected")

    assert rejected["state"] == "rejected"


def test_proposal_requires_two_distinct_recognition_ids(relations, service, scope):
    recognition = _published(service, scope, "One recognition cannot form a relation")

    with pytest.raises(RecognitionError, match="requires two recognitions"):
        relations.propose(scope, recognition.id, recognition.id, "supports", "Manual review note.")


def test_review_has_proposal_compare_and_swap(relations, service, scope):
    source = _published(service, scope, "Records have revisions")
    target = _published(service, scope, "Review uses compare and swap")
    proposal = relations.propose(scope, source.id, target.id, "derived_from", "Manual provenance note.")
    approved = relations.review(scope, proposal["id"], proposal["revision"], "approved")

    with pytest.raises(RecognitionConflict, match="relation proposal revision conflicted"):
        relations.review(scope, proposal["id"], proposal["revision"], "rejected")

    assert approved["revision"] == proposal["revision"] + 1
    assert approved["state"] == "approved"


@pytest.mark.parametrize("relation", ("supports", "refutes", "supplements", "supersedes", "derived_from"))
def test_all_supported_relation_types_are_reviewable(relations, service, scope, relation):
    source = _published(service, scope, f"source for {relation}")
    target = _published(service, scope, f"target for {relation}")

    proposal = relations.propose(scope, source.id, target.id, relation, "Manual relation evidence.")

    assert relations.review(scope, proposal["id"], proposal["revision"], "approved")["relation"] == relation
