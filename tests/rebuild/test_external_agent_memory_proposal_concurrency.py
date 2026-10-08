from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time

from core.ai_kernel import (
    AgentAdapterProfile,
    ContextEntry,
    ContextManifest,
    ExternalAgentAdmissionSnapshot,
    ExternalAgentAuthoritySnapshot,
    ExternalAgentContextBridge,
    ExternalAgentContextConflict,
    SQLiteAITurnStore,
    context_manifest_to_payload,
)


NOW = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)


def test_same_operation_is_single_candidate_and_replays_after_contention(tmp_path: Path) -> None:
    database, session_id, payload_ref = _prepared_session(tmp_path)
    entered_sink = threading.Event()
    release_sink = threading.Event()
    sink_calls: list[str] = []
    sink_lock = threading.Lock()

    def sink(proposal: dict[str, object], project_id: str) -> dict[str, object]:
        del proposal
        with sink_lock:
            sink_calls.append(project_id)
        entered_sink.set()
        assert release_sink.wait(timeout=5)
        with sink_lock:
            return _sink_result(f"candidate-{len(sink_calls)}")

    first = _bridge(database, sink)
    second = _bridge(database, sink)
    results: list[dict[str, object]] = []
    errors: list[Exception] = []
    result_lock = threading.Lock()

    def submit(bridge: ExternalAgentContextBridge) -> None:
        try:
            value = _submit(bridge, session_id, payload_ref, operation_id="proposal-same")
            with result_lock:
                results.append(dict(value))
        except Exception as error:  # assertion below distinguishes only expected conflict
            with result_lock:
                errors.append(error)

    one = threading.Thread(target=submit, args=(first,))
    two = threading.Thread(target=submit, args=(second,))
    one.start()
    assert entered_sink.wait(timeout=5)
    two.start()
    time.sleep(0.2)  # keep the first sink blocked while the second store observes prepared state
    release_sink.set()
    one.join(timeout=5)
    two.join(timeout=5)

    assert not one.is_alive() and not two.is_alive()
    assert sink_calls == ["project-a"]
    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], ExternalAgentContextConflict)

    replay = _submit(_bridge(database, sink), session_id, payload_ref, operation_id="proposal-same")
    assert replay["proposal_id"] == results[0]["proposal_id"] == "candidate-1"
    assert replay["change_cursor"] == results[0]["change_cursor"] == 1
    assert replay["replayed"] is True
    assert _proposal_operation_rows(database) == [("proposal-same", "finalized", 1)]
    assert _effect_rows(database) == [("proposal-same", "SETTLED_OK", "memory_propose", 1)]
    assert _project_changes(database) == [(1, "memory.proposed")]


def test_different_operations_complete_independently_under_sqlite_contention(tmp_path: Path) -> None:
    database, session_id, payload_ref = _prepared_session(tmp_path)
    start = threading.Barrier(3)
    sink_calls: list[str] = []
    sink_lock = threading.Lock()

    def sink(proposal: dict[str, object], project_id: str) -> dict[str, object]:
        with sink_lock:
            sink_calls.append(str(proposal["proposal_id"]))
        return _sink_result(str(proposal["proposal_id"]))

    values: list[dict[str, object]] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def submit(operation_id: str) -> None:
        bridge = _bridge(database, sink)
        start.wait(timeout=5)
        try:
            result = _submit(bridge, session_id, payload_ref, operation_id=operation_id)
            with lock:
                values.append(dict(result))
        except Exception as error:
            with lock:
                errors.append(error)

    one = threading.Thread(target=submit, args=("proposal-alpha",))
    two = threading.Thread(target=submit, args=("proposal-beta",))
    one.start()
    two.start()
    start.wait(timeout=5)
    one.join(timeout=5)
    two.join(timeout=5)

    assert not one.is_alive() and not two.is_alive()
    assert errors == []
    assert {item["operation_id"] for item in values} == {"proposal-alpha", "proposal-beta"}
    assert {item["proposal_id"] for item in values} == {"bridge-proposal-alpha", "bridge-proposal-beta"}
    assert sorted(sink_calls) == ["bridge-proposal-alpha", "bridge-proposal-beta"]
    rows = _proposal_operation_rows(database)
    assert {item[:2] for item in rows} == {
        ("proposal-alpha", "finalized"),
        ("proposal-beta", "finalized"),
    }
    assert {item[2] for item in rows} == {1, 2}
    assert _project_changes(database) == [(1, "memory.proposed"), (2, "memory.proposed")]


def test_prepared_operation_recovers_in_new_store_and_bridge_instance(tmp_path: Path) -> None:
    database, session_id, payload_ref = _prepared_session(tmp_path)

    def unavailable_sink(proposal: dict[str, object], project_id: str) -> dict[str, object]:
        del proposal, project_id
        raise RuntimeError("simulated process failure")

    try:
        _submit(_bridge(database, unavailable_sink), session_id, payload_ref, operation_id="proposal-recover")
    except RuntimeError as error:
        assert str(error) == "simulated process failure"
    else:
        raise AssertionError("failure injection must leave a prepared operation")
    assert _proposal_operation_rows(database) == [("proposal-recover", "prepared", None)]
    assert _effect_rows(database) == [("proposal-recover", "INFLIGHT", "memory_propose", 1)]

    recovered = _bridge(database, lambda proposal, project_id: _sink_result(str(proposal["proposal_id"])))
    assert recovered.recover_prepared_memory_proposals() == {"completed": 1, "failed": 0}
    assert _proposal_operation_rows(database) == [("proposal-recover", "finalized", 1)]
    assert _effect_rows(database) == [("proposal-recover", "SETTLED_OK", "memory_propose", 1)]
    replay = _submit(recovered, session_id, payload_ref, operation_id="proposal-recover")
    assert replay["proposal_id"] == "bridge-proposal-recover"
    assert replay["replayed"] is True


def _prepared_session(tmp_path: Path) -> tuple[Path, str, str]:
    database = tmp_path / "turns.sqlite3"
    store = SQLiteAITurnStore(database)
    store.claim_turn({
        "turn_id": "turn-a", "session_id": "session-a", "operation_id": "turn-operation",
        "idempotency_key": "turn-key-a", "scope": {"project_id": "project-a", "series_id": None},
        "context_policy": {"max_context_bytes": 4096},
    })
    store.append(_event(1, "turn.accepted"), expected_sequence=0)
    payload_ref = store.put("turn-a", "application-skill-instructions", {
        "schema_version": "1.0.0", "skill_id": "skill-a", "skill_fingerprint": "fingerprint-a",
        "markdown": "bounded project background",
    })
    capability_ref = store.put("turn-a", "capability-manifest", {"capability_ids": []})
    manifest = ContextManifest(
        manifest_id="context-manifest-turn-a", turn_id="turn-a", resolver_id="fixture-resolver",
        project_id="project-a", series_id=None, project_profile_id="profile-project-a",
        project_profile_revision=4, boundary_profile_id="boundary-project-a",
        boundary_profile_revision=7, capability_manifest_ref=capability_ref,
        entries=(ContextEntry(
            entry_id="skill-entry", kind="application_skill", source_ref="crp://skills/project-a/skill-a",
            payload_ref=payload_ref, source_project_id="project-a", revision_identity="skill-r4",
            content_fingerprint=None, provenance_refs=("crp://documents/project-a/doc-a",),
            disclosure="model", selection_reason="project_scope",
            content_bytes=len("bounded project background".encode("utf-8")),
        ),),
        compactions=(), excluded_reason_counts=(), max_context_bytes=4096,
        selected_context_bytes=len("bounded project background".encode("utf-8")),
    )
    manifest_ref = store.put("turn-a", "context-manifest", context_manifest_to_payload(manifest))
    store.append(_event(2, "context.resolved", payload_ref=manifest_ref), expected_sequence=1)
    bridge = _bridge(database, lambda proposal, project_id: _sink_result(str(proposal["proposal_id"])))
    started = bridge.start_session(
        operation_id="start-context-map", adapter_id="codex", adapter_revision=1,
        template_revision="template-r1", turn_id="turn-a", project_id="project-a",
        purpose="project_assistance", requested_context_bytes=4096,
        admission=_admission("start-context-map"),
    )
    bridge.resolve_context(
        operation_id="resolve-context", session_id=str(started["session_id"]), context_refs=[payload_ref],
        expected_context_manifest_revision="context-manifest-turn-a", purpose="project_assistance",
    )
    return database, str(started["session_id"]), payload_ref


def _bridge(database: Path, sink):
    return ExternalAgentContextBridge(
        store=SQLiteAITurnStore(database),
        adapters=[AgentAdapterProfile(
            adapter_id="codex", revision=1, template_revision="template-r1",
            maximum_context_bytes=8192, supported_purposes=("project_assistance",),
        )],
        authority=lambda _: ExternalAgentAuthoritySnapshot(
            project_id="project-a", project_profile_id="profile-project-a", project_profile_revision=4,
            boundary_profile_id="boundary-project-a", boundary_profile_revision=7,
        ),
        proposal_sink=sink, clock=lambda: NOW,
    )


def _submit(
    bridge: ExternalAgentContextBridge, session_id: str, payload_ref: str, *, operation_id: str,
) -> dict[str, object]:
    return dict(bridge.submit_memory_proposal(
        operation_id=operation_id, session_id=session_id,
        expected_context_manifest_revision="context-manifest-turn-a", purpose="project_assistance",
        admission=_admission(operation_id), proposal={
            "proposal_type": "memory_candidate_proposal", "summary": "candidate for review",
            "source_refs": [payload_ref], "evidence_refs": [payload_ref],
            "suggested_changes": {"memory": "candidate"}, "requires_user_review": True,
        },
    ))


def _admission(operation_id: str) -> ExternalAgentAdmissionSnapshot:
    return ExternalAgentAdmissionSnapshot(
        admission_id=operation_id, project_id="project-a", adapter_id="codex", outcome="allow",
        policy_revision=7, reason_codes=("fixture_local_proposal_allowed",),
    )


def _sink_result(proposal_id: str) -> dict[str, object]:
    return {"status": "pending_review", "project_id": "project-a", "proposal_id": proposal_id}


def _proposal_operation_rows(database: Path) -> list[tuple[str, str, int | None]]:
    connection = SQLiteAITurnStore(database)._connect()  # noqa: SLF001 - durable Gate inspection
    try:
        return [tuple(row) for row in connection.execute(
            "SELECT operation_id,status,change_cursor FROM ai_external_agent_proposal_operations ORDER BY operation_id"
        )]
    finally:
        connection.close()


def _project_changes(database: Path) -> list[tuple[int, str]]:
    connection = SQLiteAITurnStore(database)._connect()  # noqa: SLF001 - durable Gate inspection
    try:
        return [tuple(row) for row in connection.execute(
            "SELECT cursor,change_type FROM ai_project_event_feed WHERE project_id='project-a' ORDER BY cursor"
        )]
    finally:
        connection.close()


def _effect_rows(database: Path) -> list[tuple[str, str, str, int]]:
    connection = SQLiteAITurnStore(database)._connect()  # noqa: SLF001 - durable Gate inspection
    try:
        return [tuple(row) for row in connection.execute(
            "SELECT operation_id,state,kind,attempt FROM effect ORDER BY operation_id"
        )]
    finally:
        connection.close()


def _event(sequence: int, event_type: str, *, payload_ref: str | None = None) -> dict[str, object]:
    return {
        "schema_version": "1.0.0", "event_id": f"event-{sequence}-fixture", "turn_id": "turn-a",
        "session_id": "session-a", "sequence": sequence, "type": event_type, "actor": "ai-kernel",
        "correlation": {"step_id": None, "tool_call_id": None, "model_request_id": None,
                        "operation_id": "turn-operation"},
        "data": {"status": "running", "summary": "private body", "capability_id": None,
                 "payload_ref": payload_ref, "receipt_ref": None, "evidence_refs": [],
                 "error_code": None, "retryable": False},
        "occurred_at": NOW.isoformat(),
    }
