from __future__ import annotations

import json

import pytest

from core.product_core.model_route_migration import ProviderContext
from core.product_core.model_route_registry import (
    ModelRouteRegistry,
    ModelRouteRegistryConflict,
    ModelRouteRegistryError,
)
from core.product_core.model_route_runtime import ModelRouteRuntimeService


def _provider(**overrides):
    return {
        "provider_id": "local-openai",
        "updated_at": "provider-r1",
        "enabled": True,
        "model": "local-model",
        "models": ["local-model", "local-model-2"],
        **overrides,
    }


def _draft(**overrides):
    return {
        "provider_id": "local-openai",
        "model_name": "local-model",
        "adapter_kind": "openai-compatible",
        "enabled": True,
        "reason": "initial route contract",
        **overrides,
    }


def test_preview_is_zero_write_and_update_is_versioned_cas_with_restart_readback(tmp_path) -> None:
    registry = ModelRouteRegistry(tmp_path)
    preview = registry.preview("intake.classification", _draft(), provider=_provider(), egress_consented=True)
    path = tmp_path / "library/global/model-routes/model-routes.json"

    assert preview["runtime_activation"] is False
    assert preview["runtime_effect"] == "none_until_stage_c3"
    assert preview["route"]["provider_revision"] == "provider-r1"
    assert not path.exists()

    created = registry.update(
        "intake.classification",
        _draft(),
        expected_registry_revision=0,
        provider=_provider(),
        egress_consented=True,
    )
    assert created["registry_revision"] == 1
    assert created["route"]["revision"] == 1
    assert created["history"][0]["action"] == "created"

    restarted = ModelRouteRegistry(tmp_path)
    loaded = restarted.get("intake.classification")
    assert loaded["route"] == created["route"]
    assert loaded["runtime_activation"] is False

    updated = restarted.update(
        "intake.classification",
        _draft(model_name="local-model-2", reason="use second local model"),
        expected_registry_revision=1,
        provider=_provider(),
        egress_consented=True,
    )
    assert updated["registry_revision"] == 2
    assert updated["route"]["revision"] == 2
    assert [item["action"] for item in updated["history"]] == ["created", "updated"]
    assert json.loads(path.read_text(encoding="utf-8"))["registry_revision"] == 2

    with pytest.raises(ModelRouteRegistryConflict, match="revision conflict"):
        ModelRouteRegistry(tmp_path).update(
            "intake.classification",
            _draft(),
            expected_registry_revision=1,
            provider=_provider(),
            egress_consented=True,
        )


@pytest.mark.parametrize(
    ("draft", "provider", "consented", "message"),
    [
        (_draft(), _provider(enabled=False), True, "disabled"),
        (_draft(model_name="missing"), _provider(), True, "not available"),
        (_draft(adapter_kind="arbitrary-python"), _provider(), True, "unsupported"),
        (_draft(), _provider(), False, "egress consent"),
    ],
)
def test_invalid_provider_and_adapter_boundaries_fail_closed(draft, provider, consented, message, tmp_path) -> None:
    with pytest.raises(ModelRouteRegistryError, match=message):
        ModelRouteRegistry(tmp_path).preview(
            "intake.classification",
            draft,
            provider=provider,
            egress_consented=consented,
        )


def test_explicit_vision_adapter_is_durable_route_metadata(tmp_path) -> None:
    created = ModelRouteRegistry(tmp_path).update(
        "companion.vision",
        _draft(adapter_kind="openai-compatible-vision", reason="user confirmed model accepts images"),
        expected_registry_revision=0,
        provider=_provider(),
        egress_consented=True,
    )
    assert created["route"]["adapter_kind"] == "openai-compatible-vision"


def test_secret_material_and_provider_revision_drift_fail_closed(tmp_path) -> None:
    registry = ModelRouteRegistry(tmp_path)
    with pytest.raises(ModelRouteRegistryError, match="sensitive material"):
        registry.preview(
            "intake.classification",
            {**_draft(), "metadata": {"api_key": "secret"}},
            provider=_provider(),
            egress_consented=True,
        )

    created = registry.update(
        "intake.classification",
        _draft(),
        expected_registry_revision=0,
        provider=_provider(),
        egress_consented=True,
    )
    with pytest.raises(ModelRouteRegistryError, match="revision drift"):
        registry.validate_provider_reference(
            created["route"],
            provider=_provider(updated_at="provider-r2"),
            egress_consented=True,
        )


def test_tampered_persisted_route_with_secret_or_unknown_fields_is_rejected(tmp_path) -> None:
    registry = ModelRouteRegistry(tmp_path)
    created = registry.update(
        "intake.classification",
        _draft(),
        expected_registry_revision=0,
        provider=_provider(),
        egress_consented=True,
    )
    path = tmp_path / "library/global/model-routes/model-routes.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["routes"][0]["api_key"] = "must-never-be-read"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ModelRouteRegistryError, match="sensitive material"):
        ModelRouteRegistry(tmp_path).list()

    payload["routes"][0].pop("api_key")
    payload["routes"][0]["base_url"] = "https://drift.example"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ModelRouteRegistryError, match="record fields"):
        ModelRouteRegistry(tmp_path).list()
    assert created["runtime_activation"] is False


def test_batch_update_is_atomic_idempotent_and_uses_one_registry_revision(tmp_path) -> None:
    registry = ModelRouteRegistry(tmp_path)
    assignments = [
        (
            route_key,
            _draft(model_name=model, reason=f"tier preset {route_key}"),
            _provider(),
            True,
        )
        for route_key, model in (
            ("task.lightweight", "local-model"),
            ("intake.classification", "local-model"),
            ("conversation.default", "local-model-2"),
        )
    ]

    created = registry.update_batch(assignments, expected_registry_revision=0)
    assert created["registry_revision"] == 1
    assert created["replayed"] is False
    assert created["changed_route_keys"] == [item[0] for item in assignments]
    assert {item["route_key"] for item in created["routes"]} == {item[0] for item in assignments}

    replayed = registry.update_batch(assignments, expected_registry_revision=1)
    assert replayed["registry_revision"] == 1
    assert replayed["replayed"] is True
    assert replayed["changed_route_keys"] == []


def test_batch_update_rejects_duplicate_invalid_or_stale_plan_without_partial_write(tmp_path) -> None:
    registry = ModelRouteRegistry(tmp_path)
    valid = ("task.lightweight", _draft(reason="valid batch route"), _provider(), True)
    invalid = ("memory.candidate", _draft(model_name="missing", reason="invalid batch route"), _provider(), True)
    with pytest.raises(ModelRouteRegistryError, match="not available"):
        registry.update_batch([valid, invalid], expected_registry_revision=0)
    assert registry.list()["routes"] == []

    with pytest.raises(ModelRouteRegistryError, match="duplicate"):
        registry.update_batch([valid, valid], expected_registry_revision=0)
    assert registry.list()["registry_revision"] == 0

    registry.update_batch([valid], expected_registry_revision=0)
    with pytest.raises(ModelRouteRegistryConflict, match="revision conflict"):
        registry.update_batch([
            ("memory.candidate", _draft(reason="stale batch route"), _provider(), True),
        ], expected_registry_revision=0)
    assert {item["route_key"] for item in registry.list()["routes"]} == {"task.lightweight"}


def test_batch_update_requires_explicit_runtime_deactivation_before_route_change(tmp_path) -> None:
    registry = ModelRouteRegistry(tmp_path)
    provider = ProviderContext(record=_provider(), egress_consented=True)
    initial = (
        "intake.classification",
        _draft(reason="active batch route"),
        provider.record,
        True,
    )
    registry.update_batch([initial], expected_registry_revision=0)
    runtime = ModelRouteRuntimeService(tmp_path)
    shadow = runtime.preview(
        route_keys=["intake.classification"],
        compatibility={"intake.classification": provider},
        providers=[provider],
    )
    runtime.activate(
        shadow_token=shadow["shadow_token"],
        route_keys=shadow["route_keys"],
        expected_runtime_revision=shadow["runtime_revision"],
        confirm=True,
        compatibility={"intake.classification": provider},
        providers=[provider],
    )

    changed = (
        "intake.classification",
        _draft(model_name="local-model-2", reason="changed active batch route"),
        provider.record,
        True,
    )
    with pytest.raises(ModelRouteRegistryConflict, match="deactivate"):
        registry.update_batch([changed], expected_registry_revision=1)
    assert registry.list()["registry_revision"] == 1
    assert registry.get("intake.classification")["route"]["model_name"] == "local-model"
