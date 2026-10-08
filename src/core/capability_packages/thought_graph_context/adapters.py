from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping

from ...context_graph.models import ContextBinding, ContextGraphSnapshot
from ...context_graph.validation import validate_snapshot


@dataclass(frozen=True, slots=True)
class ContextPreview:
    graph_id: str
    graph_revision: str
    node_count: int
    edge_count: int
    selected_output_count: int
    message_count: int
    material_count: int
    reference_count: int
    token_cost: int
    trimmed_node_ids: tuple[str, ...]
    excluded_node_ids: tuple[str, ...]
    stale_node_ids: tuple[str, ...]
    source_refs: tuple[str, ...]
    formal_write: bool = False


class ContextPreviewAdapter:
    def preview(self, snapshot: ContextGraphSnapshot, binding: ContextBinding) -> ContextPreview:
        validate_snapshot(snapshot)
        if snapshot.graph_id != binding.graph_id or snapshot.graph_revision != binding.graph_revision:
            raise ValueError("preview_binding_drift")
        return ContextPreview(
            snapshot.graph_id, snapshot.graph_revision, len(snapshot.nodes), len(snapshot.edges),
            len(snapshot.selected_outputs), len(binding.messages), len(binding.layers.get("materials", ())),
            len(binding.layers.get("references", ())), binding.total_token_cost,
            tuple(str(item["node_id"]) for item in binding.trimmed_nodes), binding.excluded_nodes,
            binding.stale_nodes, binding.source_refs,
        )


class GraphCanvasAdapter:
    """Pure editable canvas projection; contains no model or tool operation."""

    def to_canvas(self, snapshot: ContextGraphSnapshot) -> Mapping[str, object]:
        validate_snapshot(snapshot)
        return {
            "product_name": "LineMap", "graph_id": snapshot.graph_id,
            "graph_revision": snapshot.graph_revision, "project_id": snapshot.project_id,
            "nodes": [
                {"id": node.node_id, "type": node.node_type, "title": node.title,
                 "position": dict(node.metadata.get("position", {})), "stale": node.stale,
                 "archived": bool(node.metadata.get("archived")), "trust": node.trust,
                 "source_refs": node.source_refs}
                for node in snapshot.nodes
            ],
            "edges": [asdict(edge) for edge in snapshot.edges],
            "selected_outputs": snapshot.selected_outputs,
            "read_only_execution": True,
        }
