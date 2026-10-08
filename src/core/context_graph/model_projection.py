"""The single, model-visible projection of a :class:`ContextBinding`.

Bindings deliberately retain audit receipts (trimming, stale state and budget
explanations).  Those receipts are useful to people and to recovery, but are
not model context.  Keeping this projection here makes the compiler's budget
calculation and the planner's egress payload use the same contract.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from .models import ContextBinding


class ContextModelProjectionError(ValueError):
    pass


def canonical_model_projection_json(value: Mapping[str, object]) -> str:
    """Return the stable JSON form used by the platform token estimator."""

    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as error:
        raise ContextModelProjectionError("model_projection_not_json_safe") from error


def estimate_model_projection_tokens(value: Mapping[str, object]) -> int:
    """Estimate the exact canonical model projection, including its envelope."""

    return (len(canonical_model_projection_json(value)) + 3) // 4


def context_binding_model_projection(binding: ContextBinding) -> dict[str, object]:
    """Build the only LineMap entry permitted to enter model context."""

    return context_binding_model_projection_from_parts(
        schema_version=binding.schema_version,
        graph_id=binding.graph_id,
        graph_revision=binding.graph_revision,
        capability_revision=binding.capability_revision,
        compiler_revision=binding.compiler_revision,
        boundary_revision=binding.boundary_revision,
        provider_revision=binding.provider_revision,
        model_route_revision=binding.model_route_revision,
        messages=binding.messages,
        layers=binding.layers,
    )


def context_binding_model_projection_from_parts(
    *,
    schema_version: str,
    graph_id: str,
    graph_revision: str,
    capability_revision: str,
    compiler_revision: str,
    boundary_revision: str,
    provider_revision: str,
    model_route_revision: str,
    messages: Sequence[Mapping[str, object]],
    layers: Mapping[str, Sequence[Mapping[str, object]]],
) -> dict[str, object]:
    """Build the projection before a binding exists, for compiler budgeting."""

    node_details: dict[str, Mapping[str, object]] = {}
    for layer_name in ("materials", "references", "conversation"):
        raw_layer = layers.get(layer_name)
        if not isinstance(raw_layer, Sequence):
            raise ContextModelProjectionError("model_projection_layers_invalid")
        for item in raw_layer:
            if not isinstance(item, Mapping):
                raise ContextModelProjectionError("model_projection_layer_item_invalid")
            node_id = item.get("node_id")
            if not isinstance(node_id, str) or not node_id or node_id in node_details:
                raise ContextModelProjectionError("model_projection_node_identity_invalid")
            node_details[node_id] = item

    projected_messages: list[dict[str, object]] = []
    for raw_message in messages:
        if not isinstance(raw_message, Mapping):
            raise ContextModelProjectionError("model_projection_message_invalid")
        metadata = raw_message.get("metadata")
        if not isinstance(metadata, Mapping):
            raise ContextModelProjectionError("model_projection_message_metadata_invalid")
        node_id = metadata.get("node_id")
        item = node_details.get(node_id) if isinstance(node_id, str) else None
        if item is None:
            raise ContextModelProjectionError("model_projection_message_node_missing")
        role = raw_message.get("role")
        content = raw_message.get("content")
        context_mode = item.get("context_mode")
        source_refs = item.get("source_refs")
        trust = item.get("trust")
        if (
            role not in {"user", "assistant"}
            or not isinstance(content, str)
            or not isinstance(context_mode, str)
            or not isinstance(trust, str)
            or not isinstance(source_refs, (tuple, list))
            or any(not isinstance(ref, str) for ref in source_refs)
        ):
            raise ContextModelProjectionError("model_projection_message_fields_invalid")
        projected_messages.append({
            "role": role,
            "content": content,
            "metadata": {
                "node_id": node_id,
                "context_mode": context_mode,
                "source_refs": list(source_refs),
                "trust": trust,
                "untrusted_context": True,
            },
        })

    if len(projected_messages) != len(node_details):
        raise ContextModelProjectionError("model_projection_messages_layers_mismatch")
    return {
        "kind": "context_binding",
        "content": {
            "schema_version": schema_version,
            "graph_id": graph_id,
            "graph_revision": graph_revision,
            "capability_revision": capability_revision,
            "compiler_revision": compiler_revision,
            "boundary_revision": boundary_revision,
            "provider_revision": provider_revision,
            "model_route_revision": model_route_revision,
            "messages": projected_messages,
        },
    }
