from __future__ import annotations

import json

import pytest

from core.product_core.model_route_migration import ProviderContext
from core.product_core.model_route_registry import ModelRouteRegistry
from core.product_core.model_route_runtime import (
    ModelRouteRuntimeConflict,
    ModelRouteRuntimeError,
    ModelRouteRuntimeService,
)


def _provider(provider_id: str, model: str, revision: str = "p1", *, consented: bool = True, enabled: bool = True) -> ProviderContext:
    return ProviderContext(
        record={"provider_id": provider_id, "updated_at": revision, "enabled": enabled, "model": model, "models": [model]},
        egress_consented=consented,
    )


def _create_route(root, provider: ProviderContext) -> None:
    ModelRouteRegistry(root).update(
        "intake.classification",
        {"provider_id": provider.record["provider_id"], "model_name": provider.record["model"], "adapter_kind": "openai-compatible", "enabled": True, "reason": "runtime test"},
        expected_registry_revision=0,
        provider=provider.record,
        egress_consented=provider.egress_consented,
    )


def test_shadow_activate_resolve_restart_deactivate_and_emergency_fallback(tmp_path, monkeypatch) -> None:
    selected = _provider("route-provider", "route-model")
    fallback = _provider("fixed-provider", "fixed-model")
    _create_route(tmp_path, selected)
    service = ModelRouteRuntimeService(tmp_path)

    before = service.resolve("intake.classification", compatibility=fallback, providers=[selected, fallback])
    assert before["source"] == "compatibility"
    assert before["provider_id"] == "fixed-provider"

    shadow = service.preview(route_keys=["intake.classification"], compatibility={"intake.classification": fallback}, providers=[selected, fallback])
    assert shadow["status"] == "shadow"
    assert shadow["runtime_activation"] is False
    assert shadow["comparisons"][0]["same_provider_and_model"] is False
    active = service.activate(
        shadow_token=shadow["shadow_token"], route_keys=shadow["route_keys"],
        expected_runtime_revision=shadow["runtime_revision"], confirm=True,
        compatibility={"intake.classification": fallback}, providers=[selected, fallback],
    )
    assert active["runtime_activation"] is True
    replayed = service.activate(
        shadow_token=shadow["shadow_token"], route_keys=shadow["route_keys"],
        expected_runtime_revision=shadow["runtime_revision"], confirm=True,
        compatibility={"intake.classification": fallback}, providers=[selected, fallback],
    )
    assert replayed["replayed"] is True
    with pytest.raises(ModelRouteRuntimeConflict, match="deactivate"):
        service.activate(
            shadow_token="f" * 64, route_keys=shadow["route_keys"],
            expected_runtime_revision=active["runtime_revision"], confirm=True,
            compatibility={"intake.classification": fallback}, providers=[selected, fallback],
        )
    resolved = ModelRouteRuntimeService(tmp_path).resolve("intake.classification", compatibility=fallback, providers=[selected, fallback])
    assert (resolved["source"], resolved["provider_id"], resolved["model_name"]) == ("registry", "route-provider", "route-model")
    assert ModelRouteRegistry(tmp_path).list()["runtime_activation"] is True

    monkeypatch.setenv("CHRIPTMAS_MODEL_ROUTE_RUNTIME", "off")
    emergency = service.resolve("intake.classification", compatibility=fallback, providers=[selected, fallback])
    assert emergency["source"] == "emergency_compatibility"
    monkeypatch.delenv("CHRIPTMAS_MODEL_ROUTE_RUNTIME")
    off = service.deactivate(expected_runtime_revision=active["runtime_revision"], confirm=True)
    assert off["runtime_activation"] is False
    assert ModelRouteRegistry(tmp_path).get("intake.classification")["route"]["provider_id"] == "route-provider"


def test_activation_and_resolution_fail_closed_on_preview_registry_provider_and_egress_drift(tmp_path) -> None:
    selected = _provider("route-provider", "route-model")
    fallback = _provider("fixed-provider", "fixed-model")
    _create_route(tmp_path, selected)
    service = ModelRouteRuntimeService(tmp_path)
    shadow = service.preview(route_keys=["intake.classification"], compatibility={"intake.classification": fallback}, providers=[selected, fallback])

    with pytest.raises(ModelRouteRuntimeConflict, match="preview drifted"):
        service.activate(shadow_token="0" * 64, route_keys=["intake.classification"], expected_runtime_revision=0, confirm=True, compatibility={"intake.classification": fallback}, providers=[selected, fallback])
    with pytest.raises(ModelRouteRuntimeError, match="egress consent"):
        service.preview(route_keys=["intake.classification"], compatibility={"intake.classification": fallback}, providers=[_provider("route-provider", "route-model", consented=False), fallback])

    active = service.activate(shadow_token=shadow["shadow_token"], route_keys=["intake.classification"], expected_runtime_revision=0, confirm=True, compatibility={"intake.classification": fallback}, providers=[selected, fallback])
    ModelRouteRegistry(tmp_path).update(
        "intake.classification",
        {"provider_id": "route-provider", "model_name": "route-model", "adapter_kind": "openai-compatible", "enabled": True, "reason": "drift"},
        expected_registry_revision=1, provider=selected.record, egress_consented=True,
    )
    with pytest.raises(ModelRouteRuntimeConflict, match="registry revision drifted"):
        service.resolve("intake.classification", compatibility=fallback, providers=[selected, fallback])
    assert service.deactivate(expected_runtime_revision=active["runtime_revision"], confirm=True)["mode"] == "off"


def test_runtime_state_rejects_sensitive_tampering_and_bounds_resolution_audit(tmp_path) -> None:
    selected = _provider("route-provider", "route-model")
    _create_route(tmp_path, selected)
    service = ModelRouteRuntimeService(tmp_path)
    for _ in range(105):
        service.resolve("intake.classification", compatibility=selected, providers=[selected])
    state = service.status()
    assert len(state["resolutions"]) == 100
    assert all("provider" not in item and "endpoint" not in item for item in state["resolutions"])

    path = tmp_path / "library/global/model-routes/runtime.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["api_key"] = "secret"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ModelRouteRuntimeError, match="invalid model route runtime state"):
        ModelRouteRuntimeService(tmp_path).status()
