from __future__ import annotations

from dataclasses import replace

import pytest

from core.context_graph import (
    ContextCompilationError,
    ContextBudgetEvaluator,
    ContextCompiler,
    ContextGraphEdge,
    ContextGraphNode,
    ContextGraphSnapshot,
    ContextGraphValidationError,
    ContextPermissionGrant,
    ContextProvenance,
    FrozenContextRevisions,
    StalenessEvaluationInput,
    context_binding_model_projection,
    context_binding_from_payload,
    context_binding_to_payload,
    validate_snapshot,
)


def _node(node_id: str, content: str = "content") -> ContextGraphNode:
    return ContextGraphNode(
        node_id=node_id,
        node_type="conclusion",
        title=node_id,
        content_ref=f"content:{node_id}",
        content_revision="content-r1",
        source_refs=(f"source:{node_id}",),
        trust="verified",
        created_at="2026-08-30T00:00:00Z",
        updated_at="2026-08-30T00:00:00Z",
        metadata={"project_id": "project", "content": content},
    )


def _snapshot(*, nodes: tuple[ContextGraphNode, ...] | None = None, edges: tuple[ContextGraphEdge, ...] = (), selected: tuple[str, ...] = ("out-a", "out-b")) -> ContextGraphSnapshot:
    return ContextGraphSnapshot(
        "1.0.0", "graph", "graph-r1", "project", "fixture", "source-r1", "2026-08-30T00:00:00Z",
        nodes or (_node("out-a", "A" * 600), _node("out-b", "B" * 600)), edges, selected, 0,
        ContextProvenance("fixture", "source-r1", "2026-08-30T00:00:00Z", "fixture", "1", "fixture.json"),
    )


def _revisions() -> FrozenContextRevisions:
    return FrozenContextRevisions("cap-r1", "boundary-r1", "provider-r1", "route-r1", "2.0.0")


def _compile(snapshot: ContextGraphSnapshot, *, budget: int, compiler: ContextCompiler | None = None):
    return (compiler or ContextCompiler()).compile(
        snapshot,
        revisions=_revisions(),
        expected_revisions=_revisions(),
        permission_grant=ContextPermissionGrant("project", "permission-r1", frozenset(node.content_ref for node in snapshot.nodes)),
        token_budget=budget,
        staleness_input=StalenessEvaluationInput.baseline(snapshot, _revisions()),
    )


def test_multiple_selected_outputs_remain_within_hard_budget() -> None:
    binding = _compile(_snapshot(), budget=500)
    assert binding.total_token_cost <= 500
    assert binding.budget_explanation["final_token_estimate"] <= 500
    assert binding.total_token_cost == ContextBudgetEvaluator.estimate_projection(
        context_binding_model_projection(binding),
    )
    assert context_binding_from_payload(context_binding_to_payload(binding)) == binding


def test_selected_output_fails_closed_when_the_model_projection_envelope_exceeds_budget() -> None:
    with pytest.raises(ContextCompilationError, match="hard_token_budget_unenforceable"):
        _compile(_snapshot(), budget=1)


def test_large_graph_low_budget_uses_the_exact_model_projection_cost() -> None:
    nodes = tuple(_node(f"node-{index:02d}", "X" * 240) for index in range(24))
    graph = _snapshot(
        nodes=nodes,
        edges=tuple(
            ContextGraphEdge(
                f"edge-{index:02d}", f"node-{index:02d}", f"node-{index + 1:02d}",
                "full_chain", 1, index,
            )
            for index in range(23)
        ),
        selected=("node-23",),
    )

    binding = _compile(graph, budget=350)

    projection = context_binding_model_projection(binding)
    assert binding.total_token_cost == ContextBudgetEvaluator.estimate_projection(projection)
    assert binding.total_token_cost <= binding.budget_explanation["hard_budget"]
    assert binding.budget_explanation["estimator_revision"] == "canonical-model-entry-v2"
    assert binding.trimmed_nodes
    assert "trimmed_nodes" not in projection
    assert "budget_explanation" not in projection


def test_compiler_does_not_expose_replaceable_budget_policy() -> None:
    with pytest.raises(TypeError):
        ContextCompiler(budget_evaluator=object())  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "mutate, issue",
    [
        (lambda graph: replace(graph, nodes=(replace(graph.nodes[0], node_id=""), graph.nodes[1])), "missing_node_id"),
        (lambda graph: replace(graph, nodes=(replace(graph.nodes[0], node_type="unknown"), graph.nodes[1])), "invalid_node_type"),
        (lambda graph: replace(graph, nodes=(replace(graph.nodes[0], trust="unsafe"), graph.nodes[1])), "invalid_node_trust"),
        (lambda graph: replace(graph, nodes=(replace(graph.nodes[0], source_refs=("source:out-a", "")), graph.nodes[1])), "invalid_source_ref"),
        (lambda graph: replace(graph, graph_revision=""), "missing_graph_revision"),
    ],
)
def test_runtime_node_identity_enum_and_revision_contracts_fail_closed(mutate, issue: str) -> None:
    with pytest.raises(ContextGraphValidationError, match=issue):
        validate_snapshot(mutate(_snapshot()))


def test_runtime_edge_mode_and_identifier_contracts_fail_closed() -> None:
    graph = _snapshot(edges=(ContextGraphEdge("", "out-a", "out-b", "unknown", 1, 0),))
    with pytest.raises(ContextGraphValidationError) as raised:
        validate_snapshot(graph)
    reported = ";".join(raised.value.issues)
    assert "missing_edge_id" in reported
    assert "invalid_context_mode:" in reported


def test_excluded_edge_is_still_a_dag_constraint_but_not_a_context_walk() -> None:
    cyclic = _snapshot(edges=(
        ContextGraphEdge("e1", "out-a", "out-b", "full_chain", 1, 0),
        ContextGraphEdge("e2", "out-b", "out-a", "excluded", 1, 1),
    ))
    with pytest.raises(ContextGraphValidationError, match="cycle_detected"):
        validate_snapshot(cyclic)

    acyclic = _snapshot(
        nodes=(_node("upstream", "UPSTREAM"), _node("out-a", "A"), _node("out-b", "B")),
        edges=(ContextGraphEdge("e1", "upstream", "out-a", "excluded", 1, 0),),
    )
    binding = _compile(acyclic, budget=300)
    assert "upstream" not in binding.deterministic_order
    assert "e1" in binding.budget_explanation["excluded_edge_ids"]
