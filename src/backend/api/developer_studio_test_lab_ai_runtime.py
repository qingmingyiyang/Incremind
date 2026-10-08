"""Governed, side-effect-free Developer Studio Test Lab AI Turn capability.

The HTTP adapter owns authority resolution and freezes a snapshot before it
submits a Turn.  This module intentionally owns neither Developer Studio
configuration nor provider construction: it only replays the frozen snapshot
through the same Model Gateway and routing binding as other Turn consumers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
import json
import re

from backend.api.ai_execution_control import begin_nested_model_call, execution_control_from
from backend.api.turn_model_routing_binding import load_turn_model_routing_binding
from core.ai_kernel import CapabilityDefinition, TurnPayloadStorePort, validate_turn_presentation_artifact
from core.ai_kernel.dispatcher import ToolProviderFailure
from core.ai_tooling import tool_from_capability
from core.model_gateway import ModelGatewayPort, ModelRequest


DEVELOPER_STUDIO_TEST_LAB_SNAPSHOT_KIND = "developer_studio.test_lab.snapshot.v1"
DEVELOPER_STUDIO_TEST_LAB_OUTCOME = "developer_studio.test_lab.result"
DEVELOPER_STUDIO_TEST_LAB_CAPABILITY = "developer_studio.test_lab.execute"
_RECEIPT_KIND = "developer-studio-test-lab-receipt"
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_CRP_REF = re.compile(r"^crp://[a-z0-9][a-z0-9_-]{0,63}/[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")
_SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|authorization|cookie|password|secret|token|endpoint|base[_-]?url)", re.I)
_TEST_TYPES = frozenset({"prompt", "recipe", "pipeline", "search"})


def developer_studio_test_lab_capability_definition() -> CapabilityDefinition:
    """The operation is approval-gated because it can issue remote model egress."""
    definition = CapabilityDefinition(
        DEVELOPER_STUDIO_TEST_LAB_CAPABILITY,
        1,
        "external",
        True,
        "receipt_required",
        "crp://default/contracts/developer-studio-test-lab-request.schema.json",
        "crp://default/contracts/developer-studio-test-lab-result.schema.json",
    )
    return replace(
        definition,
        tool_definition=replace(
            tool_from_capability(definition),
            nested_model_handle_budget=2,
        ),
    )


class DeveloperStudioTestLabTurnPlanner:
    """Freeze one test snapshot once, invoke once, then expose only its receipt."""

    def plan(
        self,
        request: Mapping[str, object],
        events: Sequence[Mapping[str, object]],
        capabilities: Sequence[CapabilityDefinition],
        payloads: TurnPayloadStorePort,
        execution_control: object | None = None,
    ) -> Mapping[str, object]:
        del capabilities, execution_control
        completed = _completed(events)
        if completed is not None:
            data = completed.get("data")
            return {
                "type": "complete",
                "summary": "Developer Studio Test Lab completed",
                "payload_ref": data.get("payload_ref") if isinstance(data, Mapping) else None,
                "evidence_refs": list(data.get("evidence_refs") or ()) if isinstance(data, Mapping) else [],
            }
        turn_id = _required(request.get("turn_id"), "turn id")
        snapshot = developer_studio_test_lab_snapshot_from_turn(request)
        snapshot_ref = payloads.put(
            turn_id, DEVELOPER_STUDIO_TEST_LAB_SNAPSHOT_KIND, snapshot,
        )
        return {
            "type": "tool",
            "capability_id": DEVELOPER_STUDIO_TEST_LAB_CAPABILITY,
            "arguments": {"snapshot_ref": snapshot_ref},
        }


class DeveloperStudioTestLabCapability:
    """Run a frozen test through ModelGatewayPort and persist metadata only."""

    def __init__(self, *, gateway: ModelGatewayPort | None, receipt_store: TurnPayloadStorePort) -> None:
        self._gateway = gateway
        self._receipt_store = receipt_store

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        turn_id = _required(request.get("turn_id"), "turn id")
        snapshot_ref = _required(_arguments(request).get("snapshot_ref"), "snapshot ref")
        snapshot = self._snapshot(turn_id, snapshot_ref)
        existing = self._receipt_store.get_immutable_payload(turn_id, _RECEIPT_KIND)
        if existing is not None:
            receipt_ref, receipt = existing
            if not isinstance(receipt, Mapping) or receipt.get("snapshot_ref") != snapshot_ref:
                raise ValueError("Developer Studio Test Lab receipt identity drifted")
            artifact = receipt.get("artifact")
            refs = receipt.get("evidence_refs")
            if not isinstance(artifact, Mapping) or not isinstance(refs, list):
                raise ValueError("Developer Studio Test Lab receipt is invalid")
            return {
                "summary": "Developer Studio Test Lab replayed from its immutable receipt",
                "receipt_ref": receipt_ref, "payload_ref": None,
                "evidence_refs": list(refs), "result": dict(artifact),
            }
        if self._gateway is None:
            raise ValueError("Developer Studio Test Lab model gateway is unavailable")
        routing = load_turn_model_routing_binding(
            self._receipt_store, request, required_capability=str(snapshot["model_capability"]),
        )
        selected = routing.snapshot.get("selected")
        route = snapshot.get("route")
        if (
            not isinstance(selected, Mapping)
            or not isinstance(route, Mapping)
            or any(
                selected.get(field) != route.get(field)
                for field in (
                    "route_key", "route_revision", "provider_id",
                    "provider_revision", "model_name",
                )
            )
        ):
            raise ToolProviderFailure(
                "developer_studio_test_lab.route_drift", effect_certainty="confirmed_none",
            )
        result, model_evidence_refs = self._invoke_model(
            request,
            routing=routing,
            invocation_key=(
                "input-understanding"
                if snapshot["test_type"] == "pipeline"
                else str(snapshot["test_type"])
            ),
            system_prompt=str(snapshot["system_prompt"]),
            user_input=str(snapshot["input"]),
        )
        steps: list[dict[str, object]] | None = None
        all_model_evidence_refs = list(model_evidence_refs)
        if snapshot["test_type"] == "pipeline":
            first_shape = _output_shape(result.output)
            second, second_refs = self._invoke_model(
                request,
                routing=routing,
                invocation_key="structuring",
                system_prompt="你是 Chrip_OS 的结构化测试模块。基于输入理解结果，输出 JSON：{ title, summary, tags }。",
                user_input=json.dumps(result.output, ensure_ascii=False),
            )
            all_model_evidence_refs.extend(second_refs)
            steps = [
                {"stage": "input-understanding", "output": first_shape},
                {"stage": "structuring", "output": _output_shape(second.output)},
            ]
            result = second
        artifact = developer_studio_test_lab_presentation(
            snapshot=snapshot,
            provider=result.provider,
            model=result.model,
            output=result.output,
            usage=result.usage,
            steps=steps,
        )
        evidence_refs = _refs((*_snapshot_evidence_refs(snapshot), routing.snapshot_ref, *all_model_evidence_refs))
        receipt = {
            "schema_version": "1.0.0", "kind": "developer_studio.test_lab.receipt",
            "turn_id": turn_id, "snapshot_ref": snapshot_ref,
            "route_snapshot_ref": routing.snapshot_ref,
            "route_snapshot_revision": routing.snapshot_revision,
            "artifact": artifact, "evidence_refs": list(evidence_refs),
        }
        receipt_ref = self._receipt_store.get_or_create_immutable_payload(turn_id, _RECEIPT_KIND, receipt)
        return {
            "summary": "Developer Studio Test Lab completed", "receipt_ref": receipt_ref,
            "payload_ref": None, "evidence_refs": list(evidence_refs), "result": artifact,
        }

    def _invoke_model(
        self,
        request: Mapping[str, object],
        *,
        routing: object,
        invocation_key: str,
        system_prompt: str,
        user_input: str,
    ) -> tuple[object, tuple[str, ...]]:
        nested = begin_nested_model_call(
            request,
            invocation_key=f"test-lab:{invocation_key}",
            purpose="primary",
        )
        try:
            result = self._gateway.invoke(ModelRequest(  # type: ignore[union-attr]
                capability="structured",
                input=user_input,
                parameters={
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_input},
                    ],
                    "response_format": {"type": "json_object"},
                    **routing.parameters(),  # type: ignore[attr-defined]
                },
                privacy_scope="remote_allowed",
                execution_control=execution_control_from(request),
                metadata_sink=nested,
                wire_attempt_sink=nested,
            ))
        except Exception as error:
            nested.finalize(error_code="ai.nested_model_failed")
            raise ToolProviderFailure(
                "developer_studio_test_lab.model_failed", effect_certainty="unknown",
            ) from error
        return result, nested.finalize(error_code=None)

    def _snapshot(self, turn_id: str, snapshot_ref: str) -> dict[str, object]:
        if not snapshot_ref.startswith(f"crp://session/{turn_id}/{DEVELOPER_STUDIO_TEST_LAB_SNAPSHOT_KIND}/"):
            raise ValueError("Developer Studio Test Lab snapshot is unavailable")
        return validate_developer_studio_test_lab_snapshot(self._receipt_store.get(snapshot_ref))


def developer_studio_test_lab_snapshot_from_turn(request: Mapping[str, object]) -> dict[str, object]:
    """Read the caller's immutable Turn input; no current Studio authority is read."""
    input_payload = request.get("input")
    if not isinstance(input_payload, Mapping) or input_payload.get("kind") != "text":
        raise ValueError("Developer Studio Test Lab Turn input is invalid")
    text = input_payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Developer Studio Test Lab snapshot is required")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError("Developer Studio Test Lab snapshot is invalid JSON") from error
    snapshot = validate_developer_studio_test_lab_snapshot(value)
    scope = request.get("scope")
    if not isinstance(scope, Mapping) or snapshot["project_id"] != scope.get("project_id"):
        raise ValueError("Developer Studio Test Lab project scope drifted")
    return snapshot


def validate_developer_studio_test_lab_snapshot(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version", "kind", "project_id", "test_type", "input", "system_prompt",
        "model_capability", "route", "prompt", "recipe",
    }:
        raise ValueError("Developer Studio Test Lab snapshot shape is invalid")
    if value.get("schema_version") != "1.0.0" or value.get("kind") != DEVELOPER_STUDIO_TEST_LAB_SNAPSHOT_KIND:
        raise ValueError("Developer Studio Test Lab snapshot version is invalid")
    project_id = _required(value.get("project_id"), "project id")
    if not _IDENTITY.fullmatch(project_id):
        raise ValueError("Developer Studio Test Lab project identity is invalid")
    test_type = value.get("test_type")
    if test_type not in _TEST_TYPES:
        raise ValueError("Developer Studio Test Lab test type is invalid")
    user_input, prompt = _text(value.get("input"), "input", 8_000), _text(value.get("system_prompt"), "system prompt", 16_000)
    capability = value.get("model_capability")
    if capability not in {"structured", "text"}:
        raise ValueError("Developer Studio Test Lab model capability is invalid")
    route = _authority(value.get("route"), {
        "route_key", "route_revision", "provider_id", "provider_revision",
        "model_name", "runtime_revision", "evidence_ref",
    }, "route")
    prompt_evidence = _authority(value.get("prompt"), {"prompt_id", "source", "config_revision", "evidence_ref"}, "prompt")
    recipe_value = value.get("recipe")
    recipe = None if recipe_value is None else _authority(recipe_value, {"recipe_id", "recipe_revision", "registry_revision", "evidence_ref"}, "recipe")
    _reject_sensitive(value)
    return {
        "schema_version": "1.0.0", "kind": DEVELOPER_STUDIO_TEST_LAB_SNAPSHOT_KIND,
        "project_id": project_id, "test_type": test_type, "input": user_input,
        "system_prompt": prompt, "model_capability": capability,
        "route": route, "prompt": prompt_evidence, "recipe": recipe,
    }


def developer_studio_test_lab_presentation(*, snapshot: Mapping[str, object], provider: str, model: str, output: object, usage: Mapping[str, int], steps: Sequence[Mapping[str, object]] | None = None) -> dict[str, object]:
    """Return a safe view: model response content is deliberately never persisted here."""
    shape = _output_shape(output)
    sanitized_usage = {key: value for key, value in usage.items() if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool) and value >= 0}
    return validate_turn_presentation_artifact({
        "schema_version": "1.0.0", "kind": DEVELOPER_STUDIO_TEST_LAB_OUTCOME,
        "content": {
            "status": "completed", "test_type": snapshot["test_type"],
            "provider_id": _safe_identity(provider), "model_name": _safe_identity(model),
            "provider_call_performed": True, "output": shape, "usage": sanitized_usage,
            "steps": [dict(item) for item in steps] if steps is not None else None,
            "resolved": _presentation_authorities(snapshot),
        },
    })


def _presentation_authorities(snapshot: Mapping[str, object]) -> dict[str, object]:
    result = {name: {key: value for key, value in authority.items() if key != "evidence_ref"} for name, authority in (("route", snapshot["route"]), ("prompt", snapshot["prompt"])) if isinstance(authority, Mapping)}
    if isinstance(snapshot.get("recipe"), Mapping):
        result["recipe"] = {key: value for key, value in snapshot["recipe"].items() if key != "evidence_ref"}
    return result


def _output_shape(output: object) -> dict[str, object]:
    if isinstance(output, Mapping): return {"kind": "object", "field_count": len(output)}
    if isinstance(output, str): return {"kind": "text", "character_count": len(output)}
    if isinstance(output, (list, tuple)): return {"kind": "list", "item_count": len(output)}
    return {"kind": type(output).__name__}


def _authority(value: object, fields: set[str], name: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"Developer Studio Test Lab {name} evidence is invalid")
    result = dict(value)
    for key, item in result.items():
        if key in {"runtime_revision", "route_revision", "config_revision", "recipe_revision", "registry_revision"} and (not isinstance(item, int) or isinstance(item, bool) or item < 1):
            raise ValueError(f"Developer Studio Test Lab {name} revision is invalid")
        if key == "evidence_ref" and (not isinstance(item, str) or _CRP_REF.fullmatch(item) is None):
            raise ValueError(f"Developer Studio Test Lab {name} evidence ref is invalid")
        if key not in {"evidence_ref", "runtime_revision", "route_revision", "config_revision", "recipe_revision", "registry_revision"}:
            _required(item, f"{name} {key}")
    return result


def _snapshot_evidence_refs(snapshot: Mapping[str, object]) -> tuple[str, ...]:
    refs = [snapshot[name]["evidence_ref"] for name in ("route", "prompt") if isinstance(snapshot.get(name), Mapping)]
    if isinstance(snapshot.get("recipe"), Mapping): refs.append(snapshot["recipe"]["evidence_ref"])
    return _refs(tuple(refs))


def _arguments(request: Mapping[str, object]) -> Mapping[str, object]:
    value = request.get("arguments")
    if not isinstance(value, Mapping) or set(value) != {"snapshot_ref"}: raise ValueError("Developer Studio Test Lab arguments are invalid")
    return value


def _completed(events: Sequence[Mapping[str, object]]) -> Mapping[str, object] | None:
    return next((event for event in reversed(events) if event.get("type") == "tool.completed" and isinstance(event.get("data"), Mapping) and event["data"].get("capability_id") == DEVELOPER_STUDIO_TEST_LAB_CAPABILITY), None)


def _required(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip(): raise ValueError(f"Developer Studio Test Lab {label} is required")
    return value.strip()


def _text(value: object, label: str, limit: int) -> str:
    text = _required(value, label)
    if len(text) > limit: raise ValueError(f"Developer Studio Test Lab {label} is too large")
    return text


def _safe_identity(value: object) -> str:
    return str(value).strip()[:128] or "model-gateway"


def _refs(values: tuple[object, ...]) -> tuple[str, ...]:
    refs = tuple(dict.fromkeys(str(item) for item in values))
    if any(_CRP_REF.fullmatch(ref) is None for ref in refs): raise ValueError("Developer Studio Test Lab evidence ref is invalid")
    return refs


def _reject_sensitive(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _SENSITIVE_KEY.search(str(key)): raise ValueError("Developer Studio Test Lab snapshot contains sensitive execution data")
            _reject_sensitive(item)
    elif isinstance(value, (list, tuple)):
        for item in value: _reject_sensitive(item)
