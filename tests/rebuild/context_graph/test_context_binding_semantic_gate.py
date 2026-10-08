from __future__ import annotations

from copy import deepcopy

import pytest

from core.context_graph import (
    ContextBindingPayloadError,
    context_binding_from_payload,
    context_binding_to_payload,
)
from core.context_graph.compiler import ContextCompiler, StalenessEvaluationInput
from core.context_graph.model_projection import (
    context_binding_model_projection_from_parts,
    estimate_model_projection_tokens,
)

from .test_stage_b_compiler import _grant, _node, _revisions, _snapshot


def _payload() -> dict[str, object]:
    return context_binding_to_payload(
        ContextCompiler().compile(
            _snapshot(), revisions=_revisions(), expected_revisions=_revisions(),
            permission_grant=_grant(_snapshot()), token_budget=600,
            staleness_input=StalenessEvaluationInput.baseline(_snapshot(), _revisions()),
        )
    )


@pytest.mark.parametrize(
    ("mutate", "reason"),
    (
        (lambda value: value.__setitem__("total_token_cost", 0), "budget explanation drifted"),
        (lambda value: value["budget_explanation"].__setitem__("hard_budget", 1), "hard budget exceeded"),
        (lambda value: value["layers"].__setitem__("references", ({**value["layers"]["references"][0], "content": "forged"},)), "layer token cost drifted"),
        (lambda value: value["messages"][0]["metadata"].__setitem__("node_id", "forged"), "messages do not match layers"),
        (lambda value: value.__setitem__("excluded_nodes", [value["deterministic_order"][0]]), "excluded nodes entered deterministic order"),
        (lambda value: value.__setitem__("stale_nodes", ["not-in-binding"]), "outside the graph projection"),
        (lambda value: value["budget_explanation"]["staleness"].__setitem__("current_graph_revision", "forged"), "staleness graph revision drifted"),
        (lambda value: value["budget_explanation"]["staleness"].__setitem__("confirmation_present", True), "stale confirmation evidence drifted"),
        (lambda value: value.__setitem__("source_refs", []), "source refs drifted"),
        (lambda value: value["budget_explanation"].__setitem__("hard_budget", 0), "hard budget must be positive"),
    ),
)
def test_persisted_binding_rejects_projection_and_budget_bypasses(mutate, reason: str) -> None:
    payload = deepcopy(_payload())
    mutate(payload)
    with pytest.raises(ContextBindingPayloadError, match=reason):
        context_binding_from_payload(payload)


def test_compiler_binding_is_the_only_self_consistent_projection_shape() -> None:
    payload = _payload()
    assert payload["total_token_cost"] <= payload["budget_explanation"]["hard_budget"]
    assert tuple(message["metadata"]["node_id"] for message in payload["messages"]) == payload["deterministic_order"]


def test_unknown_future_compiler_revision_fails_closed() -> None:
    payload = deepcopy(_payload())
    payload["compiler_revision"] = "3.0.0"

    with pytest.raises(ContextBindingPayloadError, match="compiler revision is unsupported"):
        context_binding_from_payload(payload)


def test_compiler_trimmed_binding_remains_persistable() -> None:
    graph = _snapshot(nodes=(
        _node("root", "R" * 400),
        _node("mid", "M" * 400),
        _node("out", "O" * 400),
    ))
    binding = ContextCompiler().compile(
        graph, revisions=_revisions(), expected_revisions=_revisions(),
        permission_grant=_grant(graph), token_budget=300,
        staleness_input=StalenessEvaluationInput.baseline(graph, _revisions()),
    )
    payload = context_binding_to_payload(binding)
    assert payload["trimmed_nodes"]
    assert payload["total_token_cost"] <= payload["budget_explanation"]["hard_budget"]


def test_content_whitespace_cannot_hide_token_cost() -> None:
    payload = deepcopy(_payload())
    message = payload["messages"][0]
    node_id = message["metadata"]["node_id"]
    layer_item = next(
        item
        for layer in payload["layers"].values()
        for item in layer
        if item["node_id"] == node_id
    )
    layer_item["content"] += " " * 400
    message["content"] += " " * 400
    with pytest.raises(ContextBindingPayloadError, match="layer token cost drifted"):
        context_binding_from_payload(payload)


def test_empty_content_requires_matching_selected_output_elision() -> None:
    payload = deepcopy(_payload())
    message = payload["messages"][0]
    node_id = message["metadata"]["node_id"]
    layer_item = next(
        item
        for layer in payload["layers"].values()
        for item in layer
        if item["node_id"] == node_id
    )
    original_content = layer_item["content"]
    layer_item["content"] = ""
    message["content"] = ""
    payload["layer_token_costs"][next(
        name for name, layer in payload["layers"].items() if layer_item in layer
    )] -= (len(original_content) + 3) // 4
    payload["total_token_cost"] = estimate_model_projection_tokens(
        context_binding_model_projection_from_parts(
            schema_version=payload["schema_version"], graph_id=payload["graph_id"],
            graph_revision=payload["graph_revision"], capability_revision=payload["capability_revision"],
            compiler_revision=payload["compiler_revision"], boundary_revision=payload["boundary_revision"],
            provider_revision=payload["provider_revision"], model_route_revision=payload["model_route_revision"],
            messages=payload["messages"], layers=payload["layers"],
        )
    )
    payload["budget_explanation"]["final_token_estimate"] = payload["total_token_cost"]

    with pytest.raises(ContextBindingPayloadError, match="selected output elision drifted"):
        context_binding_from_payload(payload)
