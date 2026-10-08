from __future__ import annotations

from dataclasses import replace

import pytest

from core.context_graph import (
    ContextGraphEdge,
    ContextGraphNode,
    ContextGraphSnapshot,
    ContextProvenance,
    FrozenContextRevisions,
    InMemoryReplayCompletionRepository,
    ReplayCompletionAuthority,
    ReplayCompletionConflict,
    ReplayCompletionError,
    ReplayPlan,
    ReplayRequest,
    TrustedCompletion,
    evaluate_staleness,
)
from core.context_graph.replay_completion import (
    _TrustedModelWireEvidence,
    _TrustedTerminalEvidence,
    _issue_trusted_model_wire_evidence,
    _issue_trusted_terminal_evidence,
)


def _revisions() -> FrozenContextRevisions:
    return FrozenContextRevisions("cap-r1", "boundary-r1", "provider-r1", "route-r1", "compiler-r1")


def _snapshot(*, stale: bool = True, positioned: bool = False) -> ContextGraphSnapshot:
    def node(node_id: str) -> ContextGraphNode:
        return ContextGraphNode(
            node_id=node_id,
            node_type="conclusion" if node_id == "out" else "note",
            title=node_id,
            content_ref=f"content:{node_id}",
            content_revision="content-r1",
            source_refs=(f"source:{node_id}",),
            trust="verified",
            created_at="2026-08-30T00:00:00Z",
            updated_at="2026-08-30T00:00:00Z",
            stale=stale,
            stale_reason="upstream_context_changed" if stale else None,
            metadata={"project_id": "project", "content": node_id, **({"position": {"x": 2, "y": 3}} if positioned else {})},
        )

    return ContextGraphSnapshot(
        "1.0.0", "graph", "graph-r2", "project", "fixture", "source-r1", "2026-08-30T00:00:00Z",
        (node("root"), node("mid"), node("out")),
        (ContextGraphEdge("root-mid", "root", "mid", "full_chain", 1, 0), ContextGraphEdge("mid-out", "mid", "out", "full_chain", 1, 1)),
        ("out",), 1,
        ContextProvenance("fixture", "source-r1", "2026-08-30T00:00:00Z", "fixture", "1", "fixture.json"),
    )


class _RevisionAuthority:
    def __init__(self, *, current: str = "graph-r2", revisions: FrozenContextRevisions | None = None) -> None:
        self.current = current
        self.revisions = revisions or _revisions()
        self.calls: list[
            tuple[str, str, str, str, str, FrozenContextRevisions]
        ] = []

    def assert_current(
        self, *, replay_plan_id: str, project_id: str, graph_id: str,
        source_graph_revision: str, binding_ref: str,
        revisions: FrozenContextRevisions,
    ) -> None:
        self.calls.append((
            replay_plan_id, project_id, graph_id, source_graph_revision,
            binding_ref, revisions,
        ))
        if (
            replay_plan_id, project_id, graph_id, source_graph_revision,
            binding_ref, revisions,
        ) != (
            "replay-plan-1", "project", "graph", self.current,
            "binding:1", self.revisions,
        ):
            raise ReplayCompletionError("replay_source_revision_not_current")


def _plan(snapshot: ContextGraphSnapshot | None = None) -> ReplayPlan:
    graph = snapshot or _snapshot()
    return ReplayPlan.from_evaluated_snapshot(
        replay_plan_id="replay-plan-1",
        result_graph_revision="graph-r3",
        source_snapshot=graph,
        binding_ref="binding:1",
        revisions=_revisions(),
        input_fingerprints={node_id: f"input:{node_id}:r2" for node_id in ("root", "mid", "out")},
    )


def _request(plan: ReplayPlan, node_id: str, predecessors: tuple[str, ...] = ()) -> ReplayRequest:
    index = plan.node_ids.index(node_id)
    return ReplayRequest(
        f"request:{node_id}", plan.replay_plan_id, plan.project_id, plan.graph_id,
        plan.source_graph_revision, plan.result_graph_revision, node_id, index,
        plan.input_fingerprint_for(node_id), plan.binding_ref, f"turn:{node_id}", f"operation:{node_id}",
        predecessors, plan.revisions,
    )


def _completion(request: ReplayRequest) -> TrustedCompletion:
    return TrustedCompletion(
        _issue_trusted_terminal_evidence(request.turn_id, request.turn_operation_id, f"terminal:{request.node_id}", "completed"),
        _issue_trusted_model_wire_evidence(request.turn_id, request.turn_operation_id, f"wire:{request.node_id}"),
        f"output:{request.node_id}",
        "2026-08-30T01:00:00Z",
    )


def _authority(repository: InMemoryReplayCompletionRepository | None = None, revisions: _RevisionAuthority | None = None) -> ReplayCompletionAuthority:
    return ReplayCompletionAuthority(repository or InMemoryReplayCompletionRepository(), revisions or _RevisionAuthority())


def test_authority_accepts_only_ordered_plan_with_exact_predecessor_receipts_and_is_idempotent() -> None:
    plan = _plan()
    repository = InMemoryReplayCompletionRepository()
    revision_authority = _RevisionAuthority()
    authority = _authority(repository, revision_authority)

    root_request = _request(plan, "root")
    root = authority.accept(plan=plan, request=root_request, completion=_completion(root_request))
    mid_request = _request(plan, "mid", (root.receipt_ref,))
    mid = authority.accept(plan=plan, request=mid_request, completion=_completion(mid_request))
    out_request = _request(plan, "out", (root.receipt_ref, mid.receipt_ref))
    out = authority.accept(plan=plan, request=out_request, completion=_completion(out_request))

    assert (root.node_id, mid.node_id, out.node_id) == plan.node_ids
    assert authority.accept(plan=plan, request=out_request, completion=_completion(out_request)) == out
    assert out.predecessor_receipt_refs == (root.receipt_ref, mid.receipt_ref)
    assert {call[3] for call in revision_authority.calls} == {"graph-r2"}
    assert {call[0] for call in revision_authority.calls} == {plan.replay_plan_id}
    assert {call[4] for call in revision_authority.calls} == {plan.binding_ref}


def test_node_receipts_do_not_advance_the_snapshot_or_become_successor_input() -> None:
    """This pure authority only records completion; a future bridge re-freezes input."""

    plan = _plan()
    repository = InMemoryReplayCompletionRepository()
    authority = _authority(repository)
    root_request = _request(plan, "root")
    root = authority.accept(plan=plan, request=root_request, completion=_completion(root_request))
    mid_request = _request(plan, "mid", (root.receipt_ref,))

    assert mid_request.source_graph_revision == plan.source_graph_revision
    assert mid_request.result_graph_revision == plan.result_graph_revision
    assert mid_request.input_fingerprint == plan.input_fingerprint_for("mid")
    assert repository.get_by_plan_node(plan.replay_plan_id, "mid") is None


def test_authority_rejects_out_of_order_or_missing_or_forged_predecessor_receipts() -> None:
    plan = _plan()
    authority = _authority()
    out_request = _request(plan, "out", ("replay-receipt:request:root", "replay-receipt:request:mid"))
    with pytest.raises(ReplayCompletionError, match="replay_predecessor_not_accepted"):
        authority.accept(plan=plan, request=out_request, completion=_completion(out_request))

    root_request = _request(plan, "root")
    root = authority.accept(plan=plan, request=root_request, completion=_completion(root_request))
    mid_request = _request(plan, "mid", ("forged-receipt",))
    with pytest.raises(ReplayCompletionError, match="replay_predecessor_receipts_mismatch"):
        authority.accept(plan=plan, request=mid_request, completion=_completion(mid_request))
    assert root.receipt_ref == "replay-receipt:request:root"


def test_request_input_fingerprint_and_idempotency_identity_are_immutable() -> None:
    plan = _plan()
    authority = _authority()
    request = _request(plan, "root")
    authority.accept(plan=plan, request=request, completion=_completion(request))

    with pytest.raises(ReplayCompletionError, match="replay_request_input_fingerprint_mismatch"):
        authority.accept(plan=plan, request=replace(request, input_fingerprint="changed"), completion=_completion(request))
    changed_output = TrustedCompletion(
        _issue_trusted_terminal_evidence(request.turn_id, request.turn_operation_id, "terminal:root", "completed"),
        _issue_trusted_model_wire_evidence(request.turn_id, request.turn_operation_id, "wire:root"),
        "output:changed", "2026-08-30T01:00:00Z",
    )
    with pytest.raises(ReplayCompletionConflict, match="replay_request_idempotency_conflict"):
        authority.accept(plan=plan, request=request, completion=changed_output)


def test_terminal_and_model_wire_evidence_must_be_platform_owned_and_exact() -> None:
    plan = _plan()
    request = _request(plan, "root")
    assert "terminal_event_ref" not in ReplayRequest.__dataclass_fields__
    assert "model_wire_receipt_ref" not in ReplayRequest.__dataclass_fields__
    with pytest.raises(TypeError, match="authority-owned"):
        _TrustedTerminalEvidence(request.turn_id, request.turn_operation_id, "terminal:root", "completed", object())
    with pytest.raises(TypeError, match="authority-owned"):
        _TrustedModelWireEvidence(request.turn_id, request.turn_operation_id, "wire:root", object())
    with pytest.raises(ReplayCompletionError, match="replay_terminal_not_completed"):
        TrustedCompletion(
            _issue_trusted_terminal_evidence(request.turn_id, request.turn_operation_id, "terminal:root", "failed"),
            _issue_trusted_model_wire_evidence(request.turn_id, request.turn_operation_id, "wire:root"),
            "output:root", "2026-08-30T01:00:00Z",
        )

    wrong_wire = TrustedCompletion(
        _issue_trusted_terminal_evidence(request.turn_id, "operation:other", "terminal:root", "completed"),
        _issue_trusted_model_wire_evidence(request.turn_id, "operation:other", "wire:other"),
        "output:root", "2026-08-30T01:00:00Z",
    )
    with pytest.raises(ReplayCompletionError, match="replay_terminal_operation_mismatch"):
        _authority().accept(plan=plan, request=request, completion=wrong_wire)


def test_authority_records_actual_terminal_and_wire_refs_only_from_trusted_completion() -> None:
    plan = _plan()
    request = _request(plan, "root")
    completion = TrustedCompletion(
        _issue_trusted_terminal_evidence(request.turn_id, request.turn_operation_id, "terminal:actual", "completed"),
        _issue_trusted_model_wire_evidence(request.turn_id, request.turn_operation_id, "wire:actual"),
        "output:root", "2026-08-30T01:00:00Z",
    )

    receipt = _authority().accept(plan=plan, request=request, completion=completion)
    assert (receipt.terminal_event_ref, receipt.model_wire_receipt_ref) == ("terminal:actual", "wire:actual")


def test_future_or_revision_drift_fails_closed() -> None:
    plan = _plan()
    request = _request(plan, "root")
    with pytest.raises(ReplayCompletionError, match="replay_source_revision_not_current"):
        _authority(revisions=_RevisionAuthority(current="graph-r3")).accept(
            plan=plan, request=request, completion=_completion(request),
        )
    with pytest.raises(ReplayCompletionError, match="replay_source_revision_not_current"):
        _authority(revisions=_RevisionAuthority(revisions=FrozenContextRevisions("cap-r2", "boundary-r1", "provider-r1", "route-r1", "compiler-r1"))).accept(
            plan=plan, request=request, completion=_completion(request),
        )


def test_only_evaluated_stale_nodes_enter_plan_and_visual_position_changes_are_not_a_replay_input() -> None:
    with pytest.raises(ReplayCompletionError, match="replay_plan_requires_stale_nodes"):
        _plan(_snapshot(stale=False))

    previous = replace(_snapshot(stale=False), graph_revision="graph-r1")
    moved = replace(_snapshot(stale=False, positioned=True), graph_revision="graph-r2")
    revision_map = {
        "capability_revision": "cap-r1", "compiler_revision": "compiler-r1",
        "boundary_revision": "boundary-r1", "provider_revision": "provider-r1",
        "model_route_revision": "route-r1",
    }
    evaluated = evaluate_staleness(previous, moved, previous_revisions=revision_map, current_revisions=revision_map)
    assert not any(node.stale for node in evaluated.nodes)
    with pytest.raises(ReplayCompletionError, match="replay_plan_requires_stale_nodes"):
        _plan(evaluated)


def test_plan_and_request_scope_mismatch_fail_closed() -> None:
    plan = _plan()
    request = _request(plan, "root")
    with pytest.raises(ReplayCompletionError, match="replay_request_plan_scope_mismatch"):
        _authority().accept(plan=plan, request=replace(request, binding_ref="binding:forged"), completion=_completion(request))
    with pytest.raises(ReplayCompletionError, match="replay_request_plan_order_mismatch"):
        _authority().accept(plan=plan, request=replace(request, node_id="mid"), completion=_completion(request))
