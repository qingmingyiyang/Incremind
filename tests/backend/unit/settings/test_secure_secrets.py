from __future__ import annotations

import json
import os
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from backend.api.app import create_app
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
import backend.api.routes.settings as settings_routes
from backend.providers import ProviderRegistry
from backend.security import InMemorySecretStore, ProviderEgressPolicyStore
from backend.security.secrets import DPAPIFileSecretStore
from backend.video_summary.infrastructure.settings_service import SettingsService
from core.effect_log import (
    EffectClass,
    EffectIntent,
    EffectPurpose,
    EffectState,
    build_effect_runtime,
)
from tests.backend.unit.settings.test_workspace_settings_service import (
    FakeFasterWhisperModelManager,
    _sample_settings_toml,
)


def test_settings_service_migrates_plaintext_key_and_never_writes_it_back(tmp_path) -> None:
    (tmp_path / "config").mkdir()
    config_path = tmp_path / "config" / "settings.toml"
    config_path.write_text(_sample_settings_toml(), encoding="utf-8")
    (tmp_path / ".env").write_text(
        "OPENAI_PROVIDER=openai\nOPENAI_MODEL=test-model\nOPENAI_API_KEY=legacy-secret\n",
        encoding="utf-8",
    )
    secrets = InMemorySecretStore()

    service = SettingsService(
        config_path=config_path,
        root_dir=tmp_path,
        faster_whisper_model_manager=FakeFasterWhisperModelManager(),
        secret_store=secrets,
    )

    assert secrets.get_snapshot("provider:openai").value == "legacy-secret"
    assert "legacy-secret" not in (tmp_path / ".env").read_text(encoding="utf-8")
    service.update_provider_settings(
        llm_provider="openai",
        openai_base_url="https://example.com",
        openai_model="new-model",
        openai_api_key="new-secret",
        hf_endpoint="",
    )
    assert secrets.get_snapshot("provider:openai").value == "new-secret"
    assert "new-secret" not in (tmp_path / ".env").read_text(encoding="utf-8")
    service.delete_openai_api_key()
    assert secrets.get_snapshot("provider:openai").value == ""


def test_in_memory_secret_snapshots_advance_for_same_value_and_preserve_delete_tombstone() -> None:
    store = InMemorySecretStore({"mcp:calendar-server:token": "first"})

    initial = store.get_snapshot("mcp:calendar-server:token")
    store.set("mcp:calendar-server:token", "first")
    rotated = store.get_snapshot("mcp:calendar-server:token")
    store.delete("mcp:calendar-server:token")
    deleted = store.get_snapshot("mcp:calendar-server:token")
    store.set("mcp:calendar-server:token", "second")
    recreated = store.get_snapshot("mcp:calendar-server:token")

    assert (initial.value, initial.generation) == ("first", 1)
    assert (rotated.value, rotated.generation) == ("first", 2)
    assert (deleted.value, deleted.generation) == ("", 3)
    assert (recreated.value, recreated.generation) == ("second", 4)


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is a Windows security service")
def test_dpapi_store_encrypts_secret_at_rest(tmp_path) -> None:
    path = tmp_path / "secrets.json"
    store = DPAPIFileSecretStore(path)

    store.set("provider:test", "sensitive-value")

    assert store.get_snapshot("provider:test").value == "sensitive-value"
    assert "sensitive-value" not in path.read_text(encoding="utf-8")
    store.delete("provider:test")
    assert store.get_snapshot("provider:test").value == ""


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is a Windows security service")
def test_dpapi_store_upgrades_legacy_records_and_keeps_generation_with_tombstone(tmp_path) -> None:
    path = tmp_path / "secrets.json"
    legacy = DPAPIFileSecretStore(path)
    legacy.set("provider:test", "legacy-value")
    ciphertext_only = json.loads(path.read_text(encoding="utf-8"))["records"]["provider:test"]["ciphertext"]
    path.write_text(json.dumps({"provider:test": ciphertext_only}), encoding="utf-8")

    store = DPAPIFileSecretStore(path)
    assert store.get_snapshot("provider:test").generation == 1
    store.set("provider:test", "legacy-value")
    store.delete("provider:test")
    deleted = store.get_snapshot("provider:test")
    store.set("provider:test", "replacement")

    assert (deleted.value, deleted.generation) == ("", 3)
    assert store.get_snapshot("provider:test").generation == 4
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == 2
    assert "replacement" not in path.read_text(encoding="utf-8")


def test_provider_secret_api_never_returns_the_secret(tmp_path) -> None:
    secrets = InMemorySecretStore({"provider:openai": "hidden-secret"})
    settings_service = SimpleNamespace(
        has_openai_api_key=lambda: secrets.has_secret("provider:openai"),
        delete_openai_api_key=lambda: secrets.delete("provider:openai"),
    )
    container = SimpleNamespace(
        root_dir=tmp_path,
        config_path=tmp_path / "config" / "settings.toml",
        secret_store=secrets,
        settings_service=settings_service,
        invalidate_agent_graph_service=lambda: None,
    )
    app = create_app(container)
    client = TestClient(app)

    response = client.get("/api/provider-settings/openai-api-key")
    assert response.status_code == 200
    assert response.json() == {"has_api_key": True}
    assert "hidden-secret" not in response.text
    saved = client.post("/api/providers/openai/secret", json={"api_key": "replacement-secret"})
    assert saved.json() == {"has_api_key": True}
    assert "replacement-secret" not in saved.text
    deleted = client.delete("/api/providers/openai/secret")
    assert deleted.json() == {"has_api_key": False}


def test_provider_registry_and_api_support_multiple_secure_providers(tmp_path, monkeypatch) -> None:
    secrets = InMemorySecretStore({"provider:openai": "default-secret"})
    applied: list[dict[str, object]] = []
    settings_service = SimpleNamespace(
        has_openai_api_key=lambda: secrets.has_secret("provider:openai"),
        delete_openai_api_key=lambda: secrets.delete("provider:openai"),
        get_provider_settings=lambda: SimpleNamespace(
            llm_provider="openai",
            openai_base_url="https://api.openai.com/v1",
            openai_model="gpt-test",
            has_openai_api_key=True,
            openai_api_key_masked="***",
            hf_endpoint="",
        ),
        update_provider_settings=lambda **values: applied.append(values),
    )
    container = SimpleNamespace(
        root_dir=tmp_path,
        config_path=tmp_path / "config" / "settings.toml",
        secret_store=secrets,
        settings_service=settings_service,
        invalidate_agent_graph_service=lambda: None,
    )
    app = create_app(container)
    client = TestClient(app)

    assert client.get("/api/providers").json()[0]["provider_id"] == "openai"
    provider_registry_path = (
        tmp_path / "library" / "global" / "providers" / "providers.json"
    )
    assert not provider_registry_path.exists()
    assert client.get("/api/providers/openai/disconnect-preview").status_code == 200
    assert not provider_registry_path.exists()
    created = client.post(
        "/api/providers",
        json={
            "provider_id": "research",
            "name": "研究模型",
            "llm_provider": "custom_openai",
            "base_url": "https://models.example/v1",
            "api_path": "/chat/completions",
            "model": "model-a",
            "models": ["model-a"],
        },
    )
    assert created.status_code == 201
    assert client.post("/api/providers/research/secret", json={"api_key": "research-secret"}).json() == {
        "has_api_key": True
    }

    class ModelResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"data": [{"id": "model-b"}, {"id": "model-a"}]}

    model_calls = 0

    def get_models(url, *, headers, timeout):
        nonlocal model_calls
        model_calls += 1
        assert url == "https://models.example/v1/models"
        assert headers == {"Authorization": "Bearer research-secret"}
        assert timeout == 15.0
        return ModelResponse()

    monkeypatch.setattr(settings_routes.httpx, "get", get_models)
    denied = client.get("/api/providers/research/models")
    assert denied.status_code == 403
    assert model_calls == 0
    provider = client.get("/api/providers").json()
    research = next(item for item in provider if item["provider_id"] == "research")
    consent = client.post(
        "/api/providers/research/egress-consent",
        json={"manifest_id": research["egress_manifest"]["manifest_id"], "confirm": True},
    )
    assert consent.status_code == 200
    assert consent.json()["egress_manifest"]["consented"] is True
    assert client.get("/api/providers/research/models").json()["models"] == ["model-a", "model-b"]
    assert model_calls == 1
    assert ProviderRegistry(tmp_path).get("research", fallback={})["models"] == ["model-a"]
    receipt_store, _settings = build_rebuild_object_store(tmp_path)
    receipt_operations_before = {
        str(receipt["operation_id"])
        for receipt in receipt_store.list("workflow_effect_receipts")
    }
    original_settle = app.state.effect_runtime.runner.settle_ok

    class InjectedSettleCrash(BaseException):
        pass

    def crash_after_receipt(*_args, **_kwargs):
        raise InjectedSettleCrash()

    monkeypatch.setattr(app.state.effect_runtime.runner, "settle_ok", crash_after_receipt)
    with pytest.raises(BaseException):
        client.get("/api/providers/research/models")
    monkeypatch.setattr(app.state.effect_runtime.runner, "settle_ok", original_settle)
    receipt_operations_after = {
        str(receipt["operation_id"])
        for receipt in receipt_store.list("workflow_effect_receipts")
    }
    crashed_operations = receipt_operations_after - receipt_operations_before
    assert len(crashed_operations) == 1
    crashed_operation = crashed_operations.pop()
    crashed_effect = app.state.effect_runtime.log.get(crashed_operation)
    assert crashed_effect.state is EffectState.INFLIGHT

    app.state.effect_recovery_coordinator.recover_once(
        now=int(crashed_effect.lease_expires_at or 0) + 1,
    )

    assert app.state.effect_runtime.log.get(crashed_operation).state is EffectState.SETTLED_OK
    assert model_calls == 2
    activated = client.post("/api/providers/research/activate")
    assert activated.status_code == 200
    assert activated.json()["is_active"] is True
    assert secrets.get_snapshot("provider:openai").value == ""
    assert applied[-1]["openai_model"] == "model-a"
    active_preview = client.get("/api/providers/research/disconnect-preview")
    assert active_preview.status_code == 200
    assert active_preview.json() == {
        "provider_id": "research",
        "name": "研究模型",
        "is_active": True,
        "has_api_key": True,
        "egress_external": True,
        "egress_consented": True,
        "route_registry_revision": 0,
        "referenced_routes": [],
        "replacement_candidates": [
            {
                "provider_id": "openai",
                "name": "默认供应商",
                "model": "gpt-test",
                "ready": False,
            }
        ],
        "delete_blockers": ["active_default"],
        "can_delete": False,
        "disconnect_effect": "model_features_paused",
    }
    assert client.delete("/api/providers/research").status_code == 409
    assert client.delete("/api/providers/research/egress-consent").status_code == 200
    assert client.delete("/api/providers/research/egress-consent").status_code == 200
    assert client.delete("/api/providers/research/secret").json() == {"has_api_key": False}
    assert client.delete("/api/providers/research/secret").json() == {"has_api_key": False}
    disconnected_preview = client.get("/api/providers/research/disconnect-preview").json()
    assert disconnected_preview["is_active"] is True
    assert disconnected_preview["has_api_key"] is False
    assert disconnected_preview["egress_consented"] is False
    assert disconnected_preview["can_delete"] is False
    assert ProviderRegistry(tmp_path).get("research", fallback={})["provider_id"] == "research"

    disposable = client.post(
        "/api/providers",
        json={
            "provider_id": "disposable",
            "name": "临时模型",
            "llm_provider": "custom_openai",
            "base_url": "https://temporary.example/v1",
            "api_path": "/chat/completions",
            "model": "temp-model",
            "models": ["temp-model"],
        },
    )
    assert disposable.status_code == 201
    client.post("/api/providers/disposable/secret", json={"api_key": "temporary-secret"})
    disposable_record = next(item for item in client.get("/api/providers").json() if item["provider_id"] == "disposable")
    assert client.post(
        "/api/providers/disposable/egress-consent",
        json={"manifest_id": disposable_record["egress_manifest"]["manifest_id"], "confirm": True},
    ).status_code == 200
    disposable_preview = client.get("/api/providers/disposable/disconnect-preview")
    assert disposable_preview.status_code == 200
    assert disposable_preview.json()["can_delete"] is True
    assert disposable_preview.json()["delete_blockers"] == []
    assert disposable_preview.json()["has_api_key"] is True
    assert disposable_preview.json()["egress_consented"] is True
    assert disposable_preview.json()["disconnect_effect"] == "provider_connection_removed"
    assert client.delete("/api/providers/disposable").status_code == 204
    assert secrets.get_snapshot("provider:disposable").value == ""
    assert all(item["provider_id"] != "disposable" for item in client.get("/api/providers").json())

    routed = client.post(
        "/api/providers",
        json={
            "provider_id": "routed",
            "name": "路由模型",
            "llm_provider": "custom_openai",
            "base_url": "https://routed.example/v1",
            "api_path": "/chat/completions",
            "model": "routed-model",
            "models": ["routed-model"],
        },
    )
    assert routed.status_code == 201

    class RoutedModelRegistry:
        def __init__(self, root_dir) -> None:
            del root_dir

        def list(self) -> dict[str, object]:
            return {"routes": [{"route_key": "conversation.default", "provider_id": "routed"}]}

    with monkeypatch.context() as scoped:
        scoped.setattr(settings_routes, "ModelRouteRegistry", RoutedModelRegistry)
        routed_preview = client.get("/api/providers/routed/disconnect-preview")
        blocked = client.delete("/api/providers/routed")
    assert routed_preview.status_code == 200
    assert routed_preview.json()["can_delete"] is False
    assert routed_preview.json()["delete_blockers"] == ["route_reference"]
    assert routed_preview.json()["referenced_routes"] == [
        {"route_key": "conversation.default", "model_name": "", "enabled": False}
    ]
    assert blocked.status_code == 409
    assert "conversation.default" in blocked.json()["detail"]
    assert ProviderRegistry(tmp_path).get("routed", fallback={})["provider_id"] == "routed"

    registry_text = (tmp_path / "library" / "global" / "providers" / "providers.json").read_text(
        encoding="utf-8"
    )
    assert "research-secret" not in registry_text
    assert ProviderRegistry(tmp_path).get("research", fallback={})["models"] == ["model-a"]


def test_provider_discovery_reaper_fails_closed_on_frozen_revision_drift(tmp_path) -> None:
    secrets = InMemorySecretStore({"provider:research": "secret"})
    settings_service = SimpleNamespace(
        get_provider_settings=lambda: SimpleNamespace(
            llm_provider="openai", openai_base_url="", openai_model="",
        ),
    )
    container = SimpleNamespace(
        root_dir=tmp_path,
        secret_store=secrets,
        settings_service=settings_service,
    )
    registry = ProviderRegistry(tmp_path)
    provider = registry.create(
        {
            "provider_id": "research",
            "name": "Research",
            "llm_provider": "custom_openai",
            "base_url": "https://models.example/v1",
            "api_path": "/chat/completions",
            "model": "model-a",
            "models": ["model-a"],
        },
        fallback={},
    )
    policy = ProviderEgressPolicyStore(tmp_path)
    manifest = settings_routes._provider_egress_manifest(provider, policy)
    policy.grant(manifest, manifest_id=manifest.manifest_id, confirm=True)
    runtime = build_effect_runtime(
        tmp_path / ".rebuild-data" / "jobs.sqlite3",
        owner_id="provider-test",
        lease_seconds=1,
    )
    settings_routes.register_provider_model_discovery_handler(runtime, container)
    intent = EffectIntent(
        session_id="provider-settings:research",
        root_id="provider-settings:research",
        step_key="discover_models",
        kind="provider_model_discovery",
        effect_class=EffectClass.QUERYABLE,
        purpose=EffectPurpose.PRIMARY,
        intent_ref="provider://research/models",
        gate_decision_id=manifest.manifest_id,
        rev_set={"provider_revision": settings_routes._provider_revision(provider)},
        payload={"provider_id": "research"},
        operation_id_override="provider-model-discovery:research:drift-test",
    )
    planned, _ = runtime.log.plan(intent, now=1)
    runtime.log.transition(
        planned.operation_id,
        expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT,
        now=1,
        lease_owner="dead-provider-worker",
        lease_expires_at=2,
        increment_attempt=True,
    )
    registry.update(
        "research", {"base_url": "https://models-v2.example/v1"}, fallback={},
    )

    outcomes = runtime.recover_expired(now=3)

    assert outcomes[0].state is EffectState.SETTLED_ERR
    assert runtime.log.get(planned.operation_id).error_ref.startswith(
        "provider.discovery.drift:"
    )
