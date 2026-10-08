from __future__ import annotations

from dataclasses import replace

from core.context_graph import (
    ContextCompiler,
    ContextGraphEdge,
    ContextGraphNode,
    ContextGraphSnapshot,
    ContextProvenance,
    ContextPermissionGrant,
    FrozenContextRevisions,
    StalenessEvaluationInput,
    evaluate_staleness,
    stale_replay_order,
)


def _node(node_id: str) -> ContextGraphNode:
    return ContextGraphNode(
        node_id=node_id,
        node_type="conclusion" if node_id == "out" else "note",
        title=node_id,
        content_ref=f"content:{node_id}",
        content_revision="r1",
        source_refs=(f"source:{node_id}",),
        trust="verified",
        created_at="2026-08-30T00:00:00Z",
        updated_at="2026-08-30T00:00:00Z",
        metadata={"project_id": "project", "content": node_id},
    )


def _snapshot(*, nodes: tuple[ContextGraphNode, ...] | None = None, edges: tuple[ContextGraphEdge, ...] | None = None, revision: str = "g1", source_revision: str = "s1") -> ContextGraphSnapshot:
    graph_nodes = nodes or tuple(_node(node_id) for node_id in ("root", "mid", "out", "side"))
    graph_edges = edges or (
        ContextGraphEdge("root-mid", "root", "mid", "full_chain", 1, 0),
        ContextGraphEdge("mid-out", "mid", "out", "full_chain", 1, 1),
    )
    return ContextGraphSnapshot(
        schema_version="1.0.0",
        graph_id="graph",
        graph_revision=revision,
        project_id="project",
        source_type="fixture",
        source_revision=source_revision,
        created_at="2026-08-30T00:00:00Z",
        nodes=graph_nodes,
        edges=graph_edges,
        selected_outputs=("out",),
        token_estimate=1,
        provenance=ContextProvenance("fixture", source_revision, "2026-08-30T00:00:00Z", "fixture", "1", "fixture.json"),
    )


def _stale_ids(snapshot: ContextGraphSnapshot) -> set[str]:
    return {node.node_id for node in snapshot.nodes if node.stale}


def _revision_map() -> dict[str, str]:
    return {
        "capability_revision": "cap-r1", "compiler_revision": "2.0.0",
        "boundary_revision": "boundary-r1", "provider_revision": "provider-r1",
        "model_route_revision": "route-r1",
    }


def test_deleted_node_invalidates_surviving_downstream_path_only() -> None:
    previous = _snapshot()
    current = _snapshot(
        nodes=tuple(node for node in previous.nodes if node.node_id != "root"),
        edges=(previous.edges[1],),
        revision="g2",
    )

    stale = evaluate_staleness(previous, current, previous_revisions=_revision_map(), current_revisions=_revision_map())

    assert _stale_ids(stale) == {"mid", "out"}
    assert stale_replay_order(stale) == ("mid", "out")


def test_deleted_edge_invalidates_target_and_current_downstream_only() -> None:
    previous = _snapshot()
    current = _snapshot(edges=(previous.edges[1],), revision="g2")

    stale = evaluate_staleness(previous, current, previous_revisions=_revision_map(), current_revisions=_revision_map())

    assert _stale_ids(stale) == {"mid", "out"}
    assert stale_replay_order(stale) == ("mid", "out")


def test_edge_mode_and_active_changes_invalidate_only_model_visible_path() -> None:
    previous = _snapshot()
    mode_changed = _snapshot(edges=(replace(previous.edges[0], context_mode="quote_only"), previous.edges[1]), revision="g2")
    deactivated = _snapshot(edges=(replace(previous.edges[0], active=False), previous.edges[1]), revision="g3")

    assert _stale_ids(evaluate_staleness(previous, mode_changed, previous_revisions=_revision_map(), current_revisions=_revision_map())) == {"mid", "out"}
    assert _stale_ids(evaluate_staleness(previous, deactivated, previous_revisions=_revision_map(), current_revisions=_revision_map())) == {"mid", "out"}


def test_edge_depth_only_change_does_not_invalidate_model_context() -> None:
    previous = _snapshot()
    current = _snapshot(
        edges=(replace(previous.edges[0], depth=2), previous.edges[1]),
        revision="g2",
    )

    assert not _stale_ids(evaluate_staleness(previous, current, previous_revisions=_revision_map(), current_revisions=_revision_map()))


def test_snapshot_source_revision_change_propagates_with_complete_revision_mapping() -> None:
    previous = _snapshot()
    current = _snapshot(revision="g2", source_revision="s2")

    stale = evaluate_staleness(previous, current, previous_revisions=_revision_map(), current_revisions=_revision_map())

    assert _stale_ids(stale) == {"root", "mid", "out", "side"}
    assert stale_replay_order(stale) == ("root", "mid", "out", "side")
    assert {node.stale_reason for node in stale.nodes if node.stale} == {"revision_drift:source_revision"}


def test_missing_current_frozen_revision_fails_closed() -> None:
    previous = _snapshot()
    current = _snapshot(revision="g2")

    import pytest
    with pytest.raises(ValueError, match="incomplete_staleness_revision_maps"):
        evaluate_staleness(previous, current, previous_revisions={"compiler_revision": "compiler-r1"}, current_revisions={})


def test_display_and_position_only_changes_do_not_invalidate_context() -> None:
    previous = _snapshot()
    moved_and_renamed = _snapshot(
        nodes=tuple(
            replace(node, title="different label", metadata={**node.metadata, "position": {"x": 12, "y": 3}})
            if node.node_id == "root"
            else node
            for node in previous.nodes
        ),
        revision="g2",
    )

    assert not _stale_ids(evaluate_staleness(previous, moved_and_renamed, previous_revisions=_revision_map(), current_revisions=_revision_map()))

    revisions = FrozenContextRevisions("cap-r1", "boundary-r1", "provider-r1", "route-r1", "2.0.0")
    grant = ContextPermissionGrant(
        "project", "permission-r1", frozenset(node.content_ref for node in moved_and_renamed.nodes)
    )
    original = ContextCompiler().compile(
        previous, revisions=revisions, expected_revisions=revisions,
        permission_grant=grant, token_budget=500,
        staleness_input=StalenessEvaluationInput.baseline(previous, revisions),
    )
    renamed = ContextCompiler().compile(
        moved_and_renamed, revisions=revisions, expected_revisions=revisions,
        permission_grant=grant, token_budget=500,
        staleness_input=StalenessEvaluationInput(previous, revisions, revisions),
    )
    assert renamed.messages == original.messages


def test_node_type_and_trust_changes_invalidate_the_model_projection() -> None:
    previous = _snapshot()
    current = _snapshot(
        nodes=tuple(
            replace(node, node_type="question", trust="untrusted")
            if node.node_id == "root"
            else node
            for node in previous.nodes
        ),
        revision="g2",
    )

    assert _stale_ids(evaluate_staleness(previous, current, previous_revisions=_revision_map(), current_revisions=_revision_map())) == {"root", "mid", "out"}
