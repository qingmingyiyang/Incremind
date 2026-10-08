from threading import RLock
from types import SimpleNamespace

import pytest

from backend.memory_app.turn_dispatch import RecognitionTurnDispatcher, _request
from backend.recognition import WorkScope
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.mark.parametrize("receipt_sequence", [5, 6])
def test_late_approval_projection_cannot_roll_back_worker_running_state(tmp_path, receipt_sequence):
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    # Polling persisted waiting at sequence 6; the capability then started the
    # model call. An already-in-flight GET still holds the waiting receipt.
    with records.begin() as tx:
        tx.put("recognition_tasks", "task-one", {"project_id": "project-a", "turn_id": "turn-one",
            "state": "running", "approval_event_id": "approval-one", "approval_sequence": 6,
            "turn_projection_sequence": 6}, expected_revision=0)
        tx.commit()
    runtime = SimpleNamespace(receipt_for=lambda _: SimpleNamespace(status="waiting_approval", current_sequence=receipt_sequence),
        events_after=lambda _: [{"type": "approval.required", "event_id": "approval-one", "sequence": 6}])
    dispatcher = RecognitionTurnDispatcher(application=SimpleNamespace(), runtime_root=tmp_path, records=records, mutation_lock=RLock())
    dispatcher._runtime = lambda: runtime
    state = dispatcher.sync(task_id="task-one", scope=WorkScope("local-user", "project-a"))
    assert state["state"] == "running"
    assert records.read("recognition_tasks", "task-one").revision == 1


@pytest.mark.parametrize("state", ["completed", "failed", "cancelled", "stale", "interrupted"])
def test_terminal_task_read_does_not_require_original_execution_runtime(tmp_path, state):
    records = SQLiteStructuredRecordStore(tmp_path / "restored.sqlite3")
    with records.begin() as tx:
        tx.put("recognition_tasks", "task-one", {"project_id": "project-a", "turn_id": "old-turn",
            "state": state}, expected_revision=0)
        tx.commit()
    dispatcher = RecognitionTurnDispatcher(application=SimpleNamespace(), runtime_root=tmp_path,
        records=records, mutation_lock=RLock())
    def unavailable_runtime():
        raise AssertionError("A terminal read tried to initialize the execution runtime")
    dispatcher._runtime = unavailable_runtime
    assert dispatcher.sync(task_id="task-one", scope=WorkScope("local-user", "project-a"))["state"] == state
    assert records.read("recognition_tasks", "task-one").revision == 1


def test_newer_approval_projection_is_still_observable(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    with records.begin() as tx:
        tx.put("recognition_tasks", "task-one", {"project_id": "project-a", "turn_id": "turn-one",
            "state": "running", "turn_projection_sequence": 2}, expected_revision=0)
        tx.commit()
    runtime = SimpleNamespace(receipt_for=lambda _: SimpleNamespace(status="waiting_approval", current_sequence=6),
        events_after=lambda _: [{"type": "approval.required", "event_id": "approval-one", "sequence": 6},
                               {"type": "approval.resolved", "event_id": "approval-done", "sequence": 7}])
    dispatcher = RecognitionTurnDispatcher(application=SimpleNamespace(), runtime_root=tmp_path, records=records, mutation_lock=RLock())
    dispatcher._runtime = lambda: runtime
    state = dispatcher.sync(task_id="task-one", scope=WorkScope("local-user", "project-a"))
    assert state["state"] == "waiting_approval" and state["approval_event_id"] == "approval-one"
    assert state["turn_projection_sequence"] == 6


def test_source_revoke_uses_the_same_mutation_boundary_as_turn_commit(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.memory_app.app import create_app

    app = create_app(runtime_root=tmp_path, legacy_app=FastAPI(),
        model_configuration=SimpleNamespace(public=lambda: {"generation": {"configured": False}}))
    service = app.state.recognition_service
    scope = WorkScope("local-user", "project-a")
    experience = service.stage_experience(scope=scope, content="source")
    candidate = service.propose(scope=scope, content="recognition", source_experience_ids=[experience])
    recognition = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer="local-user")
    original = service.revoke
    observed = []
    def revoke(**kwargs):
        observed.append(app.state.recognition_mutation_lock._is_owned())
        return original(**kwargs)
    monkeypatch.setattr(service, "revoke", revoke)
    response = TestClient(app).request("DELETE", f"/api/recognition/recognitions/{recognition.id}",
        json={"project_id": "project-a", "expected_revision": 1})
    assert response.status_code == 200
    assert observed == [True]


def test_task_request_uses_distinct_local_and_remote_capabilities():
    local = _request(task_id="task-local", project_id="project-a", packet_id="packet-a", remote=False)
    remote = _request(task_id="task-remote", project_id="project-a", packet_id="packet-b", remote=True)
    assert local["privacy"]["mode"] == "local_only"
    assert local["capability_request"]["capability_id"] == "recognition.task.execute.local"
    assert local["capability_policy"]["require_approval"] == ["recognition.task.execute.local"]
    assert remote["privacy"]["mode"] == "remote_allowed"
    assert remote["capability_request"]["capability_id"] == "recognition.task.execute"
