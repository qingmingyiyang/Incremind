from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.providers import ProviderRegistry
from backend.video_summary.infrastructure.settings_service import ProviderSettings


class _Settings:
    def get_provider_settings(self) -> ProviderSettings:
        return ProviderSettings(llm_provider="openai", openai_base_url="http://127.0.0.1:8317", openai_model="fallback-model", has_openai_api_key=False, openai_api_key_masked="", hf_endpoint="")


def _client(root) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=root, settings_service=_Settings())))


def _provider(root, provider_id, model):
    return ProviderRegistry(root).create(
        {"provider_id": provider_id, "name": provider_id, "llm_provider": "openai", "base_url": "http://127.0.0.1:8317", "api_path": "/chat/completions", "model": model, "models": [model], "enabled": True},
        fallback={},
    )


def test_runtime_api_shadow_activate_restart_deactivate_and_blocks_migration_while_active(tmp_path) -> None:
    _provider(tmp_path, "intake-main-model", "fixed-model")
    route_provider = _provider(tmp_path, "route-provider", "route-model")
    client = _client(tmp_path)
    created = client.put(
        "/api/model-routes/intake.classification",
        json={"provider_id": route_provider["provider_id"], "model_name": "route-model", "adapter_kind": "openai-compatible", "enabled": True, "reason": "runtime API", "expected_registry_revision": 0},
    )
    assert created.status_code == 200

    shadow = client.post("/api/model-route-runtime/preview", json={"route_keys": ["intake.classification"]})
    assert shadow.status_code == 200
    assert shadow.json()["comparisons"][0]["same_provider_and_model"] is False
    active = client.post(
        "/api/model-route-runtime/activate",
        json={"shadow_token": shadow.json()["shadow_token"], "route_keys": ["intake.classification"], "expected_runtime_revision": 0, "confirm": True},
    )
    assert active.status_code == 200
    assert active.json()["runtime_activation"] is True
    assert _client(tmp_path).get("/api/model-routes").json()["runtime_activation"] is True

    migration = client.post("/api/rebuild/model-routes/migration/confirm", json={"preview_token": "0" * 64, "choices": {}, "renderer_task_map": {}, "confirm": True})
    assert migration.status_code == 409
    assert "deactivate" in migration.json()["detail"]

    off = client.post("/api/model-route-runtime/deactivate", json={"expected_runtime_revision": 1, "confirm": True})
    assert off.status_code == 200
    assert off.json()["runtime_activation"] is False
    assert _client(tmp_path).get("/api/model-routes/intake.classification").json()["route"]["provider_id"] == "route-provider"


def test_runtime_api_rejects_stale_activation_and_unsupported_route(tmp_path) -> None:
    _provider(tmp_path, "intake-main-model", "fixed-model")
    route_provider = _provider(tmp_path, "route-provider", "route-model")
    client = _client(tmp_path)
    assert client.put(
        "/api/model-routes/intake.classification",
        json={"provider_id": route_provider["provider_id"], "model_name": "route-model", "adapter_kind": "openai-compatible", "enabled": True, "reason": "runtime API", "expected_registry_revision": 0},
    ).status_code == 200
    denied = client.post("/api/model-route-runtime/preview", json={"route_keys": ["memory.candidate"]})
    assert denied.status_code == 409
    shadow = client.post("/api/model-route-runtime/preview", json={}).json()
    stale = client.post("/api/model-route-runtime/activate", json={"shadow_token": shadow["shadow_token"], "expected_runtime_revision": 7, "confirm": True})
    assert stale.status_code == 409
    assert "revision conflict" in stale.json()["detail"]

    runtime_path = tmp_path / "library/global/model-routes/runtime.json"
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.write_text(json.dumps({"mode": "active", "api_key": "secret"}), encoding="utf-8")
    assert client.get("/api/model-route-runtime").status_code == 409
    assert client.get("/api/model-routes").status_code == 409
