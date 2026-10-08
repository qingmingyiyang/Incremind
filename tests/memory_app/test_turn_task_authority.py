from threading import RLock
from types import SimpleNamespace

import pytest

from backend.memory_app.app import _verify_packet_current
from backend.memory_app.packet_egress import capture_packet_egress
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.turn_routing import RecognitionModelRoutingSnapshotAuthority
from backend.memory_app.turn_task_authority import RecognitionTurnTaskAuthority
from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from core.storage_provider import SQLiteStructuredRecordStore


class Control:
    remaining_timeout_ms = 10000
    cancel_requested = False

    def __init__(self, fail_at=None):
        self.calls, self.fail_at = 0, fail_at

    def checkpoint(self):
        self.calls += 1
        if self.calls == self.fail_at:
            raise RuntimeError("cancelled")


class Models:
    def public(self):
        return {"generation": dict(purpose="generation", provider="openai", base_url="https://example.test",
            model="test", allow_remote=True, revision=1, configured=True, has_api_key=True)}


def setup_task(tmp_path):
    service = RecognitionService(SQLiteStructuredRecordStore(tmp_path / "records.sqlite3"))
    scope = WorkScope("local-user", "project-a")
    experience = service.stage_experience(scope=scope, content="private experience")
    candidate = service.propose(scope=scope, content="private recognition", source_experience_ids=[experience])
    recognition = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer="local-user")
    SourceEgressService(service.records).set_policy(scope, "experience", experience, 1, 0, ["generation", "embedding", "rerank"])
    egress = capture_packet_egress(service, scope, {"items": [{"id": recognition.id, "revision": 1}]})
    with service.records.begin() as tx:
        tx.put("recognition_tasks", "task-one", dict(project_id="project-a", turn_id="turn-one",
            context_packet_id="packet-one", input="private task", state="queued"), expected_revision=0)
        tx.put("recognition_context_packets", "packet-one", dict(project_id="project-a", task_id="task-one",
            state="consumed", query="private task", model_revision=1, source_egress=egress,
            messages=[dict(role="user", content="private recognition")],
            items=[dict(id=recognition.id, revision=1)]), expected_revision=0)
        tx.commit()
    turns = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    turns.claim_turn(dict(turn_id="turn-one", session_id="session-one", operation_id="operation-one", idempotency_key="key-one"))
    RecognitionModelRoutingSnapshotAuthority(Models(), turns).acquire(turn_id="turn-one", project_id="project-a",
        context_packet_id="packet-one", project_profile_id="profile-one", project_profile_revision=1,
        boundary_profile_id="boundary-one", boundary_profile_revision=1, capability_ids=("recognition.task.execute",),
        agent_binding=None, allow_remote=True)
    authority = RecognitionTurnTaskAuthority(service=service, models=Models(), payloads=turns,
        mutation_lock=RLock(), verify_current=_verify_packet_current, now=lambda: "2026-09-15T12:00:00Z")
    request = dict(turn_id="turn-one", scope=dict(kind="project", project_id="project-a"),
        arguments=dict(task_id="task-one", context_packet_id="packet-one"), execution_context=Control())
    return authority, request, recognition


def test_task_result_and_document_commit_together_and_replay_without_body(tmp_path):
    authority, request, _ = setup_task(tmp_path)
    loaded = authority.load_task(request)
    result = authority.commit_result(request, loaded, "private answer", dict(configuration_revision=1))
    assert set(result) == {"task_id", "document_id", "document_revision"}
    assert authority.service.records.read("recognition_tasks", "task-one").payload["state"] == "result_ready"
    replay = authority.load_task(request)
    assert replay["existing_result"] == result
    assert len(authority.service.records.list("documents")) == 1


def test_cancelled_commit_rolls_back_document_and_task_reference(tmp_path):
    authority, request, _ = setup_task(tmp_path)
    loaded = authority.load_task(request)
    request["execution_context"] = Control(fail_at=2)
    with pytest.raises(RuntimeError, match="cancelled"):
        authority.commit_result(request, loaded, "private answer", dict(configuration_revision=1))
    assert authority.service.records.list("documents") == ()
    assert authority.service.records.list("document_revisions") == ()
    assert authority.service.records.list("document_markdown") == ()
    task = authority.service.records.read("recognition_tasks", "task-one")
    assert task.payload["state"] == "running" and "document_id" not in task.payload


def test_source_revision_changed_during_model_wait_prevents_result_write(tmp_path):
    authority, request, recognition = setup_task(tmp_path)
    loaded = authority.load_task(request)
    authority.service.revise(scope=WorkScope("local-user", "project-a"), recognition_id=recognition.id,
        expected_revision=1, content="new recognition")
    with pytest.raises(RecognitionConflict, match="stale"):
        authority.commit_result(request, loaded, "old answer", dict(configuration_revision=1))
    assert authority.service.records.list("documents") == ()


def test_wire_validator_rechecks_source_after_initial_load(tmp_path):
    authority, request, recognition = setup_task(tmp_path)
    loaded = authority.load_task(request)
    loaded["validate_current"]()
    authority.service.revise(scope=WorkScope("local-user", "project-a"), recognition_id=recognition.id,
        expected_revision=1, content="changed after approval")
    with pytest.raises(RecognitionConflict, match="stale"):
        loaded["validate_current"]()


@pytest.mark.parametrize("loaded_first", [False, True])
def test_frozen_source_grant_revocation_blocks_task_load_wire_and_commit(tmp_path, loaded_first):
    authority, request, _ = setup_task(tmp_path)
    loaded = authority.load_task(request) if loaded_first else None
    scope = WorkScope("local-user", "project-a")
    source = authority.service.list_experiences(scope=scope)[0]
    SourceEgressService(authority.service.records).set_policy(scope, "experience", source.id, 1, 1, [])
    with pytest.raises(RecognitionConflict):
        if loaded is None:
            authority.load_task(request)
        else:
            loaded["validate_current"]()
    if loaded is not None:
        with pytest.raises(RecognitionConflict):
            authority.commit_result(request, loaded, "late output", dict(configuration_revision=1))
    assert authority.service.records.list("documents") == ()


def test_loaded_packet_revision_is_fenced_at_wire_and_commit(tmp_path):
    authority, request, _ = setup_task(tmp_path)
    loaded = authority.load_task(request)
    with authority.service.records.begin() as tx:
        packet = tx.read("recognition_context_packets", "packet-one")
        tx.put("recognition_context_packets", "packet-one", {
            **packet.payload, "messages": [{"role": "user", "content": "different prompt"}]},
            expected_revision=packet.revision)
        tx.commit()
    with pytest.raises(RecognitionConflict, match="input changed"):
        loaded["validate_current"]()
    with pytest.raises(RecognitionConflict, match="input changed"):
        authority.commit_result(request, loaded, "old answer", dict(configuration_revision=1))
    assert authority.service.records.list("documents") == ()


@pytest.mark.parametrize("change", ["project", "turn", "cancelled", "deleted"])
def test_task_authority_rejects_wrong_scope_and_removed_execution(tmp_path, change):
    authority, request, _ = setup_task(tmp_path)
    if change == "project":
        request["scope"]["project_id"] = "project-b"
    elif change == "turn":
        request["turn_id"] = "turn-other"
    else:
        with authority.service.records.begin() as tx:
            task = tx.read("recognition_tasks", "task-one")
            if change == "deleted":
                tx.delete("recognition_tasks", "task-one", expected_revision=task.revision)
            else:
                tx.put("recognition_tasks", "task-one", {**task.payload, "state": "cancelled"}, expected_revision=task.revision)
            tx.commit()
    with pytest.raises(RecognitionConflict):
        authority.load_task(request)


@pytest.mark.parametrize("source_changed", [False, True])
def test_failed_turn_distinguishes_source_invalidation_from_provider_failure(tmp_path, source_changed):
    authority, request, recognition = setup_task(tmp_path)
    authority.load_task(request)
    if source_changed:
        authority.service.revise(scope=WorkScope("local-user", "project-a"), recognition_id=recognition.id,
            expected_revision=1, content="corrected source")
    authority.payloads = SimpleNamespace(get_request=lambda _turn_id: {
        **request, "capability_request": {"capability_id": "recognition.task.execute", "arguments": request["arguments"]}})
    authority.observe_terminal(SimpleNamespace(turn_id="turn-one", status="failed"))
    task = authority.service.records.read("recognition_tasks", "task-one")
    assert task.payload["state"] == ("stale" if source_changed else "failed")
    assert task.payload["terminal_projection"] == {"turn_status": "failed", "prior_state": "running",
        "reason": "authority_conflict" if source_changed else "turn_receipt"}
    assert authority.service.records.list("documents") == ()


def test_terminal_projection_explains_completed_receipt_without_committed_result(tmp_path):
    authority, request, _ = setup_task(tmp_path)
    authority.load_task(request)
    authority.payloads = SimpleNamespace(get_request=lambda _turn_id: {
        **request, "capability_request": {"capability_id": "recognition.task.execute", "arguments": request["arguments"]}})
    authority.observe_terminal(SimpleNamespace(turn_id="turn-one", status="completed"))
    task = authority.service.records.read("recognition_tasks", "task-one")
    assert task.payload["state"] == "failed"
    assert task.payload["terminal_projection"] == {"turn_status": "completed", "prior_state": "running",
        "reason": "result_not_committed"}


def test_new_constraint_during_model_wait_prevents_result_commit(tmp_path):
    from backend.memory_app.constraints import ProjectConstraintService
    authority, request, _ = setup_task(tmp_path)
    loaded = authority.load_task(request)
    ProjectConstraintService(authority.service.records).upsert(WorkScope("local-user", "project-a"), "new-requirement", 0, "Only provide advice")
    with pytest.raises(RecognitionConflict, match="constraints changed"):
        authority.commit_result(request, loaded, "old answer", dict(configuration_revision=1))
    assert authority.service.records.list("documents") == ()


def test_constraints_added_while_waiting_prevent_model_inputs_from_loading(tmp_path):
    from backend.memory_app.constraints import ProjectConstraintService
    authority, request, _ = setup_task(tmp_path)
    ProjectConstraintService(authority.service.records).upsert(WorkScope("local-user", "project-a"), "new-requirement", 0, "Only provide advice")
    with pytest.raises(RecognitionConflict, match="constraints changed"):
        authority.load_task(request)
    assert authority.service.records.read("recognition_tasks", "task-one").payload["state"] == "queued"


def test_constraint_expiry_during_model_wait_prevents_result_commit(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    import backend.memory_app.constraints as constraint_module
    authority, request, _ = setup_task(tmp_path)
    moment = [datetime(2026, 9, 16, 12, tzinfo=timezone.utc)]
    monkeypatch.setattr(constraint_module, "_utc_now", lambda: moment[0])
    service = constraint_module.ProjectConstraintService(authority.service.records)
    scope = WorkScope("local-user", "project-a")
    service.upsert(scope, "timed", 0, "Temporary requirement", valid_until="2026-09-16T13:00:00Z")
    snapshot = list(service.active(scope))
    with authority.service.records.begin() as tx:
        packet = tx.read("recognition_context_packets", "packet-one")
        tx.put("recognition_context_packets", "packet-one", {**packet.payload, "constraints": snapshot}, expected_revision=packet.revision)
        tx.commit()
    loaded = authority.load_task(request)
    moment[0] = datetime(2026, 9, 16, 13, tzinfo=timezone.utc)
    with pytest.raises(RecognitionConflict, match="constraints changed"):
        authority.commit_result(request, loaded, "late answer", dict(configuration_revision=1))
    assert authority.service.records.list("documents") == ()
