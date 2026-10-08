from __future__ import annotations

from collections.abc import Mapping

from ...context_graph.models import ContextGraphSnapshot
from ...context_graph.validation import ContextGraphValidationError


def _position(value: object, *, fallback: Mapping[str, int]) -> dict[str, int | float]:
    candidate = value if isinstance(value, Mapping) else fallback
    if set(candidate) != {"x", "y"} or any(
        not isinstance(candidate.get(axis), (int, float)) or isinstance(candidate.get(axis), bool)
        for axis in ("x", "y")
    ):
        raise ContextGraphValidationError(("invalid_export_position",))
    return {"x": candidate["x"], "y": candidate["y"]}


def thoughtdag_canvas_nodes(snapshot: ContextGraphSnapshot) -> list[dict[str, object]]:
    nodes: list[dict[str, object]] = []
    for index, node in enumerate(snapshot.nodes):
        content = node.metadata.get("content", "")
        highlights = node.metadata.get("highlights", ())
        if not isinstance(content, str):
            raise ContextGraphValidationError(("invalid_export_content",))
        if not isinstance(highlights, (list, tuple)) or not all(
            isinstance(item, str) for item in highlights
        ):
            raise ContextGraphValidationError(("invalid_export_highlights",))
        position = _position(
            node.metadata.get("position"),
            fallback={"x": 80 * (index % 8), "y": 120 * (index // 8)},
        )
        nodes.append({
            "id": node.node_id,
            "type": "thought",
            "position": position,
            "data": {
                "question": node.title,
                "response": content,
                "responses": [content],
                "responseIndex": 0,
                "createdAt": node.created_at,
                "lastGeneratedAt": node.updated_at,
                "archived": bool(node.metadata.get("archived")),
                "tokenCount": (len(content) + 3) // 4,
                "highlights": [
                    {"id": f"h-{highlight_index}", "text": text}
                    for highlight_index, text in enumerate(highlights)
                ],
                "highlightMode": "off",
                "attachments": [],
                "excludedAttachmentIds": [],
                "includedAttachmentIds": [],
                "isCollapsed": False,
                "isEditing": False,
                "isEditingResponse": False,
                "isLoading": False,
                "roleMode": "inherit",
                "isRoot": False,
                "isBranch": False,
            },
        })
    return nodes


def thoughtdag_canvas_edges(snapshot: ContextGraphSnapshot) -> list[dict[str, object]]:
    return [
        {
            "id": edge.edge_id,
            "source": edge.source_node_id,
            "target": edge.target_node_id,
            "data": {
                "isCrossLink": edge.context_mode in {
                    "quote_only", "reference", "highlights_only",
                },
                **({"contextDepth": "full"} if edge.context_mode == "full_chain" else {}),
            },
        }
        for edge in snapshot.edges
    ]
