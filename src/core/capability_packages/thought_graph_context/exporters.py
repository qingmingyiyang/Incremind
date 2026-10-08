from __future__ import annotations

import json
import base64
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from ...context_graph.models import ContextGraphSnapshot
from ...context_graph.validation import ContextGraphValidationError, validate_snapshot
from .external_safety import reject_external_json_value, reject_external_secret_material
from .thoughtdag_wire import thoughtdag_canvas_edges, thoughtdag_canvas_nodes


def _jsonable(value: object, label: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise ContextGraphValidationError((f"non_serializable_{label}",)) from exc


def _encode_metadata(value: object) -> object:
    """JSON has no tuple type; reserve an explicit representation or fail closed."""
    if isinstance(value, tuple):
        return {"__linemap_type__": "tuple", "items": [_encode_metadata(item) for item in value]}
    if isinstance(value, Mapping):
        if "__linemap_type__" in value:
            raise ContextGraphValidationError(("reserved_metadata_encoding_key",))
        if any(not isinstance(key, str) for key in value):
            raise ContextGraphValidationError(("non_string_metadata_key",))
        return {key: _encode_metadata(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_encode_metadata(item) for item in value]
    return value


def _lossless_envelope(snapshot: ContextGraphSnapshot) -> dict[str, object]:
    payload = asdict(snapshot)
    for node in payload["nodes"]:
        node["metadata"] = _encode_metadata(node["metadata"])
    for edge in payload["edges"]:
        edge["metadata"] = _encode_metadata(edge["metadata"])
    return {"format_version": 1, "snapshot": _jsonable(payload, "snapshot")}


class ThoughtDAGExporter:
    exporter_id = "thought_graph_context.ThoughtDAGExporter"
    exporter_revision = "1.1.0"

    def export_snapshot(self, snapshot: ContextGraphSnapshot) -> str:
        validate_snapshot(snapshot)
        nodes = thoughtdag_canvas_nodes(snapshot)
        edges = thoughtdag_canvas_edges(snapshot)
        payload = {"version": 1, "name": snapshot.graph_id, "exportedAt": snapshot.created_at,
                   "nodes": nodes, "edges": edges, "events": [], "linemap": _lossless_envelope(snapshot)}
        serialized = json.dumps(_jsonable(payload, "thoughtdag_export"), ensure_ascii=False, sort_keys=True)
        reject_external_secret_material(serialized)
        return serialized


class MarkdownGraphExporter:
    exporter_id = "thought_graph_context.MarkdownGraphExporter"
    exporter_revision = "1.1.0"

    def export_snapshot(self, snapshot: ContextGraphSnapshot) -> str:
        validate_snapshot(snapshot)
        envelope_snapshot = _lossless_envelope(snapshot)["snapshot"]
        envelope_text = json.dumps(
            envelope_snapshot,
            ensure_ascii=False,
            sort_keys=True,
        )
        reject_external_json_value(envelope_snapshot)
        reject_external_secret_material(envelope_text)
        encoded = base64.urlsafe_b64encode(
            envelope_text.encode("utf-8")
        ).decode("ascii").rstrip("=")
        lines = [f"<!-- linemap-context-graph-v1:{encoded} -->", f"# {snapshot.graph_id}", "", f"> graph_revision: {snapshot.graph_revision}", ""]
        by_id = {node.node_id: node for node in snapshot.nodes}
        for node_id in sorted(by_id):
            node = by_id[node_id]
            lines.extend([
                f"## {node.title}", "",
                f"> node_id: {node.node_id} · type: {node.node_type} · content_revision: {node.content_revision}",
                f"> source_refs: {', '.join(node.source_refs)}", "",
                str(node.metadata.get("content", "")), "",
            ])
        serialized = "\n".join(lines).rstrip() + "\n"
        reject_external_secret_material(serialized)
        return serialized
