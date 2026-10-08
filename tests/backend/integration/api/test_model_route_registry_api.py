from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.providers import ProviderRegistry
from backend.security import (
    DEFAULT_PROVIDER_EGRESS_CATEGORIES,
    DEFAULT_PROVIDER_EGRESS_MAX_BYTES,
    DEFAULT_PROVIDER_EGRESS_PURPOSES,
    ProviderEgressPolicyStore,
)
from backend.shared.llm.base_url import resolve_openai_compatible_api_base_url
from backend.video_summary.infrastructure.settings_service import ProviderSettings


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


def _provider(tmp_path, *, provider_id="local-openai", base_url="http://127.0.0.1:8317"):
    return ProviderRegistry(tmp_path).create(
        {
            "provider_id": provider_id,
            "name": provider_id,
            "llm_provider": "openai",
            "base_url": base_url,
            "api_path": "/chat/completions",
            "model": "route-model",
            "models": ["route-model"],
            "enabled": True,
        },
        fallback={},
    )


def _payload(**overrides):
    return {
        "provider_id": "local-openai",
        "model_name": "route-model",
        "adapter_kind": "openai-compatible",
        "enabled": True,
        "reason": "API route contract",
        **overrides,
    }


def test_model_route_api_previews_without_write_then_roundtrips_cas_and_history(tmp_path) -> None:
    _provider(tmp_path)
    client = _client(tmp_path)

    preview = client.post("/api/model-routes/intake.classification/preview", json=_payload())
    assert preview.status_code == 200
    assert preview.json()["runtime_activation"] is False
    assert client.get("/api/model-routes").json()["routes"] == []

    created = client.put(
        "/api/model-routes/intake.classification",
        json={**_payload(), "expected_registry_revision": 0},
    )
    assert created.status_code == 200
    assert created.json()["route"]["revision"] == 1
    assert created.json()["history"][0]["action"] == "created"

    restarted = _client(tmp_path).get("/api/model-routes/intake.classification")
    assert restarted.status_code == 200
    assert restarted.json()["route"]["provider_id"] == "local-openai"
    assert restarted.json()["runtime_activation"] is False

    conflict = client.put(
        "/api/model-routes/intake.classification",
        json={**_payload(reason="stale update"), "expected_registry_revision": 0},
    )
    assert conflict.status_code == 409


def test_api_rejects_secret_fields_missing_provider_and_unconsented_external_provider(tmp_path) -> None:
    _provider(tmp_path)
    client = _client(tmp_path)
    secret = client.post(
        "/api/model-routes/intake.classification/preview",
        json={**_payload(), "api_key": "must-not-enter-route"},
    )
    assert secret.status_code == 422
    missing = client.post(
        "/api/model-routes/intake.classification/preview",
        json=_payload(provider_id="missing"),
    )
    assert missing.status_code == 409

    _provider(tmp_path, provider_id="cloud", base_url="https://api.example.com")
    denied = client.post(
        "/api/model-routes/memory.candidate/preview",
        json=_payload(provider_id="cloud"),
    )
    assert denied.status_code == 409
    assert "egress consent" in denied.json()["detail"]

    provider = ProviderRegistry(tmp_path).get("cloud", fallback={})
    policy = ProviderEgressPolicyStore(tmp_path)
    manifest = policy.manifest(
        provider_id="cloud",
        endpoint=resolve_openai_compatible_api_base_url("https://api.example.com"),
        purposes=DEFAULT_PROVIDER_EGRESS_PURPOSES,
        payload_categories=DEFAULT_PROVIDER_EGRESS_CATEGORIES,
        max_payload_bytes=DEFAULT_PROVIDER_EGRESS_MAX_BYTES,
    )
    policy.grant(manifest, manifest_id=manifest.manifest_id, confirm=True)
    allowed = client.post(
        "/api/model-routes/memory.candidate/preview",
        json=_payload(provider_id=provider["provider_id"]),
    )
    assert allowed.status_code == 200


def test_get_fails_closed_after_provider_revision_drift(tmp_path) -> None:
    _provider(tmp_path)
    client = _client(tmp_path)
    created = client.put(
        "/api/model-routes/intake.classification",
        json={**_payload(), "expected_registry_revision": 0},
    )
    assert created.status_code == 200

    ProviderRegistry(tmp_path).update(
        "local-openai",
        {"api_path": "/v2/chat/completions"},
        fallback={},
    )
    drifted = _client(tmp_path).get("/api/model-routes/intake.classification")
    assert drifted.status_code == 409
    assert "revision drift" in drifted.json()["detail"]


def test_batch_route_api_applies_one_cas_and_replays_without_revision_growth(tmp_path) -> None:
    provider = _provider(tmp_path)
    client = _client(tmp_path)
    assignments = [
        {
            "route_key": route_key,
            "provider_id": provider["provider_id"],
            "model_name": "route-model",
            "adapter_kind": "openai-compatible",
            "enabled": True,
            "reason": f"three tier preset {route_key}",
        }
        for route_key in (
            "task.lightweight",
            "intake.classification",
            "conversation.default",
            "memory.candidate",
            "search.answer",
        )
    ]

    created = client.put("/api/model-routes/batch", json={
        "expected_registry_revision": 0,
        "assignments": assignments,
    })
    assert created.status_code == 200
    assert created.json()["registry_revision"] == 1
    assert len(created.json()["changed_route_keys"]) == 5
    assert client.get("/api/model-routes").json()["registry_revision"] == 1

    replayed = client.put("/api/model-routes/batch", json={
        "expected_registry_revision": 1,
        "assignments": assignments,
    })
    assert replayed.status_code == 200
    assert replayed.json()["replayed"] is True
    assert replayed.json()["registry_revision"] == 1


def test_batch_route_api_rejects_unconsented_plan_without_partial_routes(tmp_path) -> None:
    _provider(tmp_path, provider_id="cloud", base_url="https://api.example.com")
    client = _client(tmp_path)
    denied = client.put("/api/model-routes/batch", json={
        "expected_registry_revision": 0,
        "assignments": [
            {
                "route_key": route_key,
                "provider_id": "cloud",
                "model_name": "route-model",
                "adapter_kind": "openai-compatible",
                "enabled": True,
                "reason": "unconsented tier plan",
            }
            for route_key in ("task.lightweight", "memory.candidate")
        ],
    })
    assert denied.status_code == 409
    assert "egress consent" in denied.json()["detail"]
    assert client.get("/api/model-routes").json()["routes"] == []
