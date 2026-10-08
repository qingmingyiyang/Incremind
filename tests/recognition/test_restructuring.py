from __future__ import annotations

import pytest

from backend.recognition import RecognitionConflict, RecognitionError, RecognitionService, WorkScope
from backend.recognition.restructuring import RestructureProposalService
from core.storage_provider import SQLiteStructuredRecordStore, SQLiteStructuredRecordUnitOfWork


@pytest.fixture
def service(tmp_path):
    return RecognitionService(SQLiteStructuredRecordStore(tmp_path / "recognitions.sqlite3"))


@pytest.fixture
def scope():
    return WorkScope("user-1", "project-1")


def _published(service, scope, content, *, experience_id=None):
    experience_id = experience_id or service.stage_experience(scope=scope, content=f"evidence: {content}")
    candidate = service.propose(scope=scope, content=content, source_experience_ids=[experience_id])
    return service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision, reviewer="user-1")


def test_capture_save_and_review_split_are_atomic_and_idempotent(service, scope):
    parent = _published(service, scope, "SQLite and web are current")
    authority = RestructureProposalService(service.records)
    snapshot = authority.capture(scope=scope, recognition_ids=[parent.id], expected_revisions={parent.id: parent.revision})

    saved = authority.save(
        scope=scope,
        proposal_id="proposal-split",
        snapshot=snapshot,
        operation="split",
        outputs=[
            {"recognition_id": "recognition-db", "content": "SQLite is current", "conditions": ["local"],
             "source_experience_ids": list(parent.source_experience_ids), "source_recognition_ids": []},
            {"recognition_id": "recognition-web", "content": "Web is current", "conditions": [],
             "source_experience_ids": list(parent.source_experience_ids), "source_recognition_ids": []},
        ],
        reason="separate independent conclusions",
        step_metadata={"source": "model", "implementation_version": "v1"},
    )
    approved = authority.review(scope=scope, proposal_id=saved["id"], expected_revision=saved["revision"], decision="approved", reviewer="user-1")
    repeated = authority.review(scope=scope, proposal_id=saved["id"], expected_revision=saved["revision"], decision="approved", reviewer="user-1")

    assert approved["state"] == "approved"
    assert approved["result_recognition_ids"] == ["recognition-db", "recognition-web"]
    assert repeated == approved
    assert service.get_recognition(scope=scope, recognition_id=parent.id).state == "superseded"
    assert {item.id for item in service.list_recognitions(scope=scope)} == {"recognition-db", "recognition-web"}


def test_snapshot_is_recursive_and_review_rejects_stale_evidence(service, scope):
    source = _published(service, scope, "source fact")
    dependent_candidate = service.propose(scope=scope, content="dependent fact", source_experience_ids=[], source_recognition_ids=[source.id])
    dependent = service.publish(scope=scope, candidate_id=dependent_candidate.id, expected_revision=dependent_candidate.revision, reviewer="user-1")
    authority = RestructureProposalService(service)
    snapshot = authority.capture(scope=scope, recognition_ids=[dependent.id], expected_revisions={dependent.id: dependent.revision})
    assert set(snapshot["target_recognition_ids"]) == {dependent.id}
    assert {item["id"] for item in snapshot["recognitions"]} == {source.id, dependent.id}
    saved = authority.save(
        scope=scope, proposal_id="proposal-stale", snapshot=snapshot, operation="revise",
        outputs=[{"content": "edited dependent", "conditions": [], "source_experience_ids": [], "source_recognition_ids": [source.id]}],
        reason="clarify wording", step_metadata={"source": "manual"},
    )
    service.revise(scope=scope, recognition_id=source.id, expected_revision=source.revision, content="changed source")

    with pytest.raises(RecognitionConflict, match="input recognition changed"):
        authority.review(scope=scope, proposal_id=saved["id"], expected_revision=saved["revision"], decision="approve", reviewer="user-1")
    assert authority.get(scope=scope, proposal_id=saved["id"])["state"] == "pending"


def test_reject_noop_and_evidence_boundary_are_valid(service, scope):
    parent = _published(service, scope, "current fact")
    authority = RestructureProposalService(service)
    snapshot = authority.capture(scope=scope, recognition_ids=[parent.id], expected_revisions={parent.id: parent.revision})
    noop = authority.save(scope=scope, proposal_id="proposal-noop", snapshot=snapshot, operation="noop", outputs=[], reason="no change is warranted")
    rejected = authority.review(scope=scope, proposal_id=noop["id"], expected_revision=noop["revision"], decision="rejected", reviewer="user-1")
    assert rejected["state"] == "rejected"
    assert service.get_recognition(scope=scope, recognition_id=parent.id).state == "active"

    foreign = service.stage_experience(scope=scope, content="not captured")
    with pytest.raises(RecognitionError, match="outside the frozen snapshot"):
        authority.save(
            scope=scope, proposal_id="proposal-forged", snapshot=snapshot, operation="revise",
            outputs=[{"content": "forged", "conditions": [], "source_experience_ids": [foreign], "source_recognition_ids": []}],
            reason="bad evidence",
        )
    with pytest.raises(RecognitionError, match="requires at least one frozen evidence"):
        authority.save(
            scope=scope, proposal_id="proposal-empty-evidence", snapshot=snapshot, operation="revise",
            outputs=[{"content": "ungrounded", "conditions": [], "source_experience_ids": [], "source_recognition_ids": []}],
            reason="bad evidence",
        )


def test_supersede_and_revoke_have_expected_lifecycle_results(service, scope):
    first = _published(service, scope, "old model")
    authority = RestructureProposalService(service)
    snapshot = authority.capture(scope=scope, recognition_ids=[first.id], expected_revisions={first.id: first.revision})
    replacement = authority.save(
        scope=scope, proposal_id="proposal-supersede", snapshot=snapshot, operation="supersede",
        outputs=[{"recognition_id": "recognition-new", "content": "new model", "conditions": [],
                  "source_experience_ids": list(first.source_experience_ids), "source_recognition_ids": []}],
        reason="new evidence framing",
    )
    authority.review(scope=scope, proposal_id=replacement["id"], expected_revision=replacement["revision"], decision="approve", reviewer="user-1")
    assert service.get_recognition(scope=scope, recognition_id=first.id).state == "superseded"
    assert service.get_recognition(scope=scope, recognition_id="recognition-new").parent_ids == (first.id,)

    current = service.get_recognition(scope=scope, recognition_id="recognition-new")
    revoke_snapshot = authority.capture(scope=scope, recognition_ids=[current.id], expected_revisions={current.id: current.revision})
    revocation = authority.save(scope=scope, proposal_id="proposal-revoke", snapshot=revoke_snapshot, operation="revoke", outputs=[], reason="evidence withdrawn")
    authority.review(scope=scope, proposal_id=revocation["id"], expected_revision=revocation["revision"], decision="approve", reviewer="user-1")
    assert service.get_recognition(scope=scope, recognition_id=current.id).state == "revoked"


def test_shared_save_rolls_back_with_its_callers_transaction(service, scope):
    parent = _published(service, scope, "original")
    authority = RestructureProposalService(service)
    snapshot = authority.capture(scope=scope, recognition_ids=[parent.id],
        expected_revisions={parent.id: parent.revision})
    with service.records.begin() as uow:
        authority.save_in_uow(uow, scope=scope, proposal_id="proposal-shared", snapshot=snapshot,
            operation="noop", outputs=[], reason="No change")
        uow.put("recognition_tasks", "task-shared", {"state": "result_ready"}, expected_revision=0)
        # No outer commit: neither write may escape the caller's transaction.
    assert service.records.read("recognition_restructure_proposals", "proposal-shared") is None
    assert service.records.read("recognition_tasks", "task-shared") is None


def test_save_is_idempotent_and_rejects_forged_snapshot_payload(service, scope):
    parent = _published(service, scope, "original body")
    authority = RestructureProposalService(service)
    snapshot = authority.capture(scope=scope, recognition_ids=[parent.id], expected_revisions={parent.id: parent.revision})
    body = [{"content": "new body", "conditions": [], "source_experience_ids": list(parent.source_experience_ids), "source_recognition_ids": []}]
    first = authority.save(scope=scope, proposal_id="proposal-stable", snapshot=snapshot, operation="supersede", outputs=body, reason="new framing")
    second = authority.save(scope=scope, proposal_id="proposal-stable", snapshot=snapshot, operation="supersede", outputs=body, reason="new framing")
    assert first == second
    assert first["output_recognition_ids"] == ["recognition-proposal-stable-0"]

    forged = authority.capture(scope=scope, recognition_ids=[parent.id], expected_revisions={parent.id: parent.revision})
    forged["recognitions"][0]["payload"]["content"] = "forged old wording"
    with pytest.raises(RecognitionConflict, match="input recognition changed"):
        authority.save(scope=scope, proposal_id="proposal-forged-snapshot", snapshot=forged, operation="noop", outputs=[], reason="bad snapshot")


def test_review_rolls_back_domain_writes_when_terminal_proposal_write_fails(service, scope, monkeypatch):
    parent = _published(service, scope, "one combined conclusion")
    authority = RestructureProposalService(service)
    snapshot = authority.capture(scope=scope, recognition_ids=[parent.id], expected_revisions={parent.id: parent.revision})
    proposal = authority.save(
        scope=scope, proposal_id="proposal-atomic", snapshot=snapshot, operation="supersede",
        outputs=[{"recognition_id": "recognition-atomic-child", "content": "reframed conclusion", "conditions": [],
                  "source_experience_ids": list(parent.source_experience_ids), "source_recognition_ids": []}],
        reason="test terminal write rollback",
    )
    before_versions = service.records.list("recognition_versions")
    before_relations = service.records.list("recognition_relations")
    original_put = SQLiteStructuredRecordUnitOfWork.put

    def fail_terminal_put(self, collection, object_id, payload, *, expected_revision):
        if collection == "recognition_restructure_proposals" and payload.get("state") == "approved":
            raise RuntimeError("injected terminal proposal failure")
        return original_put(self, collection, object_id, payload, expected_revision=expected_revision)

    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, "put", fail_terminal_put)
    with pytest.raises(RuntimeError, match="injected terminal"):
        authority.review(scope=scope, proposal_id=proposal["id"], expected_revision=proposal["revision"], decision="approve", reviewer="user-1")
    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, "put", original_put)

    assert service.get_recognition(scope=scope, recognition_id=parent.id).state == "active"
    assert service.get_recognition(scope=scope, recognition_id="recognition-atomic-child") is None
    assert service.records.list("recognition_versions") == before_versions
    assert service.records.list("recognition_relations") == before_relations
    pending = authority.get(scope=scope, proposal_id=proposal["id"])
    assert pending["state"] == "pending"
    assert pending["revision"] == proposal["revision"]

    approved = authority.review(scope=scope, proposal_id=proposal["id"], expected_revision=proposal["revision"], decision="approve", reviewer="user-1")
    repeated = authority.review(scope=scope, proposal_id=proposal["id"], expected_revision=proposal["revision"], decision="approve", reviewer="user-1")
    assert approved == repeated
    assert approved["result_recognition_ids"] == ["recognition-atomic-child"]
    assert service.get_recognition(scope=scope, recognition_id="recognition-atomic-child").state == "active"
