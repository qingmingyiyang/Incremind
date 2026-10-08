from __future__ import annotations

import pytest

from core.product_core.model_route_migration import (
    ModelRouteMigrationConflict,
    ModelRouteMigrationError,
    ModelRouteMigrationService,
    ProviderContext,
)
from core.product_core.model_route_registry import ModelRouteRegistry


def _provider(provider_id: str, model: str, *, active=False, revision="r1", consented=True):
    return ProviderContext(
        record={
            "provider_id": provider_id,
            "updated_at": revision,
            "enabled": True,
            "is_active": active,
            "model": model,
            "models": [model],
        },
        egress_consented=consented,
    )


def _preview(service, **overrides):
    values = {
        "renderer_task_map": {"intakeMain": "deepseek", "memory": "deepseek"},
        "developer_revision": 4,
        "developer_task_map": {"intakeMain": "mp-intake", "memory": {"provider_id": "local", "model_name": "local-model"}},
        "developer_model_profiles": [
            {"id": "mp-intake", "provider": "deepseek", "modelId": "deepseek-chat"},
        ],
        "providers": [
            _provider("deepseek", "deepseek-chat", active=True),
            _provider("local", "local-model"),
        ],
    }
    values.update(overrides)
    return service.preview(**values), values


def test_preview_is_zero_write_and_explains_conflicts_defaults_unknowns_and_special_keys(tmp_path) -> None:
    service = ModelRouteMigrationService(ModelRouteRegistry(tmp_path))
    preview, _values = _preview(
        service,
        renderer_task_map={"intakeMain": "deepseek", "memory": "missing", "asr": "local-asr", "futureTask": "deepseek"},
    )

    assert not (tmp_path / "library/global/model-routes/model-routes.json").exists()
    routes = {item["route_key"]: item for item in preview["routes"]}
    assert routes["intake.classification"]["conflict"] is False
    assert routes["intake.classification"]["recommended_source"] == "renderer"
    assert routes["memory.candidate"]["conflict"] is False
    assert routes["memory.candidate"]["recommended_source"] == "developer"
    renderer_memory = next(item for item in routes["memory.candidate"]["options"] if item["source"] == "renderer")
    assert renderer_memory["issue"] == "unknown_provider"
    assert routes["conversation.default"]["recommended_source"] == "compatibility"
    assert preview["retained_unmigrated"] == ["asr", "embed", "futureTask", "vision"]
    assert preview["legacy_behavior"] == "legacy maps did not control production providers"
    assert preview["runtime_activation"] is False


def test_conflicting_sources_require_an_explicit_choice_and_batch_is_replay_safe(tmp_path) -> None:
    registry = ModelRouteRegistry(tmp_path)
    service = ModelRouteMigrationService(registry)
    preview, values = _preview(service)
    routes = {item["route_key"]: item for item in preview["routes"]}
    assert routes["memory.candidate"]["conflict"] is True
    assert routes["memory.candidate"]["recommended_source"] is None

    choices = {
        "task.lightweight": "compatibility",
        "intake.classification": "renderer",
        "conversation.default": "compatibility",
        "memory.candidate": "developer",
        "memory.project_routing": "skip",
        "search.answer": "skip",
    }
    applied = service.confirm(
        preview_token=preview["preview_token"],
        confirm=True,
        choices=choices,
        **values,
    )
    assert applied["status"] == "applied"
    assert applied["replayed"] is False
    state = registry.list()
    assert state["registry_revision"] == 1
    assert {item["route_key"] for item in state["routes"]} == {
        "task.lightweight", "intake.classification", "conversation.default", "memory.candidate",
    }
    assert next(item for item in state["routes"] if item["route_key"] == "memory.candidate")["provider_id"] == "local"
    assert state["runtime_activation"] is False

    replayed = service.confirm(
        preview_token=preview["preview_token"],
        confirm=True,
        choices=choices,
        **values,
    )
    assert replayed["replayed"] is True
    assert registry.list()["registry_revision"] == 1
    changed_choices = {**choices, "memory.candidate": "compatibility"}
    with pytest.raises(ModelRouteMigrationConflict, match="preview drifted"):
        service.confirm(
            preview_token=preview["preview_token"],
            confirm=True,
            choices=changed_choices,
            **values,
        )


def test_preview_drift_and_incomplete_or_invalid_choices_fail_closed(tmp_path) -> None:
    registry = ModelRouteRegistry(tmp_path)
    service = ModelRouteMigrationService(registry)
    preview, values = _preview(service)
    with pytest.raises(ModelRouteMigrationError, match="every migration route"):
        service.confirm(
            preview_token=preview["preview_token"],
            confirm=True,
            choices={"intake.classification": "renderer"},
            **values,
        )
    with pytest.raises(ModelRouteMigrationError, match="explicit migration confirmation"):
        service.confirm(
            preview_token=preview["preview_token"],
            confirm=False,
            choices={},
            **values,
        )
    drifted = dict(values)
    drifted["developer_revision"] = 5
    with pytest.raises(ModelRouteMigrationConflict, match="preview drifted"):
        service.confirm(
            preview_token=preview["preview_token"],
            confirm=True,
            choices={route["route_key"]: "skip" for route in preview["routes"]},
            **drifted,
        )
    with pytest.raises(ModelRouteMigrationError, match="provider id string"):
        service.preview(**{**values, "renderer_task_map": {"intakeMain": {"api_key": "secret"}}})
    with pytest.raises(ModelRouteMigrationError, match="secret-like"):
        service.preview(**{**values, "renderer_task_map": {"intakeMain": "sk-this-is-not-a-provider"}})


def test_rollback_restores_exact_before_snapshot_and_replay_is_idempotent(tmp_path) -> None:
    registry = ModelRouteRegistry(tmp_path)
    registry.update(
        "conversation.default",
        {
            "provider_id": "deepseek", "model_name": "deepseek-chat",
            "adapter_kind": "openai-compatible", "enabled": True, "reason": "existing route",
        },
        expected_registry_revision=0,
        provider=_provider("deepseek", "deepseek-chat", active=True).record,
        egress_consented=True,
    )
    before = registry.list()["routes"]
    service = ModelRouteMigrationService(registry)
    preview, values = _preview(service)
    choices = {route["route_key"]: "compatibility" for route in preview["routes"]}
    applied = service.confirm(
        preview_token=preview["preview_token"], confirm=True, choices=choices, **values,
    )
    assert registry.list()["registry_revision"] == 2
    rolled_back = service.rollback(
        applied["migration"]["migration_id"], expected_registry_revision=2, confirm=True,
    )
    assert rolled_back["status"] == "rolled_back"
    assert registry.list()["routes"] == before
    assert registry.list()["registry_revision"] == 3
    replay = service.rollback(
        applied["migration"]["migration_id"], expected_registry_revision=2, confirm=True,
    )
    assert replay["replayed"] is True
    assert registry.list()["registry_revision"] == 3


def test_rollback_refuses_to_overwrite_a_later_registry_change(tmp_path) -> None:
    registry = ModelRouteRegistry(tmp_path)
    service = ModelRouteMigrationService(registry)
    preview, values = _preview(service)
    applied = service.confirm(
        preview_token=preview["preview_token"],
        confirm=True,
        choices={route["route_key"]: "compatibility" for route in preview["routes"]},
        **values,
    )
    registry.update(
        "manual.later-change",
        {
            "provider_id": "deepseek", "model_name": "deepseek-chat",
            "adapter_kind": "openai-compatible", "enabled": True, "reason": "later explicit edit",
        },
        expected_registry_revision=1,
        provider=_provider("deepseek", "deepseek-chat", active=True).record,
        egress_consented=True,
    )
    with pytest.raises(ModelRouteMigrationConflict, match="latest registry change"):
        service.rollback(
            applied["migration"]["migration_id"],
            expected_registry_revision=2,
            confirm=True,
        )
