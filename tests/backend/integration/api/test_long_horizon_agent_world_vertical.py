"""One durable vertical slice for long-horizon Agent supervision.

The test intentionally composes the production SQLite World, Turn and Profile
stores.  Only the external execution and cancellation effects are fakes: their
opaque receipts are the boundary that production code is expected to govern.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from backend.api.context_compaction import ContextCompactionError, DeterministicLocalMemoryCompactor
from backend.api.agent_dispatch_runtime import AgentDispatchRuntime
from backend.api.personal_world_model_runtime import PersonalWorldModelRuntime
from backend.api.project_provenance_runtime import ProjectProvenanceRuntime
from backend.api.project_task_graph_runtime import ProjectTaskGraphRuntime
from backend.api.project_trajectory_runtime import ProjectTrajectoryRuntime
from backend.api.world_supervision_runtime import WorldSupervisionRuntime
from core.ai_kernel import ContextEntry, ContextManifest, SQLiteAITurnStore
from core.ai_kernel.agent_contracts import AgentBudget, AgentBudgetReservation, AgentChildLink, AgentRun
from core.ai_kernel.agent_profiles import AgentProfileRegistry
from core.ai_kernel.agent_dispatch_store import SQLiteAgentDispatchStore
from core.ai_kernel.agent_store import SQLiteAgentStore
from core.long_horizon_runtime import (
    CancellationAcknowledgement,
    TaskBudget,
    TaskGraphNode,
    TaskGraphSpec,
    TraceLink,
    TraceSubject,
    ValidationFact,
    VersionBinding,
)
from core.personal_world_model import WorldEventDraft, WorldEventKind


PROJECT = "long-horizon-vertical"
NOW = "2026-09-03T12:00:00Z"
ROOT = Path(__file__).resolve().parents[4]


def test_parallel_experiment_pivot_reallocation_checkpoint_and_recover(tmp_path: Path) -> None:
    """Stale evidence must redirect the durable graph, never just its prompt."""

    database = tmp_path / ".rebuild-data" / "ai-turns.sqlite3"
    turns = SQLiteAITurnStore(database)
    # SQLiteAgentStore deliberately shares the real Turn database schema.
    profiles = AgentProfileRegistry(SQLiteAgentStore(database))
    world = _world(tmp_path)
    provenance = ProjectProvenanceRuntime(world=world)
    def verification_gate(_node, execution, result, evidence, satisfied):
        return (
            satisfied
            and execution.startswith(f"crp://executions/{PROJECT}/")
            and result.startswith(f"crp://results/{PROJECT}/")
            and evidence.startswith(f"crp://receipts/{PROJECT}/")
        )
    graph_runtime = ProjectTaskGraphRuntime(world=world, verification_gate=verification_gate)

    data = TraceSubject(PROJECT, "data", "dataset")
    data_r1 = _version("data/dataset", "r1")
    experiment_one = TraceSubject(PROJECT, "experiment", "candidate-one")
    experiment_two = TraceSubject(PROJECT, "experiment", "candidate-two")
    one_r1 = _version("experiments/candidate-one", "r1")
    two_r1 = _version("experiments/candidate-two", "r1")
    for subject, version in ((data, data_r1), (experiment_one, one_r1), (experiment_two, two_r1)):
        provenance.record_subject(subject=subject, version=version, recorded_at=NOW)
    # Link direction is essential: source depends on target, so data drift
    # propagates from the data target into both experiment sources.
    for source, version in ((experiment_one, one_r1), (experiment_two, two_r1)):
        provenance.record_link(
            link=TraceLink(PROJECT, source, version, data, data_r1, "depends_on"),
            recorded_at=NOW,
        )
    for subject, version, validation_id in (
        (data, data_r1, "data-r1-verified"),
        (experiment_one, one_r1, "experiment-one-verified"),
        (experiment_two, two_r1, "experiment-two-verified"),
    ):
        provenance.record_validation(
            validation=ValidationFact(
                PROJECT, validation_id, subject, version, "verified", "integration", "r1",
                (f"crp://receipts/{PROJECT}/{validation_id}",),
            ),
            recorded_at=NOW,
        )

    old_spec = TaskGraphSpec(
        "old-direction", PROJECT, "old-command", TaskBudget(2), 2,
        (
            TaskGraphNode("experiment-one", (), 1, TaskBudget(1), "all", experiment_one, one_r1),
            TaskGraphNode("experiment-two", (), 1, TaskBudget(1), "all", experiment_two, two_r1),
        ),
    )
    graph_runtime.create(spec=old_spec, recorded_at=NOW)
    dispatched: list[tuple[str, str]] = []
    old_graph = graph_runtime.dispatch_ready(
        project_id=PROJECT,
        graph_id=old_spec.graph_id,
        dispatch=lambda node, operation: (
            dispatched.append((node.node_id, operation))
            or f"crp://executions/{PROJECT}/{node.node_id}/{operation}"
        ),
        recorded_at=NOW,
    )
    assert {item[0] for item in dispatched} == {"experiment-one", "experiment-two"}
    assert {state.status for state in old_graph.nodes} == {"leased"}

    # A new input revision invalidates both already-running experimental paths.
    data_r2 = _version("data/dataset", "r2")
    provenance.record_subject(subject=data, version=data_r2, recorded_at=NOW)
    stale = graph_runtime.sync_provenance(
        project_id=PROJECT, graph_id=old_spec.graph_id, recorded_at=NOW,
    )
    assert {state.status for state in stale.nodes} == {"stale"}

    _declare_action(world, "old-action")
    supervision = WorldSupervisionRuntime(world=world)
    supervision.declare_default_claim(
        project_id=PROJECT, action_id="old-action", expected_outcome="Old direction is verified",
        action_sequence=world.project(PROJECT).through_sequence, recorded_at=NOW,
    )
    verification = supervision.record_verification(
        project_id=PROJECT, action_id="old-action", verdict="refuted",
        finding="The data revision refutes the old experimental direction.",
        checked_world_sequence=supervision.current_world_sequence(PROJECT),
        evidence_refs=[f"crp://receipts/{PROJECT}/reviewer-refuted"], recorded_at=NOW,
    )
    decision = supervision.record_decision(
        project_id=PROJECT, action_id="old-action",
        verification_id=str(verification.event.payload["verification_id"]),
        disposition="replan_required", rationale="Replace stale experiment work.",
        evidence_refs=[f"crp://receipts/{PROJECT}/reviewer-refuted"], recorded_at=NOW,
    )
    assert supervision.freshness_allows(PROJECT) is False

    # Intent is persisted before the injected effect; the receipt must settle it.
    # The reviewer decision is the cancellation's reason authority, not merely
    # an earlier event in the test.  First callback failure leaves a durable
    # intent that a reconstructed graph runtime settles with the same operation.
    reason_ref = decision.event.source_ref
    cancel_operations: list[str] = []

    def fail_cancel_once(operation: str) -> CancellationAcknowledgement:
        cancel_operations.append(operation)
        raise RuntimeError("cancel worker unavailable")

    with pytest.raises(RuntimeError, match="cancel worker unavailable"):
        graph_runtime.cancel(
            project_id=PROJECT, graph_id=old_spec.graph_id, node_id="experiment-one",
            reason_ref=reason_ref,
            callback=fail_cancel_once,
            recorded_at=NOW,
        )
    assert len(cancel_operations) == 1
    recovered_graph_runtime = ProjectTaskGraphRuntime(world=world, verification_gate=verification_gate)
    cancelled = recovered_graph_runtime.cancel(
        project_id=PROJECT, graph_id=old_spec.graph_id, node_id="experiment-one",
        reason_ref=reason_ref,
        callback=lambda operation: CancellationAcknowledgement(
            PROJECT, old_spec.graph_id, "experiment-one", operation,
            f"crp://receipts/{PROJECT}/cancel/{operation}",
        ),
        recorded_at=NOW,
    )
    assert cancelled.node("experiment-one").status == "cancelled"
    assert cancelled.node("experiment-one").cancellation_receipt_ref is not None
    assert cancel_operations == [
        cancelled.node("experiment-one").cancellation_receipt_ref.rsplit("/", 1)[-1]
    ]
    cancelled = recovered_graph_runtime.cancel(
        project_id=PROJECT, graph_id=old_spec.graph_id, node_id="experiment-two",
        reason_ref=reason_ref,
        callback=lambda operation: CancellationAcknowledgement(
            PROJECT, old_spec.graph_id, "experiment-two", operation,
            f"crp://receipts/{PROJECT}/cancel/{operation}",
        ),
        recorded_at=NOW,
    )
    assert {state.status for state in cancelled.nodes} == {"cancelled"}
    assert cancelled.budget_reserved.units == 0

    # Update the persisted built-in override: a new direction receives its own
    # model tier and budget revision rather than silently inheriting old limits.
    main = profiles.get("main.orchestrator")
    assert main is not None
    updated = profiles.update(
        replace(main, revision=main.revision + 1, model_tier="standard", budget_limit=AgentBudget(6, 12, 12_000, 3_000, 90_000)),
        expected_revision=main.revision,
    )
    assert updated.revision == 2 and updated.model_tier == "standard"

    replacement = TraceSubject(PROJECT, "experiment", "replacement")
    replacement_r1 = _version("experiments/replacement", "r1")
    provenance.record_subject(subject=replacement, version=replacement_r1, recorded_at=NOW)
    provenance.record_link(
        link=TraceLink(PROJECT, replacement, replacement_r1, data, data_r2, "depends_on"),
        recorded_at=NOW,
    )
    followup = TraceSubject(PROJECT, "artifact", "pivot-report")
    followup_r1 = _version("artifacts/pivot-report", "r1")
    delivery = TraceSubject(PROJECT, "artifact", "z-delivery-report")
    delivery_r1 = _version("artifacts/z-delivery-report", "r1")
    provenance.record_subject(subject=followup, version=followup_r1, recorded_at=NOW)
    provenance.record_subject(subject=delivery, version=delivery_r1, recorded_at=NOW)
    new_spec = TaskGraphSpec(
        "new-direction", PROJECT, str(decision.event.payload["decision_id"]), TaskBudget(3), 1,
        (
            TaskGraphNode("replacement", (), 1, TaskBudget(1), "all", replacement, replacement_r1),
            TaskGraphNode("pivot-report", ("replacement",), 1, TaskBudget(1), "all", followup, followup_r1),
            TaskGraphNode("z-delivery-report", ("replacement",), 1, TaskBudget(1), "all", delivery, delivery_r1),
        ),
    )
    graph_runtime.create(spec=new_spec, recorded_at=NOW)
    new_graph = graph_runtime.dispatch_ready(
        project_id=PROJECT, graph_id=new_spec.graph_id,
        dispatch=lambda node, operation: f"crp://executions/{PROJECT}/{node.node_id}/{operation}",
        recorded_at=NOW,
    )
    assert new_graph.node("replacement").status == "leased"
    settled = graph_runtime.observe_result(
        project_id=PROJECT, graph_id=new_spec.graph_id, node_id="replacement",
        result_ref=f"crp://results/{PROJECT}/replacement", satisfied=True,
        validation_status="verified", evidence_ref=f"crp://receipts/{PROJECT}/replacement",
        recorded_at=NOW,
    )
    assert settled.node("replacement").status == "settled"
    assert settled.node("pivot-report").status == "ready"
    resumed_branch = graph_runtime.dispatch_ready(
        project_id=PROJECT, graph_id=new_spec.graph_id,
        dispatch=lambda node, operation: f"crp://executions/{PROJECT}/{node.node_id}/{operation}",
        recorded_at=NOW,
    )
    assert resumed_branch.node("pivot-report").status == "leased"
    assert resumed_branch.node("z-delivery-report").status == "ready"

    main_run = _register_main(turns, SQLiteAgentStore(database), profiles, "turn-pivoted", "main-run-new")
    dispatch_runtime = AgentDispatchRuntime(
        payload_writer=lambda turn_id, kind, payload: turns.get_or_create_immutable_payload(turn_id, kind, payload),
        topology=SQLiteAgentStore(database), profiles=profiles,
        dispatch_store=SQLiteAgentDispatchStore(database),
        expert_resolver=lambda project, _request, _kind: (f"crp://experts/{project}/revised-hypothesis", 1),
        skill_resolver=lambda project, _request, _kind: (f"crp://skills/{project}/experiment-design", 1),
    )
    steward = _reserve_and_finalize_steward(turns, SQLiteAgentStore(database), profiles, main_run)
    intake = dispatch_runtime.route_intake(_turn_request("turn-pivoted", "pivot-operation"))
    plan = dispatch_runtime.publish_steward_plan(
        main_run=main_run, steward_run=steward, intake=intake,
        load=dispatch_runtime.snapshot_load(main_run, operation_id="pivot-load"),
        proposal={
            "mode": "cluster", "plan_id": "revised-direction",
            "cluster_id": "revised-experts",
            "assignments": [{
                "assignment_id": "replacement-expert", "profile_id": "subagent.worker",
                "profile_revision": 1, "task": "Validate the replacement experimental direction.",
                "budget": {"model_calls": 1, "tool_calls": 1, "input_tokens": 1_000, "output_tokens": 500, "wall_time_ms": 1_000},
                "capability_ids": ["memory.recall"],
                "expert": {"expert_id": "experiment-design"},
                "skill": {"skill_ids": ["experiment.design"]},
            }],
        },
        operation_id="publish-revised-direction",
    )
    assignment = plan.assignments[0]
    assert plan.plan.mode == "cluster" and len(plan.plan.assignment_ids) == 1
    assert assignment.profile_revision == 1 and assignment.delegated_budget.model_calls == 1
    assert assignment.expert_snapshot_ref == f"crp://experts/{PROJECT}/revised-hypothesis"
    assert assignment.skill_snapshot_ref == f"crp://skills/{PROJECT}/experiment-design"
    assert plan.cluster is not None and plan.permits[0].assignment_id == assignment.assignment_id
    topology = _Topology(PROJECT, SQLiteAgentStore(database), dispatch_runtime, main_run.run_id)
    trajectory = ProjectTrajectoryRuntime(world=world, profiles=profiles, topology=topology)
    route_ref = f"crp://routes/{PROJECT}/pivot/{decision.event.event_id}"
    checkpoint = trajectory.create_checkpoint(
        project_id=PROJECT, graph_id=new_spec.graph_id, segment_id="pivoted-branch-active",
        profile_id="main.orchestrator", route_revision=2,
        route_ref=route_ref, reason="reviewer-directed pivot",
        evidence_ref=reason_ref, recorded_at=NOW,
    )
    assert checkpoint.segment.profile_revision == 2
    assert checkpoint.verified_node_ids == ("replacement",)
    assert checkpoint.segment.evidence_ref == reason_ref and checkpoint.segment.route_ref == route_ref

    # Re-open composition on the same SQLite state.  The leased branch resumes
    # but does not get duplicated, and no non-ready node is redispatched.
    restarted_world = _world(tmp_path)
    restarted_profiles = AgentProfileRegistry(SQLiteAgentStore(database))
    restarted_store = SQLiteAgentStore(database)
    restarted_dispatch = AgentDispatchRuntime(
        payload_writer=lambda turn_id, kind, payload: turns.get_or_create_immutable_payload(turn_id, kind, payload),
        topology=restarted_store, profiles=restarted_profiles,
        dispatch_store=SQLiteAgentDispatchStore(database),
    )
    recovered_trajectory = ProjectTrajectoryRuntime(
        world=restarted_world, profiles=restarted_profiles,
        topology=_Topology(PROJECT, restarted_store, restarted_dispatch, "main-run-new"),
    )
    recovery = recovered_trajectory.recover(project_id=PROJECT, graph_id=new_spec.graph_id, operation_id="restart-leased")
    assert recovery.skip_verified == ("replacement",)
    assert recovery.resume_pending == ("pivot-report",)
    assert recovery.redispatch_ready == ()

    # Once the pending branch receives an external cancellation receipt, the
    # next checkpoint exposes only the genuinely ready sibling for redispatch.
    graph_runtime.cancel(
        project_id=PROJECT, graph_id=new_spec.graph_id, node_id="pivot-report",
        reason_ref=reason_ref,
        callback=lambda operation: CancellationAcknowledgement(
            PROJECT, new_spec.graph_id, "pivot-report", operation,
            f"crp://receipts/{PROJECT}/cancel/{operation}",
        ),
        recorded_at=NOW,
    )
    ready_checkpoint = trajectory.create_checkpoint(
        project_id=PROJECT, graph_id=new_spec.graph_id, segment_id="pivoted-branch-ready",
        profile_id="main.orchestrator", route_revision=3, route_ref=route_ref,
        reason="cancelled branch released the verified delivery path",
        evidence_ref=reason_ref, recorded_at=NOW,
    )
    assert ready_checkpoint.verified_node_ids == ("replacement",)
    restarted_world = _world(tmp_path)
    restarted_profiles = AgentProfileRegistry(SQLiteAgentStore(database))
    restarted_store = SQLiteAgentStore(database)
    restarted_dispatch = AgentDispatchRuntime(
        payload_writer=lambda turn_id, kind, payload: turns.get_or_create_immutable_payload(turn_id, kind, payload),
        topology=restarted_store, profiles=restarted_profiles,
        dispatch_store=SQLiteAgentDispatchStore(database),
    )
    recovery = ProjectTrajectoryRuntime(
        world=restarted_world, profiles=restarted_profiles,
        topology=_Topology(PROJECT, restarted_store, restarted_dispatch, "main-run-new"),
    ).recover(project_id=PROJECT, graph_id=new_spec.graph_id, operation_id="restart-ready")
    assert recovery.skip_verified == ("replacement",)
    assert recovery.resume_pending == ()
    assert recovery.redispatch_ready == ("z-delivery-report",)
    resumed_calls: list[str] = []
    resumed = ProjectTaskGraphRuntime(world=restarted_world).dispatch_ready(
        project_id=PROJECT, graph_id=new_spec.graph_id,
        dispatch=lambda node, operation: resumed_calls.append(node.node_id) or f"crp://executions/{PROJECT}/{node.node_id}/{operation}",
        recorded_at=NOW,
    )
    assert resumed_calls == ["z-delivery-report"] and resumed.node("z-delivery-report").status == "leased"

    _assert_turn_scoped_compaction(turns)


def _world(root: Path) -> PersonalWorldModelRuntime:
    return PersonalWorldModelRuntime.for_root(
        root, now=lambda: datetime(2026, 9, 3, 12, tzinfo=timezone.utc),
    )


def _version(kind: str, revision: str) -> VersionBinding:
    return VersionBinding(f"crp://{kind.split('/')[0]}/{PROJECT}/{kind.split('/')[1]}", revision, None)


def _declare_action(world: PersonalWorldModelRuntime, action_id: str) -> None:
    world.append_event(WorldEventDraft(
        event_id=f"world-action-{action_id}", project_id=PROJECT,
        kind=WorldEventKind.ACTION_PLANNED, actor="system",
        source_ref=f"crp://plans/{PROJECT}/{action_id}", source_revision="1",
        occurred_at=NOW, recorded_at=NOW,
        payload={
            "action_id": action_id, "title": "Review experimental direction",
            "expected_outcome": "Old direction is verified", "effect_class": "QUERYABLE",
            "gate_requirement": "none", "due_at": None,
            "evidence_refs": [f"crp://plans/{PROJECT}/{action_id}"],
        },
    ))


class _Topology:
    """Read main Run from the durable store; delegate snapshots to runtime."""

    def __init__(self, project_id: str, store: SQLiteAgentStore, dispatch: AgentDispatchRuntime, run_id: str) -> None:
        self._project_id = project_id
        self._store = store
        self._dispatch = dispatch
        self._run_id = run_id

    def main_run(self, *, project_id: str) -> AgentRun:
        assert project_id == self._project_id
        main = self._store.get_run(self._run_id)
        assert main is not None
        return main

    def snapshot_load(self, main_run: AgentRun, *, operation_id: str) -> object:
        assert main_run.run_id == self._run_id
        return self._dispatch.snapshot_load(main_run, operation_id=operation_id)


def _register_main(
    turns: SQLiteAITurnStore,
    store: SQLiteAgentStore,
    profiles: AgentProfileRegistry,
    turn_id: str,
    run_id: str,
) -> AgentRun:
    turns.claim_turn(_turn_request(turn_id, "pivot-operation"))
    profile = profiles.resolve_tier("main.orchestrator")
    run = AgentRun(
        run_id, turn_id, PROJECT, profile.profile_id, profile.profile_revision,
        "main", profile.model_tier, "queued", 0, 0, profile.budget_limit,
        profile.capability_ids, profile.max_concurrent_children, profile.max_depth,
        profile.max_steps, profile.timeout_ms, profile.allow_child_spawn,
        None, None, None, f"crp://agents/{PROJECT}/budget", None,
    )
    return store.register_run(run, operation_id=f"register-{run_id}")


def _reserve_and_finalize_steward(
    turns: SQLiteAITurnStore,
    store: SQLiteAgentStore,
    profiles: AgentProfileRegistry,
    main: AgentRun,
) -> AgentRun:
    profile = profiles.resolve_tier("steward.scheduler")
    child = AgentRun(
        "steward-run-new", "turn-steward-pivoted", PROJECT, profile.profile_id,
        profile.profile_revision, "subagent", profile.model_tier, "queued", 1, main.cancel_epoch,
        profile.budget_limit, profile.capability_ids, profile.max_concurrent_children,
        profile.max_depth, profile.max_steps, profile.timeout_ms, profile.allow_child_spawn,
        None, None, None, f"crp://agents/{PROJECT}/steward-budget", None, main.run_id,
    )
    operation = "reserve-steward-new"
    link = AgentChildLink(
        "steward-link-new", main.run_id, child.run_id, PROJECT, PROJECT, operation,
        main.cancel_epoch, 1, child.budget_limit, child.capability_ids, "reserved",
    )
    reservation = AgentBudgetReservation(
        "steward-reservation-new", PROJECT, main.run_id, child.run_id, operation,
        main.cancel_epoch, child.budget_limit, None, "reserved",
    )
    stored, _link, _reservation, _created = store.reserve_spawn(
        parent=main, child=child, link=link, reservation=reservation,
    )
    turns.claim_turn(_turn_request(child.turn_id, "steward-pivot-operation"))
    finalized = store.finalize_spawn(
        link.link_id, operation_id="finalize-steward-new", expected_cancel_epoch=main.cancel_epoch,
    )
    assert finalized.status == "spawned"
    return stored


def _turn_request(turn_id: str, operation_id: str) -> dict[str, object]:
    request = json.loads((ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8"))
    request.update({
        "turn_id": turn_id, "session_id": "vertical-session", "operation_id": operation_id,
        "idempotency_key": f"vertical-key-{turn_id}",
    })
    request["scope"] = {"kind": "project", "project_id": PROJECT, "series_id": None}
    return request


def _assert_turn_scoped_compaction(turns: SQLiteAITurnStore) -> None:
    first = _manifest_for_turn(turns, "turn-vertical-one", "r1")
    second = _manifest_for_turn(turns, "turn-vertical-two", "r2")
    compactor = DeterministicLocalMemoryCompactor(turns)
    first_compacted = compactor.compact(first)
    second_compacted = compactor.compact(second)
    first_summary = next(entry for entry in first_compacted.entries if entry.kind == "context_summary")
    second_summary = next(entry for entry in second_compacted.entries if entry.kind == "context_summary")
    first_payload = turns.get(first_summary.payload_ref)
    second_payload = turns.get(second_summary.payload_ref)
    assert first_payload["turn_id"] == "turn-vertical-one"
    assert first_payload["source_revisions"][0]["revision"] == "r1"
    assert second_payload["turn_id"] == "turn-vertical-two"
    assert second_payload["source_revisions"][0]["revision"] == "r2"
    assert first_summary.payload_ref != second_summary.payload_ref
    with pytest.raises(ContextCompactionError, match="source binding"):
        compactor.compact(replace(first, turn_id="turn-vertical-two"))
    # A revision change cannot replace the immutable summary inside the same
    # Turn. It must be rebuilt under a new Turn, leaving the earlier audit
    # source and summary untouched.
    same_turn_r2 = _manifest_for_existing_turn(
        turns, "turn-vertical-one", "r2",
    )
    with pytest.raises(ValueError, match="immutable payload identity conflict"):
        compactor.compact(same_turn_r2)
    assert turns.get(first_summary.payload_ref) == first_payload


def _manifest_for_turn(turns: SQLiteAITurnStore, turn_id: str, revision: str) -> ContextManifest:
    turns.claim_turn(_turn_request(turn_id, f"memory-operation-{revision}"))
    return _manifest_for_existing_turn(turns, turn_id, revision)


def _manifest_for_existing_turn(
    turns: SQLiteAITurnStore,
    turn_id: str,
    revision: str,
) -> ContextManifest:
    capability_ref = turns.get_or_create_immutable_payload(turn_id, "capability", {"schema_version": "1.0.0"})
    entries = []
    for index in (1, 2):
        markdown = (f"verified {revision} evidence {index} " * 100).strip()
        payload_ref = turns.get_or_create_immutable_payload(turn_id, f"memory-{revision}-{index}", {
            "schema_version": "1.0.0", "kind": "memory_r1", "project_id": PROJECT,
            "object_id": f"atom-{index}", "revision": revision, "trust_status": "trusted", "markdown": markdown,
        })
        entries.append(ContextEntry(
            f"memory-{index}", "memory_r1", f"crp://memory/{PROJECT}/atom-{index}", payload_ref,
            PROJECT, revision, None, (), "model", "published", len(markdown.encode("utf-8")),
        ))
    return ContextManifest(
        f"context-manifest-{turn_id}", turn_id, "vertical", PROJECT, None, "profile", 1,
        "boundary", 1, capability_ref, tuple(entries), (), (), 8192,
        sum(item.content_bytes for item in entries),
    )
