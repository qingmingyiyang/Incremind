from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
import json

from .models import ContextBinding
from .model_projection import (
    ContextModelProjectionError,
    context_binding_model_projection_from_parts,
    estimate_model_projection_tokens,
)


class ContextBindingPayloadError(ValueError):
    pass


_FIELDS = {
    "schema_version", "graph_id", "graph_revision", "capability_revision",
    "compiler_revision", "boundary_revision", "provider_revision",
    "model_route_revision", "messages", "layers", "layer_token_costs",
    "total_token_cost", "trimmed_nodes", "excluded_nodes", "stale_nodes",
    "source_refs", "deterministic_order", "budget_explanation",
}
_LAYERS = ("materials", "references", "conversation")
_CONTEXT_MODES = {"full_chain", "quote_only", "highlights_only", "reference"}
_TRUST_LEVELS = {"untrusted", "user_authored", "verified"}
_MAX_MESSAGES = 4096
_MAX_PAYLOAD_BYTES = 1_048_576


def context_binding_to_payload(binding: ContextBinding) -> dict[str, object]:
    payload = asdict(binding)
    validate_context_binding_payload(payload)
    return payload


def context_binding_from_payload(value: object) -> ContextBinding:
    data = validate_context_binding_payload(value)
    layers = _mapping(data["layers"], "layers")
    costs = _mapping(data["layer_token_costs"], "layer token costs")
    return ContextBinding(
        schema_version=_text(data["schema_version"], "schema version"),
        graph_id=_text(data["graph_id"], "graph id"),
        graph_revision=_text(data["graph_revision"], "graph revision"),
        capability_revision=_text(data["capability_revision"], "capability revision"),
        compiler_revision=_text(data["compiler_revision"], "compiler revision"),
        boundary_revision=_text(data["boundary_revision"], "Boundary revision"),
        provider_revision=_text(data["provider_revision"], "Provider revision"),
        model_route_revision=_text(data["model_route_revision"], "Model Route revision"),
        messages=tuple(dict(_mapping(item, "message")) for item in _sequence(data["messages"], "messages")),
        layers={name: tuple(_layer_item(item, name) for item in _sequence(layers[name], name)) for name in _LAYERS},
        layer_token_costs={name: _integer(costs[name], f"{name} token cost") for name in _LAYERS},
        total_token_cost=_integer(data["total_token_cost"], "total token cost"),
        trimmed_nodes=tuple(dict(_mapping(item, "trimmed node")) for item in _sequence(data["trimmed_nodes"], "trimmed nodes")),
        excluded_nodes=_texts(data["excluded_nodes"], "excluded nodes"),
        stale_nodes=_texts(data["stale_nodes"], "stale nodes"),
        source_refs=_texts(data["source_refs"], "source refs"),
        deterministic_order=_texts(data["deterministic_order"], "deterministic order"),
        budget_explanation=dict(_mapping(data["budget_explanation"], "budget explanation")),
    )


def validate_context_binding_payload(value: object) -> Mapping[str, object]:
    data = _mapping(value, "ContextBinding")
    if set(data) != _FIELDS or data.get("schema_version") != "1.0.0":
        raise ContextBindingPayloadError("ContextBinding shape is invalid")
    for key in (
        "graph_id", "graph_revision", "capability_revision", "compiler_revision",
        "boundary_revision", "provider_revision", "model_route_revision",
    ):
        _text(data[key], key)
    messages = _sequence(data["messages"], "messages")
    if len(messages) > _MAX_MESSAGES:
        raise ContextBindingPayloadError("ContextBinding message limit exceeded")
    for item in messages:
        message = _mapping(item, "message")
        if set(message) != {"role", "content", "metadata"}:
            raise ContextBindingPayloadError("ContextBinding message shape is invalid")
        if message.get("role") not in {"user", "assistant"}:
            raise ContextBindingPayloadError("ContextBinding message role is invalid")
        _content(message.get("content"), "message content")
        metadata = _mapping(message.get("metadata"), "message metadata")
        if metadata.get("untrusted_context") is not True:
            raise ContextBindingPayloadError("ContextBinding external text must stay untrusted")
    layers = _mapping(data["layers"], "layers")
    costs = _mapping(data["layer_token_costs"], "layer token costs")
    if set(layers) != set(_LAYERS) or set(costs) != set(_LAYERS):
        raise ContextBindingPayloadError("ContextBinding layers are invalid")
    for name in _LAYERS:
        _sequence(layers[name], name)
        _integer(costs[name], f"{name} token cost")
    _integer(data["total_token_cost"], "total token cost")
    for key in ("trimmed_nodes", "excluded_nodes", "stale_nodes", "source_refs", "deterministic_order"):
        _sequence(data[key], key)
    _mapping(data["budget_explanation"], "budget explanation")
    _validate_semantics(data, layers=layers, costs=costs, messages=messages)
    if len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        raise ContextBindingPayloadError("ContextBinding payload limit exceeded")
    return data


def _validate_semantics(
    data: Mapping[str, object], *, layers: Mapping[str, object],
    costs: Mapping[str, object], messages: Sequence[object],
) -> None:
    """Reject bindings that are structurally valid but cannot be compiler output.

    A persisted binding is deliberately a projection, not a signed compiler
    attestation.  It therefore cannot prove *which* graph was compiled.  It
    can, however, prove every invariant that is representable in the binding
    itself.  Keep this gate format-neutral so every capability package uses
    the same persistence contract.
    """
    ordered = _texts(data["deterministic_order"], "deterministic order")
    excluded = _texts(data["excluded_nodes"], "excluded nodes")
    stale = _texts(data["stale_nodes"], "stale nodes")
    if set(ordered) & set(excluded):
        raise ContextBindingPayloadError("ContextBinding excluded nodes entered deterministic order")
    if len(stale) != len(set(stale)) or tuple(sorted(stale)) != stale:
        raise ContextBindingPayloadError("ContextBinding stale nodes must be unique and sorted")
    if not set(stale).issubset(set(ordered) | set(excluded)):
        raise ContextBindingPayloadError("ContextBinding stale nodes are outside the graph projection")

    layered: dict[str, dict[str, tuple[str, tuple[str, ...]]]] = {}
    layered_node_ids: list[str] = []
    all_refs: set[str] = set()
    for layer in _LAYERS:
        entries: dict[str, tuple[str, tuple[str, ...]]] = {}
        expected_cost = 0
        for raw_item in _sequence(layers[layer], layer):
            item = _mapping(raw_item, f"{layer} item")
            if set(item) != {"node_id", "content", "context_mode", "source_refs", "trust"}:
                raise ContextBindingPayloadError("ContextBinding layer item shape is invalid")
            node_id = _text(item["node_id"], "layer node id")
            content = _content(item["content"], "layer content")
            if item["context_mode"] not in _CONTEXT_MODES:
                raise ContextBindingPayloadError("ContextBinding context mode is invalid")
            if item["trust"] not in _TRUST_LEVELS:
                raise ContextBindingPayloadError("ContextBinding trust is invalid")
            refs = _texts(item["source_refs"], "layer source refs")
            if node_id in entries:
                raise ContextBindingPayloadError("ContextBinding layer node ids must be unique")
            entries[node_id] = (content, refs)
            layered_node_ids.append(node_id)
            all_refs.update(refs)
            expected_cost += _estimate_tokens(content)
        if _integer(costs[layer], f"{layer} token cost") != expected_cost:
            raise ContextBindingPayloadError("ContextBinding layer token cost drifted")
        layered[layer] = entries

    if set(layered_node_ids) != set(ordered):
        raise ContextBindingPayloadError("ContextBinding layers do not match deterministic order")
    if len(layered_node_ids) != len(set(layered_node_ids)):
        raise ContextBindingPayloadError("ContextBinding node appears in multiple layers")
    if tuple(_texts(data["source_refs"], "source refs")) != tuple(sorted(all_refs)):
        raise ContextBindingPayloadError("ContextBinding source refs drifted")

    layer_items = {
        node_id: item
        for entries in layered.values()
        for node_id, item in entries.items()
    }
    message_ids: list[str] = []
    for raw_message in messages:
        message = _mapping(raw_message, "message")
        metadata = _mapping(message["metadata"], "message metadata")
        if set(metadata) != {"node_id", "untrusted_context"}:
            raise ContextBindingPayloadError("ContextBinding message metadata shape is invalid")
        node_id = _text(metadata["node_id"], "message node id")
        message_ids.append(node_id)
        item = layer_items.get(node_id)
        if item is None or _content(message["content"], "message content") != item[0]:
            raise ContextBindingPayloadError("ContextBinding messages do not match layers")
    if tuple(message_ids) != ordered:
        raise ContextBindingPayloadError("ContextBinding messages do not match deterministic order")

    compiler_revision = _text(data["compiler_revision"], "compiler revision")
    total = _integer(data["total_token_cost"], "total token cost")
    if compiler_revision == "1.0.0" and total != sum(int(costs[name]) for name in _LAYERS):
        raise ContextBindingPayloadError("ContextBinding token total drifted")
    explanation = _mapping(data["budget_explanation"], "budget explanation")
    hard_budget = _integer(explanation.get("hard_budget"), "hard budget")
    if hard_budget < 1:
        raise ContextBindingPayloadError("ContextBinding hard budget must be positive")
    if total > hard_budget:
        raise ContextBindingPayloadError("ContextBinding hard budget exceeded")
    original = _integer(explanation.get("original_token_estimate"), "original token estimate")
    final = _integer(explanation.get("final_token_estimate"), "final token estimate")
    if original < final or final != total:
        raise ContextBindingPayloadError("ContextBinding budget explanation drifted")
    if compiler_revision == "1.0.0":
        # Read old persisted bindings without reinterpreting their receipt.
        pass
    else:
        try:
            model_projection = context_binding_model_projection_from_parts(
                schema_version=_text(data["schema_version"], "schema version"),
                graph_id=_text(data["graph_id"], "graph id"),
                graph_revision=_text(data["graph_revision"], "graph revision"),
                capability_revision=_text(data["capability_revision"], "capability revision"),
                compiler_revision=compiler_revision,
                boundary_revision=_text(data["boundary_revision"], "Boundary revision"),
                provider_revision=_text(data["provider_revision"], "Provider revision"),
                model_route_revision=_text(data["model_route_revision"], "Model Route revision"),
                messages=tuple(_mapping(item, "message") for item in messages),
                layers={
                    name: tuple(_mapping(item, f"{name} item") for item in _sequence(layers[name], name))
                    for name in _LAYERS
                },
            )
            estimated_total = estimate_model_projection_tokens(model_projection)
        except ContextModelProjectionError as error:
            raise ContextBindingPayloadError("ContextBinding model projection is invalid") from error
        if total != estimated_total:
            raise ContextBindingPayloadError("ContextBinding model projection token total drifted")
    trimmed = _sequence(data["trimmed_nodes"], "trimmed nodes")
    trimmed_ids: list[str] = []
    elided_ids: set[str] = set()
    for raw_item in trimmed:
        item = _mapping(raw_item, "trimmed node")
        required = {"node_id", "reason", "token_estimate", "selected_output"}
        if set(item) != required:
            raise ContextBindingPayloadError("ContextBinding trimmed node shape is invalid")
        node_id = _text(item["node_id"], "trimmed node id")
        reason = _text(item["reason"], "trimmed node reason")
        _integer(item["token_estimate"], "trimmed node token estimate")
        if not isinstance(item["selected_output"], bool):
            raise ContextBindingPayloadError("ContextBinding trimmed node selection is invalid")
        if reason == "token_budget" and (node_id not in excluded or item["selected_output"]):
            raise ContextBindingPayloadError("ContextBinding trimmed exclusion drifted")
        if reason in {"selected_output_truncated", "selected_output_elided"} and (
            node_id not in ordered or not item["selected_output"]
        ):
            raise ContextBindingPayloadError("ContextBinding selected output trim drifted")
        if reason == "selected_output_elided":
            elided_ids.add(node_id)
        if reason not in {"token_budget", "selected_output_truncated", "selected_output_elided"}:
            raise ContextBindingPayloadError("ContextBinding trim reason is invalid")
        trimmed_ids.append(node_id)
    if len(trimmed_ids) != len(set(trimmed_ids)):
        raise ContextBindingPayloadError("ContextBinding trimmed node ids must be unique")
    empty_ids = {node_id for node_id, (content, _) in layer_items.items() if content == ""}
    if empty_ids != elided_ids:
        raise ContextBindingPayloadError("ContextBinding selected output elision drifted")
    declared_trimmed = explanation.get("trimmed_node_ids")
    if declared_trimmed is not None and tuple(_texts(declared_trimmed, "trimmed node ids")) != tuple(trimmed_ids):
        raise ContextBindingPayloadError("ContextBinding trimmed node ids drifted")
    compiler_revision = _text(data["compiler_revision"], "compiler revision")
    if compiler_revision == "2.0.0":
        _validate_staleness_explanation(data, explanation, stale)
    elif compiler_revision != "1.0.0":
        raise ContextBindingPayloadError("ContextBinding compiler revision is unsupported")


def _validate_staleness_explanation(
    data: Mapping[str, object],
    explanation: Mapping[str, object],
    stale: tuple[str, ...],
) -> None:
    staleness = _mapping(explanation.get("staleness"), "staleness explanation")
    required = {
        "previous_graph_revision",
        "current_graph_revision",
        "affected_node_ids",
        "replay_order",
        "stale_reasons",
        "confirmation_required",
        "confirmation_present",
        "confirmed_by",
        "confirmed_at",
    }
    if set(staleness) != required:
        raise ContextBindingPayloadError("ContextBinding staleness explanation shape is invalid")
    _text(staleness["previous_graph_revision"], "previous graph revision")
    if _text(staleness["current_graph_revision"], "current graph revision") != data["graph_revision"]:
        raise ContextBindingPayloadError("ContextBinding staleness graph revision drifted")
    affected = _texts(staleness["affected_node_ids"], "affected node ids")
    if affected != stale:
        raise ContextBindingPayloadError("ContextBinding stale impact drifted")
    replay_order = _texts(staleness["replay_order"], "stale replay order")
    if len(replay_order) != len(set(replay_order)) or set(replay_order) != set(stale):
        raise ContextBindingPayloadError("ContextBinding stale replay order drifted")
    reasons = _mapping(staleness["stale_reasons"], "stale reasons")
    if set(reasons) != set(stale):
        raise ContextBindingPayloadError("ContextBinding stale reasons drifted")
    for node_id, reason in reasons.items():
        _text(node_id, "stale reason node id")
        _text(reason, "stale reason")
    required_confirmation = staleness["confirmation_required"]
    present_confirmation = staleness["confirmation_present"]
    if type(required_confirmation) is not bool or required_confirmation != bool(stale):
        raise ContextBindingPayloadError("ContextBinding stale confirmation requirement drifted")
    if type(present_confirmation) is not bool or present_confirmation != required_confirmation:
        raise ContextBindingPayloadError("ContextBinding stale confirmation evidence drifted")
    confirmed_by = staleness["confirmed_by"]
    confirmed_at = staleness["confirmed_at"]
    if required_confirmation:
        _text(confirmed_by, "staleness confirmation actor")
        _text(confirmed_at, "staleness confirmation time")
    elif confirmed_by is not None or confirmed_at is not None:
        raise ContextBindingPayloadError("ContextBinding unexpected stale confirmation evidence")


def _estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ContextBindingPayloadError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, (list, tuple)):
        raise ContextBindingPayloadError(f"{label} must be an array")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextBindingPayloadError(f"{label} must be non-empty")
    return value.strip()


def _content(value: object, label: str) -> str:
    """Validate model context text while preserving intentional empty elision.

    The hard-budget evaluator can explicitly elide a selected output to an
    empty string when even one token would exceed the approved budget.  Empty
    content is therefore valid only as content; identifiers and references
    continue to use the non-empty ``_text`` contract.
    """
    if not isinstance(value, str):
        raise ContextBindingPayloadError(f"{label} must be text")
    return value


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ContextBindingPayloadError(f"{label} must be a non-negative integer")
    return value


def _texts(value: object, label: str) -> tuple[str, ...]:
    result = tuple(_text(item, label) for item in _sequence(value, label))
    if len(result) != len(set(result)):
        raise ContextBindingPayloadError(f"{label} must be unique")
    return result


def _layer_item(value: object, layer: str) -> dict[str, object]:
    item = dict(_mapping(value, f"{layer} item"))
    source_refs = item.get("source_refs")
    if isinstance(source_refs, (list, tuple)):
        item["source_refs"] = tuple(source_refs)
    return item
