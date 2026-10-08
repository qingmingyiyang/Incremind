from __future__ import annotations

import pytest

from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
from backend.recognition.restructuring import RestructureProposalService
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.fixture
def service(tmp_path):
    return RecognitionService(SQLiteStructuredRecordStore(tmp_path / "recognitions.sqlite3"))


@pytest.fixture
def scope():
    return WorkScope("user-1", "project-1")


def _published(service, scope, content):
    experience_id = service.stage_experience(scope=scope, content=f"evidence: {content}")
    candidate = service.propose(scope=scope, content=content, source_experience_ids=[experience_id])
    return service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision, reviewer="user-1")


def _task(service, scope, task_id, proposal_id, *, state="running", kind="restructure", task_scope=None):
    task_scope = task_scope if task_scope is not None else {"user_id": scope.user_id, "project_id": scope.project_id}
    with service.records.begin() as uow:
        record = uow.put("recognition_tasks", task_id, {
            "id": task_id,
            "project_id": scope.project_id,
            "scope": task_scope,
            "kind": kind,
            "proposal_id": proposal_id,
            "state": state,
        }, expected_revision=0)
        uow.commit()
    return record


def _set_task_state(service, task_id, state):
    with service.records.begin() as uow:
        task = uow.read("recognition_tasks", task_id)
        uow.put("recognition_tasks", task_id, {**task.payload, "state": state}, expected_revision=task.revision)
        uow.commit()


def _save_model_proposal(service, scope, *, task_id="task-model", proposal_id="proposal-model"):
    parent = _published(service, scope, "original conclusion")
    snapshot = RestructureProposalService(service).capture(
        scope=scope, recognition_ids=[parent.id], expected_revisions={parent.id: parent.revision})
    _task(service, scope, task_id, proposal_id)
    proposal = RestructureProposalService(service).save(
        scope=scope, proposal_id=proposal_id, origin_task_id=task_id, snapshot=snapshot,
        operation="noop", outputs=[], reason="model found no safe change")
    return proposal


def test_model_proposal_saves_while_running_but_requires_completed_task_to_be_visible(service, scope):
    proposal = _save_model_proposal(service, scope)
    authority = RestructureProposalService(service)

    assert proposal["origin_task_id"] == "task-model"
    with pytest.raises(RecognitionConflict, match="model restructure task is unavailable"):
        authority.get(scope=scope, proposal_id=proposal["id"])
    assert authority.list(scope=scope) == ()
    with pytest.raises(RecognitionConflict, match="model restructure task is unavailable"):
        authority.review(scope=scope, proposal_id=proposal["id"], expected_revision=proposal["revision"],
                         decision="reject", reviewer="user-1")

    _set_task_state(service, "task-model", "completed")
    assert authority.get(scope=scope, proposal_id=proposal["id"])["id"] == proposal["id"]
    assert [item["id"] for item in authority.list(scope=scope)] == [proposal["id"]]
    assert authority.review(scope=scope, proposal_id=proposal["id"], expected_revision=proposal["revision"],
                            decision="reject", reviewer="user-1")["state"] == "rejected"


def test_shared_save_accepts_running_origin_before_its_result_ready_projection(service, scope):
    parent = _published(service, scope, "original conclusion")
    authority = RestructureProposalService(service)
    snapshot = authority.capture(scope=scope, recognition_ids=[parent.id], expected_revisions={parent.id: parent.revision})
    _task(service, scope, "task-shared", "proposal-shared", state="running")

    with service.records.begin() as uow:
        proposal = authority.save_in_uow(
            uow, scope=scope, proposal_id="proposal-shared", origin_task_id="task-shared", snapshot=snapshot,
            operation="noop", outputs=[], reason="no safe change")
        task = uow.read("recognition_tasks", "task-shared")
        uow.put("recognition_tasks", "task-shared", {**task.payload, "state": "result_ready"}, expected_revision=task.revision)
        uow.commit()

    assert proposal["origin_task_id"] == "task-shared"
    with pytest.raises(RecognitionConflict, match="model restructure task is unavailable"):
        authority.get(scope=scope, proposal_id="proposal-shared")


@pytest.mark.parametrize("state", ["result_ready", "failed", "cancelled", "stale", "interrupted"])
def test_model_proposal_is_hidden_when_its_task_is_not_completed(service, scope, state):
    proposal = _save_model_proposal(service, scope)
    _set_task_state(service, "task-model", state)
    authority = RestructureProposalService(service)

    with pytest.raises(RecognitionConflict, match="model restructure task is unavailable"):
        authority.get(scope=scope, proposal_id=proposal["id"])
    assert authority.list(scope=scope) == ()
    with pytest.raises(RecognitionConflict, match="model restructure task is unavailable"):
        authority.review(scope=scope, proposal_id=proposal["id"], expected_revision=proposal["revision"],
                         decision="reject", reviewer="user-1")


def test_terminal_review_replay_still_requires_completed_origin_task(service, scope):
    proposal = _save_model_proposal(service, scope)
    _set_task_state(service, "task-model", "completed")
    authority = RestructureProposalService(service)
    approved = authority.review(scope=scope, proposal_id=proposal["id"], expected_revision=proposal["revision"],
                                decision="approve", reviewer="user-1")
    _set_task_state(service, "task-model", "result_ready")

    with pytest.raises(RecognitionConflict, match="model restructure task is unavailable"):
        authority.review(scope=scope, proposal_id=proposal["id"], expected_revision=approved["revision"],
                         decision="approve", reviewer="user-1")


def test_model_origin_must_match_task_kind_scope_and_proposal(service, scope):
    parent = _published(service, scope, "original conclusion")
    snapshot = RestructureProposalService(service).capture(
        scope=scope, recognition_ids=[parent.id], expected_revisions={parent.id: parent.revision})
    authority = RestructureProposalService(service)
    _task(service, scope, "task-wrong-kind", "proposal-a", kind="ordinary")
    _task(service, scope, "task-wrong-proposal", "proposal-other")
    _task(service, scope, "task-wrong-scope", "proposal-c", task_scope={"user_id": "other", "project_id": scope.project_id})

    for task_id, proposal_id in (("task-wrong-kind", "proposal-a"), ("task-wrong-proposal", "proposal-b"),
                                 ("task-wrong-scope", "proposal-c")):
        with pytest.raises(RecognitionConflict, match="model restructure task is unavailable"):
            authority.save(scope=scope, proposal_id=proposal_id, origin_task_id=task_id, snapshot=snapshot,
                           operation="noop", outputs=[], reason="invalid model origin")


def test_origin_is_part_of_idempotency_and_legacy_manual_rows_remain_public(service, scope):
    parent = _published(service, scope, "original conclusion")
    snapshot = RestructureProposalService(service).capture(
        scope=scope, recognition_ids=[parent.id], expected_revisions={parent.id: parent.revision})
    authority = RestructureProposalService(service)
    manual = authority.save(scope=scope, proposal_id="proposal-manual", snapshot=snapshot,
                            operation="noop", outputs=[], reason="manual review")
    assert manual["origin_task_id"] is None
    assert authority.get(scope=scope, proposal_id=manual["id"])["id"] == manual["id"]

    # Simulate a manual proposal written before origin binding existed.  A
    # retry with the old call shape keeps its idempotency key and public form.
    with service.records.begin() as uow:
        stored = uow.read("recognition_restructure_proposals", "proposal-manual")
        legacy_payload = dict(stored.payload)
        legacy_payload.pop("origin_task_id")
        uow.put("recognition_restructure_proposals", "proposal-manual", legacy_payload,
                expected_revision=stored.revision)
        uow.commit()
    legacy = authority.save(scope=scope, proposal_id="proposal-manual", snapshot=snapshot,
                            operation="noop", outputs=[], reason="manual review")
    assert legacy["origin_task_id"] is None

    _task(service, scope, "task-manual", "proposal-manual")
    with pytest.raises(RecognitionConflict, match="already exists with different content"):
        authority.save(scope=scope, proposal_id="proposal-manual", origin_task_id="task-manual", snapshot=snapshot,
                       operation="noop", outputs=[], reason="manual review")
