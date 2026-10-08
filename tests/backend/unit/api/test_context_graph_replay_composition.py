from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path

import pytest

from backend.api.context_graph_replay_composition import (
    ContextGraphReplayCompositionError,
    ContextGraphReplayCompositionService,
    ReplayPlanCommand,
)
from backend.api.context_graph_snapshot_runtime import ContextGraphSnapshotRepository
from core.context_graph import (
    ContextBinding,
    ContextGraphEdge,
    ContextGraphNode,
    ContextGraphSnapshot,
    ContextPermissionGrant,
    ContextProvenance,
    FrozenContextRevisions,
)
from core.context_graph.replay_completion import (
    TrustedCompletion,
    _issue_trusted_model_wire_evidence,
    _issue_trusted_terminal_evidence,
)
from core.storage_provider import SQLiteStructuredRecordStore, SQLiteStructuredRecordUnitOfWork


def _revisions() -> FrozenContextRevisions:
    return FrozenContextRevisions("1.0.0", "boundary-1", "provider-1", "route-1", "2.0.0")


def _snapshot() -> ContextGraphSnapshot:
    when = "2026-08-30T00:00:00Z"
    def node(node_id: str, *, stale: bool = False) -> ContextGraphNode:
        return ContextGraphNode(node_id, "answer", node_id, f"ref://{node_id}", "r1", (f"source://{node_id}",), "verified", when, when, stale=stale, stale_reason="content_changed" if stale else None, metadata={"content": f"old {node_id}"})
    return ContextGraphSnapshot("1.0.0", "graph-a", "r1", "project-a", "fixture", "r1", when,
        (node("node-a", stale=True), node("node-b", stale=True), node("node-z")),
        (ContextGraphEdge("edge-a-b", "node-a", "node-b", "full_chain", 1, 1),),
        ("node-b",), 10, ContextProvenance("fixture", "r1", when, "fixture", "1", "source://graph"))


def _binding() -> ContextBinding:
    return ContextBinding("1.0.0", "graph-a", "r1", "1.0.0", "2.0.0", "boundary-1", "provider-1", "route-1", (), {"materials": (), "references": (), "conversation": ()}, {"materials": 0, "references": 0, "conversation": 0}, 0, (), (), ("node-a", "node-b"), (), (), {"staleness": {"current_graph_revision": "r1", "affected_node_ids": ("node-a", "node-b"), "replay_order": ("node-a", "node-b"), "confirmation_present": True, "confirmed_by": "actor-a", "confirmed_at": "2026-08-30T01:00:00Z"}})


def _service(
    tmp_path: Path, current_revisions=None,
) -> tuple[ContextGraphReplayCompositionService, ContextGraphSnapshotRepository]:
    snapshots = ContextGraphSnapshotRepository(SQLiteStructuredRecordStore(tmp_path / "snapshots.sqlite3"))
    source = _snapshot()
    snapshots.append(source, None, capability_id="thought_graph_context", capability_revision="1.0.0",
        permission_grant=ContextPermissionGrant("project-a", "permission-1", frozenset(node.content_ref for node in source.nodes)),
        permission_evidence_refs=("evidence://permission",))
    service = ContextGraphReplayCompositionService(SQLiteStructuredRecordStore(tmp_path / "replay.sqlite3"), snapshots,
        current_revisions or (lambda *_: _revisions()),
        lambda: "2026-08-30T02:00:00Z")
    return service, snapshots


def _plan(service: ContextGraphReplayCompositionService):
    return service.create_plan(ReplayPlanCommand("cmd-1", "project-a", "graph-a", "r1", "crp://context-bindings/project-a/binding-a", _binding(), "session-a", False, ()))


@dataclass(frozen=True)
class _Verified:
    completion: TrustedCompletion
    output_text: str


def _complete(monkeypatch: pytest.MonkeyPatch, service: ContextGraphReplayCompositionService, plan_id: str, text: str):
    import backend.api.context_graph_replay_composition as module
    def fake(*, replay_request, **_kwargs):
        return _Verified(TrustedCompletion(
            _issue_trusted_terminal_evidence(replay_request.turn_id, replay_request.turn_operation_id, f"event:{replay_request.node_id}", "completed"),
            _issue_trusted_model_wire_evidence(replay_request.turn_id, replay_request.turn_operation_id, f"wire:{replay_request.node_id}"),
            f"output:{replay_request.node_id}", "2026-08-30T02:00:00Z"), text)
    monkeypatch.setattr(module, "verified_replay_completion_from_turn", fake)
    return service.accept_completed_turn(plan_id, object())  # fake bridge never reads it


def test_prepares_only_normal_turns_in_dependency_order_and_includes_trusted_predecessor_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service, _ = _service(tmp_path)
    plan = _plan(service)
    first = service.prepare_next(plan.replay_plan_id)
    assert first and first.request.node_id == "node-a" and first.turn_envelope["desired_outcome"] == "context.evaluate"
    assert first.turn_envelope["input"]["refs"] == [{"kind": "context_binding", "object_id": "binding-a", "uri": "crp://context-bindings/project-a/binding-a"}]
    assert first.predecessor_outputs == ()
    _complete(monkeypatch, service, plan.replay_plan_id, "generated A")
    second = service.prepare_next(plan.replay_plan_id)
    assert second and second.request.node_id == "node-b"
    assert second.request.input_fingerprint != first.request.input_fingerprint
    assert second.predecessor_outputs == (("node-a", "generated A"),)
    payload = json.loads(second.turn_envelope["input"]["text"])
    assert payload["target_node_id"] == "node-b"
    assert payload["predecessor_outputs"] == [{"node_id": "node-a", "content": "generated A", "untrusted_context": True}]
    restarted = ContextGraphReplayCompositionService(
        SQLiteStructuredRecordStore(tmp_path / "replay.sqlite3"), _,
        lambda *_: _revisions(), lambda: "later-clock-value",
    )
    assert restarted.prepare_next(plan.replay_plan_id).turn_envelope == second.turn_envelope  # type: ignore[union-attr]
    assert _plan(restarted) == plan


def test_all_trusted_receipts_append_one_fresh_snapshot_and_leave_unrelated_node_stable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service, snapshots = _service(tmp_path)
    plan = _plan(service)
    assert _complete(monkeypatch, service, plan.replay_plan_id, "generated A").finalized_snapshot is None
    final = _complete(monkeypatch, service, plan.replay_plan_id, "generated B").finalized_snapshot
    assert final and final.graph_revision == plan.result_graph_revision and final.predecessor == "r1"
    nodes = {item.node_id: item for item in final.snapshot.nodes}
    assert nodes["node-a"].metadata["content"] == "generated A" and not nodes["node-a"].stale
    assert nodes["node-b"].content_ref == "output:node-b" and not nodes["node-b"].stale
    assert nodes["node-z"] == _snapshot().nodes[2]


def test_evidence_failure_never_clears_stale_or_issues_success_receipt(tmp_path: Path):
    service, snapshots = _service(tmp_path)
    plan = _plan(service)
    with pytest.raises(ContextGraphReplayCompositionError, match="evidence_rejected"):
        service.accept_completed_turn(plan.replay_plan_id, object())
    current = snapshots.current("project-a", "graph-a")
    assert current and current.graph_revision == "r1" and all(node.stale for node in current.snapshot.nodes[:2])
    assert service.prepare_next(plan.replay_plan_id).request.node_id == "node-a"  # type: ignore[union-attr]


def test_plan_request_and_finalization_are_idempotent_and_restart_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service, snapshots = _service(tmp_path)
    plan = _plan(service)
    assert _plan(service) == plan
    assert service.prepare_next(plan.replay_plan_id) == service.prepare_next(plan.replay_plan_id)
    _complete(monkeypatch, service, plan.replay_plan_id, "generated A")
    _complete(monkeypatch, service, plan.replay_plan_id, "generated B")
    restarted = ContextGraphReplayCompositionService(SQLiteStructuredRecordStore(tmp_path / "replay.sqlite3"), snapshots, lambda *_: _revisions(), lambda: "later")
    assert restarted.prepare_next(plan.replay_plan_id) is None
    result = restarted.accept_completed_turn(plan.replay_plan_id, object())
    assert result.idempotent and result.finalized_snapshot and result.finalized_snapshot.graph_revision == plan.result_graph_revision


def test_source_or_revision_drift_fails_closed_before_request(tmp_path: Path):
    service, snapshots = _service(tmp_path)
    plan = _plan(service)
    original = _snapshot()
    changed = replace(original, graph_revision="r2", source_revision="r2", provenance=replace(original.provenance, source_revision="r2"))
    snapshots.append(changed, "r1", capability_id="thought_graph_context", capability_revision="1.0.0",
        permission_grant=ContextPermissionGrant("project-a", "permission-2", frozenset(node.content_ref for node in changed.nodes)), permission_evidence_refs=("evidence://permission-2",))
    with pytest.raises(ContextGraphReplayCompositionError, match="source_graph_drift"):
        service.prepare_next(plan.replay_plan_id)


def test_receipt_and_output_are_one_atomic_record_on_injected_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service, snapshots = _service(tmp_path)
    plan = _plan(service)
    original = SQLiteStructuredRecordUnitOfWork.put
    def fail_receipt(self, collection, *args, **kwargs):
        if collection == "context_graph_replay_receipts":
            raise RuntimeError("injected receipt crash")
        return original(self, collection, *args, **kwargs)
    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, "put", fail_receipt)
    with pytest.raises(RuntimeError, match="injected receipt crash"):
        _complete(monkeypatch, service, plan.replay_plan_id, "generated A")
    assert SQLiteStructuredRecordStore(tmp_path / "replay.sqlite3").list("context_graph_replay_receipts") == ()
    assert snapshots.current("project-a", "graph-a").graph_revision == "r1"  # type: ignore[union-attr]


def test_existing_result_revision_with_different_snapshot_or_authority_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service, snapshots = _service(tmp_path)
    plan = _plan(service)
    _complete(monkeypatch, service, plan.replay_plan_id, "generated A")
    final = _complete(monkeypatch, service, plan.replay_plan_id, "generated B").finalized_snapshot
    assert final is not None
    original = snapshots.current
    for forged in (
        replace(final, snapshot=replace(final.snapshot, token_estimate=999)),
        replace(final, predecessor="forged-predecessor"),
    ):
        snapshots.current = lambda *_, value=forged: value  # type: ignore[method-assign]
        with pytest.raises(ContextGraphReplayCompositionError, match="result_snapshot_identity_drift"):
            service.accept_completed_turn(plan.replay_plan_id, object())
    snapshots.current = original  # type: ignore[method-assign]


def test_remote_replay_requires_consent_and_freezes_possible_pii_policy(tmp_path: Path):
    service, _ = _service(tmp_path)
    with pytest.raises(ContextGraphReplayCompositionError, match="plan_command_invalid"):
        service.create_plan(ReplayPlanCommand(
            "cmd-remote-invalid", "project-a", "graph-a", "r1",
            "crp://context-bindings/project-a/binding-a", _binding(),
            "session-a", True, (),
        ))

    plan = service.create_plan(ReplayPlanCommand(
        "cmd-remote", "project-a", "graph-a", "r1",
        "crp://context-bindings/project-a/binding-a", _binding(),
        "session-a", True, ("crp://consents/project-a/replay",),
    ))
    prepared = service.prepare_next(plan.replay_plan_id)
    assert prepared is not None
    assert prepared.turn_envelope["privacy"] == {
        "mode": "remote_allowed", "allow_remote": True, "pii": "possible",
        "consent_refs": ["crp://consents/project-a/replay"],
        "retention": "local_durable",
    }


def test_revision_fence_replays_exact_binding_and_remote_policy_and_session(
    tmp_path: Path,
):
    calls: list[tuple[str, str, str, bool]] = []

    def current(
        project_id: str, capability_id: str, binding_id: str,
        allow_remote: bool,
    ) -> FrozenContextRevisions:
        calls.append((project_id, capability_id, binding_id, allow_remote))
        return _revisions()

    service, _ = _service(tmp_path, current)
    plan = _plan(service)
    service.assert_session(plan.replay_plan_id, "session-a")
    with pytest.raises(ContextGraphReplayCompositionError, match="session_drift"):
        service.assert_session(plan.replay_plan_id, "session-b")
    service.prepare_next(plan.replay_plan_id)

    assert calls == [
        ("project-a", "thought_graph_context", "binding-a", False),
        ("project-a", "thought_graph_context", "binding-a", False),
    ]
