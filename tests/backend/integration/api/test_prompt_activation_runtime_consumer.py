from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.routes.product.repositories import _object_store
from backend.api import workbench_input_classifier_runtime as classifier_runtime
from backend.providers import ProviderRegistry
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


def _container(root):
    return SimpleNamespace(
        root_dir=root,
        settings_service=_Settings(),
        secret_store=type("Secrets", (), {"get": lambda _self, _key: ""})(),
    )


def _prompt(prompt_id: str, content: str, version: int) -> dict[str, object]:
    return {
        "id": prompt_id,
        "stageId": prompt_id.removeprefix("pt-"),
        "name": prompt_id,
        "content": content,
        "variables": [],
        "version": version,
        "isProtected": False,
    }


def _prompts(prefix: str, version: int) -> list[dict[str, object]]:
    return [
        _prompt("pt-input-understanding", f"{prefix} intake instruction", version),
        _prompt("pt-title", f"{prefix} title", version),
        _prompt("pt-detail-summary", f"{prefix} detail", version),
        _prompt("pt-longterm-organize", f"{prefix} organize", version),
        _prompt("pt-output-validate", f"{prefix} validate", version),
    ]


def _seed_legacy(root) -> None:
    store, _settings = _object_store(root)
    store.write(
        "developer_studio_configs",
        "default",
        {
            "schema_version": "1.0.0",
            "id": "default",
            "revision": 4,
            "model_profiles": [],
            "task_model_map": {},
            "prompts": _prompts("ACTIVE", 1),
            "skills": [],
            "workflow_steps": [],
            "snapshots": [],
            "updated_at": "2026-07-17T00:00:00+08:00",
        },
        expected_revision=None,
    )


def _activate_model_route(root, client: TestClient) -> None:
    registry = ProviderRegistry(root)
    for provider_id, model in (
        ("deepseek", "deepseek-chat"),
        ("intake-main-model", "fixed-model"),
        ("route-provider", "route-model"),
    ):
        registry.create(
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
    assert client.put(
        "/api/model-routes/intake.classification",
        json={
            "provider_id": "route-provider",
            "model_name": "route-model",
            "adapter_kind": "openai-compatible",
            "enabled": True,
            "reason": "prompt activation consumer proof",
            "expected_registry_revision": 0,
        },
    ).status_code == 200
    shadow = client.post("/api/model-route-runtime/preview", json={}).json()
    assert client.post(
        "/api/model-route-runtime/activate",
        json={"shadow_token": shadow["shadow_token"], "expected_runtime_revision": 0, "confirm": True},
    ).status_code == 200


class _PromptSpyProvider:
    provider_name = "route-provider"

    def __init__(self, prompts: list[str]) -> None:
        self.prompts = prompts

    def complete_json(self, *, system_prompt, user_payload):
        self.prompts.append(system_prompt)
        return {
            "input_type": "bookmark_collection",
            "intent": "knowledge_supplement",
            "route": "multi_link_provider_routed",
            "confidence": 0.94,
            "workflow_steps": ["save_original_links"],
            "child_inputs": [
                {
                    "input_type": "webpage",
                    "intent": "knowledge_supplement",
                    "route": "webpage_intake",
                    "raw_input": "https://example.com/a",
                },
                {
                    "input_type": "webpage",
                    "intent": "knowledge_supplement",
                    "route": "webpage_intake",
                    "raw_input": "https://example.com/b",
                },
            ],
            "reason": "prompt activation consumer proof",
        }


def test_input_classifier_and_auto_intake_keep_active_prompt_until_explicit_activation(
    tmp_path,
) -> None:
    _seed_legacy(tmp_path)
    client = TestClient(create_app(_container(tmp_path)))
    _activate_model_route(tmp_path, client)
    store, _settings = _object_store(tmp_path)
    runtime = classifier_runtime.build_workbench_input_classifier_runtime(
        _container(tmp_path), store,
    )

    draft = client.put(
        "/api/rebuild/developer-studio/config",
        json={
            "expected_revision": 4,
            "model_profiles": [],
            "prompts": _prompts("DRAFT", 2),
            "skills": [],
            "workflow_steps": [],
            "snapshots": [],
        },
    )
    assert draft.status_code == 200
    before = runtime.active_prompt()
    assert before is not None
    assert before["content"] == "ACTIVE intake instruction"

    preview = client.post(
        "/api/rebuild/developer-studio/prompt-activation/preview",
        json={
            "unit_id": "intake.classification",
            "expected_config_revision": 5,
            "expected_activation_revision": 0,
        },
    ).json()
    activated = client.post(
        "/api/rebuild/developer-studio/prompt-activation/activate",
        json={
            "unit_id": "intake.classification",
            "expected_config_revision": 5,
            "expected_activation_revision": 0,
            "preview_token": preview["preview_token"],
            "confirm": True,
            "reason": "runtime consumer verified",
        },
    )
    assert activated.status_code == 200

    after = runtime.active_prompt()
    assert after is not None
    assert after["content"] == "DRAFT intake instruction"
