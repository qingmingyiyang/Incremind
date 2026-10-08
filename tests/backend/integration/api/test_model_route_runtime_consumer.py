from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api import workbench_input_classifier_runtime as classifier_runtime
from backend.providers import ProviderRegistry
from backend.video_summary.infrastructure.settings_service import ProviderSettings


class _Settings:
    def get_provider_settings(self) -> ProviderSettings:
        return ProviderSettings(llm_provider="openai", openai_base_url="http://127.0.0.1:8317", openai_model="fallback-model", has_openai_api_key=False, openai_api_key_masked="", hf_endpoint="")


def _container(root):
    return SimpleNamespace(root_dir=root, settings_service=_Settings(), secret_store=type("Secrets", (), {"get": lambda _self, _key: ""})())


def _provider(root, provider_id, model):
    return ProviderRegistry(root).create(
        {"provider_id": provider_id, "name": provider_id, "llm_provider": "openai", "base_url": "http://127.0.0.1:8317", "api_path": "/chat/completions", "model": model, "models": [model], "enabled": True},
        fallback={},
    )


def _activate(root):
    fixed = _provider(root, "intake-main-model", "fixed-model")
    selected = _provider(root, "route-provider", "route-model")
    client = TestClient(create_app(_container(root)))
    assert client.put(
        "/api/model-routes/intake.classification",
        json={"provider_id": selected["provider_id"], "model_name": "route-model", "adapter_kind": "openai-compatible", "enabled": True, "reason": "consumer spy", "expected_registry_revision": 0},
    ).status_code == 200
    shadow = client.post("/api/model-route-runtime/preview", json={}).json()
    assert client.post(
        "/api/model-route-runtime/activate",
        json={"shadow_token": shadow["shadow_token"], "expected_runtime_revision": 0, "confirm": True},
    ).status_code == 200
    return client, fixed, selected


class _FakeProvider:
    provider_name = "route-provider"

    def complete_json(self, *, system_prompt, user_payload):
        return {
            "input_type": "bookmark_collection", "intent": "knowledge_supplement",
            "route": "multi_link_provider_routed", "confidence": 0.94,
            "workflow_steps": ["save_original_links"],
            "child_inputs": [
                {"input_type": "webpage", "intent": "knowledge_supplement", "route": "webpage_intake", "raw_input": "https://example.com/a"},
                {"input_type": "webpage", "intent": "knowledge_supplement", "route": "webpage_intake", "raw_input": "https://example.com/b"},
            ],
            "reason": "route provider selected",
        }


def test_active_route_without_turn_authority_fails_closed_and_auto_intake_stays_local(tmp_path, monkeypatch) -> None:
    client, _fixed, _selected = _activate(tmp_path)
    builds: list[tuple[str, str]] = []
    del monkeypatch
    content = "https://example.com/a https://example.com/b"
    classified = client.post("/api/rebuild/workbench/input-classifier", json={"content": content, "allow_provider_enhancement": True, "request_id": "route-classifier-1"})
    assert classified.status_code == 409

    intake = client.post("/api/rebuild/workbench/auto-intake", json={"content": content, "add_to_knowledge_base": True})
    assert intake.status_code == 201
    assert builds == []
    evidence = client.get("/api/model-route-runtime").json()["resolutions"]
    assert evidence == []
    assert "endpoint" not in str(evidence).lower() and "secret" not in str(evidence).lower()


def test_active_provider_revision_drift_propagates_without_compatibility_fallback(tmp_path, monkeypatch) -> None:
    client, _fixed, _selected = _activate(tmp_path)
    ProviderRegistry(tmp_path).update("route-provider", {"api_path": "/v2/chat/completions"}, fallback={})
    response = client.post("/api/rebuild/workbench/input-classifier", json={"content": "https://example.com/a https://example.com/b", "allow_provider_enhancement": True, "request_id": "route-drift-1"})
    assert response.status_code == 409


def test_inactive_runtime_without_registered_provider_fails_closed_for_remote_request(tmp_path) -> None:
    client = TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))

    response = client.post(
        "/api/rebuild/workbench/input-classifier",
        json={
            "content": "https://example.com/a https://example.com/b",
                "allow_provider_enhancement": True,
                "request_id": "route-local-1",
        },
    )

    assert response.status_code == 409
