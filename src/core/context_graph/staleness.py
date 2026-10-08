from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, replace
from typing import Mapping

from .models import ContextGraphEdge, ContextGraphNode, ContextGraphSnapshot
from .validation import validate_snapshot


@dataclass(frozen=True, slots=True)
class StalenessImpactPreview:
    """Read-only impact evidence produced before a stale graph is compiled.

    This is deliberately a data contract, not a replay command.  It makes the
    affected set and deterministic replay order visible to a caller without
    giving LineMap any authority to regenerate model nodes or trigger effects.
    """

    graph_id: str
    graph_revision: str
    affected_node_ids: tuple[str, ...]
    replay_order: tuple[str, ...]
    stale_reasons: tuple[tuple[str, str], ...]

    @property
    def confirmation_required(self) -> bool:
        return bool(self.affected_node_ids)


@dataclass(frozen=True, slots=True)
class StalenessConfirmation:
    """User acknowledgement of one exact stale-impact preview.

    A confirmation intentionally contains no execution instruction.  Normal
    Turn submission and the platform Effect path remain responsible for every
    later regeneration or formal write.
    """

    graph_id: str
    graph_revision: str
    affected_node_ids: tuple[str, ...]
    replay_order: tuple[str, ...]
    stale_reasons: tuple[tuple[str, str], ...]
    actor_id: str
    confirmed_at: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.graph_id, self.graph_revision, self.actor_id, self.confirmed_at)
        ):
            raise ValueError("incomplete_staleness_confirmation")
        if (
            type(self.affected_node_ids) is not tuple
            or len(self.affected_node_ids) != len(set(self.affected_node_ids))
            or any(not isinstance(node_id, str) or not node_id.strip() for node_id in self.affected_node_ids)
        ):
            raise ValueError("invalid_staleness_confirmation_nodes")
        if (
            type(self.replay_order) is not tuple
            or any(not isinstance(node_id, str) or not node_id.strip() for node_id in self.replay_order)
            or len(self.replay_order) != len(set(self.replay_order))
            or set(self.replay_order) != set(self.affected_node_ids)
        ):
            raise ValueError("invalid_staleness_confirmation_replay_order")
        if (
            type(self.stale_reasons) is not tuple
            or any(
                type(item) is not tuple
                or len(item) != 2
                or any(not isinstance(value, str) or not value.strip() for value in item)
                for item in self.stale_reasons
            )
            or len({item[0] for item in self.stale_reasons}) != len(self.stale_reasons)
            or {item[0] for item in self.stale_reasons} != set(self.affected_node_ids)
        ):
            raise ValueError("invalid_staleness_confirmation_reasons")


def _semantic_node(node: ContextGraphNode) -> tuple[object, ...]:
    return (
        node.node_type,
        node.trust,
        node.content_revision,
        node.content_ref,
        tuple(node.source_refs),
        node.metadata.get("content"),
        tuple(node.metadata.get("highlights", ())),
        bool(node.metadata.get("archived")),
    )


def _semantic_edge(edge: ContextGraphEdge) -> tuple[object, ...]:
    # ``depth`` is a structural validation hint; the compiler does not project
    # it into model context.  Only fields that can alter the compiled input
    # participate in stale propagation.
    return (edge.source_node_id, edge.target_node_id, edge.context_mode, edge.ordering, edge.active)


def _edge_contributes_context(edge: ContextGraphEdge) -> bool:
    """Return whether an edge can change model-visible graph context."""
    return edge.active and edge.context_mode != "excluded"


def _changed_edge_targets(
    old_edges: Mapping[str, ContextGraphEdge],
    new_edges: Mapping[str, ContextGraphEdge],
) -> set[str]:
    """Find surviving targets whose effective inbound context changed.

    Comparing both graph revisions is essential: a deleted edge is absent from
    ``current`` but can still invalidate the context used to produce its old
    target.  Inactive and excluded edges are intentionally ignored because
    they are not model-visible context.
    """
    targets: set[str] = set()
    for edge_id in sorted(set(old_edges) | set(new_edges)):
        old = old_edges.get(edge_id)
        new = new_edges.get(edge_id)
        if old is not None and new is not None and _semantic_edge(old) == _semantic_edge(new):
            continue
        if old is not None and _edge_contributes_context(old):
            targets.add(old.target_node_id)
        if new is not None and _edge_contributes_context(new):
            targets.add(new.target_node_id)
    return targets


def evaluate_staleness(
    previous: ContextGraphSnapshot,
    current: ContextGraphSnapshot,
    *,
    previous_revisions: Mapping[str, str] | None = None,
    current_revisions: Mapping[str, str] | None = None,
) -> ContextGraphSnapshot:
    validate_snapshot(previous)
    validate_snapshot(current)
    if previous.graph_id != current.graph_id or previous.project_id != current.project_id:
        raise ValueError("staleness_scope_mismatch")
    if previous.graph_revision == current.graph_revision and previous != current:
        raise ValueError("immutable_graph_revision_drift")
    old_nodes = {item.node_id: item for item in previous.nodes}
    new_nodes = {item.node_id: item for item in current.nodes}
    changed = {
        node_id
        for node_id, node in new_nodes.items()
        if node_id not in old_nodes or _semantic_node(node) != _semantic_node(old_nodes[node_id])
    }
    old_edges = {item.edge_id: item for item in previous.edges}
    new_edges = {item.edge_id: item for item in current.edges}
    changed.update(node_id for node_id in _changed_edge_targets(old_edges, new_edges) if node_id in new_nodes)

    # A deleted node has no node record in ``current`` to seed invalidation.
    # Its old effective outgoing edges still identify surviving results that
    # were compiled using it, so treat those targets like deleted edges.
    deleted_node_ids = set(old_nodes) - set(new_nodes)
    for edge in old_edges.values():
        if edge.source_node_id in deleted_node_ids and _edge_contributes_context(edge) and edge.target_node_id in new_nodes:
            changed.add(edge.target_node_id)

    previous_revision_map = _complete_revision_map(previous_revisions)
    current_revision_map = _complete_revision_map(current_revisions)
    revision_drift = {
        key
        for key in set(previous_revision_map) | set(current_revision_map)
        if previous_revision_map.get(key) != current_revision_map.get(key)
    }
    if previous.source_revision != current.source_revision:
        revision_drift.add("source_revision")
    revision_drift = tuple(sorted(revision_drift))
    if revision_drift:
        changed.update(new_nodes)
    downstream: dict[str, list[str]] = defaultdict(list)
    for edge in current.edges:
        if edge.active and edge.context_mode != "excluded":
            downstream[edge.source_node_id].append(edge.target_node_id)
    # A snapshot can carry a previously evaluated stale marker.  Treating a
    # no-change comparison as permission to clear it would let an old caller
    # bypass the user-confirmation gate merely by compiling again.  Refreshing
    # content is an explicit graph change and is the only way to clear it.
    stale = set(changed)
    preserved_stale = {node.node_id for node in current.nodes if node.stale}
    stale.update(preserved_stale)
    # Existing stale roots are just as authoritative as newly detected
    # changes.  Propagate both sets so an imported/evaluated stale upstream
    # node cannot leave a dependent result incorrectly fresh.
    queue = deque(sorted(stale))
    while queue:
        source = queue.popleft()
        for target in sorted(downstream[source]):
            if target not in stale:
                stale.add(target)
                queue.append(target)
    reason = "revision_drift:" + ",".join(revision_drift) if revision_drift else "upstream_context_changed"
    stale_reasons = {
        node.node_id: node.stale_reason or "upstream_context_changed"
        for node in current.nodes
        if node.node_id in preserved_stale
    }
    stale_reasons.update({node_id: reason for node_id in changed})
    for node_id in stale:
        stale_reasons.setdefault(node_id, reason)
    return replace(
        current,
        nodes=tuple(
            replace(
                node,
                stale=node.node_id in stale,
                stale_reason=stale_reasons[node.node_id] if node.node_id in stale else None,
            )
            for node in current.nodes
        ),
    )


def staleness_impact_preview(snapshot: ContextGraphSnapshot) -> StalenessImpactPreview:
    """Return deterministic stale impact evidence for an evaluated snapshot."""
    validate_snapshot(snapshot)
    affected = tuple(sorted(node.node_id for node in snapshot.nodes if node.stale))
    reasons = tuple(sorted(
        (node.node_id, node.stale_reason or "upstream_context_changed")
        for node in snapshot.nodes
        if node.stale
    ))
    return StalenessImpactPreview(
        graph_id=snapshot.graph_id,
        graph_revision=snapshot.graph_revision,
        affected_node_ids=affected,
        replay_order=stale_replay_order(snapshot),
        stale_reasons=reasons,
    )


def validate_staleness_confirmation(
    preview: StalenessImpactPreview,
    confirmation: StalenessConfirmation | None,
) -> None:
    """Require acknowledgement of the exact preview before compiling stale input."""
    if not preview.confirmation_required:
        if confirmation is not None:
            raise ValueError("staleness_confirmation_not_required")
        return
    if confirmation is None:
        raise ValueError("staleness_confirmation_required")
    if confirmation.graph_id != preview.graph_id or confirmation.graph_revision != preview.graph_revision:
        raise ValueError("staleness_confirmation_scope_mismatch")
    if tuple(sorted(confirmation.affected_node_ids)) != preview.affected_node_ids:
        raise ValueError("staleness_confirmation_impact_mismatch")
    if confirmation.replay_order != preview.replay_order:
        raise ValueError("staleness_confirmation_replay_order_mismatch")
    if tuple(sorted(confirmation.stale_reasons)) != preview.stale_reasons:
        raise ValueError("staleness_confirmation_reason_mismatch")


def _complete_revision_map(value: Mapping[str, str] | None) -> Mapping[str, str]:
    fields = {
        "capability_revision",
        "compiler_revision",
        "boundary_revision",
        "provider_revision",
        "model_route_revision",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != fields
        or any(not isinstance(item, str) or not item.strip() for item in value.values())
    ):
        raise ValueError("incomplete_staleness_revision_maps")
    return value


def stale_replay_order(snapshot: ContextGraphSnapshot) -> tuple[str, ...]:
    validate_snapshot(snapshot)
    stale = {node.node_id for node in snapshot.nodes if node.stale}
    indegree = {node_id: 0 for node_id in stale}
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in snapshot.edges:
        if edge.active and edge.context_mode != "excluded" and edge.source_node_id in stale and edge.target_node_id in stale:
            adjacency[edge.source_node_id].append(edge.target_node_id)
            indegree[edge.target_node_id] += 1
    ready = sorted(node_id for node_id, degree in indegree.items() if degree == 0)
    ordered: list[str] = []
    while ready:
        node_id = ready.pop(0)
        ordered.append(node_id)
        for target in sorted(adjacency[node_id]):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort()
    return tuple(ordered)
