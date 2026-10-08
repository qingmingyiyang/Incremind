from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.api import task_world_action_reader as subject
from backend.api.personal_world_model_runtime import PersonalWorldModelRuntime
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from core.personal_world_model import WorldEventDraft, WorldEventKind


PROJECT = "project-a"
TURN = "world-turn-command-a"
ACTION = "world-action-command-a"


class _Store:
    def __init__(self, request=None, events=()):
        self.request = request
        self.events = events

    def get_request(self, _turn_id): return self.request
    def events_after(self, _turn_id): return self.events


class _NoPayloadTurnStore(SQLiteAITurnStore):
    """Make a private-payload read fail if the reader ever starts one."""

    def get(self, _payload_ref):
        raise AssertionError("Task reader must not load a Turn payload")

    def get_immutable_payload(self, _turn_id, _kind):
        raise AssertionError("Task reader must not load immutable Turn content")


def _request():
    return {
        "schema_version": "1.0.0", "turn_id": TURN,
        "session_id": "world-project", "operation_id": ACTION,
        "idempotency_key": "world-reader-command-a",
        "scope": {"kind": "project", "project_id": PROJECT, "series_id": None},
        "input": {"kind": "text", "text": "Run the plan", "refs": []},
        "desired_outcome": "workbench.question.answer",
        "privacy": {"mode": "local_only", "allow_remote": False, "pii": "possible", "consent_refs": [], "retention": "local_durable"},
        "capability_policy": {"allowed": ["workbench.question.answer"], "denied": [], "require_approval": []},
        "context_policy": {"include_project_skill": True, "include_memory": True, "include_session_history": False, "max_context_bytes": 1024},
        "approval_policy": {"mode": "risk_based", "auto_approve_read_only": True},
        "created_at": "2026-09-06T08:00:00Z",
    }


def _event(sequence, event_type, status, *, payload_ref=None):
    return {
        "schema_version": "1.0.0", "event_id": f"event-{sequence}",
        "turn_id": TURN, "session_id": "world-project", "sequence": sequence,
        "type": event_type, "actor": "kernel",
        "correlation": {"step_id": None, "tool_call_id": None, "model_request_id": None, "operation_id": ACTION},
        "data": {"status": status, "summary": "state", "capability_id": None, "payload_ref": payload_ref, "receipt_ref": None, "evidence_refs": [], "error_code": None, "retryable": False},
        "occurred_at": "2026-09-06T08:00:00Z",
    }


def _reader(monkeypatch, tmp_path, *, request=None, events=(), planned=True, feedback=False):
    state = SimpleNamespace(
        planned_actions=(SimpleNamespace(action_id=ACTION),) if planned else (),
        feedback_facts=(SimpleNamespace(action_id=ACTION),) if feedback else (),
    )
    monkeypatch.setattr(
        subject.PersonalWorldModelRuntime, "for_root",
        lambda _root: SimpleNamespace(project=lambda _project: state),
    )
    return subject.TaskWorldActionReader(
        root_dir=tmp_path, turn_store=_Store(request=request, events=events),
    )


def test_unadmitted_fixed_world_turn_is_not_promoted_to_a_task(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subject.PersonalWorldModelRuntime, "for_root",
        lambda _root: SimpleNamespace(),
    )
    reader = subject.TaskWorldActionReader(root_dir=tmp_path, turn_store=_Store())
    assert reader.action_status(
        project_id=PROJECT, turn_id=TURN,
    ) == {
        "turn_id": TURN,
        "action_id": ACTION,
        "status": "not_admitted",
    }


def test_latest_outcome_reference_reads_event_metadata_only():
    assert subject._latest_outcome_ref((
        {"sequence": 1, "type": "tool.outcome.recorded", "data": {"payload_ref": "crp://a"}},
        {"sequence": 2, "type": "turn.completed", "data": {}},
        {"sequence": 3, "type": "tool.outcome.recorded", "data": {"payload_ref": "crp://b"}},
    )) == "crp://b"


def test_admitted_world_action_returns_terminal_receipt_and_feedback_readiness(monkeypatch, tmp_path):
    reader = _reader(
        monkeypatch, tmp_path, request=_request(), events=(
            _event(1, "turn.accepted", "accepted"),
            _event(2, "tool.outcome.recorded", "running", payload_ref="crp://outcomes/a"),
            _event(3, "turn.completed", "completed"),
        ),
    )

    assert reader.action_status(project_id=PROJECT, turn_id=TURN) == {
        "turn_id": TURN, "action_id": ACTION, "status": "completed",
        "terminal": True, "ready_for_feedback": True,
    }


def test_terminal_receipt_remains_terminal_when_a_nonterminal_event_follows(monkeypatch, tmp_path):
    reader = _reader(
        monkeypatch, tmp_path, request=_request(), events=(
            _event(1, "tool.outcome.recorded", "running", payload_ref="crp://outcomes/a"),
            _event(2, "turn.completed", "completed"),
            _event(3, "model.completed", "running"),
        ),
    )

    result = reader.action_status(project_id=PROJECT, turn_id=TURN)

    assert result["status"] == "completed"
    assert result["terminal"] is True
    assert result["ready_for_feedback"] is True


def test_terminal_world_action_with_existing_feedback_is_not_ready(monkeypatch, tmp_path):
    reader = _reader(
        monkeypatch, tmp_path, request=_request(), feedback=True, events=(
            _event(1, "tool.outcome.recorded", "running", payload_ref="crp://outcomes/a"),
            _event(2, "turn.completed", "completed"),
        ),
    )

    assert reader.action_status(project_id=PROJECT, turn_id=TURN)["ready_for_feedback"] is False


@pytest.mark.parametrize(("field", "value"), [
    ("turn_id", "world-turn-other"),
    ("session_id", "other-session"),
    ("operation_id", "world-action-other"),
])
def test_admitted_request_binding_drift_is_rejected(monkeypatch, tmp_path, field, value):
    request = _request()
    request[field] = value
    reader = _reader(monkeypatch, tmp_path, request=request, events=(_event(1, "turn.accepted", "accepted"),))

    with pytest.raises(subject.TaskWorldActionReaderError, match="binding drifted"):
        reader.action_status(project_id=PROJECT, turn_id=TURN)


def test_admitted_request_wrong_project_is_rejected(monkeypatch, tmp_path):
    request = _request()
    request["scope"] = request["scope"] | {"project_id": "project-other"}
    reader = _reader(monkeypatch, tmp_path, request=request, events=(_event(1, "turn.accepted", "accepted"),))

    with pytest.raises(subject.TaskWorldActionReaderError, match="binding drifted"):
        reader.action_status(project_id=PROJECT, turn_id=TURN)


def test_missing_planned_action_or_receipt_is_rejected(monkeypatch, tmp_path):
    reader = _reader(monkeypatch, tmp_path, request=_request(), planned=False, events=(_event(1, "turn.accepted", "accepted"),))
    with pytest.raises(subject.TaskWorldActionReaderError, match="plan binding drifted"):
        reader.action_status(project_id=PROJECT, turn_id=TURN)

    reader = _reader(monkeypatch, tmp_path, request=_request(), events=())
    with pytest.raises(subject.TaskWorldActionReaderError, match="receipt is unavailable"):
        reader.action_status(project_id=PROJECT, turn_id=TURN)


@pytest.mark.parametrize("mutate", [
    lambda event: event | {"turn_id": "world-turn-other"},
    lambda event: event | {"session_id": "other-session"},
    lambda event: event | {"correlation": event["correlation"] | {"operation_id": "world-action-other"}},
])
def test_event_binding_drift_is_rejected(monkeypatch, tmp_path, mutate):
    reader = _reader(monkeypatch, tmp_path, request=_request(), events=(mutate(_event(1, "turn.accepted", "accepted")),))

    with pytest.raises(subject.TaskWorldActionReaderError, match="event binding drifted"):
        reader.action_status(project_id=PROJECT, turn_id=TURN)


def test_malformed_event_is_normalized_to_reader_error(monkeypatch, tmp_path):
    reader = _reader(monkeypatch, tmp_path, request=_request(), events=(object(),))

    with pytest.raises(subject.TaskWorldActionReaderError, match="receipt is invalid"):
        reader.action_status(project_id=PROJECT, turn_id=TURN)


def test_reader_composes_real_world_plan_with_durable_turn_store_without_payload_reads(tmp_path):
    world = PersonalWorldModelRuntime.for_root(tmp_path)
    world.append_event(WorldEventDraft(
        event_id="world-reader-plan", project_id=PROJECT,
        kind=WorldEventKind.ACTION_PLANNED, actor="user",
        source_ref="crp://plans/project-a/world-reader", source_revision="1",
        occurred_at="2026-09-06T08:00:00Z", recorded_at="2026-09-06T08:00:00Z",
        payload={
            "action_id": ACTION, "title": "Read durable task state",
            "expected_outcome": "A task status is available", "effect_class": "QUERYABLE",
            "gate_requirement": "approval", "due_at": None,
            "evidence_refs": ["crp://plans/project-a/world-reader"],
        },
    ))
    turns = _NoPayloadTurnStore(
        tmp_path / ".rebuild-data" / "ai-turns.sqlite3",
        effect_runner=SimpleNamespace(log=object()),
    )
    turns.claim_turn(_request())
    turns.append(_event(1, "turn.accepted", "accepted"), expected_sequence=0)
    turns.append(_event(2, "turn.completed", "completed"), expected_sequence=1)

    result = subject.TaskWorldActionReader(root_dir=tmp_path, turn_store=turns).action_status(
        project_id=PROJECT, turn_id=TURN,
    )

    assert result == {
        "turn_id": TURN, "action_id": ACTION, "status": "completed",
        "terminal": True, "ready_for_feedback": False,
    }
