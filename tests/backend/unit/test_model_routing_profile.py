from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.model_routing_profile import (
    ModelRoutingProfileConflict,
    ModelRoutingProfileStore,
    ModelRoutingProfileStoreError,
)


def _routes(**changes: str | None) -> dict[str, str | None]:
    routes: dict[str, str | None] = {
        "fast": "tier.fast",
        "standard": "tier.standard",
        "deep": "tier.deep",
        "vision": "tier.vision",
        "image_generation": None,
    }
    routes.update(changes)
    return routes


def test_default_profile_is_non_persistent_and_image_generation_unavailable(
    tmp_path: Path,
) -> None:
    snapshot = ModelRoutingProfileStore(tmp_path).get()
    assert snapshot.persisted is False
    assert snapshot.profile.revision == 1
    assert snapshot.profile.route_key_for("standard") == "search.answer"
    assert snapshot.profile.route_key_for("image_generation") is None


def test_profile_update_is_cas_atomic_and_restart_safe(tmp_path: Path) -> None:
    store = ModelRoutingProfileStore(tmp_path)
    saved = store.update(
        expected_revision=1, rules_version=2,
        text_default_tier="standard", tier_routes=_routes(),
    )
    assert saved.profile.revision == 2 and saved.persisted is True
    assert ModelRoutingProfileStore(tmp_path).get() == saved
    with pytest.raises(ModelRoutingProfileConflict, match="revision conflict"):
        store.update(
            expected_revision=1, rules_version=2,
            text_default_tier="fast", tier_routes=_routes(),
        )


def test_profile_authority_binding_round_trips_without_provider_material(
    tmp_path: Path,
) -> None:
    saved = ModelRoutingProfileStore(tmp_path).update(
        expected_revision=1,
        rules_version=2,
        text_default_tier="standard",
        tier_routes=_routes(),
        authority_binding={
            "registry_revision": 7,
            "runtime_revision": 3,
            "activation_fingerprint": "a" * 64,
        },
    )
    binding = saved.profile.authority_binding
    assert binding is not None
    assert (binding.registry_revision, binding.runtime_revision) == (7, 3)
    payload = json.loads((
        tmp_path / "library/global/model-routes/tier-routing-profile.json"
    ).read_text(encoding="utf-8"))
    assert payload["authority_binding"]["activation_fingerprint"] == "a" * 64
    assert "provider" not in json.dumps(payload).lower()


def test_profile_allows_dedicated_image_generation_route_without_provider_material(
    tmp_path: Path,
) -> None:
    store = ModelRoutingProfileStore(tmp_path)
    saved = store.update(
        expected_revision=1, rules_version=1,
        text_default_tier="standard",
        tier_routes=_routes(image_generation="tier.image_generation"),
    )
    assert saved.profile.route_key_for("image_generation") == "tier.image_generation"


def test_profile_rejects_sensitive_extra(tmp_path: Path) -> None:
    store = ModelRoutingProfileStore(tmp_path)
    store.update(
        expected_revision=1, rules_version=1,
        text_default_tier="standard", tier_routes=_routes(),
    )
    path = tmp_path / "library/global/model-routes/tier-routing-profile.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["api_key"] = "private"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ModelRoutingProfileStoreError, match="schema"):
        store.get()
