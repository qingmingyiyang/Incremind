from __future__ import annotations

from dataclasses import replace

import pytest

from core.context_graph import (
    ContextCompilationError, ContextCompiler, ContextGraphEdge, ContextGraphNode,
    ContextGraphSnapshot, ContextPermissionGrant, ContextProvenance, FrozenContextRevisions,
    StalenessEvaluationInput, evaluate_staleness, stale_replay_order,
)


def _node(node_id: str, content: str, *, node_type: str = "note", archived: bool = False, revision: str = "r1") -> ContextGraphNode:
    return ContextGraphNode(
        node_id=node_id, node_type=node_type, title=node_id, content_ref=f"content:{node_id}",
        content_revision=revision, source_refs=(f"source:{node_id}",), trust="untrusted",
        created_at="2026-08-29T00:00:00Z", updated_at="2026-08-29T00:00:00Z",
        metadata={"project_id": "p1", "content": content, "highlights": (f"highlight-{node_id}",), "archived": archived},
    )


def _snapshot(*, nodes=None, edges=None, selected=("out",), revision="g1") -> ContextGraphSnapshot:
    items = nodes or (_node("root", "ROOT"), _node("mid", "MIDDLE"), _node("out", "OUTPUT", node_type="conclusion"))
    links = edges or (
        ContextGraphEdge("e1", "root", "mid", "full_chain", 1, 0),
        ContextGraphEdge("e2", "mid", "out", "full_chain", 1, 1),
    )
    return ContextGraphSnapshot(
        "1.0.0", "graph", revision, "p1", "fixture", "s1", "2026-08-29T00:00:00Z",
        tuple(items), tuple(links), tuple(selected), 10,
        ContextProvenance("fixture", "s1", "2026-08-29T00:00:00Z", "fixture", "1", "fixture.json"), (),
    )


def _revisions(**changes: str) -> FrozenContextRevisions:
    values = {"capability_revision": "c1", "compiler_revision": "2.0.0", "boundary_revision": "b1", "provider_revision": "p1", "model_route_revision": "m1"}
    values.update(changes)
    return FrozenContextRevisions(**values)


def _grant(graph: ContextGraphSnapshot) -> ContextPermissionGrant:
    return ContextPermissionGrant("p1", "permission-1", frozenset(node.content_ref for node in graph.nodes))


def _baseline(graph: ContextGraphSnapshot) -> StalenessEvaluationInput:
    return StalenessEvaluationInput.baseline(graph, _revisions())


def _revision_map() -> dict[str, str]:
    revisions = _revisions()
    return {
        "capability_revision": revisions.capability_revision,
        "compiler_revision": revisions.compiler_revision,
        "boundary_revision": revisions.boundary_revision,
        "provider_revision": revisions.provider_revision,
        "model_route_revision": revisions.model_route_revision,
    }


def test_full_chain_is_deterministic_and_topological() -> None:
    compiler = ContextCompiler()
    graph = _snapshot()
    first = compiler.compile(graph, revisions=_revisions(), expected_revisions=_revisions(), permission_grant=_grant(graph), token_budget=500, staleness_input=_baseline(graph))
    second = compiler.compile(graph, revisions=_revisions(), expected_revisions=_revisions(), permission_grant=_grant(graph), token_budget=500, staleness_input=_baseline(graph))
    assert first == second
    assert first.deterministic_order == ("root", "mid", "out")


@pytest.mark.parametrize(
    "mode, expected, absent",
    [
        ("quote_only", "MIDDLE", "ROOT"),
        ("highlights_only", "highlight-mid", "ROOT"),
        ("reference", "References: source:mid", "ROOT"),
    ],
)
def test_local_context_modes_do_not_pull_the_full_ancestor_chain(mode: str, expected: str, absent: str) -> None:
    graph = _snapshot(edges=(
        ContextGraphEdge("e1", "root", "mid", "full_chain", 1, 0),
        ContextGraphEdge("e2", "mid", "out", mode, 1, 1),
    ))
    binding = ContextCompiler().compile(graph, revisions=_revisions(), expected_revisions=_revisions(), permission_grant=_grant(graph), token_budget=500, staleness_input=_baseline(graph))
    text = "\n".join(str(message["content"]) for message in binding.messages)
    assert expected in text
    assert absent not in text


def test_excluded_and_archived_nodes_never_enter_default_context() -> None:
    graph = _snapshot(
        nodes=(_node("root", "ROOT", archived=True), _node("mid", "MIDDLE"), _node("out", "OUTPUT")),
        edges=(ContextGraphEdge("e1", "root", "mid", "full_chain", 1, 0), ContextGraphEdge("e2", "mid", "out", "excluded", 1, 1)),
    )
    binding = ContextCompiler().compile(graph, revisions=_revisions(), expected_revisions=_revisions(), permission_grant=_grant(graph), token_budget=500, staleness_input=_baseline(graph))
    assert binding.deterministic_order == ("out",)
    assert set(binding.excluded_nodes) == {"root", "mid"}


def test_hard_budget_is_explainable_and_never_exceeded() -> None:
    graph = _snapshot(nodes=(_node("root", "R" * 400), _node("mid", "M" * 400), _node("out", "O" * 400)))
    binding = ContextCompiler().compile(graph, revisions=_revisions(), expected_revisions=_revisions(), permission_grant=_grant(graph), token_budget=350, staleness_input=_baseline(graph))
    assert binding.total_token_cost <= 350
    assert binding.trimmed_nodes
    assert binding.budget_explanation["original_token_estimate"] > binding.budget_explanation["final_token_estimate"]
    assert binding.budget_explanation["adjustments"]


@pytest.mark.parametrize("field", ["capability_revision", "boundary_revision", "provider_revision", "model_route_revision"])
def test_frozen_revision_drift_fails_closed(field: str) -> None:
    with pytest.raises(ContextCompilationError, match=field):
        graph = _snapshot()
        ContextCompiler().compile(graph, revisions=_revisions(), expected_revisions=_revisions(**{field: "drift"}), permission_grant=_grant(graph), token_budget=100, staleness_input=_baseline(graph))


def test_permission_denial_and_project_scope_fail_closed() -> None:
    graph = _snapshot()
    denied = ContextPermissionGrant("p1", "permission-1", frozenset({"content:out"}))
    with pytest.raises(ValueError, match="content_permission_denied"):
        ContextCompiler().compile(graph, revisions=_revisions(), expected_revisions=_revisions(), permission_grant=denied, token_budget=100, staleness_input=_baseline(graph))
    wrong_project = ContextPermissionGrant("other", "permission-1", frozenset(node.content_ref for node in graph.nodes))
    with pytest.raises(ValueError, match="project_scope_violation"):
        ContextCompiler().compile(graph, revisions=_revisions(), expected_revisions=_revisions(), permission_grant=wrong_project, token_budget=100, staleness_input=_baseline(graph))
    with pytest.raises(ValueError, match="invalid_permission_grant"):
        ContextPermissionGrant("p1", "permission-1", {"content:root"})  # type: ignore[arg-type]


def test_content_change_propagates_only_downstream_and_position_does_not() -> None:
    side = _node("side", "SIDE")
    previous = _snapshot(nodes=(*_snapshot().nodes, side))
    changed_nodes = tuple(replace(node, content_revision="r2", metadata={**node.metadata, "content": "CHANGED"}) if node.node_id == "root" else node for node in previous.nodes)
    current = replace(previous, graph_revision="g2", nodes=changed_nodes)
    stale = evaluate_staleness(previous, current, previous_revisions=_revision_map(), current_revisions=_revision_map())
    assert {node.node_id for node in stale.nodes if node.stale} == {"root", "mid", "out"}
    moved = replace(previous, graph_revision="g3", nodes=tuple(replace(node, metadata={**node.metadata, "position": {"x": 99}}) if node.node_id == "root" else node for node in previous.nodes))
    visually_changed = evaluate_staleness(previous, moved, previous_revisions=_revision_map(), current_revisions=_revision_map())
    assert not any(node.stale for node in visually_changed.nodes)


def test_edge_mode_change_stales_target_and_downstream_in_replay_order() -> None:
    previous = _snapshot()
    current = replace(previous, graph_revision="g2", edges=(replace(previous.edges[0], context_mode="quote_only"), previous.edges[1]))
    stale = evaluate_staleness(previous, current, previous_revisions=_revision_map(), current_revisions=_revision_map())
    assert {node.node_id for node in stale.nodes if node.stale} == {"mid", "out"}
    assert stale_replay_order(stale) == ("mid", "out")


def test_display_name_change_does_not_stale_but_source_revision_does() -> None:
    previous = _snapshot()
    renamed = replace(previous, graph_revision="g2", nodes=tuple(replace(node, title="Display only") if node.node_id == "root" else node for node in previous.nodes))
    assert not any(node.stale for node in evaluate_staleness(previous, renamed, previous_revisions=_revision_map(), current_revisions=_revision_map()).nodes)
    changed_source = replace(
        previous, graph_revision="g3", source_revision="s2",
        provenance=replace(previous.provenance, source_revision="s2"),
    )
    drifted = evaluate_staleness(
        previous, changed_source,
        previous_revisions=_revision_map(), current_revisions=_revision_map(),
    )
    assert all(node.stale for node in drifted.nodes)


def test_excluded_state_change_stales_only_affected_path() -> None:
    side = _node("side", "SIDE")
    previous = _snapshot(nodes=(*_snapshot().nodes, side), edges=(
        ContextGraphEdge("e1", "root", "mid", "excluded", 1, 0),
        ContextGraphEdge("e2", "mid", "out", "full_chain", 1, 1),
    ))
    current = replace(previous, graph_revision="g2", edges=(replace(previous.edges[0], context_mode="full_chain"), previous.edges[1]))
    stale = evaluate_staleness(previous, current, previous_revisions=_revision_map(), current_revisions=_revision_map())
    assert {node.node_id for node in stale.nodes if node.stale} == {"mid", "out"}


def test_archived_selected_output_fails_instead_of_silently_empty_binding() -> None:
    graph = _snapshot(nodes=(_node("root", "ROOT"), _node("mid", "MIDDLE"), _node("out", "OUTPUT", archived=True)))
    with pytest.raises(ContextCompilationError, match="selected_output_archived"):
        ContextCompiler().compile(graph, revisions=_revisions(), expected_revisions=_revisions(), permission_grant=_grant(graph), token_budget=100, staleness_input=_baseline(graph))


def test_multi_path_full_chain_wins_and_expands_ancestors() -> None:
    graph = _snapshot(
        nodes=(_node("ancestor", "ANCESTOR"), _node("shared", "SHARED"), _node("left", "LEFT"), _node("right", "RIGHT"), _node("out", "OUT")),
        edges=(
            ContextGraphEdge("e0", "ancestor", "shared", "full_chain", 1, 0),
            ContextGraphEdge("e1", "shared", "left", "reference", 1, 1),
            ContextGraphEdge("e2", "shared", "right", "full_chain", 1, 2),
            ContextGraphEdge("e3", "left", "out", "full_chain", 1, 3),
            ContextGraphEdge("e4", "right", "out", "full_chain", 1, 4),
        ),
    )
    binding = ContextCompiler().compile(graph, revisions=_revisions(), expected_revisions=_revisions(), permission_grant=_grant(graph), token_budget=500, staleness_input=_baseline(graph))
    assert "ancestor" in binding.deterministic_order
    shared = next(item for items in binding.layers.values() for item in items if item["node_id"] == "shared")
    assert shared["context_mode"] == "full_chain"
