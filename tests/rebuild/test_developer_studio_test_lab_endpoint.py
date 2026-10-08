from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from time import sleep
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.ai_runtime import get_or_build_ai_runtime
from backend.api.routes.product import developer_test_lab as product_developer_test_lab
from backend.providers import ProviderRegistry
from backend.video_summary.infrastructure.settings_service import ProviderSettings
from backend.model_routing_profile import ModelRoutingProfileStore
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from core.ai_kernel import RunLeaseToken, TurnReceipt
from core.storage_provider import JsonObjectStore
from tests.openai_transport_testlib import OpenAITransportFixture


class _Settings:
    def get_provider_settings(self) -> ProviderSettings:
        return ProviderSettings(
            llm_provider="openai",
            openai_base_url="http://127.0.0.1:8317",
            openai_model="fixed-model",
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


def _client(root) -> TestClient:
    return TestClient(create_app(_container(root)))


def _store(root) -> JsonObjectStore:
    return JsonObjectStore(root / ".rebuild-data", legacy_root=root / "library")


def _provider(root, provider_id: str, model: str, *, base_url: str = "http://127.0.0.1:8317"):
    return ProviderRegistry(root).create(
        {
            "provider_id": provider_id,
            "name": provider_id,
            "llm_provider": "openai",
            "base_url": base_url,
            "api_path": "/chat/completions",
            "model": model,
            "models": [model],
            "enabled": True,
        },
        fallback={},
    )


def _prompt(content: str, version: int) -> dict[str, object]:
    return {
        "id": "pt-input-understanding",
        "stageId": "input-understanding",
        "name": "输入理解",
        "description": "Test Lab fidelity fixture",
        "content": content,
        "variables": [],
        "outputSchema": "{}",
        "modelProfileId": "mp-default",
        "version": version,
        "isProtected": False,
        "updatedAt": f"2026-07-17T08:00:0{version}+00:00",
    }


def _save_prompt(client: TestClient, *, expected_revision: int, content: str, version: int):
    response = client.put(
        "/api/rebuild/developer-studio/config",
        json={
            "expected_revision": expected_revision,
            "model_profiles": [],
            "prompts": [_prompt(content, version)],
            "skills": [],
            "workflow_steps": [],
            "snapshots": [],
        },
    )
    assert response.status_code == 200
    return response.json()


def _seed_authorities(root, *, base_url: str = "http://127.0.0.1:8317") -> tuple[TestClient, dict[str, int]]:
    _provider(root, "intake-main-model", "fixed-model")
    selected = _provider(root, "test-lab-route", "test-lab-model", base_url=base_url)
    client = _client(root)
    route = client.put(
        "/api/model-routes/intake.classification",
        json={
            "provider_id": selected["provider_id"],
            "model_name": "test-lab-model",
            "adapter_kind": "openai-compatible",
            "enabled": True,
            "reason": "Test Lab fidelity",
            "expected_registry_revision": 0,
        },
    )
    assert route.status_code == 200
    shadow = client.post("/api/model-route-runtime/preview", json={})
    assert shadow.status_code == 200
    runtime = client.post(
        "/api/model-route-runtime/activate",
        json={
            "shadow_token": shadow.json()["shadow_token"],
            "expected_runtime_revision": 0,
            "confirm": True,
        },
    )
    assert runtime.status_code == 200

    _save_prompt(client, expected_revision=0, content="ACTIVE PROMPT V1", version=1)
    preview = client.post(
        "/api/rebuild/developer-studio/prompt-activation/preview",
        json={
            "unit_id": "intake.classification",
            "expected_config_revision": 1,
            "expected_activation_revision": 0,
        },
    )
    assert preview.status_code == 200
    active = client.post(
        "/api/rebuild/developer-studio/prompt-activation/activate",
        json={
            "unit_id": "intake.classification",
            "expected_config_revision": 1,
            "expected_activation_revision": 0,
            "preview_token": preview.json()["preview_token"],
            "confirm": True,
            "reason": "Test Lab active Prompt fixture",
        },
    )
    assert active.status_code == 200
    draft = _save_prompt(client, expected_revision=2, content="DRAFT PROMPT V2", version=2)
    unit = next(
        item
        for item in draft["prompt_activation_status"]["units"]
        if item["unit_id"] == "intake.classification"
    )
    return client, {
        "runtime": runtime.json()["runtime_revision"],
        "config": draft["revision"],
        "activation": draft["prompt_activation_status"]["activation_revision"],
        "unit": unit["unit_revision"],
    }


def _prompt_request(revisions: dict[str, int], *, source: str = "draft") -> dict[str, object]:
    return {
        "test_type": "prompt",
        "input": "测试输入",
        "provider_call_confirmed": True,
        "route_key": "intake.classification",
        "expected_runtime_revision": revisions["runtime"],
        "prompt_id": "pt-input-understanding",
        "prompt_source": source,
        "expected_config_revision": revisions["config"],
        "expected_activation_revision": revisions["activation"],
        "expected_prompt_unit_revision": revisions["unit"],
    }


class _FakeTestLabRuntime:
    """Minimal governed Turn surface used by the HTTP adapter tests."""

    def __init__(self, *, content: dict[str, object] | None = None, failure: Exception | None = None, completed_status: str = "completed") -> None:
        self.submissions: list[dict[str, object]] = []
        self.actions: list[dict[str, object]] = []
        self._failure = failure
        self._completed_status = completed_status
        self._content = content or {
            "provider_id": "test-lab-route",
            "model_name": "test-lab-model",
            "provider_call_performed": True,
            "output": {"kind": "object", "field_count": 2},
            "usage": {"input_tokens": 3, "output_tokens": 5},
        }
        self.leases: dict[str, RunLeaseToken] = {}

    def submit_turn(self, request: dict[str, object]):
        if self._failure is not None:
            raise self._failure
        self.submissions.append(request)
        return SimpleNamespace(status="waiting_approval", turn_id=request["turn_id"], current_sequence=7)

    def events_after(self, turn_id: str):
        if self._completed_status == "failed" and self.actions:
            return (
                {"type": "model.attempt.dispatched", "event_id": f"wire-{turn_id}"},
                {"type": "tool.failed", "data": {"error_code": "ai.tool_outcome_unknown"}},
            )
        return ({"type": "approval.required", "event_id": f"approval-{turn_id}"},)

    def try_acquire_run_lease(self, turn_id, owner_id, *, now, stale_after):
        if turn_id in self.leases:
            return None
        token = RunLeaseToken(turn_id, owner_id, 1)
        self.leases[turn_id] = token
        return token

    def renew_run_lease(self, token, *, now, stale_after):
        return object() if self.leases.get(token.turn_id) == token else None

    def release_strict_run_lease(self, token):
        if self.leases.get(token.turn_id) == token:
            self.leases.pop(token.turn_id)

    def request_background_cancel(self, _turn_id, _run_lease=None):
        return False

    def fail_accepted_turn(self, turn_id, _run_lease=None):
        return TurnReceipt(turn_id, "session-test", "operation-test", "failed", 8, False)

    def apply_action(self, action: dict[str, object], _run_lease=None):
        if self._failure is not None:
            raise self._failure
        self.actions.append(action)
        return TurnReceipt(
            str(action["turn_id"]), "session-test", "operation-test",
            self._completed_status, 8, False,
        )

    def presentation_for(self, turn_id: str):
        if self._failure is not None:
            raise self._failure
        return dict(self._content)

    def execution_projection_for(self, turn_id: str, view: str = "simple"):
        assert view == "developer"
        return {
            "schema_version": "1.0.0",
            "turn_id": turn_id,
            "view": "developer",
            "status": "completed",
            "current_sequence": 8,
            "updated_at": "2026-08-27T08:00:00+00:00",
            "terminal": True,
            "current_stage": {
                "kind": "completed", "status": "completed",
                "label": "已完成", "detail": "测试执行已经完成。",
            },
            "stages": [
                {"kind": "context", "status": "completed", "label": "准备上下文", "completed_count": 1},
                {"kind": "planning", "status": "completed", "label": "规划", "completed_count": 1},
                {"kind": "tool", "status": "completed", "label": "使用工具", "completed_count": 1},
                {"kind": "approval", "status": "completed", "label": "审批", "completed_count": 1},
                {"kind": "result", "status": "completed", "label": "生成结果", "completed_count": 1},
            ],
            "next_action": "none",
            "model_steps": [],
            "tool_steps": [],
            "diagnostics": [],
        }


def _install_runtime(monkeypatch, runtime: _FakeTestLabRuntime) -> _FakeTestLabRuntime:
    monkeypatch.setattr(product_developer_test_lab, "get_or_build_ai_runtime", lambda *_args: runtime)
    return runtime


def _recipe() -> dict[str, object]:
    return {
        "id": "recipe-empty-guard",
        "name": "空输入防护",
        "description": "Test Lab matcher and schema fixture",
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


def _save_recipe(client: TestClient) -> dict[str, object]:
    preview = client.post(
        "/api/rebuild/developer-studio/processing-recipes/drafts/preview",
        json={"recipe": _recipe()},
    )
    assert preview.status_code == 200
    saved = client.put(
        "/api/rebuild/developer-studio/processing-recipes/drafts",
        json={
            "recipe": _recipe(),
            "expected_registry_revision": 0,
            "validation_token": preview.json()["validation_token"],
        },
    )
    assert saved.status_code == 200
    return saved.json()


def test_test_lab_prompt_resolves_exact_draft_and_active_authorities(tmp_path, monkeypatch) -> None:
    client, revisions = _seed_authorities(tmp_path)
    runtime = _install_runtime(monkeypatch, _FakeTestLabRuntime())

    draft = client.post("/api/rebuild/developer-studio/test-lab", json=_prompt_request(revisions))
    active = client.post(
        "/api/rebuild/developer-studio/test-lab",
        json=_prompt_request(revisions, source="active"),
    )

    assert draft.status_code == active.status_code == 200
    snapshots = [
        json.loads(request["input"]["text"])
        for request in runtime.submissions
    ]
    assert [snapshot["system_prompt"] for snapshot in snapshots] == ["DRAFT PROMPT V2", "ACTIVE PROMPT V1"]
    assert all(request["desired_outcome"] == "developer_studio.test_lab.result" for request in runtime.submissions)
    assert all(request["capability_policy"]["allowed"] == ["developer_studio.test_lab.execute"] for request in runtime.submissions)
    assert len(runtime.actions) == 2
    assert draft.json()["resolved"]["prompt"]["source"] == "draft"
    assert active.json()["resolved"]["prompt"]["unit_revision"] == revisions["unit"]
    assert active.json()["resolved"]["route"]["runtime_revision"] == revisions["runtime"]
    execution = active.json()["execution"]
    assert execution["turn_id"] == active.json()["turn_id"]
    assert execution["view"] == "developer"
    assert execution["terminal"] is True
    assert execution["current_stage"]["kind"] == "completed"
    assert execution["model_steps"] == []
    assert execution["tool_steps"] == []
    assert "secret" not in str(active.json()).lower() and "endpoint" not in str(active.json()).lower()
    assert "ACTIVE PROMPT V1" not in json.dumps(execution, ensure_ascii=False)
    assert "测试输入" not in json.dumps(execution, ensure_ascii=False)


def test_test_lab_fails_closed_without_confirmation_or_on_revision_drift(tmp_path, monkeypatch) -> None:
    client, revisions = _seed_authorities(tmp_path)
    runtime = _install_runtime(monkeypatch, _FakeTestLabRuntime())
    request = _prompt_request(revisions)
    request["provider_call_confirmed"] = False
    denied = client.post("/api/rebuild/developer-studio/test-lab", json=request)
    assert denied.status_code == 409
    assert denied.json()["provider_call_performed"] is False

    request = _prompt_request(revisions)
    request["expected_runtime_revision"] = revisions["runtime"] - 1
    stale = client.post("/api/rebuild/developer-studio/test-lab", json=request)
    assert stale.status_code == 409
    assert stale.json()["provider_call_performed"] is False
    assert runtime.submissions == []


def test_test_lab_recipe_uses_registry_matcher_and_schema_without_product_writes(tmp_path, monkeypatch) -> None:
    client, revisions = _seed_authorities(tmp_path)
    recipe_status = _save_recipe(client)
    runtime = _install_runtime(monkeypatch, _FakeTestLabRuntime())
    request = {
        "test_type": "recipe",
        "input": "short sample",
        "content_type": "text",
        "trigger": "",
        "provider_call_confirmed": True,
        "expected_runtime_revision": revisions["runtime"],
        "recipe_id": "recipe-empty-guard",
        "recipe_source": "draft",
        "expected_recipe_registry_revision": recipe_status["registry_revision"],
        "expected_recipe_revision": 1,
        "expected_config_revision": revisions["config"],
        "expected_activation_revision": revisions["activation"],
    }
    response = client.post("/api/rebuild/developer-studio/test-lab", json=request)
    assert response.status_code == 200
    payload = response.json()
    assert payload["valid"] is True
    assert payload["resolved"]["recipe"]["source"] == "draft"
    assert payload["resolved"]["recipe"]["recipe_revision"] == 1
    assert payload["resolved"]["output_schema"] is None
    assert payload["resolved"]["result_metadata"] == {"kind": "object", "field_count": 2}
    snapshot = json.loads(runtime.submissions[0]["input"]["text"])
    assert snapshot["system_prompt"] == "ACTIVE PROMPT V1"
    assert snapshot["recipe"]["recipe_id"] == "recipe-empty-guard"
    store = _store(tmp_path)
    for collection in ("sources", "memory_candidates", "documents", "jobs", "memory_objects"):
        assert not store.list(collection)


def test_test_lab_recipe_no_match_skips_provider_and_stale_recipe_fails_closed(tmp_path, monkeypatch) -> None:
    client, revisions = _seed_authorities(tmp_path)
    recipe_status = _save_recipe(client)
    runtime = _install_runtime(monkeypatch, _FakeTestLabRuntime())
    request = {
        "test_type": "recipe",
        "input": "x" * 30,
        "content_type": "text",
        "provider_call_confirmed": True,
        "expected_runtime_revision": revisions["runtime"],
        "recipe_id": "recipe-empty-guard",
        "recipe_source": "draft",
        "expected_recipe_registry_revision": recipe_status["registry_revision"],
        "expected_recipe_revision": 1,
        "expected_config_revision": revisions["config"],
        "expected_activation_revision": revisions["activation"],
    }
    no_match = client.post("/api/rebuild/developer-studio/test-lab", json=request)
    assert no_match.status_code == 200
    assert no_match.json()["skipped"] is True
    request["expected_recipe_revision"] = 0
    stale = client.post("/api/rebuild/developer-studio/test-lab", json=request)
    assert stale.status_code == 409
    assert runtime.submissions == []


def test_test_lab_recipe_returns_metadata_only_turn_presentation(tmp_path, monkeypatch) -> None:
    client, revisions = _seed_authorities(tmp_path)
    recipe_status = _save_recipe(client)
    runtime = _install_runtime(monkeypatch, _FakeTestLabRuntime(content={
        "provider_id": "test-lab-route", "model_name": "test-lab-model",
        "provider_call_performed": True, "output": {"kind": "object", "field_count": 1},
        "usage": {"input_tokens": 3, "output_tokens": 5},
    }))
    response = client.post(
        "/api/rebuild/developer-studio/test-lab",
        json={
            "test_type": "recipe",
            "input": "short sample",
            "content_type": "text",
            "provider_call_confirmed": True,
            "expected_runtime_revision": revisions["runtime"],
            "recipe_id": "recipe-empty-guard",
            "recipe_source": "draft",
            "expected_recipe_registry_revision": recipe_status["registry_revision"],
            "expected_recipe_revision": 1,
            "expected_config_revision": revisions["config"],
            "expected_activation_revision": revisions["activation"],
        },
    )
    assert response.status_code == 200
    assert response.json()["valid"] is True
    assert response.json()["raw"] == ""
    assert response.json()["parsed"] is None
    assert response.json()["resolved"]["result_metadata"] == {"kind": "object", "field_count": 1}
    assert len(runtime.submissions) == len(runtime.actions) == 1


def test_test_lab_runtime_failure_is_sanitized_and_not_claimed_as_a_call(tmp_path, monkeypatch) -> None:
    client, revisions = _seed_authorities(tmp_path)
    secret = "sk-f608ed77e172482c81eb316a65b02e4b"
    _install_runtime(monkeypatch, _FakeTestLabRuntime(failure=RuntimeError(f"Authorization: Bearer {secret} denied")))
    response = client.post(
        "/api/rebuild/developer-studio/test-lab",
        json=_prompt_request(revisions),
    )
    assert response.status_code == 409
    assert response.json()["provider_call_performed"] is False
    assert secret not in str(response.json())
    assert "REDACTED" in response.json()["reason"]


def test_test_lab_unknown_effect_reports_dispatched_and_not_retry_safe(tmp_path, monkeypatch) -> None:
    client, revisions = _seed_authorities(tmp_path)
    _install_runtime(monkeypatch, _FakeTestLabRuntime(completed_status="failed"))

    response = client.post(
        "/api/rebuild/developer-studio/test-lab",
        json=_prompt_request(revisions),
    )

    assert response.status_code == 409
    assert response.json()["provider_call_performed"] is True
    assert response.json()["effect_certainty"] == "unknown"
    assert response.json()["retry_safe"] is False


def test_test_lab_pipeline_uses_a_single_governed_turn(tmp_path, monkeypatch) -> None:
    client, revisions = _seed_authorities(tmp_path)
    runtime = _install_runtime(monkeypatch, _FakeTestLabRuntime(content={
        "provider_id": "test-lab-route", "model_name": "test-lab-model",
        "provider_call_performed": True, "output": {"kind": "object", "field_count": 3},
        "usage": {"input_tokens": 6, "output_tokens": 9},
        "steps": [
            {"stage": "input-understanding", "output": {"kind": "object", "field_count": 2}},
            {"stage": "structuring", "output": {"kind": "object", "field_count": 3}},
        ],
    }))
    response = client.post(
        "/api/rebuild/developer-studio/test-lab",
        json={
            "test_type": "pipeline",
            "input": "两步测试",
            "provider_call_confirmed": True,
            "route_key": "intake.classification",
            "expected_runtime_revision": revisions["runtime"],
        },
    )
    assert response.status_code == 200
    assert [item["stage"] for item in response.json()["steps"]] == [
        "input-understanding", "structuring",
    ]
    assert len(runtime.submissions) == len(runtime.actions) == 1
    snapshot = json.loads(runtime.submissions[0]["input"]["text"])
    assert snapshot["test_type"] == "pipeline"
    assert snapshot["route"]["route_key"] == "intake.classification"


def test_test_lab_real_loopback_provider_uses_one_keyless_governed_turn(tmp_path) -> None:
    """The real tiered gateway keeps authoritative input but projects no model content."""
    transport = OpenAITransportFixture(expected_model="test-lab-model", fault=None)
    transport.start()
    try:
        client, revisions = _seed_authorities(tmp_path, base_url=transport.base_url)
        routing = ModelRoutingProfileStore(tmp_path)
        routing.update(
            expected_revision=routing.get().profile.revision,
            rules_version=1,
            text_default_tier="standard",
            tier_routes={
                "fast": None,
                "standard": "intake.classification",
                "deep": None,
                "vision": None,
                "image_generation": None,
            },
        )
        boundary = ProjectBoundaryProfileStore(tmp_path).get("default").profile
        ProjectCapabilityProfileStore(tmp_path).update(
            "default",
            expected_revision=0,
            boundary_profile_id=boundary.profile_id,
            boundary_profile_revision=boundary.revision,
            preferred_model_tier="standard",
        )

        prompt_canary = "PROMPT-TEST-LAB-LOOPBACK-CANARY"
        input_canary = "INPUT-TEST-LAB-LOOPBACK-CANARY"
        _save_prompt(
            client,
            expected_revision=revisions["config"],
            content=prompt_canary,
            version=3,
        )
        response = client.post(
            "/api/rebuild/developer-studio/test-lab",
            json={
                **_prompt_request({
                    **revisions,
                    "config": revisions["config"] + 1,
                }),
                "input": input_canary,
            },
        )

        assert response.status_code == 200, response.json()
        payload = response.json()
        assert payload["provider_call_performed"] is True
        assert payload["raw"] == ""
        assert payload["parsed"] is None
        assert payload["resolved"]["route"]["route_key"] == "intake.classification"
        assert payload["resolved"]["route"]["model_name"] == "test-lab-model"
        assert payload["resolved"]["result_metadata"] == {"kind": "object", "field_count": 1}
        assert transport.requests == [{
            "model": "test-lab-model", "path": "/v1/chat/completions", "ordinal": 1,
            "has_authorization": False,
        }]
        wire_body = json.dumps(transport.bodies, ensure_ascii=False)
        assert input_canary in wire_body
        assert prompt_canary in wire_body

        runtime = client.app.state.ai_runtime
        events = tuple(runtime.events_after(payload["turn_id"]))
        nested_requests = [
            event for event in events
            if event["type"] == "model.requested"
            and event.get("correlation", {}).get("tool_call_id") is not None
        ]
        assert len(nested_requests) == 1
        assert nested_requests[0]["data"]["model_call_purpose"] == "primary"
        assert [event["type"] for event in events].count("model.attempt.dispatched") == 1
        assert [event["type"] for event in events].count("model.attempt.terminal") == 1
        assert [event["type"] for event in events].count("turn.completed") == 1
        projection = runtime.execution_projection_for(payload["turn_id"], view="developer")
        nested = next(
            item for item in projection["model_steps"]
            if item["parent_tool_call_id"] is not None
        )
        observation = nested["dispatch_authority"]
        assert nested["dispatch_authority_status"] == "recorded"
        assert observation["outcome"] == "completed"
        assert observation["wait_ms"] >= 0 and observation["hold_ms"] >= 0
        restarted_client = _client(tmp_path)
        restarted = get_or_build_ai_runtime(
            SimpleNamespace(app=restarted_client.app), _container(tmp_path),
        )
        restarted_projection = restarted.execution_projection_for(
            payload["turn_id"], view="developer",
        )
        restarted_nested = next(
            item for item in restarted_projection["model_steps"]
            if item["parent_tool_call_id"] is not None
        )
        assert restarted_nested["dispatch_authority"] == observation
        database = tmp_path / ".rebuild-data" / "ai-turns.sqlite3"
        with sqlite3.connect(database) as connection:
            stored_events = [row[0] for row in connection.execute(
                "SELECT event_json FROM ai_turn_events WHERE turn_id=? ORDER BY sequence",
                (payload["turn_id"],),
            )]
            immutable_payloads = [row[0] for row in connection.execute(
                "SELECT payload_json FROM ai_turn_immutable_payloads WHERE turn_id=?",
                (payload["turn_id"],),
            )]
            turn_request = connection.execute(
                "SELECT request_json FROM ai_turns WHERE turn_id=?",
                (payload["turn_id"],),
            ).fetchone()[0]
            projected_payloads = [row[0] for row in connection.execute(
                "SELECT payload_json FROM ai_turn_payloads WHERE turn_id=?",
                (payload["turn_id"],),
            )]
        assert len(stored_events) == len(events)
        assert immutable_payloads
        assert input_canary in turn_request
        assert prompt_canary in turn_request
        assert "fallback" not in "\n".join(projected_payloads)
        serialized_events = "\n".join([json.dumps(event, ensure_ascii=False) for event in events] + stored_events)
        for forbidden in (input_canary, prompt_canary, "fallback"):
            assert forbidden not in json.dumps(payload, ensure_ascii=False)
            assert forbidden not in serialized_events
    finally:
        transport.close()


def test_test_lab_real_loopback_single_flight_waits_and_persists_safe_observations(tmp_path) -> None:
    """The second real Provider call cannot start before the first releases the one fence."""
    transport = OpenAITransportFixture(
        expected_model="test-lab-model", fault=None, block_first_request=True,
    )
    transport.start()
    try:
        client, revisions = _seed_authorities(tmp_path, base_url=transport.base_url)
        routing = ModelRoutingProfileStore(tmp_path)
        routing.update(
            expected_revision=routing.get().profile.revision,
            rules_version=1,
            text_default_tier="standard",
            tier_routes={
                "fast": None, "standard": "intake.classification", "deep": None,
                "vision": None, "image_generation": None,
            },
        )
        boundary = ProjectBoundaryProfileStore(tmp_path).get("default").profile
        ProjectCapabilityProfileStore(tmp_path).update(
            "default", expected_revision=0, boundary_profile_id=boundary.profile_id,
            boundary_profile_revision=boundary.revision, preferred_model_tier="standard",
        )
        _save_prompt(
            client, expected_revision=revisions["config"],
            content="SINGLE-FLIGHT-PROMPT-CANARY", version=3,
        )
        request = _prompt_request({**revisions, "config": revisions["config"] + 1})
        second_client = TestClient(client.app)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(client.post, "/api/rebuild/developer-studio/test-lab", json={
                **request, "input": "SINGLE-FLIGHT-INPUT-A",
            })
            assert transport.first_request_started.wait(timeout=5)
            second = executor.submit(second_client.post, "/api/rebuild/developer-studio/test-lab", json={
                **request, "input": "SINGLE-FLIGHT-INPUT-B",
            })
            sleep(0.2)
            assert len(transport.requests) == 1
            transport.release_first_request.set()
            first_response, second_response = first.result(timeout=10), second.result(timeout=10)

        assert first_response.status_code == second_response.status_code == 200
        assert [item["ordinal"] for item in transport.requests] == [1, 2]
        runtime = client.app.state.ai_runtime
        observations = []
        for response in (first_response, second_response):
            turn_id = response.json()["turn_id"]
            events = tuple(runtime.events_after(turn_id))
            assert [event["type"] for event in events].count("model.attempt.dispatched") == 1
            assert [event["type"] for event in events].count("model.attempt.terminal") == 1
            projection = runtime.execution_projection_for(turn_id, view="developer")
            nested = next(item for item in projection["model_steps"] if item["parent_tool_call_id"] is not None)
            assert nested["dispatch_authority_status"] == "recorded"
            observations.append(nested["dispatch_authority"])
        assert all(item["outcome"] == "completed" for item in observations)
        assert max(item["wait_ms"] for item in observations) >= 0
        serialized = json.dumps(observations, ensure_ascii=False)
        for forbidden in (str(tmp_path), "test-lab-route", "test-lab-model", "SINGLE-FLIGHT-INPUT"):
            assert forbidden not in serialized
    finally:
        transport.release_first_request.set()
        transport.close()


def test_test_lab_video_remains_disabled_without_provider_call(tmp_path, monkeypatch) -> None:
    runtime = _install_runtime(monkeypatch, _FakeTestLabRuntime())
    response = _client(tmp_path).post(
        "/api/rebuild/developer-studio/test-lab",
        json={"test_type": "video", "input": "https://example.com/video.mp4"},
    )
    assert response.status_code == 200
    assert response.json()["skipped"] is True
    assert response.json()["provider_call_performed"] is False
    assert runtime.submissions == []


def test_test_lab_rejects_invalid_type_empty_input_and_get(tmp_path) -> None:
    client = _client(tmp_path)
    assert client.post(
        "/api/rebuild/developer-studio/test-lab", json={"test_type": "invalid", "input": "hello"},
    ).status_code == 400
    assert client.post(
        "/api/rebuild/developer-studio/test-lab", json={"test_type": "prompt", "input": ""},
    ).status_code == 400
    assert client.get("/api/rebuild/developer-studio/test-lab").status_code == 405
