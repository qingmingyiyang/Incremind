from __future__ import annotations

import pytest

from core.long_horizon_runtime.task_graph import (
    CancellationAcknowledgement,
    TaskBudget,
    TaskGraphContractError,
    TaskGraphEvent,
    TaskGraphNode,
    TaskGraphSpec,
    project_task_graph,
)
from core.long_horizon_runtime.provenance import TraceSubject, VersionBinding


PROJECT = "project-dag-alpha"
GRAPH = "graph-alpha"


def _node(node_id: str, dependencies: tuple[str, ...], priority: int, budget: TaskBudget, join: str = "all") -> TaskGraphNode:
    return TaskGraphNode(
        node_id, dependencies, priority, budget, join,
        TraceSubject(PROJECT, "task", f"subject-{node_id}"),
        VersionBinding(f"crp://tasks/{PROJECT}/{node_id}", "revision-1", None),
    )


def _spec() -> TaskGraphSpec:
    return TaskGraphSpec(
        graph_id=GRAPH,
        project_id=PROJECT,
        command_id="command-alpha",
        budget_limit=TaskBudget(units=6),
        max_concurrency=2,
        nodes=(
            _node("source", (), 3, TaskBudget(units=2)),
            _node("left", ("source",), 2, TaskBudget(units=2)),
            _node("right", ("source",), 1, TaskBudget(units=2)),
            _node("join", ("left", "right"), 0, TaskBudget(units=1)),
        ),
    )


def _event(sequence: int, kind: str, *, operation_id: str | None = None, **payload: object) -> TaskGraphEvent:
    if "spec" in payload and isinstance(payload["spec"], TaskGraphSpec):
        payload["spec"] = payload["spec"].to_payload()
    return TaskGraphEvent(
        event_id=f"event-{sequence}", project_id=PROJECT, graph_id=GRAPH,
        sequence=sequence, kind=kind, operation_id=operation_id or f"operation-{sequence}", payload=payload,
    )


def _dispatch(sequence: int, node_id: str, ref: str, attempt: int = 1) -> tuple[TaskGraphEvent, TaskGraphEvent]:
    operation_id = f"dispatch-{node_id}-{attempt}"
    return (
        _event(sequence, "node.dispatch_requested", operation_id=operation_id, node_id=node_id, attempt=attempt),
        _event(sequence + 1, "node.dispatched", operation_id=operation_id, node_id=node_id, attempt=attempt, execution_ref=ref),
    )


def test_dependencies_parallel_ready_priority_and_budget_are_derived() -> None:
    state = project_task_graph((_event(1, "graph.created", spec=_spec()),))
    assert state.ready_node_ids == ("source",)

    state = project_task_graph((
        _event(1, "graph.created", spec=_spec()),
            *_dispatch(2, "source", "crp://agent/source"),
            _event(4, "node.result_observed", node_id="source", attempt=1, result_ref="crp://receipt/source", satisfied=True),
            _event(5, "node.validation", node_id="source", status="verified", evidence_ref="crp://validation/source"),
    ))
    assert state.ready_node_ids == ("left", "right")
    assert state.next_dispatchable_node_ids == ("left", "right")

    state = project_task_graph((
        _event(1, "graph.created", spec=_spec()),
            *_dispatch(2, "source", "crp://agent/source"),
            _event(4, "node.result_observed", node_id="source", attempt=1, result_ref="crp://receipt/source", satisfied=True),
            _event(5, "node.validation", node_id="source", status="verified", evidence_ref="crp://validation/source"),
            *_dispatch(6, "left", "crp://agent/left"),
    ))
    assert state.next_dispatchable_node_ids == ("right",)
    assert state.budget_reserved.units == 2


def test_join_failure_blocks_required_downstream_and_any_prunes_losers() -> None:
    spec = TaskGraphSpec(
        graph_id=GRAPH, project_id=PROJECT, command_id="command-join",
        budget_limit=TaskBudget(units=4), max_concurrency=2,
        nodes=(
            _node("a", (), 1, TaskBudget(units=1)), _node("b", (), 1, TaskBudget(units=1)),
            _node("all", ("a", "b"), 0, TaskBudget(units=1)), _node("any", ("a", "b"), 0, TaskBudget(units=1), "any"),
        ),
    )
    state = project_task_graph((
        _event(1, "graph.created", spec=spec),
        *_dispatch(2, "a", "crp://agent/a"),
        _event(4, "node.result_observed", node_id="a", attempt=1, result_ref="crp://receipt/a", satisfied=True),
        _event(5, "node.validation", node_id="a", status="verified", evidence_ref="crp://validation/a"),
        *_dispatch(6, "b", "crp://agent/b"),
        _event(8, "node.result_observed", node_id="b", attempt=1, result_ref="crp://receipt/b", satisfied=False),
        _event(9, "node.validation", node_id="b", status="rejected", evidence_ref="crp://validation/b"),
    ))
    assert state.node("all").status == "blocked"
    assert state.node("any").status == "ready"


def test_cancel_prune_reschedule_and_validation_gate_do_not_forge_external_terminal() -> None:
    state = project_task_graph((
        _event(1, "graph.created", spec=_spec()),
        _event(2, "node.validation", node_id="source", status="stale", evidence_ref="crp://validation/source"),
    ))
    assert state.node("source").status == "stale"
    assert state.next_dispatchable_node_ids == ()

    state = project_task_graph((
        _event(1, "graph.created", spec=_spec()),
        _event(2, "node.pruned", node_id="source", reason_ref="crp://decision/source"),
        _event(3, "node.rescheduled", node_id="source", attempt=1, reason_ref="crp://decision/retry"),
    ))
    assert state.node("source").attempt == 0
    assert state.node("source").status == "ready"
    assert state.node("source").result_ref is None


def test_cancellation_requires_a_matching_durable_receipt_before_releasing_capacity() -> None:
    intent = project_task_graph((
        _event(1, "graph.created", spec=_spec()),
        *_dispatch(2, "source", "crp://agent/source"),
        _event(
            4,
            "node.cancel_requested",
            operation_id="cancel-source",
            node_id="source",
            reason_ref="crp://decisions/project-dag-alpha/cancel-source",
        ),
    ))
    assert intent.node("source").status == "cancel_requested"
    assert intent.budget_reserved.units == 2

    acknowledged = project_task_graph((
        _event(1, "graph.created", spec=_spec()),
        *_dispatch(2, "source", "crp://agent/source"),
        _event(4, "node.cancel_requested", operation_id="cancel-source", node_id="source", reason_ref="crp://decisions/project-dag-alpha/cancel-source"),
        _event(5, "node.cancelled", operation_id="cancel-source", node_id="source", cancellation_receipt_ref="crp://cancellations/project-dag-alpha/source"),
    ))
    assert acknowledged.node("source").status == "cancelled"
    assert acknowledged.node("source").cancellation_receipt_ref == "crp://cancellations/project-dag-alpha/source"
    assert acknowledged.budget_reserved.units == 0

    rescheduled = project_task_graph((
        _event(1, "graph.created", spec=_spec()),
        *_dispatch(2, "source", "crp://agent/source"),
        _event(4, "node.cancel_requested", operation_id="cancel-source", node_id="source", reason_ref="crp://decisions/project-dag-alpha/cancel-source"),
        _event(5, "node.cancelled", operation_id="cancel-source", node_id="source", cancellation_receipt_ref="crp://cancellations/project-dag-alpha/source"),
        _event(6, "node.rescheduled", node_id="source", attempt=2, reason_ref="crp://decisions/project-dag-alpha/retry-source"),
    ))
    assert rescheduled.node("source").status == "ready"
    assert rescheduled.node("source").attempt == 1

    with pytest.raises(TaskGraphContractError, match="acknowledgement"):
        project_task_graph((
            _event(1, "graph.created", spec=_spec()),
            _event(2, "node.cancelled", operation_id="cancel-source", node_id="source", cancellation_receipt_ref="crp://cancellations/project-dag-alpha/source"),
        ))


def test_project_scope_event_replay_and_cycles_fail_closed() -> None:
    created = _event(1, "graph.created", spec=_spec())
    assert project_task_graph((created, created)).replayed_event_ids == ("event-1",)
    with pytest.raises(TaskGraphContractError, match="project"):
        project_task_graph((created, TaskGraphEvent(
            "other", "project-other", GRAPH, 2, "node.cancel_requested", "op-other",
            {"node_id": "source", "reason_ref": "crp://decision/project-other/source"},
        )))
    with pytest.raises(TaskGraphContractError, match="cycle"):
        TaskGraphSpec(
            graph_id=GRAPH, project_id=PROJECT, command_id="cycle", budget_limit=TaskBudget(units=2), max_concurrency=1,
            nodes=(_node("a", ("b",), 1, TaskBudget(units=1)), _node("b", ("a",), 1, TaskBudget(units=1))),
        )


def test_facts_round_trip_as_strict_world_event_payloads() -> None:
    event = _event(1, "graph.created", spec=_spec())
    assert TaskGraphEvent.from_payload(event.to_payload()) == event
    with pytest.raises(TypeError):
        event.payload["spec"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        event.payload["spec"]["graph_id"] = "mutated"  # type: ignore[index]
    assert isinstance(event.to_payload()["payload"]["spec"]["nodes"], list)  # type: ignore[index]
    with pytest.raises(TaskGraphContractError, match="shape"):
        TaskGraphEvent.from_payload({**event.to_payload(), "unknown": True})
    with pytest.raises(TaskGraphContractError, match="receipt"):
        CancellationAcknowledgement(PROJECT, GRAPH, "source", "cancel-source", "not-a-reference")
    with pytest.raises(TaskGraphContractError, match="project scope"):
        CancellationAcknowledgement(
            PROJECT, GRAPH, "source", "cancel-source",
            "crp://cancellations/other-project/source",
        )


def test_node_provenance_is_project_scoped_and_subject_version_unique() -> None:
    foreign = TaskGraphNode(
        "foreign", (), 1, TaskBudget(1), "all",
        TraceSubject("project-other", "task", "subject-foreign"),
        VersionBinding("crp://tasks/project-other/foreign", "revision-1", None),
    )
    with pytest.raises(TaskGraphContractError, match="crossed project"):
        TaskGraphSpec(GRAPH, PROJECT, "foreign", TaskBudget(2), 1, (foreign,))
    smuggled = TaskGraphNode(
        "smuggled", (), 1, TaskBudget(1), "all",
        TraceSubject(PROJECT, "task", "subject-smuggled"),
        VersionBinding("crp://tasks/project-other/smuggled", "revision-1", None),
    )
    with pytest.raises(TaskGraphContractError, match="authority crossed project"):
        TaskGraphSpec(GRAPH, PROJECT, "smuggled", TaskBudget(2), 1, (smuggled,))
    source = _node("source", (), 1, TaskBudget(1))
    duplicate = TaskGraphNode(
        "duplicate", (), 1, TaskBudget(1), "all", source.subject, source.subject_version,
    )
    with pytest.raises(TaskGraphContractError, match="duplicated"):
        TaskGraphSpec(GRAPH, PROJECT, "duplicate", TaskBudget(2), 1, (source, duplicate))


def test_result_waits_for_external_validation_and_invalidates_descendants() -> None:
    base = (
        _event(1, "graph.created", spec=_spec()),
        *_dispatch(2, "source", "crp://agent/source"),
        _event(4, "node.result_observed", node_id="source", attempt=1, result_ref="crp://receipt/source", satisfied=True),
    )
    assert project_task_graph(base).node("source").status == "awaiting_validation"
    assert project_task_graph(base).ready_node_ids == ()
    invalidated = project_task_graph(base + (
        _event(5, "node.validation", node_id="source", status="invalidated", evidence_ref="crp://validation/source"),
    ))
    assert invalidated.node("source").status == "invalidated"
    assert invalidated.node("left").status == "invalidated"
    assert invalidated.node("join").status == "invalidated"


def test_dispatch_cannot_bypass_dependencies_concurrency_or_budget() -> None:
    with pytest.raises(TaskGraphContractError, match="dispatch binding"):
        project_task_graph((
            _event(1, "graph.created", spec=_spec()),
            _event(2, "node.dispatched", node_id="left", attempt=1, execution_ref="crp://agent/left"),
        ))
    spec = TaskGraphSpec(
        graph_id=GRAPH, project_id=PROJECT, command_id="capacity", budget_limit=TaskBudget(1), max_concurrency=1,
        nodes=(_node("a", (), 1, TaskBudget(1)), _node("b", (), 1, TaskBudget(1))),
    )
    with pytest.raises(TaskGraphContractError, match="dispatch causality"):
        project_task_graph((
            _event(1, "graph.created", spec=spec),
            *_dispatch(2, "a", "crp://agent/a"),
            _event(4, "node.dispatch_requested", node_id="b", attempt=1),
        ))
    assert project_task_graph((_event(1, "graph.created", spec=_spec()),)).budget_reserved.units == 0


def test_dispatch_intent_reserves_capacity_until_matching_binding() -> None:
    state = project_task_graph((
        _event(1, "graph.created", spec=_spec()),
        _event(2, "node.dispatch_requested", operation_id="dispatch-source-1", node_id="source", attempt=1),
    ))
    assert state.node("source").status == "dispatching"
    assert state.node("source").dispatch_operation_id == "dispatch-source-1"
    assert state.budget_reserved.units == 2
    with pytest.raises(TaskGraphContractError, match="dispatch binding"):
        project_task_graph((
            _event(1, "graph.created", spec=_spec()),
            _event(2, "node.dispatch_requested", operation_id="dispatch-source-1", node_id="source", attempt=1),
            _event(3, "node.dispatched", operation_id="other-operation", node_id="source", attempt=1, execution_ref="crp://agent/source"),
        ))


def test_any_and_quorum_block_when_no_remaining_dependency_can_satisfy() -> None:
    spec = TaskGraphSpec(
        graph_id=GRAPH, project_id=PROJECT, command_id="impossible", budget_limit=TaskBudget(5), max_concurrency=3,
        nodes=(
            _node("a", (), 1, TaskBudget(1)), _node("b", (), 1, TaskBudget(1)), _node("c", (), 1, TaskBudget(1)),
            _node("any", ("a", "b"), 0, TaskBudget(1), "any"),
            _node("quorum", ("a", "b", "c"), 0, TaskBudget(1), "quorum"),
        ),
    )
    events = [_event(1, "graph.created", spec=spec)]
    for node, sequence in (("a", 2), ("b", 6), ("c", 10)):
        events.extend((
            *_dispatch(sequence, node, f"crp://agent/{node}"),
            _event(sequence + 2, "node.result_observed", node_id=node, attempt=1, result_ref=f"crp://receipt/{node}", satisfied=False),
            _event(sequence + 3, "node.validation", node_id=node, status="rejected", evidence_ref=f"crp://validation/{node}"),
        ))
    state = project_task_graph(tuple(events))
    assert state.node("any").status == "blocked"
    assert state.node("quorum").status == "blocked"
