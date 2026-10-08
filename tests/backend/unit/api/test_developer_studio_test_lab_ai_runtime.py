from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.api import developer_studio_test_lab_ai_runtime as runtime
from core.ai_kernel import InMemoryTurnPayloadStore
from core.ai_kernel.dispatcher import ToolProviderFailure
from core.model_gateway import ModelResult


class _Nested:
    def __init__(self) -> None: self.finalized: list[str | None] = []
    def model_call_routed(self, **_kwargs) -> None: pass
    def model_call_started(self, **_kwargs) -> None: pass
    def model_call_completed(self, **_kwargs) -> None: pass
    def model_call_failed(self) -> None: pass
    def model_call_cache_observed(self, **_kwargs) -> None: pass
    def finalize(self, *, error_code: str | None) -> tuple[str, ...]:
        self.finalized.append(error_code)
        return ("crp://default/model-evidence/test-lab-1",)


class _Control:
    remaining_timeout_ms = 30_000
    cancel_requested = False
    def __init__(self) -> None: self.nested = _Nested()
    def checkpoint(self) -> None: pass
    def take_nested_model_handle(self, **_kwargs) -> _Nested: return self.nested


class _Gateway:
    def __init__(self, output: object = None, error: Exception | None = None) -> None:
        self.output, self.error, self.requests = output if output is not None else {"answer": "private"}, error, []
    def invoke(self, request):
        self.requests.append(request)
        if self.error is not None: raise self.error
        return ModelResult(self.output, "provider-test", "model-test", {"input_tokens": 3, "output_tokens": 5})


def _snapshot(*, input_value: str = "private test input", prompt: str = "private system prompt", test_type: str = "prompt") -> dict[str, object]:
    return {
        "schema_version": "1.0.0", "kind": runtime.DEVELOPER_STUDIO_TEST_LAB_SNAPSHOT_KIND,
        "project_id": "project-a", "test_type": test_type, "input": input_value,
        "system_prompt": prompt, "model_capability": "structured",
        "route": {"route_key": "intake.classification", "route_revision": 3, "provider_id": "provider-test", "provider_revision": "provider-revision-2", "model_name": "model-test", "runtime_revision": 4, "evidence_ref": "crp://default/model-routes/intake.classification"},
        "prompt": {"prompt_id": "pt-input", "source": "active", "config_revision": 7, "evidence_ref": "crp://default/prompts/pt-input"},
        "recipe": None,
    }


def _request(snapshot: dict[str, object]) -> dict[str, object]:
    return {
        "turn_id": "turn-test-lab", "scope": {"kind": "project", "project_id": "project-a"},
        "input": {"kind": "text", "text": json.dumps(snapshot, ensure_ascii=False), "refs": []},
        "privacy": {"mode": "remote_allowed", "allow_remote": True}, "execution_context": _Control(),
    }


def _routing(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "load_turn_model_routing_binding", lambda *_args, **_kwargs: SimpleNamespace(
        snapshot_ref="crp://default/model-routing/frozen-a", snapshot_revision="routing-revision-a",
        snapshot={"selected": {"route_key": "intake.classification", "route_revision": 3, "provider_id": "provider-test", "provider_revision": "provider-revision-2", "model_name": "model-test"}},
        parameters=lambda: {"_routing": "frozen"},
    ))


def test_planner_freezes_snapshot_without_placing_private_input_in_tool_arguments() -> None:
    payloads, request = InMemoryTurnPayloadStore(), _request(_snapshot())

    decision = runtime.DeveloperStudioTestLabTurnPlanner().plan(request, [], [], payloads)

    assert decision["capability_id"] == runtime.DEVELOPER_STUDIO_TEST_LAB_CAPABILITY
    assert set(decision["arguments"]) == {"snapshot_ref"}
    stored = payloads.get(decision["arguments"]["snapshot_ref"])
    assert stored["input"] == "private test input"
    definition = runtime.developer_studio_test_lab_capability_definition()
    assert definition.tool_definition is not None
    assert definition.tool_definition.nested_model_handle_budget == 2


def test_capability_uses_nested_gateway_and_persists_metadata_only_result(monkeypatch) -> None:
    _routing(monkeypatch)
    payloads, request, gateway = InMemoryTurnPayloadStore(), _request(_snapshot()), _Gateway({"answer": "private response", "details": "hidden"})
    decision = runtime.DeveloperStudioTestLabTurnPlanner().plan(request, [], [], payloads)
    request["arguments"] = decision["arguments"]

    result = runtime.DeveloperStudioTestLabCapability(gateway=gateway, receipt_store=payloads).invoke(request)

    assert gateway.requests[0].metadata_sink is request["execution_context"].nested
    assert gateway.requests[0].parameters["_routing"] == "frozen"
    assert gateway.requests[0].parameters["response_format"] == {"type": "json_object"}
    assert result["result"]["content"]["output"] == {"kind": "object", "field_count": 2}
    stored = payloads.get_immutable_payload(request["turn_id"], "developer-studio-test-lab-receipt")
    assert stored is not None
    serialized = json.dumps(stored[1], ensure_ascii=False)
    assert "private test input" not in serialized and "private system prompt" not in serialized
    assert "private response" not in serialized and "details" not in serialized
    assert request["execution_context"].nested.finalized == [None]


def test_capability_replays_immutable_metadata_receipt_without_second_model_call(monkeypatch) -> None:
    _routing(monkeypatch)
    payloads, request, gateway = InMemoryTurnPayloadStore(), _request(_snapshot()), _Gateway()
    request["arguments"] = runtime.DeveloperStudioTestLabTurnPlanner().plan(request, [], [], payloads)["arguments"]
    capability = runtime.DeveloperStudioTestLabCapability(gateway=gateway, receipt_store=payloads)

    first, second = capability.invoke(request), capability.invoke(request)

    assert gateway.requests and len(gateway.requests) == 1
    assert first["receipt_ref"] == second["receipt_ref"]
    assert "replayed" in second["summary"].lower()


def test_invalid_scope_or_sensitive_snapshot_fails_before_gateway(monkeypatch) -> None:
    _routing(monkeypatch)
    snapshot, gateway = _snapshot(), _Gateway()
    request = _request(snapshot)
    request["scope"]["project_id"] = "project-b"
    with pytest.raises(ValueError, match="scope drifted"):
        runtime.DeveloperStudioTestLabTurnPlanner().plan(request, [], [], InMemoryTurnPayloadStore())
    snapshot["route"]["api_key"] = "forbidden"
    with pytest.raises(ValueError):
        runtime.validate_developer_studio_test_lab_snapshot(snapshot)
    assert gateway.requests == []


def test_gateway_failure_is_unknown_effect_and_finalizes_nested_handle(monkeypatch) -> None:
    _routing(monkeypatch)
    payloads, request = InMemoryTurnPayloadStore(), _request(_snapshot())
    request["arguments"] = runtime.DeveloperStudioTestLabTurnPlanner().plan(request, [], [], payloads)["arguments"]
    capability = runtime.DeveloperStudioTestLabCapability(gateway=_Gateway(error=RuntimeError("wire interrupted")), receipt_store=payloads)

    with pytest.raises(ToolProviderFailure) as failure:
        capability.invoke(request)

    assert failure.value.effect_certainty == "unknown"
    assert request["execution_context"].nested.finalized == ["ai.nested_model_failed"]


def test_pipeline_runs_two_nested_calls_with_one_frozen_route(monkeypatch) -> None:
    _routing(monkeypatch)
    payloads, request, gateway = InMemoryTurnPayloadStore(), _request(_snapshot(test_type="pipeline")), _Gateway({"stage": "ok"})
    request["arguments"] = runtime.DeveloperStudioTestLabTurnPlanner().plan(request, [], [], payloads)["arguments"]

    result = runtime.DeveloperStudioTestLabCapability(gateway=gateway, receipt_store=payloads).invoke(request)

    assert len(gateway.requests) == 2
    assert [item["stage"] for item in result["result"]["content"]["steps"]] == [
        "input-understanding", "structuring",
    ]
    assert all(item.parameters["_routing"] == "frozen" for item in gateway.requests)


def test_route_drift_fails_before_model_dispatch(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "load_turn_model_routing_binding", lambda *_args, **_kwargs: SimpleNamespace(
        snapshot_ref="crp://default/model-routing/frozen-b", snapshot_revision="routing-revision-b",
        snapshot={"selected": {"route_key": "search.answer", "route_revision": 3, "provider_id": "provider-test", "provider_revision": "provider-revision-2", "model_name": "model-test"}}, parameters=lambda: {"_routing": "frozen"},
    ))
    payloads, request, gateway = InMemoryTurnPayloadStore(), _request(_snapshot()), _Gateway()
    request["arguments"] = runtime.DeveloperStudioTestLabTurnPlanner().plan(request, [], [], payloads)["arguments"]

    with pytest.raises(ToolProviderFailure) as failure:
        runtime.DeveloperStudioTestLabCapability(gateway=gateway, receipt_store=payloads).invoke(request)

    assert failure.value.effect_certainty == "confirmed_none"
    assert gateway.requests == []
