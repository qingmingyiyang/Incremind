from __future__ import annotations

from dataclasses import replace

import pytest

from core.context_graph import (
    ContextCompilationError,
    ContextCompiler,
    ContextGraphEdge,
    ContextGraphNode,
    ContextGraphSnapshot,
    ContextPermissionGrant,
    ContextProvenance,
    FrozenContextRevisions,
    StalenessConfirmation,
    StalenessEvaluationInput,
)


def _node(node_id: str, content: str) -> ContextGraphNode:
    return ContextGraphNode(
        node_id=node_id,
        node_type="conclusion" if node_id.startswith("out") else "note",
        title=node_id,
        content_ref=f"content:{node_id}",
        content_revision="content-r1",
        source_refs=(f"source:{node_id}",),
        trust="verified",
        created_at="2026-08-30T00:00:00Z",
        updated_at="2026-08-30T00:00:00Z",
        metadata={"project_id": "project", "content": content},
    )


def _snapshot(*, revision: str = "graph-r1", nodes: tuple[ContextGraphNode, ...] | None = None) -> ContextGraphSnapshot:
    return ContextGraphSnapshot(
        "1.0.0", "graph", revision, "project", "fixture", "source-r1", "2026-08-30T00:00:00Z",
        nodes or (_node("root", "root evidence"), _node("mid", "mid analysis"), _node("out", "conclusion")),
        (
            ContextGraphEdge("root-mid", "root", "mid", "full_chain", 1, 0),
            ContextGraphEdge("mid-out", "mid", "out", "full_chain", 1, 1),
        ),
        ("out",),
        1,
        ContextProvenance("fixture", "source-r1", "2026-08-30T00:00:00Z", "fixture", "1", "fixture.json"),
    )


def _revisions(revision: str = "r1") -> FrozenContextRevisions:
    return FrozenContextRevisions(
        f"cap-{revision}", f"boundary-{revision}", f"provider-{revision}", f"route-{revision}", "2.0.0"
    )


def _grant(snapshot: ContextGraphSnapshot) -> ContextPermissionGrant:
    return ContextPermissionGrant("project", "permission-r1", frozenset(node.content_ref for node in snapshot.nodes))


def _compile(
    snapshot: ContextGraphSnapshot,
    *,
    staleness_input: StalenessEvaluationInput | None = None,
    budget: int = 500,
):
    revisions = _revisions()
    return ContextCompiler().compile(
        snapshot,
        revisions=revisions,
        expected_revisions=revisions,
        permission_grant=_grant(snapshot),
        token_budget=budget,
        staleness_input=staleness_input or StalenessEvaluationInput.baseline(snapshot, revisions),
    )


def test_changed_input_requires_exact_preview_confirmation_before_binding() -> None:
    previous = _snapshot()
    current = replace(
        previous,
        graph_revision="graph-r2",
        nodes=tuple(
            replace(node, content_revision="content-r2", metadata={**node.metadata, "content": "changed evidence"})
            if node.node_id == "root"
            else node
            for node in previous.nodes
        ),
    )
    comparison = StalenessEvaluationInput(previous, _revisions(), _revisions())

    with pytest.raises(ContextCompilationError, match="staleness_confirmation_required"):
        _compile(current, staleness_input=comparison)

    confirmed = StalenessEvaluationInput(
        previous,
        _revisions(),
        _revisions(),
        StalenessConfirmation(
            "graph", "graph-r2", ("root", "mid", "out"), ("root", "mid", "out"),
            (("root", "upstream_context_changed"), ("mid", "upstream_context_changed"), ("out", "upstream_context_changed")),
            "user-1", "2026-08-30T01:00:00Z",
        ),
    )
    binding = _compile(current, staleness_input=confirmed)

    assert binding.stale_nodes == ("mid", "out", "root")
    assert binding.budget_explanation["staleness"] == {
        "previous_graph_revision": "graph-r1",
        "current_graph_revision": "graph-r2",
        "affected_node_ids": ("mid", "out", "root"),
        "replay_order": ("root", "mid", "out"),
        "stale_reasons": {
            "root": "upstream_context_changed",
            "mid": "upstream_context_changed",
            "out": "upstream_context_changed",
        },
        "confirmation_required": True,
        "confirmation_present": True,
        "confirmed_by": "user-1",
        "confirmed_at": "2026-08-30T01:00:00Z",
    }


def test_visual_only_change_evaluates_staleness_but_does_not_require_confirmation() -> None:
    previous = _snapshot()
    current = replace(
        previous,
        graph_revision="graph-r2",
        nodes=tuple(
            replace(node, title="renamed", metadata={**node.metadata, "position": {"x": 5, "y": 7}})
            if node.node_id == "root"
            else node
            for node in previous.nodes
        ),
    )
    binding = _compile(current, staleness_input=StalenessEvaluationInput(previous, _revisions(), _revisions()))

    assert not binding.stale_nodes
    assert binding.budget_explanation["staleness"]["confirmation_required"] is False
    assert binding.budget_explanation["staleness"]["replay_order"] == ()


def test_fresh_graph_rejects_unnecessary_or_unscoped_confirmation() -> None:
    graph = _snapshot()
    confirmation = StalenessConfirmation(
        "graph", "graph-r1", (), (), (), "user-1", "2026-08-30T01:00:00Z",
    )

    with pytest.raises(ContextCompilationError, match="staleness_confirmation_not_required"):
        _compile(
            graph,
            staleness_input=StalenessEvaluationInput.baseline(
                graph, _revisions(), confirmation=confirmation,
            ),
        )


def test_existing_stale_marker_cannot_be_cleared_by_legacy_compile_call() -> None:
    graph = _snapshot(nodes=tuple(
        replace(node, stale=True, stale_reason="upstream_context_changed") if node.node_id == "out" else node
        for node in _snapshot().nodes
    ))

    with pytest.raises(ContextCompilationError, match="staleness_confirmation_required"):
        _compile(graph, staleness_input=StalenessEvaluationInput.baseline(graph, _revisions()))


def test_existing_stale_upstream_marker_propagates_to_all_dependants() -> None:
    graph = _snapshot(nodes=tuple(
        replace(node, stale=True, stale_reason="source_revision_changed")
        if node.node_id == "root"
        else node
        for node in _snapshot().nodes
    ))
    confirmation = StalenessConfirmation(
        "graph",
        "graph-r1",
        ("mid", "out", "root"),
        ("root", "mid", "out"),
        (
            ("mid", "upstream_context_changed"),
            ("out", "upstream_context_changed"),
            ("root", "source_revision_changed"),
        ),
        "user-1",
        "2026-08-30T01:00:00Z",
    )

    binding = _compile(
        graph,
        staleness_input=StalenessEvaluationInput.baseline(
            graph,
            _revisions(),
            confirmation=confirmation,
        ),
    )

    assert binding.stale_nodes == ("mid", "out", "root")
    assert binding.budget_explanation["staleness"]["replay_order"] == ("root", "mid", "out")


def test_explicit_comparison_requires_complete_and_matching_frozen_revisions() -> None:
    previous = _snapshot()
    current = replace(previous, graph_revision="graph-r2")
    with pytest.raises(ContextCompilationError, match="incomplete_staleness_frozen_revisions"):
        StalenessEvaluationInput(previous, None, _revisions())  # type: ignore[arg-type]
    with pytest.raises(ContextCompilationError, match="staleness_current_revisions_mismatch"):
        _compile(current, staleness_input=StalenessEvaluationInput(previous, _revisions(), _revisions("other")))


def test_changed_graph_content_cannot_reuse_an_immutable_graph_revision() -> None:
    previous = _snapshot()
    current = replace(
        previous,
        nodes=tuple(
            replace(node, content_revision="content-r2")
            if node.node_id == "root"
            else node
            for node in previous.nodes
        ),
    )

    with pytest.raises(ContextCompilationError, match="immutable_graph_revision_drift"):
        _compile(
            current,
            staleness_input=StalenessEvaluationInput(
                previous, _revisions(), _revisions(),
            ),
        )


def test_compiler_revision_drift_fails_closed() -> None:
    graph = _snapshot()
    drifted = FrozenContextRevisions("cap-r1", "boundary-r1", "provider-r1", "route-r1", "compiler-r9")
    with pytest.raises(ContextCompilationError, match="compiler_revision_drift"):
        ContextCompiler().compile(
            graph, revisions=drifted, expected_revisions=drifted,
            permission_grant=_grant(graph), token_budget=100,
            staleness_input=StalenessEvaluationInput.baseline(graph, drifted),
        )


@pytest.mark.parametrize(
    "confirmation, expected",
    (
        (
            StalenessConfirmation("graph", "graph-r2", ("root", "mid", "out"), ("mid", "root", "out"),
                (("root", "upstream_context_changed"), ("mid", "upstream_context_changed"), ("out", "upstream_context_changed")), "user", "2026-08-30T01:00:00Z"),
            "staleness_confirmation_replay_order_mismatch",
        ),
        (
            StalenessConfirmation("graph", "graph-r2", ("root", "mid", "out"), ("root", "mid", "out"),
                (("root", "wrong_reason"), ("mid", "upstream_context_changed"), ("out", "upstream_context_changed")), "user", "2026-08-30T01:00:00Z"),
            "staleness_confirmation_reason_mismatch",
        ),
    ),
)
def test_confirmation_must_match_preview_replay_and_reasons(confirmation: StalenessConfirmation, expected: str) -> None:
    previous = _snapshot()
    current = replace(previous, graph_revision="graph-r2", nodes=tuple(
        replace(node, content_revision="content-r2") if node.node_id == "root" else node
        for node in previous.nodes
    ))
    with pytest.raises(ContextCompilationError, match=expected):
        _compile(current, staleness_input=StalenessEvaluationInput(previous, _revisions(), _revisions(), confirmation))


def test_selected_output_budget_below_the_model_projection_envelope_fails_closed() -> None:
    graph = _snapshot(nodes=(
        _node("out-a", "A" * 600),
        _node("out-b", "B" * 600),
    ))
    graph = replace(graph, selected_outputs=("out-a", "out-b"), edges=())
    with pytest.raises(ContextCompilationError, match="hard_token_budget_unenforceable"):
        _compile(graph, budget=1)
