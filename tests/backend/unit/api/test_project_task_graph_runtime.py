from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.api.personal_world_model_runtime import PersonalWorldModelRuntime
from backend.api.project_provenance_runtime import ProjectProvenanceRuntime
from backend.api.project_task_graph_runtime import ProjectTaskGraphError, ProjectTaskGraphRuntime
from core.long_horizon_runtime import (
    CancellationAcknowledgement,
    TaskBudget,
    TaskGraphNode,
    TaskGraphSpec,
    TraceSubject,
    VersionBinding,
)


PROJECT = "project-graph"
NOW = "2026-09-03T09:00:00Z"


def test_create_replay_dispatch_and_validation_gated_join(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    spec = _spec("graph-alpha", (
        _node("first", (), "artifact-first"),
        _node("second", ("first",), "artifact-second"),
    ))
    _declare(runtime, spec)
    created = runtime.create(spec=spec, recorded_at=NOW)
    assert runtime.create(spec=spec, recorded_at=NOW).to_payload() == created.to_payload()
    with pytest.raises(ProjectTaskGraphError, match="identity conflicts"):
        runtime.create(
            spec=replace(
                spec,
                nodes=(replace(spec.nodes[0], priority=2), spec.nodes[1]),
            ),
            recorded_at=NOW,
        )
    calls: list[str] = []
    dispatched = runtime.dispatch_ready(
        project_id=PROJECT, graph_id=spec.graph_id,
        dispatch=lambda node, operation: calls.append(operation) or f"crp://executions/{node.node_id}",
        recorded_at=NOW,
    )
    assert calls and dispatched.node("first").status == "leased"
    waiting = runtime.observe_result(
        project_id=PROJECT, graph_id=spec.graph_id, node_id="first",
        result_ref="crp://results/first", satisfied=True, validation_status="inconclusive",
        evidence_ref="crp://receipts/first", recorded_at=NOW,
    )
    assert waiting.node("second").status == "blocked"
    settled = runtime.observe_result(
        project_id=PROJECT, graph_id=spec.graph_id, node_id="first",
        result_ref="crp://results/first", satisfied=True, validation_status="verified",
        evidence_ref="crp://receipts/first", recorded_at=NOW,
    )
    assert settled.node("second").status == "ready"


def test_two_node_dispatch_and_restart_does_not_repeat_callbacks(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    spec = _spec("graph-parallel", (_node("one", (), "artifact-one"), _node("two", (), "artifact-two")))
    _declare(runtime, spec)
    runtime.create(spec=spec, recorded_at=NOW)
    calls: list[str] = []
    runtime.dispatch_ready(
        project_id=PROJECT, graph_id=spec.graph_id,
        dispatch=lambda node, operation: calls.append(operation) or f"crp://executions/{node.node_id}", recorded_at=NOW,
    )
    assert len(calls) == 2
    restarted = _runtime(tmp_path)
    restarted.dispatch_ready(
        project_id=PROJECT, graph_id=spec.graph_id,
        dispatch=lambda node, operation: calls.append(operation) or f"crp://executions/{node.node_id}", recorded_at=NOW,
    )
    assert len(calls) == 2
    cancelled = restarted.cancel(
        project_id=PROJECT,
        graph_id=spec.graph_id,
        node_id="one",
        reason_ref="crp://decisions/project-graph/cancel-one",
        recorded_at=NOW,
    )
    assert cancelled.node("one").status == "cancel_requested"


def test_sync_stales_dependants_and_reschedule_then_prune_remain_facts(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    first = _node("first", (), "artifact-first")
    second = _node("second", ("first",), "artifact-second")
    provenance = ProjectProvenanceRuntime(world=runtime._world)  # type: ignore[attr-defined]
    for node in (first, second):
        provenance.record_subject(subject=node.subject, version=node.subject_version, recorded_at=NOW)
    spec = _spec("graph-stale", (first, second))
    runtime.create(spec=spec, recorded_at=NOW)
    runtime.dispatch_ready(
        project_id=PROJECT, graph_id=spec.graph_id,
        dispatch=lambda node, _operation: f"crp://executions/{node.node_id}", recorded_at=NOW,
    )
    runtime.observe_result(
        project_id=PROJECT, graph_id=spec.graph_id, node_id="first", result_ref="crp://results/first",
        satisfied=True, validation_status="verified", evidence_ref="crp://receipts/first", recorded_at=NOW,
    )
    provenance.record_subject(
        subject=first.subject,
        version=VersionBinding(first.subject_version.authority_ref, "r2", None),
        recorded_at=NOW,
    )
    stale = runtime.sync_provenance(project_id=PROJECT, graph_id=spec.graph_id, recorded_at=NOW)
    assert stale.node("first").status == "stale"
    assert stale.node("second").status == "stale"

    graph = _spec("graph-mutations", (_node("only", (), "artifact-only"),))
    _declare(runtime, graph)
    runtime.create(spec=graph, recorded_at=NOW)
    runtime.dispatch_ready(project_id=PROJECT, graph_id=graph.graph_id, dispatch=lambda _node, _op: "crp://executions/only", recorded_at=NOW)
    runtime.observe_result(project_id=PROJECT, graph_id=graph.graph_id, node_id="only", result_ref="crp://results/only", satisfied=True, validation_status="verified", evidence_ref="crp://receipts/only", recorded_at=NOW)
    rescheduled = runtime.reschedule(project_id=PROJECT, graph_id=graph.graph_id, node_id="only", reason_ref="crp://reasons/retry", recorded_at=NOW)
    assert rescheduled.node("only").status == "ready"
    pruned = runtime.prune(project_id=PROJECT, graph_id=graph.graph_id, node_id="only", reason_ref="crp://reasons/prune", recorded_at=NOW)
    assert pruned.node("only").status == "pruned"


def test_create_and_verified_result_fail_closed_without_external_authority(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    undeclared = _spec("graph-undeclared", (_node("only", (), "artifact-only"),))
    with pytest.raises(ProjectTaskGraphError, match="provenance"):
        runtime.create(spec=undeclared, recorded_at=NOW)

    spec = _spec("graph-no-gate", (_node("only", (), "artifact-gated"),))
    _declare(runtime, spec)
    runtime.create(spec=spec, recorded_at=NOW)
    runtime.dispatch_ready(
        project_id=PROJECT,
        graph_id=spec.graph_id,
        dispatch=lambda _node, _operation: "crp://executions/only",
        recorded_at=NOW,
    )
    no_gate = ProjectTaskGraphRuntime(world=runtime._world)  # type: ignore[attr-defined]
    with pytest.raises(ProjectTaskGraphError, match="external gate"):
        no_gate.observe_result(
            project_id=PROJECT,
            graph_id=spec.graph_id,
            node_id="only",
            result_ref="crp://results/only",
            satisfied=True,
            validation_status="verified",
            evidence_ref="crp://receipts/only",
            recorded_at=NOW,
        )
    assert no_gate.project(project_id=PROJECT, graph_id=spec.graph_id).node("only").result_ref is None


def test_cancel_intent_survives_external_callback_failure(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    spec = _spec("graph-cancel-intent", (_node("only", (), "artifact-cancel"),))
    _declare(runtime, spec)
    runtime.create(spec=spec, recorded_at=NOW)
    runtime.dispatch_ready(
        project_id=PROJECT,
        graph_id=spec.graph_id,
        dispatch=lambda _node, _operation: "crp://executions/only",
        recorded_at=NOW,
    )

    def fail_after_intent(_operation: str) -> CancellationAcknowledgement:
        raise RuntimeError("external cancellation unavailable")

    with pytest.raises(RuntimeError, match="unavailable"):
        runtime.cancel(
            project_id=PROJECT,
            graph_id=spec.graph_id,
            node_id="only",
            reason_ref="crp://decisions/project-graph/cancel-only",
            recorded_at=NOW,
            callback=fail_after_intent,
        )
    assert runtime.project(
        project_id=PROJECT,
        graph_id=spec.graph_id,
    ).node("only").status == "cancel_requested"


def test_cancel_recovery_persists_same_operation_and_acknowledgement(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    spec = _spec("graph-cancel-recovery", (_node("only", (), "artifact-cancel-recovery"),))
    _declare(runtime, spec)
    runtime.create(spec=spec, recorded_at=NOW)
    runtime.dispatch_ready(
        project_id=PROJECT,
        graph_id=spec.graph_id,
        dispatch=lambda _node, _operation: "crp://executions/only",
        recorded_at=NOW,
    )
    calls: list[str] = []

    def crash_once(operation: str) -> CancellationAcknowledgement:
        calls.append(operation)
        if len(calls) == 1:
            raise RuntimeError("cancel transport failed")
        return CancellationAcknowledgement(
            PROJECT, spec.graph_id, "only", operation,
            f"crp://cancellations/{PROJECT}/only",
        )

    with pytest.raises(RuntimeError, match="transport"):
        runtime.cancel(
            project_id=PROJECT, graph_id=spec.graph_id, node_id="only",
            reason_ref=f"crp://decisions/{PROJECT}/cancel-only", recorded_at=NOW,
            callback=crash_once,
        )
    recovered = _runtime(tmp_path).cancel(
        project_id=PROJECT, graph_id=spec.graph_id, node_id="only",
        reason_ref=f"crp://decisions/{PROJECT}/cancel-only", recorded_at=NOW,
        callback=crash_once,
    )
    assert calls == [calls[0], calls[0]]
    assert recovered.node("only").status == "cancelled"
    assert recovered.node("only").cancellation_receipt_ref == f"crp://cancellations/{PROJECT}/only"


@pytest.mark.parametrize(
    ("acknowledgement", "label"),
    (
        (lambda operation, graph: CancellationAcknowledgement(PROJECT, "other-graph", "only", operation, f"crp://receipts/{PROJECT}/opaque"), "graph"),
        (lambda operation, graph: CancellationAcknowledgement(PROJECT, graph, "other-node", operation, f"crp://receipts/{PROJECT}/opaque"), "node"),
        (lambda operation, graph: CancellationAcknowledgement(PROJECT, graph, "only", "other-operation", f"crp://receipts/{PROJECT}/opaque"), "operation"),
        (lambda operation, graph: CancellationAcknowledgement("other-project", graph, "only", operation, "crp://receipts/other-project/opaque"), "project"),
    ),
)
def test_cancel_rejects_mismatched_acknowledgement_but_keeps_durable_intent(
    tmp_path: Path,
    acknowledgement: Callable[[str, str], CancellationAcknowledgement],
    label: str,
) -> None:
    runtime = _runtime(tmp_path)
    spec = _spec("graph-cancel-receipt", (_node("only", (), "artifact-cancel-receipt"),))
    _declare(runtime, spec)
    runtime.create(spec=spec, recorded_at=NOW)
    runtime.dispatch_ready(
        project_id=PROJECT, graph_id=spec.graph_id,
        dispatch=lambda _node, _operation: "crp://executions/only", recorded_at=NOW,
    )
    assert label
    with pytest.raises(ProjectTaskGraphError, match="does not match"):
        runtime.cancel(
            project_id=PROJECT, graph_id=spec.graph_id, node_id="only",
            reason_ref=f"crp://decisions/{PROJECT}/cancel-only", recorded_at=NOW,
            callback=lambda operation: acknowledgement(operation, spec.graph_id),
        )
    assert runtime.project(project_id=PROJECT, graph_id=spec.graph_id).node("only").status == "cancel_requested"


def test_cancel_rejects_non_acknowledgement_but_keeps_durable_intent(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    spec = _spec("graph-cancel-invalid-receipt", (_node("only", (), "artifact-cancel-invalid-receipt"),))
    _declare(runtime, spec)
    runtime.create(spec=spec, recorded_at=NOW)
    runtime.dispatch_ready(
        project_id=PROJECT, graph_id=spec.graph_id,
        dispatch=lambda _node, _operation: "crp://executions/only", recorded_at=NOW,
    )
    with pytest.raises(ProjectTaskGraphError, match="does not match"):
        runtime.cancel(
            project_id=PROJECT, graph_id=spec.graph_id, node_id="only",
            reason_ref=f"crp://decisions/{PROJECT}/cancel-only", recorded_at=NOW,
            callback=lambda _operation: object(),  # type: ignore[arg-type]
        )
    assert runtime.project(project_id=PROJECT, graph_id=spec.graph_id).node("only").status == "cancel_requested"


def test_dispatch_intent_recovers_with_the_same_operation_after_callback_crash(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    spec = _spec("graph-dispatch-recovery", (_node("only", (), "artifact-dispatch"),))
    _declare(runtime, spec)
    runtime.create(spec=spec, recorded_at=NOW)
    accepted: dict[str, str] = {}
    calls: list[str] = []

    def crashes_after_accepting(node: TaskGraphNode, operation: str) -> str:
        calls.append(operation)
        accepted.setdefault(operation, f"crp://executions/{node.node_id}")
        if len(calls) == 1:
            raise RuntimeError("crash after external acceptance")
        return accepted[operation]

    with pytest.raises(RuntimeError, match="external acceptance"):
        runtime.dispatch_ready(
            project_id=PROJECT,
            graph_id=spec.graph_id,
            dispatch=crashes_after_accepting,
            recorded_at=NOW,
        )
    intent = runtime.project(project_id=PROJECT, graph_id=spec.graph_id).node("only")
    assert intent.status == "dispatching"
    assert intent.dispatch_operation_id == calls[0]

    recovered = _runtime(tmp_path).dispatch_ready(
        project_id=PROJECT,
        graph_id=spec.graph_id,
        dispatch=crashes_after_accepting,
        recorded_at=NOW,
    )
    assert recovered.node("only").status == "leased"
    assert calls == [calls[0], calls[0]]
    assert len(accepted) == 1


def _runtime(root: Path) -> ProjectTaskGraphRuntime:
    return ProjectTaskGraphRuntime(
        world=PersonalWorldModelRuntime.for_root(
            root, now=lambda: datetime(2026, 9, 3, 9, tzinfo=timezone.utc),
        ),
        verification_gate=lambda _node, _execution, result, evidence, _satisfied: (
            result.startswith("crp://results/") and evidence.startswith("crp://receipts/")
        ),
    )


def _declare(runtime: ProjectTaskGraphRuntime, spec: TaskGraphSpec) -> None:
    provenance = ProjectProvenanceRuntime(world=runtime._world)  # type: ignore[attr-defined]
    for node in spec.nodes:
        provenance.record_subject(
            subject=node.subject,
            version=node.subject_version,
            recorded_at=NOW,
        )


def _spec(graph_id: str, nodes: tuple[TaskGraphNode, ...]) -> TaskGraphSpec:
    return TaskGraphSpec(graph_id, PROJECT, f"command-{graph_id}", TaskBudget(2), min(2, len(nodes)), nodes)


def _node(node_id: str, dependencies: tuple[str, ...], artifact: str) -> TaskGraphNode:
    subject = TraceSubject(PROJECT, "artifact", artifact)
    version = VersionBinding(f"crp://artifacts/{PROJECT}/{artifact}", "r1", None)
    return TaskGraphNode(node_id, dependencies, 1, TaskBudget(1), "all", subject, version)
