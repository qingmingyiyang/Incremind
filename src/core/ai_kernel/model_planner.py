from __future__ import annotations

from collections.abc import Mapping, Sequence
import json

from core.model_gateway import ModelExecutionControlPort, ModelGatewayPort, ModelRequest
from core.context_graph import (
    ContextBindingPayloadError,
    ContextCompiler,
    context_binding_from_payload,
    context_binding_model_projection,
)

from .agent_contracts import AgentContractError, agent_role_brief_from_payload
from .turn_templates import turn_purpose

from .ports import CapabilityDefinition, TurnPayloadStorePort
from .context_manifest import (
    ContextManifestError,
    compacted_source_entry_ids,
    context_manifest_from_payload,
)
from .model_routing_snapshot_contract import (
    ModelRoutingSnapshotContractError,
    planner_routing_snapshot_revision,
    validate_planner_routing_snapshot,
)
from .context_entries import TypedContextEntryError, typed_context_model_projection


class ModelPlannerError(ValueError):
    pass


class ModelGatewayAgentPlanner:
    """Provider-neutral planner. Provider selection and egress stay behind ModelGatewayPort."""

    def __init__(self, gateway: ModelGatewayPort) -> None:
        self._gateway = gateway

    def plan(
        self,
        request: Mapping[str, object],
        events: Sequence[Mapping[str, object]],
        capabilities: Sequence[CapabilityDefinition],
        payloads: TurnPayloadStorePort,
        execution_control: ModelExecutionControlPort | None = None,
    ) -> Mapping[str, object]:
        context = {
            "goal": request.get("desired_outcome"),
            "input": request.get("input"),
            "scope": request.get("scope"),
            "capabilities": [_capability(item) for item in capabilities],
            "events": [_materialize_event(event, payloads) for event in events],
            "context": _materialize_model_context(events, payloads),
            "decision_contract": {
                "type": "complete | tool | tools",
                "complete": {"summary": "string", "payload_ref": "crp://... or null", "evidence_refs": []},
                "tool": {"capability_id": "registered id", "arguments": {}},
                "tools": {"calls": [{"capability_id": "registered id", "arguments": {}}],
                          "limit": 16, "order": "results preserve the call order"},
            },
        }
        turn_id = request.get("turn_id")
        if isinstance(turn_id, str) and turn_id:
            stored_role = payloads.get_immutable_payload(turn_id, "agent-role-brief-v1")
            if stored_role is not None:
                try:
                    context["role"] = agent_role_brief_from_payload(stored_role[1])
                except AgentContractError as error:
                    raise ModelPlannerError("frozen agent role brief is invalid") from error
        privacy_scope = _privacy_scope(request.get("privacy"))
        routing_parameters = _turn_routing_parameters(request, payloads)
        result = self._gateway.invoke(ModelRequest(
            capability="structured",
            purpose=turn_purpose(request),
            input=json.dumps(context, ensure_ascii=False, separators=(",", ":")),
            parameters={
                "temperature": 0,
                "response_format": {"type": "json_object"},
                **routing_parameters,
            },
            privacy_scope=privacy_scope,
            execution_control=execution_control,
            metadata_sink=execution_control,  # type: ignore[arg-type]
        ))
        if not isinstance(result.output, Mapping):
            raise ModelPlannerError("model planner output must be an object")
        return _validate_decision(dict(result.output), {item.capability_id for item in capabilities})


def _turn_routing_parameters(
    request: Mapping[str, object], payloads: TurnPayloadStorePort,
) -> dict[str, object]:
    """Copy the pre-frozen routing authority into the gateway-only parameters.

    The Planner must not reproject routing from mutable request state.  The
    Tiered gateway owns schema/revision validation immediately before egress;
    this boundary only reads the immutable Turn payload and makes a detached
    JSON copy so a caller cannot drift the in-flight parameters after planning.
    """

    turn_id = request.get("turn_id")
    scope = request.get("scope")
    if not isinstance(turn_id, str) or not turn_id.strip() or not isinstance(scope, Mapping):
        if request.get("desired_outcome") == "context.evaluate":
            raise ModelPlannerError("context evaluation routing authority is unavailable")
        return {}
    project_id = scope.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        if request.get("desired_outcome") == "context.evaluate":
            raise ModelPlannerError("context evaluation routing authority is unavailable")
        return {}
    stored = payloads.get_immutable_payload(turn_id, "turn-model-routing-snapshot-v1")
    if stored is None:
        if request.get("desired_outcome") == "context.evaluate":
            raise ModelPlannerError("context evaluation routing authority is unavailable")
        return {}
    snapshot_ref, snapshot = stored
    base_ref = f"crp://session/{turn_id}/turn-model-routing-snapshot-v1"
    if (
        not isinstance(snapshot_ref, str)
        or not (snapshot_ref == base_ref or snapshot_ref.startswith(base_ref + "/"))
    ):
        raise ModelPlannerError("Turn model routing snapshot reference is invalid")
    try:
        validated_snapshot = validate_planner_routing_snapshot(snapshot)
        snapshot_revision = planner_routing_snapshot_revision(validated_snapshot)
    except ModelRoutingSnapshotContractError as error:
        raise ModelPlannerError("Turn model routing snapshot is invalid") from error
    turn = validated_snapshot.get("turn")
    project = validated_snapshot.get("project")
    requirement = validated_snapshot.get("requirement")
    expected_privacy_scope = _privacy_scope(request.get("privacy"))
    if (
        not isinstance(turn, Mapping)
        or not isinstance(project, Mapping)
        or not isinstance(requirement, Mapping)
        or turn.get("turn_id") != turn_id
        or project.get("project_id") != project_id.strip()
        or requirement.get("required_capability") != "structured"
        or requirement.get("modality") != "text"
        or requirement.get("output_contract") != "json_object"
        or requirement.get("privacy_scope") != expected_privacy_scope
    ):
        raise ModelPlannerError("Turn model routing snapshot identity drifted")
    return {
        "_routing_project_id": project_id.strip(),
        "_model_routing_snapshot": validated_snapshot,
        "_model_routing_snapshot_ref": snapshot_ref,
        "_model_routing_snapshot_revision": snapshot_revision,
    }


def _privacy_scope(privacy: object) -> str:
    return (
        "remote_allowed"
        if isinstance(privacy, Mapping)
        and privacy.get("allow_remote") is True
        and privacy.get("mode") == "remote_allowed"
        else "local_only"
    )


def _capability(item: CapabilityDefinition) -> dict[str, object]:
    return {"capability_id": item.capability_id, "mode": item.mode, "requires_approval": item.requires_approval, "input_schema_uri": item.input_schema_uri, "output_schema_uri": item.output_schema_uri}


def _materialize_event(event: Mapping[str, object], payloads: TurnPayloadStorePort) -> dict[str, object]:
    materialized = dict(event)
    data = event.get("data")
    if event.get("type") == "context.resolved":
        # The model-visible subset is rebuilt through ContextManifest below.
        # Materializing the audit manifest here would duplicate it into the
        # prompt and bypass each context source's own disclosure/budget gate.
        return materialized
    if isinstance(data, Mapping) and isinstance(data.get("payload_ref"), str):
        materialized["resolved_payload"] = payloads.get(str(data["payload_ref"]))
    elif event.get("type") == "expert.binding.frozen" and isinstance(data, Mapping):
        refs = data.get("evidence_refs")
        if isinstance(refs, list) and len(refs) == 1 and isinstance(refs[0], str):
            materialized["resolved_payload"] = payloads.get(refs[0])
    return materialized


def _materialize_model_context(
    events: Sequence[Mapping[str, object]], payloads: TurnPayloadStorePort,
) -> list[dict[str, object]]:
    context_event = next(
        (event for event in reversed(events) if event.get("type") == "context.resolved"),
        None,
    )
    data = context_event.get("data") if isinstance(context_event, Mapping) else None
    manifest_ref = data.get("payload_ref") if isinstance(data, Mapping) else None
    if not isinstance(manifest_ref, str):
        return []
    try:
        manifest = context_manifest_from_payload(payloads.get(manifest_ref))
    except (ContextManifestError, KeyError, TypeError, ValueError) as error:
        raise ModelPlannerError("model context manifest is invalid") from error
    compacted = compacted_source_entry_ids(manifest)
    selected: list[dict[str, object]] = []
    for entry in manifest.entries:
        if entry.disclosure != "model" or entry.entry_id in compacted:
            continue
        if entry.payload_ref is None:
            raise ModelPlannerError("model context payload ref is unavailable")
        content = _model_entry_content(
            entry.kind,
            payloads.get(entry.payload_ref),
            turn_id=manifest.turn_id,
            project_id=manifest.project_id,
        )
        if entry.kind == "context_binding":
            if not isinstance(content, dict):
                raise ModelPlannerError("ContextBinding model projection is invalid")
            # The Manifest wrapper remains audit evidence.  The compiler and
            # model egress share this single entry projection, so dynamic
            # source/ref metadata cannot escape the LineMap hard budget.
            selected.append(content)
        else:
            selected.append({
                "kind": entry.kind,
                "source_ref": entry.source_ref,
                "revision_identity": entry.revision_identity,
                "provenance_refs": list(entry.provenance_refs),
                "content": content,
            })
    return selected


def _model_entry_content(
    kind: str,
    value: object,
    *,
    turn_id: str | None = None,
    project_id: str | None = None,
) -> object:
    if kind == "context_summary":
        return _context_summary_text(value, turn_id=turn_id, project_id=project_id)
    if kind in {"tool_artifact", "task_graph_change", "agent_message"}:
        try:
            return typed_context_model_projection(
                value, kind=kind, turn_id=turn_id, project_id=project_id,
            )
        except TypedContextEntryError as error:
            raise ModelPlannerError("typed context model content is invalid") from error
    if kind != "context_binding":
        return value
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version", "binding_id", "project_id", "capability_id",
        "capability_revision", "registry_revision", "binding",
    }:
        raise ModelPlannerError("ContextBinding Turn payload is invalid")
    binding_payload = value.get("binding")
    if not isinstance(binding_payload, Mapping):
        raise ModelPlannerError("ContextBinding model content is invalid")
    try:
        binding = context_binding_from_payload(binding_payload)
    except (ContextBindingPayloadError, ValueError, TypeError) as error:
        raise ModelPlannerError("ContextBinding model content is invalid") from error
    if binding.compiler_revision != ContextCompiler.compiler_revision:
        raise ModelPlannerError("ContextBinding compiler revision is unsupported")
    try:
        return context_binding_model_projection(binding)
    except (ValueError, TypeError) as error:
        raise ModelPlannerError("ContextBinding model content is invalid") from error


def _context_summary_text(
    value: object,
    *,
    turn_id: str | None,
    project_id: str | None,
) -> str:
    fields = {
        "schema_version", "snapshot_kind", "projection_authority", "turn_id",
        "project_id", "source_entry_ids", "source_revisions", "provenance_refs",
        "input_bytes", "output_bytes", "summary",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ModelPlannerError("context summary payload is invalid")
    if (
        value.get("schema_version") != "1.0.0"
        or value.get("snapshot_kind") != "turn_frozen_deterministic_memory_summary"
        or value.get("projection_authority") != "derived_only"
    ):
        raise ModelPlannerError("context summary authority is invalid")
    payload_turn, payload_project, summary = (
        value.get("turn_id"), value.get("project_id"), value.get("summary"),
    )
    if not all(isinstance(item, str) and item.strip() for item in (payload_turn, payload_project, summary)):
        raise ModelPlannerError("context summary identity is invalid")
    if (turn_id is not None and payload_turn != turn_id) or (
        project_id is not None and payload_project != project_id
    ):
        raise ModelPlannerError("context summary scope drifted")
    source_ids = value.get("source_entry_ids")
    revisions = value.get("source_revisions")
    provenance = value.get("provenance_refs")
    if (
        not isinstance(source_ids, list)
        or not source_ids
        or len(source_ids) != len(set(source_ids))
        or not all(isinstance(item, str) and item.strip() for item in source_ids)
        or not isinstance(revisions, list)
        or len(revisions) != len(source_ids)
        or not isinstance(provenance, list)
        or len(provenance) != len(set(provenance))
        or not all(isinstance(item, str) and item.startswith("crp://") for item in provenance)
    ):
        raise ModelPlannerError("context summary lineage is invalid")
    if any(
        not isinstance(item, Mapping)
        or set(item) != {"entry_id", "object_id", "revision"}
        or item.get("entry_id") != source_ids[index]
        or not all(isinstance(item.get(field), str) and item[field].strip() for field in item)
        for index, item in enumerate(revisions)
    ):
        raise ModelPlannerError("context summary revisions are invalid")
    input_bytes, output_bytes = value.get("input_bytes"), value.get("output_bytes")
    if (
        not isinstance(input_bytes, int)
        or isinstance(input_bytes, bool)
        or input_bytes < 1
        or not isinstance(output_bytes, int)
        or isinstance(output_bytes, bool)
        or output_bytes < 0
        or output_bytes >= input_bytes
        or output_bytes != len(summary.encode("utf-8"))
    ):
        raise ModelPlannerError("context summary bytes are invalid")
    return summary


def _validate_decision(value: dict[str, object], capability_ids: set[str]) -> dict[str, object]:
    decision_type = value.get("type")
    if decision_type == "tools":
        calls = value.get("calls")
        if not isinstance(calls, list) or not 1 <= len(calls) <= 16:
            raise ModelPlannerError("tool batch must contain between one and sixteen calls")
        normalized = []
        for call in calls:
            if not isinstance(call, Mapping) or set(call) - {"capability_id", "arguments"}:
                raise ModelPlannerError("tool batch call is invalid")
            checked = _validate_decision({"type": "tool", **call}, capability_ids)
            normalized.append({key: checked[key] for key in ("capability_id", "arguments")})
        return {"type": "tools", "calls": normalized}
    if decision_type == "complete":
        if not isinstance(value.get("summary"), str) or not str(value["summary"]).strip():
            raise ModelPlannerError("complete decision requires summary")
        refs = value.get("evidence_refs", [])
        if not isinstance(refs, list) or any(not isinstance(item, str) or not item.startswith("crp://") for item in refs):
            raise ModelPlannerError("complete decision evidence refs are invalid")
        return {"type": "complete", "summary": str(value["summary"]).strip(), "payload_ref": value.get("payload_ref"), "evidence_refs": refs}
    if decision_type == "tool":
        capability_id = value.get("capability_id")
        if capability_id not in capability_ids:
            raise ModelPlannerError("tool decision capability is not registered")
        arguments = value.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise ModelPlannerError("tool decision arguments must be an object")
        return {"type": "tool", "capability_id": capability_id, "arguments": dict(arguments)}
    raise ModelPlannerError("model planner decision type is invalid")

# Public read helpers for specialized planners; private compatibility is retained.
turn_routing_parameters = _turn_routing_parameters
privacy_scope_of = _privacy_scope
