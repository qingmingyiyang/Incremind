from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import re

from core.ai_tooling.contracts import ToolContractError, is_external_task_execution_contract, validate_tool_contract_identity


TERMINAL_EVENT_TYPES = frozenset({"turn.completed", "turn.failed", "turn.cancelled"})
APPROVAL_ACTIONS = frozenset({"approve", "reject", "mcp_continue", "mcp_reject"})
_SENSITIVE_KEYS = frozenset({
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "cookies",
    "password",
    "secret",
    "token",
    "local_path",
    "windows_path",
})
_MODEL_RECEIPT_STATUSES = frozenset({"completed", "failed", "cancelled", "timed_out"})
_PROMPT_CACHE_STATUSES = frozenset({"reported", "unavailable"})
_PROMPT_CACHE_SOURCE_FORMATS = frozenset({"provider_usage", "unavailable"})
_MODEL_WIRE_ATTEMPT_STATUSES = frozenset({"succeeded", "failed_transport", "consumer_cancelled"})
_RECEIPT_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_APPLICATION_SKILL_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_MODEL_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:~/-]{0,127}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{2,127}$")
_REVISION_IDENTIFIER = re.compile(r"^[a-f0-9]{64}$")
_POLICY_VERSION = re.compile(r"@[1-9][0-9]*")
_POLICY_INTERFACES = frozenset({
    "organize", "place", "extract", "route", "steer", "scope", "retrieve", "rank",
    "strength", "forget", "enough", "compose", "trigger", "consolidate", "search", "nudge", "handoff", "image_read", "retry",
    "skill_export", "skill_author",
})


class AIKernelContractError(ValueError):
    pass


def validate_turn_request(request: Mapping[str, object]) -> dict[str, object]:
    fields = {
        "schema_version", "turn_id", "session_id", "operation_id", "idempotency_key", "scope", "input", "desired_outcome", "privacy", "capability_policy", "context_policy", "approval_policy", "created_at",
    }
    # Existing persisted V1 requests do not carry the optional direct Tool
    # selection. Preserve their replay shape while strictly validating it when
    # a newly accepted request supplies the field.
    if "capability_request" in request:
        fields.add("capability_request")
    if "expert_request" in request:
        fields.add("expert_request")
    if "agent_binding" in request:
        fields.add("agent_binding")
    if "agent_policy_binding" in request:
        fields.add("agent_policy_binding")
    if "execution_policy" in request:
        fields.add("execution_policy")
    if "policy_versions" in request:
        fields.add("policy_versions")
    payload = _shape(request, "turn request", fields)
    _reject_sensitive(payload)
    if "policy_versions" in payload:
        versions = _mapping(payload["policy_versions"], "policy versions")
        if (not versions or any(name not in _POLICY_INTERFACES
                or not isinstance(version, str) or not _POLICY_VERSION.fullmatch(version)
                for name, version in versions.items())):
            raise AIKernelContractError("policy versions are invalid")
    scope = _mapping(payload.get("scope"), "turn scope")
    scope_fields = {"kind", "project_id", "series_id", "authority"}
    missing_scope_fields = {"kind", "project_id", "series_id"} - set(scope)
    unknown_scope_fields = set(scope) - scope_fields
    if missing_scope_fields:
        raise AIKernelContractError(
            "turn scope is missing fields: " + ", ".join(sorted(missing_scope_fields)),
        )
    if unknown_scope_fields:
        raise AIKernelContractError(
            "turn scope contains unknown fields: " + ", ".join(sorted(unknown_scope_fields)),
        )
    kind = scope.get("kind")
    project_id = scope.get("project_id")
    series_id = scope.get("series_id")
    authority = scope.get("authority")
    if kind not in {"global", "project", "series"}:
        raise AIKernelContractError("turn scope kind is invalid")
    if project_id is not None and not _strict_match(project_id, _RECEIPT_IDENTIFIER):
        raise AIKernelContractError("turn project identity is invalid")
    if series_id is not None and not _strict_match(series_id, _RECEIPT_IDENTIFIER):
        raise AIKernelContractError("turn series identity is invalid")
    if kind == "global" and (project_id is not None or series_id is not None):
        raise AIKernelContractError("global turn must not carry project or series identity")
    if kind == "project" and (not _text(project_id) or series_id is not None):
        raise AIKernelContractError("project turn requires only project identity")
    if kind in {"global", "project"} and authority is not None:
        raise AIKernelContractError("non-series turn cannot carry scope authority")
    if kind == "series":
        if not _text(project_id) or not _text(series_id):
            raise AIKernelContractError("series turn requires project and series identity")
        _validate_project_series_scope_authority(authority, project_id, series_id)

    input_fields = {"kind", "text", "refs"}
    if "situation" in _mapping(payload.get("input"), "turn input"):
        input_fields.add("situation")
    turn_input = _shape(payload.get("input"), "turn input", input_fields)
    if "situation" in turn_input:
        situation = turn_input["situation"]
        if (payload.get("desired_outcome") not in {"project.answer", "project.task"}
                or not isinstance(situation, str) or len(situation) > 60):
            raise AIKernelContractError("turn situation is invalid")
    for item in _sequence(turn_input.get("refs"), "turn input refs"):
        _shape(item, "turn input ref", {"kind", "object_id", "uri"})

    privacy_fields = {"mode", "allow_remote", "pii", "consent_refs", "retention"}
    frozen_privacy_fields = {"privacy_revision", "excluded_refs", "source_snapshots", "material_refs"}
    if frozen_privacy_fields & set(_mapping(payload.get("privacy"), "turn privacy")):
        privacy_fields |= frozen_privacy_fields
    privacy = _shape(payload.get("privacy"), "turn privacy", privacy_fields)
    if "privacy_revision" in privacy:
        if type(privacy["privacy_revision"]) is not int or privacy["privacy_revision"] < 0:
            raise AIKernelContractError("turn privacy revision is invalid")
        for excluded in _sequence(privacy["excluded_refs"], "excluded turn inputs"):
            _shape(excluded, "excluded turn input", {"kind", "object_id", "project_id"})
            if any(not isinstance(value, str) or not value for value in excluded.values()):
                raise AIKernelContractError("excluded turn input identity is invalid")
        for snapshot in _sequence(privacy["source_snapshots"], "turn source snapshots"):
            _shape(snapshot, "turn source snapshot", {"schema_version", "scope", "privacy_revision", "roots", "nodes"})
        for material in _sequence(privacy["material_refs"], "turn material references"):
            _shape(material, "turn material reference", {"type", "id", "revision", "project_id"})
            if (material["type"] not in {"original_item", "original_source", "document", "recognition", "experience"}
                    or type(material["revision"]) is not int or material["revision"] < 1
                    or any(not isinstance(material[key], str) or not material[key] for key in ("id", "project_id"))):
                raise AIKernelContractError("turn material reference identity is invalid")
    allow_remote = privacy.get("allow_remote") is True
    if privacy.get("mode") == "local_only" and allow_remote:
        raise AIKernelContractError("local_only turn cannot allow remote egress")
    if allow_remote and privacy.get("mode") != "remote_allowed":
        raise AIKernelContractError("remote egress requires remote_allowed mode")
    consent_refs = _sequence(privacy.get("consent_refs"), "privacy consent refs")
    if allow_remote and privacy.get("pii") in {"possible", "present"} and not consent_refs:
        raise AIKernelContractError("remote turn with possible pii requires explicit consent ref")

    policy = _shape(payload.get("capability_policy"), "capability policy", {"allowed", "denied", "require_approval"})
    allowed = {_text(item) for item in _sequence(policy.get("allowed"), "allowed capabilities")}
    denied = {_text(item) for item in _sequence(policy.get("denied"), "denied capabilities")}
    approval = {_text(item) for item in _sequence(policy.get("require_approval"), "approval capabilities")}
    if "" in allowed | denied | approval:
        raise AIKernelContractError("capability policy contains invalid identity")
    if allowed & denied:
        raise AIKernelContractError("capability cannot be both allowed and denied")
    if not approval <= allowed:
        raise AIKernelContractError("approval capability must also be allowed")
    capability_request = payload.get("capability_request")
    if capability_request is not None:
        exact = _shape(
            capability_request,
            "exact capability request",
            {"mode", "capability_id", "arguments"},
        )
        if exact.get("mode") != "execute_exact_v1":
            raise AIKernelContractError("exact capability request mode is invalid")
        capability_id = _text(exact.get("capability_id"))
        if not _ERROR_CODE.fullmatch(capability_id):
            raise AIKernelContractError("exact capability request capability identity is invalid")
        _mapping(exact.get("arguments"), "exact capability request arguments")
        if capability_id not in allowed or capability_id in denied:
            raise AIKernelContractError("exact capability request must select an allowed capability")
    expert_request = payload.get("expert_request")
    if expert_request is not None:
        expert_fields = {"expert_id", "task_intents", "budget"}
        if "skill_ids" in expert_request:
            expert_fields.add("skill_ids")
        expert = _shape(
            expert_request,
            "expert request",
            expert_fields,
        )
        expert_id = expert.get("expert_id")
        if expert_id is not None and not _strict_match(expert_id, _RECEIPT_IDENTIFIER):
            raise AIKernelContractError("expert request identity is invalid")
        intents = _sequence(expert.get("task_intents"), "expert request task intents")
        if not intents or any(not _strict_match(item, _ERROR_CODE) for item in intents):
            raise AIKernelContractError("expert request task intents are invalid")
        if not _strict_match(expert.get("budget"), _RECEIPT_IDENTIFIER):
            raise AIKernelContractError("expert request budget is invalid")
        if "skill_ids" in expert:
            skill_ids = _sequence(expert.get("skill_ids"), "expert request skill ids")
            if (
                not skill_ids
                or len(skill_ids) > 16
                or len(set(skill_ids)) != len(skill_ids)
                or any(not _strict_match(item, _APPLICATION_SKILL_IDENTIFIER) for item in skill_ids)
            ):
                raise AIKernelContractError("expert request skill ids are invalid")
    agent_binding = payload.get("agent_binding")
    if agent_binding is not None:
        _validate_internal_agent_binding(agent_binding)
    agent_policy_binding = payload.get("agent_policy_binding")
    if agent_policy_binding is not None:
        _validate_internal_agent_policy_binding(agent_policy_binding)
    _shape(payload.get("context_policy"), "context policy", {"include_project_skill", "include_memory", "include_session_history", "max_context_bytes"})
    _shape(payload.get("approval_policy"), "approval policy", {"mode", "auto_approve_read_only"})
    external_execution = (
        "external.task.execute" in allowed
        or (isinstance(capability_request, Mapping)
            and capability_request.get("capability_id") == "external.task.execute")
    )
    if external_execution:
        execution = payload.get("execution_policy")
        if (payload.get("desired_outcome") != "project.task"
                or not isinstance(execution, Mapping)
                or type(execution.get("template_version")) is not int
                or execution.get("template_version") != 2):
            raise AIKernelContractError("external execution requires the exact task template")
    if "execution_policy" in payload or payload.get("desired_outcome") == "external.context" or external_execution:
        from .turn_templates import validate_execution_policy
        try:
            validate_execution_policy(payload)
        except ValueError as error:
            raise AIKernelContractError(str(error)) from error
    return payload


def _validate_internal_agent_binding(value: object) -> dict[str, object]:
    """Validate the untrusted shape of a coordinator-issued child binding.

    Acceptance of this shape never grants authority.  The runtime routing
    authority additionally verifies it against the durable coordinator store.
    """

    binding = _mapping(value, "agent binding")
    common_fields = {
        "schema_version", "kind", "run_id", "role", "profile_id",
        "profile_revision", "model_tier", "depth", "cancel_epoch", "budget_snapshot_ref",
    }
    route_fields = {"model_route_key", "model_route_revision"}
    child_fields = {"parent_run_id", "link_id", "reservation_id", "spawn_operation_id"}
    role = binding.get("role")
    expected_fields = common_fields | (child_fields if role == "subagent" else set())
    has_route_binding = bool(route_fields & set(binding))
    if has_route_binding:
        expected_fields |= route_fields
    missing = expected_fields - set(binding)
    unknown = set(binding) - expected_fields
    if missing:
        raise AIKernelContractError("agent binding is missing fields: " + ", ".join(sorted(missing)))
    if unknown:
        raise AIKernelContractError("agent binding contains unknown fields: " + ", ".join(sorted(unknown)))
    if binding.get("schema_version") != "1.0.0" or binding.get("kind") != "internal_agent_run_v1":
        raise AIKernelContractError("agent binding kind is invalid")
    if role not in {"main", "subagent"}:
        raise AIKernelContractError("agent binding role is invalid")
    for field in ("run_id", "profile_id"):
        if not _strict_match(binding.get(field), _RECEIPT_IDENTIFIER):
            raise AIKernelContractError(f"agent binding {field} is invalid")
    revision = binding.get("profile_revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise AIKernelContractError("agent binding profile revision is invalid")
    if binding.get("model_tier") not in {"fast", "standard", "deep"}:
        raise AIKernelContractError("agent binding model tier is invalid")
    if has_route_binding:
        if not _strict_match(binding.get("model_route_key"), _RECEIPT_IDENTIFIER):
            raise AIKernelContractError("agent binding model route key is invalid")
        route_revision = binding.get("model_route_revision")
        if not isinstance(route_revision, int) or isinstance(route_revision, bool) or route_revision < 1:
            raise AIKernelContractError("agent binding model route revision is invalid")
    depth = binding.get("depth")
    if not isinstance(depth, int) or isinstance(depth, bool) or not 0 <= depth <= 4:
        raise AIKernelContractError("agent binding depth is invalid")
    if role == "main":
        if binding.get("profile_id") != "main.orchestrator" or depth != 0:
            raise AIKernelContractError("main agent binding is invalid")
    else:
        if depth < 1:
            raise AIKernelContractError("subagent binding depth is invalid")
        for field in child_fields:
            if not _strict_match(binding.get(field), _RECEIPT_IDENTIFIER):
                raise AIKernelContractError(f"agent binding {field} is invalid")
    epoch = binding.get("cancel_epoch")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        raise AIKernelContractError("agent binding cancel epoch is invalid")
    ref = binding.get("budget_snapshot_ref")
    if not isinstance(ref, str) or not re.fullmatch(r"crp://[a-z0-9][a-z0-9_-]*/.+", ref):
        raise AIKernelContractError("agent binding budget snapshot ref is invalid")
    return binding


def _validate_internal_agent_policy_binding(value: object) -> dict[str, object]:
    """Validate the coordinator-owned audit pointer for a frozen policy."""

    binding = _mapping(value, "agent policy binding")
    expected = {"policy_id", "revision", "snapshot_ref"}
    if set(binding) != expected:
        raise AIKernelContractError("agent policy binding fields are invalid")
    if not _strict_match(binding.get("policy_id"), _RECEIPT_IDENTIFIER):
        raise AIKernelContractError("agent policy binding policy identity is invalid")
    revision = binding.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise AIKernelContractError("agent policy binding revision is invalid")
    ref = binding.get("snapshot_ref")
    if not isinstance(ref, str) or not re.fullmatch(r"crp://[a-z0-9][a-z0-9_-]*/.+", ref):
        raise AIKernelContractError("agent policy binding snapshot ref is invalid")
    return binding


def validate_turn_action(action: Mapping[str, object]) -> dict[str, object]:
    payload = _shape(action, "turn action", {"schema_version", "action_id", "turn_id", "type", "target_event_id", "reason", "actor", "expected_sequence", "idempotency_key", "created_at"})
    _reject_sensitive(payload)
    action_type = payload.get("type")
    target = payload.get("target_event_id")
    if action_type in APPROVAL_ACTIONS and not _text(target):
        raise AIKernelContractError("approval action requires target approval event")
    if action_type in {"cancel", "resume"} and target is not None:
        raise AIKernelContractError("cancel or resume action must target the turn")
    if payload.get("actor") != "user":
        raise AIKernelContractError("turn control action requires user actor")
    return payload


def validate_capability_manifest(manifest: Mapping[str, object]) -> dict[str, object]:
    payload = _mapping(manifest, "capability manifest")
    _reject_sensitive(payload)
    mode = payload.get("mode")
    semantics = payload.get("operation_semantics")
    write_scope = payload.get("write_scope")
    external_execution = write_scope == "external_execute"
    if external_execution:
        _validate_external_execution_manifest(payload)
    elif write_scope is not None and (write_scope != "draft_create_only" or mode != "write"):
        raise AIKernelContractError("draft_create_only is restricted to local write capabilities")
    if mode in {"write", "external", "platform"} and semantics != "receipt_required":
        raise AIKernelContractError("mutating, external or platform capability requires operation receipt")
    if (mode in {"write", "external", "platform"}
            and payload.get("requires_approval") is not True
            and not (mode == "write" and write_scope == "draft_create_only")
            and not external_execution):
        raise AIKernelContractError("mutating, external or platform capability requires approval")
    if mode == "read" and semantics not in {"read_only", "none"}:
        raise AIKernelContractError("read capability cannot claim write operation semantics")
    if payload.get("tool_exposed") is not True:
        raise AIKernelContractError("AI capability must be exposed through the scoped tool registry")
    return payload


def _validate_external_execution_manifest(payload: Mapping[str, object]) -> None:
    """窄口声明只描述已登记的原生工具，运行资格仍由宿主权威核验。"""
    if (payload.get("capability_id") != "external.task.execute"
            or type(payload.get("version")) is not int or payload.get("version") != 1
            or payload.get("mode") != "external"
            or type(payload.get("requires_approval")) is not bool):
        raise AIKernelContractError("external execution capability identity is invalid")
    try:
        contract = validate_tool_contract_identity(payload.get("tool_contract"))
    except ToolContractError as error:
        raise AIKernelContractError("external execution native contract is invalid") from error
    if not is_external_task_execution_contract(contract):
        raise AIKernelContractError("external execution native contract is invalid")


def validate_event_transition(
    previous: Mapping[str, object] | None,
    current: Mapping[str, object],
) -> dict[str, object]:
    event = _mapping(current, "turn event")
    _reject_sensitive(event)
    sequence = event.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise AIKernelContractError("turn event sequence must be positive")
    if previous is None:
        if sequence != 1 or event.get("type") != "turn.accepted":
            raise AIKernelContractError("first event must be turn.accepted at sequence 1")
        return event
    prior = _mapping(previous, "previous turn event")
    if prior.get("turn_id") != event.get("turn_id") or prior.get("session_id") != event.get("session_id"):
        raise AIKernelContractError("turn event identity drifted")
    if sequence != int(prior.get("sequence", 0)) + 1:
        raise AIKernelContractError("turn event sequence must be contiguous")
    if prior.get("type") in TERMINAL_EVENT_TYPES:
        raise AIKernelContractError("terminal turn cannot accept later events")
    if event.get("type") == "turn.accepted":
        raise AIKernelContractError("turn.accepted can only be the first event")
    return event


def validate_governed_payload(payload: object) -> object:
    """Reject secret-shaped fields before model-visible payload persistence."""
    _reject_sensitive(payload)
    return payload


def validate_model_call_receipt(payload: object) -> dict[str, object]:
    """Validate the terminal metadata receipt for one Planner model request.

    This contract deliberately has no field capable of carrying prompt content,
    model output, provider response bodies, endpoints, paths, or credentials.
    """

    fields = {
        "schema_version", "receipt_id", "turn_id", "model_request_id", "status",
        "requested_at", "completed_at", "duration_ms", "provider_id", "model_id",
        "usage_status", "usage", "input_recorded", "output_recorded", "error_code",
    }
    if isinstance(payload, Mapping) and "model_call_purpose" in payload:
        fields.add("model_call_purpose")
    receipt = _shape(payload, "model call receipt", fields)
    _reject_sensitive(receipt)
    if receipt.get("schema_version") != "1.0.0":
        raise AIKernelContractError("model call receipt schema version is unsupported")
    receipt_id = _text(receipt.get("receipt_id"))
    if not receipt_id.startswith("model-receipt-") or not _RECEIPT_IDENTIFIER.fullmatch(receipt_id):
        raise AIKernelContractError("model call receipt identity is invalid")
    turn_id = _text(receipt.get("turn_id"))
    if not _RECEIPT_IDENTIFIER.fullmatch(turn_id):
        raise AIKernelContractError("model call receipt Turn identity is invalid")
    model_request_id = _text(receipt.get("model_request_id"))
    if not _RECEIPT_IDENTIFIER.fullmatch(model_request_id):
        raise AIKernelContractError("model call receipt request identity is invalid")
    if receipt.get("status") not in _MODEL_RECEIPT_STATUSES:
        raise AIKernelContractError("model call receipt status is invalid")
    _validate_datetime(receipt.get("requested_at"), "model call receipt requested time")
    _validate_datetime(receipt.get("completed_at"), "model call receipt completed time")
    duration = receipt.get("duration_ms")
    if not isinstance(duration, int) or isinstance(duration, bool) or not 0 <= duration <= 86_400_000:
        raise AIKernelContractError("model call receipt duration is invalid")
    for field, pattern in (("provider_id", _RECEIPT_IDENTIFIER), ("model_id", _MODEL_IDENTIFIER)):
        value = receipt.get(field)
        if value is not None and (not isinstance(value, str) or not pattern.fullmatch(value)):
            raise AIKernelContractError(f"model call receipt {field} is invalid")
    usage_status = receipt.get("usage_status")
    usage = receipt.get("usage")
    if usage_status not in {"recorded", "not_recorded"}:
        raise AIKernelContractError("model call receipt usage status is invalid")
    if usage_status == "not_recorded" and usage is not None:
        raise AIKernelContractError("unrecorded model usage must be null")
    if usage_status == "recorded":
        values = _shape(usage, "model call receipt usage", {"input_tokens", "output_tokens", "total_tokens"})
        for field in values:
            count = values[field]
            if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= 2_147_483_647:
                raise AIKernelContractError("model call receipt usage is invalid")
    if receipt.get("input_recorded") is not False or receipt.get("output_recorded") is not False:
        raise AIKernelContractError("model call receipt cannot record input or output")
    error_code = receipt.get("error_code")
    if error_code is not None and (not isinstance(error_code, str) or not _ERROR_CODE.fullmatch(error_code)):
        raise AIKernelContractError("model call receipt error code is invalid")
    if receipt["status"] == "completed" and error_code is not None:
        raise AIKernelContractError("completed model call receipt cannot carry an error code")
    if receipt["status"] != "completed" and error_code is None:
        raise AIKernelContractError("non-completed model call receipt requires an error code")
    if "model_call_purpose" in receipt and receipt["model_call_purpose"] not in {"primary", "aux", "probe"}:
        raise AIKernelContractError("model call receipt purpose is invalid")
    return receipt


def validate_model_dispatch_authority_receipt(payload: object) -> dict[str, object]:
    """Validate one privacy-safe observation of the dispatch authority fence.

    The receipt is intentionally separate from a wire-attempt receipt: wait and
    hold include local authority work, whereas a wire duration remains transport
    only.  It carries no provider, model, root path, endpoint, prompt, output,
    or credential.
    """

    receipt = _shape(payload, "model dispatch authority receipt", {
        "schema_version", "receipt_id", "turn_id", "model_request_id",
    "wait_ms", "hold_ms", "outcome",
        "input_recorded", "output_recorded",
    })
    _reject_sensitive(receipt)
    if receipt.get("schema_version") != "1.0.0":
        raise AIKernelContractError("model dispatch authority receipt schema version is unsupported")
    receipt_id = _text(receipt.get("receipt_id"))
    if (
        not receipt_id.startswith("model-dispatch-authority-receipt-")
        or not _RECEIPT_IDENTIFIER.fullmatch(receipt_id)
    ):
        raise AIKernelContractError("model dispatch authority receipt identity is invalid")
    for field in ("turn_id", "model_request_id"):
        value = _text(receipt.get(field))
        if not _RECEIPT_IDENTIFIER.fullmatch(value):
            raise AIKernelContractError(f"model dispatch authority receipt {field} is invalid")
    for field in ("wait_ms", "hold_ms"):
        value = receipt.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 86_400_000:
            raise AIKernelContractError(f"model dispatch authority receipt {field} is invalid")
    if receipt.get("outcome") not in {"completed", "failed", "timed_out"}:
        raise AIKernelContractError("model dispatch authority receipt outcome is invalid")
    if receipt.get("input_recorded") is not False or receipt.get("output_recorded") is not False:
        raise AIKernelContractError("model dispatch authority receipt cannot record input or output")
    return receipt


def validate_prompt_cache_receipt(payload: object) -> dict[str, object]:
    """Validate provider-reported prompt-cache metadata for one routed request.

    The receipt records only token counters explicitly exposed by a provider.
    It does not infer a cache outcome and has no field for prompt content,
    output, endpoint, cache key, credential, path, or raw provider response.
    """

    receipt = _shape(payload, "prompt cache receipt", {
        "schema_version", "receipt_id", "turn_id", "model_request_id",
        "routing_snapshot_revision", "prompt_cache_scope_identity", "provider_id", "model_id",
        "cache_status", "source_format", "cache_read_input_tokens", "cache_write_input_tokens",
        "uncached_input_tokens", "input_recorded", "output_recorded",
    })
    _reject_sensitive(receipt)
    if receipt.get("schema_version") != "1.0.0":
        raise AIKernelContractError("prompt cache receipt schema version is unsupported")
    receipt_id = _text(receipt.get("receipt_id"))
    if not receipt_id.startswith("prompt-cache-receipt-") or not _RECEIPT_IDENTIFIER.fullmatch(receipt_id):
        raise AIKernelContractError("prompt cache receipt identity is invalid")
    for field, pattern in (
        ("turn_id", _RECEIPT_IDENTIFIER),
        ("model_request_id", _RECEIPT_IDENTIFIER),
        ("routing_snapshot_revision", _REVISION_IDENTIFIER),
        ("prompt_cache_scope_identity", _REVISION_IDENTIFIER),
        ("provider_id", _RECEIPT_IDENTIFIER),
        ("model_id", _MODEL_IDENTIFIER),
    ):
        value = _text(receipt.get(field))
        if not pattern.fullmatch(value):
            raise AIKernelContractError(f"prompt cache receipt {field} is invalid")
    status = receipt.get("cache_status")
    source_format = receipt.get("source_format")
    if status not in _PROMPT_CACHE_STATUSES:
        raise AIKernelContractError("prompt cache receipt status is invalid")
    if source_format not in _PROMPT_CACHE_SOURCE_FORMATS:
        raise AIKernelContractError("prompt cache receipt source format is invalid")
    counters = tuple(
        receipt.get(field)
        for field in ("cache_read_input_tokens", "cache_write_input_tokens", "uncached_input_tokens")
    )
    for count in counters:
        if count is not None and (
            not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= 2_147_483_647
        ):
            raise AIKernelContractError("prompt cache receipt token count is invalid")
    if status == "reported":
        if source_format != "provider_usage":
            raise AIKernelContractError("reported prompt cache metadata requires provider usage format")
        if all(count is None for count in counters):
            raise AIKernelContractError("reported prompt cache metadata requires a token count")
    elif source_format != "unavailable" or any(count is not None for count in counters):
        raise AIKernelContractError("unavailable prompt cache metadata cannot carry source usage")
    if receipt.get("input_recorded") is not False or receipt.get("output_recorded") is not False:
        raise AIKernelContractError("prompt cache receipt cannot record input or output")
    return receipt


def validate_model_wire_attempt_receipt(payload: object) -> dict[str, object]:
    """Validate one metadata-only terminal record of a provider wire attempt.

    A wire attempt is narrower than a logical model request: one request may
    eventually have several attempts, but this contract never carries prompts,
    output, endpoint, credential, provider response, or error body data.
    """

    receipt = _shape_with_optional_execution_location(payload, "model wire attempt receipt", {
        "schema_version", "attempt_id", "turn_id", "model_request_id", "attempt_number",
        "routing_snapshot_revision", "provider_id", "model_id", "status", "started_at",
        "completed_at", "duration_ms", "usage_status", "usage", "cache_status",
        "cache_metadata", "input_stored", "output_stored", "error_code",
    })
    _reject_sensitive(receipt)
    if receipt.get("schema_version") != "1.0.0":
        raise AIKernelContractError("model wire attempt receipt schema version is unsupported")
    attempt_id = _text(receipt.get("attempt_id"))
    attempt_suffix = attempt_id.removeprefix("model-wire-attempt-")
    if (
        not attempt_id.startswith("model-wire-attempt-")
        or len(attempt_suffix) < 8
        or not _RECEIPT_IDENTIFIER.fullmatch(attempt_id)
    ):
        raise AIKernelContractError("model wire attempt receipt identity is invalid")
    for field, pattern in (
        ("turn_id", _RECEIPT_IDENTIFIER),
        ("model_request_id", _RECEIPT_IDENTIFIER),
        ("routing_snapshot_revision", _REVISION_IDENTIFIER),
        ("provider_id", _RECEIPT_IDENTIFIER),
        ("model_id", _MODEL_IDENTIFIER),
    ):
        value = _text(receipt.get(field))
        if not pattern.fullmatch(value):
            raise AIKernelContractError(f"model wire attempt receipt {field} is invalid")
    attempt_number = receipt.get("attempt_number")
    if not isinstance(attempt_number, int) or isinstance(attempt_number, bool) or not 1 <= attempt_number <= 10_000:
        raise AIKernelContractError("model wire attempt receipt attempt number is invalid")
    if receipt.get("status") not in _MODEL_WIRE_ATTEMPT_STATUSES:
        raise AIKernelContractError("model wire attempt receipt status is invalid")
    _validate_datetime(receipt.get("started_at"), "model wire attempt receipt started time")
    _validate_datetime(receipt.get("completed_at"), "model wire attempt receipt completed time")
    duration = receipt.get("duration_ms")
    if not isinstance(duration, int) or isinstance(duration, bool) or not 0 <= duration <= 86_400_000:
        raise AIKernelContractError("model wire attempt receipt duration is invalid")
    _validate_attempt_usage(receipt)
    _validate_attempt_cache(receipt)
    if receipt.get("input_stored") is not False or receipt.get("output_stored") is not False:
        raise AIKernelContractError("model wire attempt receipt cannot store input or output")
    error_code = receipt.get("error_code")
    if error_code is not None and (not isinstance(error_code, str) or not _ERROR_CODE.fullmatch(error_code)):
        raise AIKernelContractError("model wire attempt receipt error code is invalid")
    if receipt["status"] == "succeeded" and error_code is not None:
        raise AIKernelContractError("succeeded model wire attempt receipt cannot carry an error code")
    if receipt["status"] != "succeeded" and error_code is None:
        raise AIKernelContractError("non-succeeded model wire attempt receipt requires an error code")
    if receipt["status"] == "consumer_cancelled" and error_code != "ai.consumer_cancelled":
        raise AIKernelContractError("consumer-cancelled model wire attempt receipt has an invalid error code")
    return receipt


def validate_model_wire_attempt_dispatch(payload: object) -> dict[str, object]:
    """Validate the metadata-only durable marker written before one wire attempt.

    The dispatch marker carries only the immutable identity and route binding
    needed to correlate a later terminal receipt.  It deliberately cannot
    retain request input, output, provider response bodies, endpoints, paths,
    credentials, or error text.
    """

    dispatch = _shape_with_optional_execution_location(payload, "model wire attempt dispatch", {
        "schema_version", "attempt_id", "turn_id", "model_request_id", "attempt_number",
        "routing_snapshot_revision", "provider_id", "model_id", "dispatched_at",
        "input_stored", "output_stored",
    })
    _reject_sensitive(dispatch)
    if dispatch.get("schema_version") != "1.0.0":
        raise AIKernelContractError("model wire attempt dispatch schema version is unsupported")
    attempt_id = _text(dispatch.get("attempt_id"))
    attempt_suffix = attempt_id.removeprefix("model-wire-attempt-")
    if (
        not attempt_id.startswith("model-wire-attempt-")
        or len(attempt_suffix) < 8
        or not _RECEIPT_IDENTIFIER.fullmatch(attempt_id)
    ):
        raise AIKernelContractError("model wire attempt dispatch identity is invalid")
    for field, pattern in (
        ("turn_id", _RECEIPT_IDENTIFIER),
        ("model_request_id", _RECEIPT_IDENTIFIER),
        ("routing_snapshot_revision", _REVISION_IDENTIFIER),
        ("provider_id", _RECEIPT_IDENTIFIER),
        ("model_id", _MODEL_IDENTIFIER),
    ):
        value = _text(dispatch.get(field))
        if not pattern.fullmatch(value):
            raise AIKernelContractError(f"model wire attempt dispatch {field} is invalid")
    attempt_number = dispatch.get("attempt_number")
    if not isinstance(attempt_number, int) or isinstance(attempt_number, bool) or not 1 <= attempt_number <= 10_000:
        raise AIKernelContractError("model wire attempt dispatch attempt number is invalid")
    _validate_datetime(dispatch.get("dispatched_at"), "model wire attempt dispatch time")
    if dispatch.get("input_stored") is not False or dispatch.get("output_stored") is not False:
        raise AIKernelContractError("model wire attempt dispatch cannot store input or output")
    return dispatch


def _validate_attempt_usage(receipt: Mapping[str, object]) -> None:
    status = receipt.get("usage_status")
    usage = receipt.get("usage")
    if status not in _PROMPT_CACHE_STATUSES:
        raise AIKernelContractError("model wire attempt receipt usage status is invalid")
    if status == "unavailable":
        if usage is not None:
            raise AIKernelContractError("unavailable model wire attempt usage must be null")
        return
    if not isinstance(usage, Mapping) or not usage or set(usage) - {"input_tokens", "output_tokens", "total_tokens"}:
        raise AIKernelContractError("model wire attempt receipt usage is invalid")
    values = usage
    for count in values.values():
        if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= 2_147_483_647:
            raise AIKernelContractError("model wire attempt receipt usage is invalid")


def _validate_attempt_cache(receipt: Mapping[str, object]) -> None:
    status = receipt.get("cache_status")
    metadata = receipt.get("cache_metadata")
    if status not in _PROMPT_CACHE_STATUSES:
        raise AIKernelContractError("model wire attempt receipt cache status is invalid")
    if status == "unavailable":
        if metadata is not None:
            raise AIKernelContractError("unavailable model wire attempt cache metadata must be null")
        return
    cache = _shape(metadata, "model wire attempt receipt cache metadata", {
        "source_format", "cache_read_input_tokens", "cache_write_input_tokens", "uncached_input_tokens",
    })
    if cache.get("source_format") != "provider_usage":
        raise AIKernelContractError("reported model wire attempt cache metadata requires provider usage format")
    counters = tuple(
        cache[field]
        for field in ("cache_read_input_tokens", "cache_write_input_tokens", "uncached_input_tokens")
    )
    for count in counters:
        if count is not None and (
            not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= 2_147_483_647
        ):
            raise AIKernelContractError("model wire attempt receipt cache token count is invalid")
    if all(count is None for count in counters):
        raise AIKernelContractError("reported model wire attempt cache metadata requires a token count")


def validate_turn_presentation_artifact(payload: object) -> dict[str, object]:
    """Validate an explicitly public UI projection stored behind a Turn payload ref."""
    artifact = _shape(payload, "turn presentation artifact", {"schema_version", "kind", "content"})
    if artifact.get("schema_version") != "1.0.0":
        raise AIKernelContractError("turn presentation artifact schema version is unsupported")
    kind = _text(artifact.get("kind"))
    if not kind or len(kind) > 128:
        raise AIKernelContractError("turn presentation artifact kind is invalid")
    artifact["content"] = _mapping(artifact.get("content"), "turn presentation content")
    _reject_sensitive(artifact)
    return artifact


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise AIKernelContractError(f"{label} must be an object")
    return dict(value)


def _shape(value: object, label: str, fields: set[str]) -> dict[str, object]:
    payload = _mapping(value, label)
    actual = {str(key) for key in payload}
    missing = fields - actual
    if missing:
        raise AIKernelContractError(f"{label} is missing fields: {', '.join(sorted(missing))}")
    unknown = actual - fields
    if unknown:
        raise AIKernelContractError(f"{label} contains unknown fields: {', '.join(sorted(unknown))}")
    return payload


def _shape_with_optional_execution_location(
    value: object,
    label: str,
    fields: set[str],
) -> dict[str, object]:
    """Keep historical receipts readable while validating new wire evidence."""

    payload = _mapping(value, label)
    actual = {str(key) for key in payload}
    missing = fields - actual
    if missing:
        raise AIKernelContractError(f"{label} is missing fields: {', '.join(sorted(missing))}")
    unknown = actual - fields - {"execution_location"}
    if unknown:
        raise AIKernelContractError(f"{label} contains unknown fields: {', '.join(sorted(unknown))}")
    if "execution_location" in payload and payload["execution_location"] not in {
        "remote", "local_loopback",
    }:
        raise AIKernelContractError(f"{label} execution location is invalid")
    return payload


def _sequence(value: object, label: str) -> tuple[object, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise AIKernelContractError(f"{label} must be an array")
    return tuple(value)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _strict_match(value: object, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and value == value.strip() and pattern.fullmatch(value) is not None


def _validate_project_series_scope_authority(
    value: object,
    project_id: object,
    series_id: object,
) -> None:
    authority = _shape(value, "series scope authority", {
        "kind", "object_id", "payload_revision", "storage_revision",
        "authority_identity", "authority_ref",
    })
    if authority.get("kind") != "project_series_scope_v1":
        raise AIKernelContractError("series scope authority kind is invalid")
    raw_object_id = authority.get("object_id")
    object_id = _text(raw_object_id)
    if not _strict_match(raw_object_id, _RECEIPT_IDENTIFIER):
        raise AIKernelContractError("series scope authority object identity is invalid")
    for field in ("payload_revision", "storage_revision"):
        revision = authority.get(field)
        if (
            not isinstance(revision, (int, float))
            or isinstance(revision, bool)
            or revision <= 0
            or not float(revision).is_integer()
        ):
            raise AIKernelContractError(f"series scope authority {field} is invalid")
    raw_authority_identity = authority.get("authority_identity")
    authority_identity = _text(raw_authority_identity)
    if not _strict_match(raw_authority_identity, re.compile(r"[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}")):
        raise AIKernelContractError("series scope authority identity is invalid")
    raw_authority_ref = authority.get("authority_ref")
    authority_ref = _text(raw_authority_ref)
    namespace, separator, suffix = authority_ref.removeprefix("crp://").partition("/")
    expected_suffix = f"memory/series/{object_id}"
    if (
        not isinstance(raw_authority_ref, str)
        or raw_authority_ref != authority_ref
        or not authority_ref.startswith("crp://")
        or not separator
        or not _RECEIPT_IDENTIFIER.fullmatch(namespace)
        or suffix != expected_suffix
    ):
        raise AIKernelContractError("series scope authority ref is invalid")
    # The frozen authority maps the same identifiers accepted by the Turn; no
    # project identity is duplicated inside the snapshot to avoid two facts.
    if not _text(project_id) or not _text(series_id):
        raise AIKernelContractError("series scope authority identity binding is invalid")


def _validate_datetime(value: object, label: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 80:
        raise AIKernelContractError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise AIKernelContractError(f"{label} is invalid") from error
    if parsed.tzinfo is None:
        raise AIKernelContractError(f"{label} must include a timezone")


def _reject_sensitive(value: object, *, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SENSITIVE_KEYS or normalized.endswith("_secret"):
                raise AIKernelContractError(f"sensitive field is forbidden at {path}.{key}")
            _reject_sensitive(item, path=f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            _reject_sensitive(item, path=f"{path}[{index}]")
