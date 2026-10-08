from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.providers import ProviderRegistry
from backend.video_summary.infrastructure.settings_service import ProviderSettings
from core.storage_provider import JsonObjectStore


class _Settings:
    def get_provider_settings(self) -> ProviderSettings:
        return ProviderSettings(
            llm_provider="openai",
            openai_base_url="http://127.0.0.1:8317",
            openai_model="fallback-model",
            has_openai_api_key=False,
            openai_api_key_masked="",
            hf_endpoint="",
        )


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path, settings_service=_Settings())))


def _provider(tmp_path, provider_id, model):
    registry = ProviderRegistry(tmp_path)
    record = registry.create(
        {
            "provider_id": provider_id,
            "name": provider_id,
            "llm_provider": "openai",
            "base_url": "http://127.0.0.1:8317",
            "api_path": "/chat/completions",
            "model": model,
            "models": [model],
            "enabled": True,
        },
        fallback={},
    )
    registry.activate(provider_id, fallback={})
    return record


def _developer_payload(revision=None, provider_id="local-main", model="model-main"):
    return {
        "expected_revision": revision,
        "model_profiles": [
            {"id": "mp-intake", "provider": provider_id, "modelId": model},
            {"id": "mp-memory", "provider": provider_id, "modelId": model},
        ],
        "prompts": [],
        "skills": [],
        "workflow_steps": [],
        "snapshots": [],
    }


def _seed_legacy_developer_config(tmp_path, provider_id="local-main", model="model-main") -> None:
    payload = _developer_payload(provider_id=provider_id, model=model)
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    store.write(
        "developer_studio_configs",
        "default",
        {
            "schema_version": "1.0.0",
            "id": "default",
            "revision": 1,
            **{key: value for key, value in payload.items() if key != "expected_revision"},
            "task_model_map": {"intakeMain": "mp-intake", "memory": "mp-memory"},
            "updated_at": "2026-07-01T00:00:00+08:00",
        },
        expected_revision=None,
    )


def _choices(preview):
    return {
        route["route_key"]: route["recommended_source"] or "compatibility"
        for route in preview["routes"]
    }


def test_migration_api_preview_confirm_replay_restart_and_rollback_preserve_legacy_sources(tmp_path) -> None:
    _provider(tmp_path, "local-main", "model-main")
    _seed_legacy_developer_config(tmp_path)
    client = _client(tmp_path)
    assert client.get("/api/rebuild/developer-studio/config").json()["revision"] == 1
    renderer_map = {"intakeMain": "local-main", "memory": "local-main", "asr": "local-asr"}

    preview_response = client.post(
        "/api/rebuild/model-routes/migration/preview",
        json={"renderer_task_map": renderer_map},
    )
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["runtime_activation"] is False
    assert preview["retained_unmigrated"] == ["asr", "embed", "vision"]
    assert not (tmp_path / "library/global/model-routes/model-routes.json").exists()

    confirm_body = {
        "preview_token": preview["preview_token"],
        "confirm": True,
        "choices": _choices(preview),
        "renderer_task_map": renderer_map,
    }
    confirmed = client.post("/api/rebuild/model-routes/migration/confirm", json=confirm_body)
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "applied"
    assert confirmed.json()["runtime_activation"] is False
    migration_id = confirmed.json()["migration"]["migration_id"]

    replay = _client(tmp_path).post("/api/rebuild/model-routes/migration/confirm", json=confirm_body)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    routes = _client(tmp_path).get("/api/model-routes").json()
    assert routes["registry_revision"] == 1
    assert routes["runtime_activation"] is False

    developer_after = client.get("/api/rebuild/developer-studio/config").json()
    assert developer_after["task_model_map"] == {"intakeMain": "mp-intake", "memory": "mp-memory"}
    assert developer_after["revision"] == 1

    rolled_back = client.post(
        f"/api/rebuild/model-routes/migrations/{migration_id}/rollback",
        json={"expected_registry_revision": 1, "confirm": True},
    )
    assert rolled_back.status_code == 200
    assert rolled_back.json()["status"] == "rolled_back"
    assert _client(tmp_path).get("/api/model-routes").json()["routes"] == []
    assert client.get("/api/rebuild/developer-studio/config").json()["revision"] == 1


def test_migration_api_rejects_source_drift_missing_confirmation_and_secret_shaped_renderer_values(tmp_path) -> None:
    _provider(tmp_path, "local-main", "model-main")
    _seed_legacy_developer_config(tmp_path)
    client = _client(tmp_path)
    renderer_map = {"intakeMain": "local-main"}
    preview = client.post(
        "/api/rebuild/model-routes/migration/preview", json={"renderer_task_map": renderer_map},
    ).json()

    denied = client.post(
        "/api/rebuild/model-routes/migration/confirm",
        json={
            "preview_token": preview["preview_token"], "confirm": False,
            "choices": _choices(preview), "renderer_task_map": renderer_map,
        },
    )
    assert denied.status_code == 400
    invalid = client.post(
        "/api/rebuild/model-routes/migration/preview",
        json={"renderer_task_map": {"intakeMain": {"api_key": "secret"}}},
    )
    assert invalid.status_code == 409

    assert client.put("/api/rebuild/developer-studio/config", json=_developer_payload(revision=1)).status_code == 200
    drifted = client.post(
        "/api/rebuild/model-routes/migration/confirm",
        json={
            "preview_token": preview["preview_token"], "confirm": True,
            "choices": _choices(preview), "renderer_task_map": renderer_map,
        },
    )
    assert drifted.status_code == 409
    assert "preview drifted" in drifted.json()["detail"]
    assert not (tmp_path / "library/global/model-routes/model-routes.json").exists()
