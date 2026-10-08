"""One focused full-composition Turn probe; the model transport is local fake."""

import json
from pathlib import Path
from threading import Event
from time import monotonic
from types import SimpleNamespace

from fastapi import FastAPI

from backend.api.ai_runtime import get_or_build_ai_runtime
from backend.memory_app.app import create_app
from backend.memory_app.model_config import ModelConfiguration
from backend.security.secrets import InMemorySecretStore
from core.storage_provider import SQLiteStructuredRecordStore


def test_recognition_task_runs_through_existing_coordinator_runner_and_wire_receipts(tmp_path):
    calls = []

    def completion(**kwargs):
        calls.append(True)
        return {"choices": [{"finish_reason": "stop", "message": {"content": "private result"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}}

    records = SQLiteStructuredRecordStore(tmp_path / "recognition.sqlite3")
    models = ModelConfiguration(records, tmp_path, InMemorySecretStore(), completion_fn=completion)
    models.update("generation", dict(base_url="https://example.test", model="test-model", api_key="synthetic-only",
                                    allow_remote=True, expected_revision=0))
    app = create_app(runtime_root=tmp_path, legacy_app=FastAPI(), model_configuration=models)
    installer = app.state.recognition_turn_installer
    installations = []

    def observe_installation(registry, payloads, application_state):
        installations.append(application_state)
        return installer(registry, payloads, application_state)

    app.state.recognition_turn_installer = observe_installation
    with records.begin() as tx:
        tx.put("recognition_tasks", "task-one", dict(project_id="project-a", turn_id="turn-one",
            context_packet_id="packet-one", input="private prompt", state="queued"), expected_revision=0)
        tx.put("recognition_context_packets", "packet-one", dict(project_id="project-a", task_id="task-one",
            state="consumed", query="private prompt", model_revision=1,
            messages=[dict(role="user", content="private prompt")], items=[]), expected_revision=0)
        tx.commit()
    runtime = get_or_build_ai_runtime(SimpleNamespace(app=app), SimpleNamespace(root_dir=tmp_path))
    assert installations == [app.state]
    runner = app.state.ai_turn_runner
    failures = []
    original_fail = runtime._fail
    def observe_failure(turn_id, error):
        failures.append((repr(error), repr(error.__cause__), repr(getattr(error.__cause__, "__cause__", None))))
        return original_fail(turn_id, error)
    runtime._fail = observe_failure
    coordinator = app.state.agent_runtime_composition.coordinator
    request = json.loads((Path(__file__).resolve().parents[2] / "core-contracts/ai/fixtures/turn-request/valid-project-answer.json").read_text(encoding="utf-8"))
    request.update(turn_id="turn-one", session_id="session-one", operation_id="operation-one", idempotency_key="key-one",
        scope=dict(kind="project", project_id="project-a", series_id=None),
        input=dict(kind="text", text="Execute confirmed recognition task", refs=[]),
        desired_outcome="recognition.task.result",
        privacy=dict(mode="remote_allowed", allow_remote=True, pii="possible",
                     consent_refs=["crp://default/consent/packet-one"], retention="local_durable"),
        capability_policy=dict(allowed=["recognition.task.execute"], denied=[], require_approval=["recognition.task.execute"]),
        capability_request=dict(mode="execute_exact_v1", capability_id="recognition.task.execute",
                                arguments=dict(task_id="task-one", context_packet_id="packet-one")),
        context_policy=dict(include_project_skill=False, include_memory=False, include_session_history=False, max_context_bytes=11744))
    try:
        prepared = coordinator.accept_and_register_main(request)
        coordinator.submit_accepted_turn(prepared)
        deadline = monotonic() + 15
        while runner.active_turn_ids and monotonic() < deadline:
            Event().wait(.02)
        events = tuple(runtime.events_after("turn-one"))
        approvals = [event for event in events if event["type"] == "approval.required"]
        assert approvals, failures
        assert calls == []
        approval = approvals[-1]
        runner.accept_action_and_submit(dict(schema_version="1.0.0", action_id="action-one", turn_id="turn-one",
            type="approve", target_event_id=approval["event_id"], reason="User confirmed this exact task",
            actor="user", expected_sequence=events[-1]["sequence"], idempotency_key="approve-one", created_at="2026-09-15T12:00:00Z"))
        terminal = runner.wait_for_terminal("turn-one", timeout_seconds=20)
        events = tuple(runtime.events_after("turn-one"))
        assert terminal is not None and terminal.status == "completed", failures
        assert calls == [True]
        deadline = monotonic() + 5
        while records.read("recognition_tasks", "task-one").payload["state"] == "result_ready" and monotonic() < deadline:
            Event().wait(.02)
        assert records.read("recognition_tasks", "task-one").payload["state"] == "completed"
        main = app.state.agent_runtime_composition.store.get_run("main-run-turn-one")
        assert main is not None and main.status == "completed"
        assert main.model_routing_snapshot_ref is not None
        assert len(records.list("documents")) == 1
        assert any(event["type"] == "model.routed" for event in events)
        # No duplicated prompt/result bodies in old Turn events or payloads.
        import sqlite3
        with sqlite3.connect(tmp_path / ".rebuild-data" / "ai-turns.sqlite3") as connection:
            attempts = connection.execute("SELECT status, terminal_receipt_ref, lease_owner_id, lease_generation FROM ai_model_attempt_reservations WHERE turn_id=?", ("turn-one",)).fetchall()
            dump = "\n".join(connection.iterdump())
        assert "private prompt" not in dump and "private result" not in dump and "synthetic-only" not in dump
        assert len(attempts) == 1 and attempts[0][0] == "terminal"
        assert attempts[0][1] and attempts[0][2] and attempts[0][3] >= 1
    finally:
        runner.shutdown(timeout_seconds=5)
