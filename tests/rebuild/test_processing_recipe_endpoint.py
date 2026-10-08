from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from core.product_core import ModelRouteRegistry
from core.storage_provider import JsonObjectStore


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def _draft() -> dict[str, object]:
    return {
        "id": "recipe-empty-guard",
        "name": "空输入防护",
        "description": "无副作用合同",
        "content_matcher": {"content_types": ["text"], "min_length": 0, "max_length": 20},
        "trigger_matcher": {"mode": "any", "values": []},
        "prompt_ref": {
            "prompt_id": "pt-input-understanding",
            "source": "active",
            "unit_id": "intake.classification",
            "unit_revision": 1,
        },
        "model_route_key": "intake.classification",
        "model_route_revision": 1,
        "output_schema": {
            "type": "object",
            "required": ["empty"],
            "properties": {"empty": {"type": "boolean"}},
        },
        "executor_id": "empty_guard.deterministic",
        "side_effect_class": "none",
        "priority": 100,
        "fallback": {"mode": "continue_default", "reason": "保持默认流程"},
    }


def _seed_authorities(client: TestClient, tmp_path) -> None:
    ModelRouteRegistry(tmp_path).update(
        "intake.classification",
        {
            "provider_id": "local-openai",
            "model_name": "local-model",
            "adapter_kind": "openai-compatible",
            "enabled": True,
            "reason": "recipe contract test route",
        },
        expected_registry_revision=0,
        provider={
            "provider_id": "local-openai",
            "updated_at": "provider-r1",
            "enabled": True,
            "model": "local-model",
            "models": ["local-model"],
        },
        egress_consented=True,
    )
    config = client.put(
        "/api/rebuild/developer-studio/config",
        json={
            "expected_revision": 0,
            "model_profiles": [],
            "prompts": [{
                "id": "pt-input-understanding",
                "stageId": "input-understanding",
                "name": "输入理解",
                "description": "测试active authority",
                "content": "判断输入类型",
                "variables": [],
                "outputSchema": "{}",
                "modelProfileId": "mp-default",
                "version": 1,
                "isProtected": False,
                "updatedAt": "2026-07-17T08:00:00+00:00",
            }],
            "skills": [],
            "workflow_steps": [],
            "snapshots": [],
        },
    )
    assert config.status_code == 200
    preview = client.post(
        "/api/rebuild/developer-studio/prompt-activation/preview",
        json={
            "unit_id": "intake.classification",
            "expected_config_revision": 1,
            "expected_activation_revision": 0,
        },
    )
    assert preview.status_code == 200
    activated = client.post(
        "/api/rebuild/developer-studio/prompt-activation/activate",
        json={
            "unit_id": "intake.classification",
            "expected_config_revision": 1,
            "expected_activation_revision": 0,
            "preview_token": preview.json()["preview_token"],
            "confirm": True,
            "reason": "recipe authority fixture",
        },
    )
    assert activated.status_code == 200


def _save_draft(client: TestClient) -> dict[str, object]:
    preview = client.post(
        "/api/rebuild/developer-studio/processing-recipes/drafts/preview",
        json={"recipe": _draft()},
    )
    assert preview.status_code == 200
    saved = client.put(
        "/api/rebuild/developer-studio/processing-recipes/drafts",
        json={
            "recipe": _draft(),
            "expected_registry_revision": 0,
            "validation_token": preview.json()["validation_token"],
        },
    )
    assert saved.status_code == 200
    return saved.json()


def _activate_recipe(client: TestClient) -> dict[str, object]:
    preview = client.post(
        "/api/rebuild/developer-studio/processing-recipes/activation/preview",
        json={
            "recipe_id": "recipe-empty-guard",
            "expected_registry_revision": 1,
            "expected_recipe_revision": 1,
        },
    )
    assert preview.status_code == 200, preview.text
    activated = client.post(
        "/api/rebuild/developer-studio/processing-recipes/activate",
        json={
            "recipe_id": "recipe-empty-guard",
            "expected_registry_revision": 1,
            "expected_recipe_revision": 1,
            "activation_token": preview.json()["activation_token"],
            "confirm": True,
            "reason": "验证首条无副作用纵向链",
        },
    )
    assert activated.status_code == 200, activated.text
    return activated.json()


def test_recipe_api_draft_activation_restart_deactivate_and_rollback(tmp_path) -> None:
    client = _client(tmp_path)
    initial = client.get("/api/rebuild/developer-studio/processing-recipes")
    assert initial.status_code == 200
    assert initial.json()["production_feature_enabled"] is True

    saved = _save_draft(client)
    assert saved["registry_revision"] == 1
    assert saved["active"] == []
    _seed_authorities(client, tmp_path)

    preview = client.post(
        "/api/rebuild/developer-studio/processing-recipes/activation/preview",
        json={
            "recipe_id": "recipe-empty-guard",
            "expected_registry_revision": 1,
            "expected_recipe_revision": 1,
        },
    )
    assert preview.status_code == 200
    assert preview.json()["runtime_effect"] == "active_empty_guard_preflight"
    activated = client.post(
        "/api/rebuild/developer-studio/processing-recipes/activate",
        json={
            "recipe_id": "recipe-empty-guard",
            "expected_registry_revision": 1,
            "expected_recipe_revision": 1,
            "activation_token": preview.json()["activation_token"],
            "confirm": True,
            "reason": "API激活合同验证",
        },
    )
    assert activated.status_code == 200
    assert activated.json()["status"] == "activated"
    assert activated.json()["production_feature_enabled"] is True

    with _client(tmp_path) as restarted:
        status = restarted.get("/api/rebuild/developer-studio/processing-recipes")
        assert status.status_code == 200
        assert status.json()["active"][0]["id"] == "recipe-empty-guard"
        deactivated = restarted.post(
            "/api/rebuild/developer-studio/processing-recipes/deactivate",
            json={
                "recipe_id": "recipe-empty-guard",
                "expected_registry_revision": 2,
                "confirm": True,
                "reason": "验证禁用",
            },
        )
        assert deactivated.status_code == 200
        assert deactivated.json()["active"] == []
        rollback = restarted.post(
            "/api/rebuild/developer-studio/processing-recipes/rollback",
            json={
                "recipe_id": "recipe-empty-guard",
                "expected_registry_revision": 3,
                "confirm": True,
                "reason": "恢复禁用前快照",
            },
        )
        assert rollback.status_code == 200
        assert rollback.json()["active"][0]["id"] == "recipe-empty-guard"


def test_recipe_api_conflict_confirmation_and_invalid_executor_status_codes(tmp_path) -> None:
    client = _client(tmp_path)
    _save_draft(client)
    _seed_authorities(client, tmp_path)
    preview = client.post(
        "/api/rebuild/developer-studio/processing-recipes/activation/preview",
        json={
            "recipe_id": "recipe-empty-guard",
            "expected_registry_revision": 1,
            "expected_recipe_revision": 1,
        },
    ).json()
    missing_confirmation = client.post(
        "/api/rebuild/developer-studio/processing-recipes/activate",
        json={
            "recipe_id": "recipe-empty-guard",
            "expected_registry_revision": 1,
            "expected_recipe_revision": 1,
            "activation_token": preview["activation_token"],
            "confirm": False,
            "reason": "未确认",
        },
    )
    assert missing_confirmation.status_code == 400

    invalid = _draft()
    invalid["executor_id"] = "python.eval"
    invalid_response = client.post(
        "/api/rebuild/developer-studio/processing-recipes/drafts/preview",
        json={"recipe": invalid},
    )
    assert invalid_response.status_code == 400
    assert "executor" in invalid_response.json()["detail"]

    stale = client.post(
        "/api/rebuild/developer-studio/processing-recipes/activation/preview",
        json={
            "recipe_id": "recipe-empty-guard",
            "expected_registry_revision": 0,
            "expected_recipe_revision": 1,
        },
    )
    assert stale.status_code == 409


def test_legacy_preview_uses_server_config_and_never_writes_or_activates(tmp_path) -> None:
    client = _client(tmp_path)
    saved = client.put(
        "/api/rebuild/developer-studio/config",
        json={
            "expected_revision": 0,
            "model_profiles": [],
            "prompts": [],
            "skills": [
                {
                    "id": "sk-empty-guard",
                    "enabled": True,
                    "promptTemplateId": "pt-empty-handle",
                    "modelProfileId": "mp-default",
                }
            ],
            "workflow_steps": [],
            "snapshots": [],
        },
    )
    assert saved.status_code == 200

    preview = client.get("/api/rebuild/developer-studio/processing-recipes/legacy-preview")
    assert preview.status_code == 200
    assert preview.json()["write_effect"] == "none"
    assert preview.json()["candidates"][0]["legacy_enabled_is_production_active"] is False
    status = client.get("/api/rebuild/developer-studio/processing-recipes").json()
    assert status["registry_revision"] == 0
    assert status["active"] == []


def test_activation_fails_closed_when_model_route_authority_drifted(tmp_path) -> None:
    client = _client(tmp_path)
    _save_draft(client)
    _seed_authorities(client, tmp_path)
    preview = client.post(
        "/api/rebuild/developer-studio/processing-recipes/activation/preview",
        json={
            "recipe_id": "recipe-empty-guard",
            "expected_registry_revision": 1,
            "expected_recipe_revision": 1,
        },
    )
    assert preview.status_code == 200

    ModelRouteRegistry(tmp_path).update(
        "intake.classification",
        {
            "provider_id": "local-openai",
            "model_name": "local-model-2",
            "adapter_kind": "openai-compatible",
            "enabled": True,
            "reason": "drift route after recipe preview",
        },
        expected_registry_revision=1,
        provider={
            "provider_id": "local-openai",
            "updated_at": "provider-r1",
            "enabled": True,
            "model": "local-model",
            "models": ["local-model", "local-model-2"],
        },
        egress_consented=True,
    )
    activation = client.post(
        "/api/rebuild/developer-studio/processing-recipes/activate",
        json={
            "recipe_id": "recipe-empty-guard",
            "expected_registry_revision": 1,
            "expected_recipe_revision": 1,
            "activation_token": preview.json()["activation_token"],
            "confirm": True,
            "reason": "漂移后不得激活",
        },
    )
    assert activation.status_code == 400
    assert "model route revision drifted" in activation.json()["detail"]


def test_workbench_recipe_preflight_preserves_empty_errors_and_product_zero_write(tmp_path) -> None:
    with _client(tmp_path) as client:
        inactive_classifier = client.post(
            "/api/rebuild/workbench/input-classifier", json={"content": ""}
        )
        inactive_auto_intake = client.post(
            "/api/rebuild/workbench/auto-intake", json={"content": ""}
        )
        assert inactive_classifier.status_code == 400
        assert inactive_auto_intake.status_code == 200
        assert inactive_classifier.json()["processing_recipe_trace"]["status"] == "no_match"
        assert inactive_auto_intake.json()["processing_recipe_trace"]["status"] == "no_match"
        classifier_error = {
            key: value
            for key, value in inactive_classifier.json().items()
            if key != "processing_recipe_trace"
        }
        auto_intake_error = {
            key: value
            for key, value in inactive_auto_intake.json().items()
            if key != "processing_recipe_trace"
        }

        _save_draft(client)
        _seed_authorities(client, tmp_path)
        _activate_recipe(client)
        active_classifier = client.post(
            "/api/rebuild/workbench/input-classifier", json={"content": "  "}
        )
        active_auto_intake = client.post(
            "/api/rebuild/workbench/auto-intake", json={"content": "  "}
        )

    assert active_classifier.status_code == 400
    assert active_auto_intake.status_code == 200
    assert {
        key: value
        for key, value in active_classifier.json().items()
        if key != "processing_recipe_trace"
    } == classifier_error
    assert {
        key: value
        for key, value in active_auto_intake.json().items()
        if key != "processing_recipe_trace"
    } == auto_intake_error
    for response, consumer in (
        (active_classifier, "workbench.input-classifier"),
        (active_auto_intake, "workbench.auto-intake"),
    ):
        trace = response.json()["processing_recipe_trace"]
        assert trace["status"] == "executed"
        assert trace["action"] == "reject_empty"
        assert trace["consumer"] == consumer
        assert trace["selected"]["recipe_id"] == "recipe-empty-guard"
        assert trace["selected"]["recipe_revision"] == 1
        assert trace["output_schema"] == {"valid": True, "errors": []}
        assert "text" not in trace

    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    for collection in (
        "sources", "jobs", "memory_candidates", "memory_atoms", "memory_scenarios",
        "memory_series_memory", "documents", "workbench_original_assets",
    ):
        assert store.list(collection) == ()


def test_workbench_recipe_nonempty_deactivate_restart_and_authority_drift(tmp_path) -> None:
    client = _client(tmp_path)
    _save_draft(client)
    _seed_authorities(client, tmp_path)
    _activate_recipe(client)

    nonempty = client.post(
        "/api/rebuild/workbench/input-classifier", json={"content": "hello"}
    )
    assert nonempty.status_code == 200, nonempty.text
    assert nonempty.json()["processing_recipe_trace"]["status"] == "executed"
    assert nonempty.json()["processing_recipe_trace"]["action"] == "continue_default"
    assert nonempty.json()["input_type"] == "direct_idea"

    deactivated = client.post(
        "/api/rebuild/developer-studio/processing-recipes/deactivate",
        json={
            "recipe_id": "recipe-empty-guard",
            "expected_registry_revision": 2,
            "confirm": True,
            "reason": "验证禁用后不执行",
        },
    )
    assert deactivated.status_code == 200
    client.close()
    with _client(tmp_path) as restarted:
        inactive = restarted.post(
            "/api/rebuild/workbench/input-classifier", json={"content": ""}
        )
        assert inactive.status_code == 400
        assert inactive.json()["processing_recipe_trace"]["status"] == "no_match"

        rollback = restarted.post(
            "/api/rebuild/developer-studio/processing-recipes/rollback",
            json={
                "recipe_id": "recipe-empty-guard",
                "expected_registry_revision": 3,
                "confirm": True,
                "reason": "恢复纵向链用于漂移验证",
            },
        )
        assert rollback.status_code == 200
        ModelRouteRegistry(tmp_path).update(
            "intake.classification",
            {
                "provider_id": "local-openai",
                "model_name": "local-model-2",
                "adapter_kind": "openai-compatible",
                "enabled": True,
                "reason": "纵向链漂移验证",
            },
            expected_registry_revision=1,
            provider={
                "provider_id": "local-openai",
                "updated_at": "provider-r1",
                "enabled": True,
                "model": "local-model",
                "models": ["local-model", "local-model-2"],
            },
            egress_consented=True,
        )
        drifted_classifier = restarted.post(
            "/api/rebuild/workbench/input-classifier", json={"content": ""}
        )
        drifted_auto_intake = restarted.post(
            "/api/rebuild/workbench/auto-intake", json={"content": ""}
        )

    assert drifted_classifier.status_code == 409
    assert drifted_auto_intake.status_code == 409
    assert "model route revision drifted" in drifted_classifier.json()["detail"]
    assert "model route revision drifted" in drifted_auto_intake.json()["detail"]
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    assert store.list("sources") == ()
    assert store.list("jobs") == ()
