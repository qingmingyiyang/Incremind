"""Focused authority tests for model-generated restructuring proposals."""

import json
from threading import RLock
from types import SimpleNamespace

import pytest

from backend.memory_app.app import _verify_packet_current
from backend.memory_app.packet_egress import capture_packet_egress
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.restructure_generation import STEP_VERSION, build_messages
from backend.memory_app.turn_routing import RecognitionModelRoutingSnapshotAuthority
from backend.memory_app.turn_task_authority import RecognitionTurnTaskAuthority
from backend.recognition import RecognitionConflict, RecognitionError, RecognitionService, WorkScope
from backend.recognition.restructuring import RestructureProposalService
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from core.storage_provider import SQLiteStructuredRecordStore


class Control:
    remaining_timeout_ms = 10_000
    cancel_requested = False

    def checkpoint(self):
        return None


class Models:
    def public(self):
        return {"generation": {"purpose": "generation", "provider": "openai", "base_url": "https://example.test",
            "model": "test", "allow_remote": True, "revision": 1, "configured": True, "has_api_key": True}}


def setup_restructure_task(tmp_path):
    service = RecognitionService(SQLiteStructuredRecordStore(tmp_path / "records.sqlite3"))
    scope = WorkScope("local-user", "project-a")
    experience = service.stage_experience(scope=scope, content="source evidence")
    candidate = service.propose(scope=scope, content="old recognition", source_experience_ids=[experience])
    recognition = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer="local-user")
    snapshot = RestructureProposalService(service).capture(
        scope=scope, recognition_ids=[recognition.id], expected_revisions={recognition.id: recognition.revision},
    )
    messages = build_messages(scope=scope, snapshot=snapshot, operation="revise", instruction="make wording precise")
    SourceEgressService(service.records).set_policy(scope, "experience", experience, 1, 0, ["generation", "embedding", "rerank"])
    egress = capture_packet_egress(service, scope, {"kind": "restructure", "snapshot": snapshot,
        "items": [{"id": row["id"], "revision": row["revision"]} for row in snapshot["recognitions"]]})
    proposal_id = "proposal-restructure-one"
    with service.records.begin() as tx:
        tx.put("recognition_tasks", "task-restructure-one", {
            "scope": {"user_id": "local-user", "project_id": "project-a"}, "project_id": "project-a",
            "kind": "restructure", "proposal_id": proposal_id, "turn_id": "turn-restructure-one",
            "context_packet_id": "packet-restructure-one", "input": "make wording precise", "state": "queued",
        }, expected_revision=0)
        tx.put("recognition_context_packets", "packet-restructure-one", {
            "scope": {"user_id": "local-user", "project_id": "project-a"}, "project_id": "project-a",
            "kind": "restructure", "proposal_id": proposal_id, "task_id": "task-restructure-one", "state": "consumed",
            "query": "make wording precise", "instruction": "make wording precise", "snapshot": snapshot,
            "operation": "revise", "step_version": STEP_VERSION, "messages": messages, "model_revision": 1, "source_egress": egress,
            "constraints": [], "items": [{"id": row["id"], "revision": row["revision"]} for row in snapshot["recognitions"]],
        }, expected_revision=0)
        tx.commit()
    turns = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    turns.claim_turn({"turn_id": "turn-restructure-one", "session_id": "session-one", "operation_id": "operation-one", "idempotency_key": "key-one"})
    RecognitionModelRoutingSnapshotAuthority(Models(), turns).acquire(
        turn_id="turn-restructure-one", project_id="project-a", context_packet_id="packet-restructure-one",
        project_profile_id="profile-one", project_profile_revision=1, boundary_profile_id="boundary-one",
        boundary_profile_revision=1, capability_ids=("recognition.task.execute",), agent_binding=None, allow_remote=True,
    )
    authority = RecognitionTurnTaskAuthority(service=service, models=Models(), payloads=turns,
        mutation_lock=RLock(), verify_current=_verify_packet_current, now=lambda: "2026-09-16T12:00:00Z")
    request = {"turn_id": "turn-restructure-one", "scope": {"kind": "project", "project_id": "project-a"},
        "arguments": {"task_id": "task-restructure-one", "context_packet_id": "packet-restructure-one"},
        "execution_context": Control()}
    return authority, request, recognition


def _response(authority):
    packet = authority.service.records.read("recognition_context_packets", "packet-restructure-one")
    experience_id = packet.payload["snapshot"]["experiences"][0]["id"]
    return json.dumps({"operation": "revise", "outputs": [{
        "content": "precise recognition", "conditions": [], "source_experience_ids": [experience_id],
        "source_recognition_ids": [],
    }], "reason": "clarify the frozen evidence"})


def test_restructure_result_saves_internal_document_and_pending_proposal_atomically(tmp_path):
    authority, request, _ = setup_restructure_task(tmp_path)
    # The service generates deterministic first IDs for the fixture store.
    answer = _response(authority)
    loaded = authority.load_task(request)
    result = authority.commit_result(request, loaded, answer, {"configuration_revision": 1, "model": "test"})
    assert set(result) == {"task_id", "document_id", "document_revision"}
    task = authority.service.records.read("recognition_tasks", "task-restructure-one")
    assert task.payload["state"] == "result_ready" and task.payload["proposal_id"] == "proposal-restructure-one"
    document = authority.service.records.read("documents", result["document_id"])
    assert document.payload["type"] == "restructure-internal"
    proposal = authority.service.records.read("recognition_restructure_proposals", "proposal-restructure-one")
    assert proposal.payload["origin_task_id"] == "task-restructure-one"
    assert proposal.payload["state"] == "pending"


def test_invalid_model_json_writes_neither_internal_document_nor_proposal(tmp_path):
    authority, request, _ = setup_restructure_task(tmp_path)
    loaded = authority.load_task(request)
    with pytest.raises(RecognitionError):
        authority.commit_result(request, loaded, "not JSON", {"configuration_revision": 1})
    assert authority.service.records.list("documents") == ()
    assert authority.service.records.list("recognition_restructure_proposals") == ()


def test_changed_restructure_source_rejects_model_result(tmp_path):
    authority, request, recognition = setup_restructure_task(tmp_path)
    loaded = authority.load_task(request)
    authority.service.revise(scope=WorkScope("local-user", "project-a"), recognition_id=recognition.id,
        expected_revision=recognition.revision, content="changed after preview")
    with pytest.raises(RecognitionConflict, match="capture again"):
        authority.commit_result(request, loaded, _response(authority), {"configuration_revision": 1})
    assert authority.service.records.list("documents") == ()


def test_existing_restructure_result_returns_without_second_proposal(tmp_path):
    authority, request, _ = setup_restructure_task(tmp_path)
    loaded = authority.load_task(request)
    first = authority.commit_result(request, loaded, _response(authority), {"configuration_revision": 1})
    second = authority.commit_result(request, loaded, _response(authority), {"configuration_revision": 1})
    assert second == first
    assert len(authority.service.records.list("documents")) == 1
    assert len(authority.service.records.list("recognition_restructure_proposals")) == 1


@pytest.mark.parametrize("proposal_state", ["present", "missing", "foreign"])
def test_terminal_projection_records_proposal_decision_without_output(tmp_path, proposal_state):
    authority, request, _ = setup_restructure_task(tmp_path)
    loaded = authority.load_task(request)
    authority.commit_result(request, loaded, _response(authority), {"configuration_revision": 1})
    if proposal_state != "present":
        with authority.service.records.begin() as tx:
            proposal = tx.read("recognition_restructure_proposals", "proposal-restructure-one")
            if proposal_state == "missing":
                tx.delete("recognition_restructure_proposals", proposal.object_id, expected_revision=proposal.revision)
            else:
                tx.put("recognition_restructure_proposals", proposal.object_id,
                    {**proposal.payload, "origin_task_id": "another-task"}, expected_revision=proposal.revision)
            tx.commit()
    authority.payloads = SimpleNamespace(get_request=lambda _turn_id: {
        **request, "capability_request": {"capability_id": "recognition.task.execute", "arguments": request["arguments"]}})
    receipt = SimpleNamespace(turn_id=request["turn_id"], status="completed")
    authority.observe_terminal(receipt)
    task = authority.service.records.read("recognition_tasks", request["arguments"]["task_id"])
    assert task.payload["state"] == ("completed" if proposal_state == "present" else "stale")
    assert task.payload["terminal_projection"] == {"turn_status": "completed", "prior_state": "result_ready",
        "reason": "turn_receipt" if proposal_state == "present" else "proposal_unavailable"}
    authority.observe_terminal(receipt)
    replay = authority.service.records.read("recognition_tasks", task.object_id)
    assert replay.revision == task.revision
    assert replay.payload["terminal_projection"] == task.payload["terminal_projection"]
