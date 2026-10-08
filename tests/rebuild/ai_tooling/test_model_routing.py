from __future__ import annotations

import pytest

from core.ai_tooling import (
    ModelRouteCandidate,
    ModelRoutingAuthorityBinding,
    ModelRoutingError,
    ModelRoutingProfile,
    ModelRoutingRouter,
    ModelSelectionRequest,
)


def test_text_uses_project_preferred_tier_with_public_revision_evidence() -> None:
    route = _route("deep-route", ("text", "structured"), revision=7)
    decision = _router().resolve(_profile(), (_route("fast-route", ("text",)), route), ModelSelectionRequest("text", "deep"))

    assert decision.available is True
    assert decision.tier == "deep"
    assert decision.route is route
    assert decision.reason == "project_preferred_tier"
    assert decision.profile_revision == 4
    assert decision.route.revision_evidence == "adapter-manifest:7"


def test_text_falls_back_only_to_configured_text_default() -> None:
    decision = _router().resolve(
        _profile(),
        (_route("fast-route", ("text",)),),
        ModelSelectionRequest("text", "deep"),
    )

    assert decision.available is True
    assert decision.tier == "fast"
    assert decision.route.route_key == "fast-route"
    assert decision.reason == "text_default_fallback"


@pytest.mark.parametrize("capability", ("vision", "image_generation"))
def test_hard_modalities_never_fall_back_to_text(capability: str) -> None:
    decision = _router().resolve(
        _profile(),
        (_route("fast-route", ("text", "vision", "image_generation")),),
        ModelSelectionRequest(capability),  # type: ignore[arg-type]
    )

    assert decision.available is False
    assert decision.tier == capability
    assert decision.route is None
    assert decision.reason == "required_modality_unavailable"


def test_image_generation_route_is_always_unconfigured_and_hard_unavailable() -> None:
    with pytest.raises(ModelRoutingError, match="image generation route"):
        _profile(image_generation="image-route")

    decision = _router().resolve(
        _profile(),
        (_route("fast-route", ("text", "image_generation")),),
        ModelSelectionRequest("image_generation"),
    )

    assert decision.available is False
    assert decision.tier == "image_generation"
    assert decision.route is None
    assert decision.reason == "required_modality_unavailable"


def test_capability_is_declared_not_inferred_from_route_or_model_name() -> None:
    decision = _router().resolve(
        _profile(vision="vision-model-name-but-no-declared-capability"),
        (_route("vision-model-name-but-no-declared-capability", ("text",)),),
        ModelSelectionRequest("vision"),
    )

    assert decision.available is False
    assert decision.reason == "required_modality_unavailable"


def test_profile_and_candidate_contracts_fail_closed_on_ambiguous_or_invalid_values() -> None:
    with pytest.raises(ModelRoutingError, match="canonical tier order"):
        ModelRoutingProfile(1, 1, "fast", (("standard", None), ("fast", None), ("deep", None), ("vision", None), ("image_generation", None)))
    with pytest.raises(ModelRoutingError, match="unique"):
        _router().resolve(_profile(), (_route("fast-route", ("text",)), _route("fast-route", ("structured",))), ModelSelectionRequest("text"))
    with pytest.raises(ModelRoutingError, match="text project preferred"):
        ModelSelectionRequest("text", "vision")
    with pytest.raises(ModelRoutingError, match="revision evidence"):
        ModelRouteCandidate("fast-route", ("text",), 1, "https://private.example")
    with pytest.raises(ModelRoutingError, match="activation fingerprint"):
        ModelRoutingAuthorityBinding(1, 1, "private-endpoint")


def _router() -> ModelRoutingRouter:
    return ModelRoutingRouter()


def _profile(**changes: str | None) -> ModelRoutingProfile:
    routes = {
        "fast": "fast-route",
        "standard": "standard-route",
        "deep": "deep-route",
        "vision": "vision-route",
        "image_generation": None,
    }
    routes.update(changes)
    return ModelRoutingProfile(4, 2, "fast", tuple(routes.items()))  # type: ignore[arg-type]


def _route(route_key: str, capabilities: tuple[str, ...], *, revision: int = 1) -> ModelRouteCandidate:
    return ModelRouteCandidate(route_key, capabilities, revision, f"adapter-manifest:{revision}")  # type: ignore[arg-type]
