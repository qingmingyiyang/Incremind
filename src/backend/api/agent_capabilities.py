"""Native, host-governed capability adapters for coordinated Agent Runs.

This module deliberately has no runtime composition responsibility.  It is a
small provider boundary: the AI Kernel supplies frozen Turn authority and the
coordinator owns every durable Agent Run transition.  In particular providers
must never accept a model-supplied project, path, credential, or child Turn
identity as an authority.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
from typing import Literal, Protocol

from core.ai_kernel import CapabilityDefinition
from core.ai_kernel.dispatcher import ToolProviderFailure
from core.ai_tooling import ToolDefinition, ToolRetryPolicy


AGENT_SPAWN_CAPABILITY = "agent.spawn"
AGENT_MESSAGE_CAPABILITY = "agent.message"
AGENT_INTERRUPT_CAPABILITY = "agent.interrupt"
AGENT_WAIT_CAPABILITY = "agent.wait"
AGENT_FAN_IN_CAPABILITY = "agent.fan_in"
AGENT_LIST_CAPABILITY = "agent.list"
AGENT_PLAN_CAPABILITY = "agent.plan"
AGENT_CAPABILITY_IDS = (
    AGENT_SPAWN_CAPABILITY, AGENT_MESSAGE_CAPABILITY, AGENT_INTERRUPT_CAPABILITY,
    AGENT_WAIT_CAPABILITY, AGENT_FAN_IN_CAPABILITY, AGENT_LIST_CAPABILITY,
    AGENT_PLAN_CAPABILITY,
)

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_TURN_ID = re.compile(
    r"^(?:turn-[0-9a-f]{32}|world-turn-[A-Za-z0-9][A-Za-z0-9._~-]{0,63})$"
)
_OPERATION = re.compile(
    r"^(?:op-[A-Za-z0-9._~-]{8,125}|world-action-[A-Za-z0-9][A-Za-z0-9._~-]{0,63})$"
)
_CRP_REF = re.compile(r"^crp://[a-z0-9][a-z0-9_-]*/[A-Za-z0-9._:/~-]{1,511}$")
_SENSITIVE = re.compile(r"(?:api[_-]?key|authorization|cookie|credential|password|secret|token|endpoint|base[_-]?url|hidden|context|path|filesystem|directory)", re.I)
_MUTATING = frozenset({AGENT_SPAWN_CAPABILITY, AGENT_MESSAGE_CAPABILITY, AGENT_INTERRUPT_CAPABILITY, AGENT_FAN_IN_CAPABILITY, AGENT_PLAN_CAPABILITY})
_PLAN_SENSITIVE_FIELDS = frozenset({"provider", "model", "endpoint", "secret", "api_key", "token", "credential", "context", "path"})


class AgentCapabilityError(ValueError):
    """Raised for a malformed or unauthorized agent capability request."""


class AgentCoordinatorPort(Protocol):
    """The only authority which can create or change Agent Runs."""

    def spawn(self, **request: object) -> Mapping[str, object]: ...
    def message(self, **request: object) -> Mapping[str, object]: ...
    def interrupt(self, **request: object) -> Mapping[str, object]: ...
    def wait(self, **request: object) -> Mapping[str, object]: ...
    def fan_in(self, **request: object) -> Mapping[str, object]: ...
    def list(self, **request: object) -> Mapping[str, object]: ...
    def plan(self, **request: object) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class _ProviderRequest:
    turn_id: str
    operation_id: str
    tool_call_id: str
    project_id: str
    scope: Mapping[str, object]
    privacy: Mapping[str, object]
    arguments: Mapping[str, object]


def agent_capability_definitions() -> tuple[CapabilityDefinition, ...]:
    """Return stable native Tool contracts; registration remains composition-owned."""
    return tuple(agent_capability_definition(capability_id) for capability_id in AGENT_CAPABILITY_IDS)


def agent_capability_definition(capability_id: str) -> CapabilityDefinition:
    if capability_id not in AGENT_CAPABILITY_IDS:
        raise AgentCapabilityError("agent capability is unsupported")
    operation = capability_id.removeprefix("agent.").replace("_", "-")
    input_schema = f"crp://default/contracts/agent-{operation}-request.schema.json"
    output_schema = "crp://default/contracts/agent-operation-result.schema.json"
    mutating = capability_id in _MUTATING
    requires_approval = mutating and capability_id != AGENT_PLAN_CAPABILITY
    mode: Literal["read", "platform"] = "platform" if mutating else "read"
    semantics: Literal["read_only", "receipt_required"] = "receipt_required" if mutating else "read_only"
    definition = CapabilityDefinition(
        capability_id, 1, mode, requires_approval, semantics, input_schema, output_schema,
    )
    return CapabilityDefinition(
        definition.capability_id, definition.version, definition.mode,
        definition.requires_approval, definition.operation_semantics,
        definition.input_schema_uri, definition.output_schema_uri,
        tool_definition=ToolDefinition(
            tool_id=capability_id, version=1, display_name=capability_id,
            description="Host-governed coordinated Agent Run operation",
            source="core", owner_id="agent-coordinator", effect=mode,
            data_classes=("local_metadata",), destination="platform",
            input_schema_uri=input_schema, output_schema_uri=output_schema,
            receipt_schema_uri=("crp://default/contracts/agent-operation-receipt.schema.json" if mutating else None),
            operation_semantics=semantics,
            execution_mode="parallel",
            resource_locks=("agent:coordination",) if mutating else (),
            idempotency="never_retry" if mutating else "idempotent",
            retry_policy=ToolRetryPolicy(1 if mutating else 2, 0 if mutating else 100, () if mutating else ("timeout", "temporarily_unavailable")),
            verification_tool_id=None, compensation_tool_id=None,
            mutability="reversible" if mutating else "read_only",
            egress_class="none", network_scope=(), data_egress_scope=(),
            timeout_ms=120_000, required_scopes=("project",),
            boundary_requirements=("agent_run_parent", "frozen_turn_scope"),
        ),
    )


class AgentCapabilityProvider:
    """Validate one frozen Tool provider request and invoke its coordinator method."""

    def __init__(self, *, coordinator: AgentCoordinatorPort, capability_id: str) -> None:
        if capability_id not in AGENT_CAPABILITY_IDS:
            raise AgentCapabilityError("agent capability is unsupported")
        self._coordinator, self._capability_id = coordinator, capability_id

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        try:
            validated = _validate_provider_request(request, self._capability_id)
            operation = self._capability_id.removeprefix("agent.")
            handler = getattr(self._coordinator, operation, None)
            if not callable(handler):
                raise AgentCapabilityError("agent coordinator operation is unavailable")
        except ToolProviderFailure:
            raise
        except Exception as error:
            raise ToolProviderFailure(
                "agent.request_invalid", effect_certainty="confirmed_none",
            ) from error
        try:
            outcome = handler(
                parent_turn_id=validated.turn_id,
                operation_id=validated.operation_id,
                tool_call_id=validated.tool_call_id,
                project_id=validated.project_id,
                scope=dict(validated.scope), privacy=dict(validated.privacy),
                arguments=dict(validated.arguments),
            )
            if not isinstance(outcome, Mapping):
                raise AgentCapabilityError("agent coordinator returned an invalid result")
            return _public_result(self._capability_id, validated, outcome)
        except ToolProviderFailure:
            raise
        except Exception as error:
            raise ToolProviderFailure(
                "agent.coordinator_failed", effect_certainty="unknown",
            ) from error


def _validate_provider_request(request: Mapping[str, object], capability_id: str) -> _ProviderRequest:
    if not isinstance(request, Mapping):
        raise AgentCapabilityError("agent provider request is invalid")
    if request.get("capability_id") != capability_id:
        raise AgentCapabilityError("agent capability identity drifted")
    turn_id = _identifier(request.get("turn_id"), "turn id", _TURN_ID)
    operation_id = _identifier(request.get("operation_id"), "operation id", _OPERATION)
    tool_call_id = _identifier(request.get("tool_call_id"), "tool call id", _ID)
    scope = _mapping(request.get("scope"), "scope")
    privacy = _mapping(request.get("privacy"), "privacy")
    arguments = _mapping(request.get("arguments"), "arguments")
    scope_kind = scope.get("kind")
    expected_scope_fields = (
        {"kind", "project_id", "series_id", "authority"}
        if scope_kind == "series" else {"kind", "project_id", "series_id"}
    )
    if set(scope) != expected_scope_fields:
        raise AgentCapabilityError("agent scope shape is invalid")
    if scope_kind not in {"project", "series"} or scope.get("project_id") is None:
        raise AgentCapabilityError("agent capability requires a project scope")
    project_id = _identifier(scope.get("project_id"), "project id", _ID)
    if scope_kind == "project" and scope.get("series_id") is not None:
        raise AgentCapabilityError("agent project scope is invalid")
    if scope_kind == "series" and (
        not isinstance(scope.get("series_id"), str)
        or not isinstance(scope.get("authority"), Mapping)
    ):
        raise AgentCapabilityError("agent scope authority is invalid")
    legacy_privacy = {"mode", "allow_remote", "pii", "consent_refs", "retention"}
    product_privacy = legacy_privacy | {"privacy_revision", "excluded_refs", "source_snapshots", "material_refs"}
    if set(privacy) not in (legacy_privacy, product_privacy):
        raise AgentCapabilityError("agent privacy shape is invalid")
    if set(privacy) == product_privacy and (
        type(privacy['privacy_revision']) is not int or privacy['privacy_revision'] < 0
        or any(not isinstance(privacy[key],list) for key in ('excluded_refs','source_snapshots','material_refs'))
    ):
        raise AgentCapabilityError('agent product privacy shape is invalid')
    if privacy.get("mode") not in {"local_only", "local_first", "remote_allowed"} or not isinstance(privacy.get("allow_remote"), bool):
        raise AgentCapabilityError("agent privacy authority is invalid")
    if privacy.get("pii") not in {"none", "possible", "present"} or privacy.get("retention") not in {"none", "session", "local_durable"}:
        raise AgentCapabilityError("agent privacy authority is invalid")
    consent_refs = privacy.get("consent_refs")
    if not isinstance(consent_refs, list) or any(not isinstance(item, str) or _CRP_REF.fullmatch(item) is None for item in consent_refs):
        raise AgentCapabilityError("agent privacy consent references are invalid")
    _validate_arguments(capability_id, arguments)
    return _ProviderRequest(turn_id, operation_id, tool_call_id, project_id, scope, privacy, arguments)


def _validate_arguments(capability_id: str, arguments: Mapping[str, object]) -> None:
    allowed = {
        AGENT_SPAWN_CAPABILITY: {"profile_id", "task", "budget", "capability_ids", "input_refs"},
        AGENT_MESSAGE_CAPABILITY: {"recipient_run_id", "kind", "payload_ref"},
        AGENT_INTERRUPT_CAPABILITY: {"child_run_id", "reason"},
        AGENT_WAIT_CAPABILITY: {"child_run_ids", "timeout_ms"},
        AGENT_FAN_IN_CAPABILITY: {"child_run_ids", "policy", "quorum"},
        AGENT_LIST_CAPABILITY: {"include_messages"},
        AGENT_PLAN_CAPABILITY: {"mode", "plan_id", "cluster_id", "assignments"},
    }[capability_id]
    if not arguments or set(arguments) - allowed:
        raise AgentCapabilityError("agent capability arguments are invalid")
    if capability_id == AGENT_PLAN_CAPABILITY:
        _reject_plan_sensitive(arguments)
    else:
        _reject_sensitive(arguments)
    if capability_id == AGENT_SPAWN_CAPABILITY:
        if not {"profile_id", "task"}.issubset(arguments):
            raise AgentCapabilityError("agent spawn arguments are incomplete")
        _identifier(arguments["profile_id"], "child profile id", _ID)
        if not isinstance(arguments["task"], str) or not arguments["task"].strip() or len(arguments["task"]) > 16_000:
            raise AgentCapabilityError("agent child task is invalid")
        if "budget" in arguments:
            _budget(arguments["budget"])
        _identifier_list(arguments.get("capability_ids"), "child capability ids", required=False)
        _refs(arguments.get("input_refs"), "child input references", required=False)
    elif capability_id == AGENT_MESSAGE_CAPABILITY:
        if set(arguments) != {"recipient_run_id", "kind", "payload_ref"}:
            raise AgentCapabilityError("agent message arguments are incomplete")
        _identifier(arguments["recipient_run_id"], "recipient run id", _ID)
        if arguments["kind"] not in {"task", "progress", "result", "control"}:
            raise AgentCapabilityError("agent message kind is invalid")
        _refs(arguments["payload_ref"], "message payload reference", required=True)
    elif capability_id == AGENT_INTERRUPT_CAPABILITY:
        if set(arguments) != {"child_run_id", "reason"}:
            raise AgentCapabilityError("agent interrupt arguments are incomplete")
        _identifier(arguments["child_run_id"], "child run id", _ID)
        if not isinstance(arguments["reason"], str) or not arguments["reason"].strip() or len(arguments["reason"]) > 256:
            raise AgentCapabilityError("agent interrupt reason is invalid")
    elif capability_id == AGENT_WAIT_CAPABILITY:
        if set(arguments) != {"child_run_ids", "timeout_ms"}:
            raise AgentCapabilityError("agent wait arguments are incomplete")
        _identifier_list(arguments["child_run_ids"], "wait child run ids", required=True)
        _timeout(arguments["timeout_ms"])
    elif capability_id == AGENT_FAN_IN_CAPABILITY:
        if not {"child_run_ids", "policy"}.issubset(arguments) or set(arguments) - {"child_run_ids", "policy", "quorum"}:
            raise AgentCapabilityError("agent fan-in arguments are incomplete")
        count = len(_identifier_list(arguments["child_run_ids"], "fan-in child run ids", required=True))
        policy = arguments["policy"]
        if policy not in {"all", "any", "quorum"}:
            raise AgentCapabilityError("agent fan-in policy is invalid")
        quorum = arguments.get("quorum")
        if policy == "quorum":
            if not isinstance(quorum, int) or isinstance(quorum, bool) or not 1 <= quorum <= count:
                raise AgentCapabilityError("agent fan-in quorum is invalid")
        elif quorum is not None:
            raise AgentCapabilityError("agent fan-in quorum is invalid")
    elif capability_id == AGENT_LIST_CAPABILITY and (set(arguments) != {"include_messages"} or not isinstance(arguments["include_messages"], bool)):
        raise AgentCapabilityError("agent list arguments are invalid")
    elif capability_id == AGENT_PLAN_CAPABILITY:
        _validate_plan(arguments)


def _identifier_list(value: object, label: str, *, required: bool) -> tuple[str, ...]:
    if value is None and not required:
        return ()
    if not isinstance(value, list) or not value or len(value) > 128:
        raise AgentCapabilityError(f"agent {label} are invalid")
    result = tuple(_identifier(item, label, _ID) for item in value)
    if len(set(result)) != len(result):
        raise AgentCapabilityError(f"agent {label} are invalid")
    return result


def _refs(value: object, label: str, *, required: bool) -> tuple[str, ...]:
    if isinstance(value, str):
        values: object = [value]
    else:
        values = value
    if values is None and not required:
        return ()
    if not isinstance(values, list) or not values or len(values) > 128:
        raise AgentCapabilityError(f"agent {label} are invalid")
    refs = tuple(item for item in values if isinstance(item, str) and _CRP_REF.fullmatch(item))
    if len(refs) != len(values) or len(set(refs)) != len(refs):
        raise AgentCapabilityError(f"agent {label} are invalid")
    return refs


def _timeout(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 120_000:
        raise AgentCapabilityError("agent wait timeout is invalid")
    return value


def _budget(value: object) -> Mapping[str, object]:
    budget = _mapping(value, "child budget")
    fields = {"model_calls", "tool_calls", "input_tokens", "output_tokens", "wall_time_ms"}
    if set(budget) != fields:
        raise AgentCapabilityError("agent child budget shape is invalid")
    for field, item in budget.items():
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise AgentCapabilityError("agent child budget is invalid")
    if int(budget["wall_time_ms"]) > 86_400_000:
        raise AgentCapabilityError("agent child budget is invalid")
    return budget


def _validate_plan(arguments: Mapping[str, object]) -> None:
    mode = arguments.get("mode")
    plan_id = arguments.get("plan_id")
    if mode not in {"main_only", "cluster"}:
        raise AgentCapabilityError("agent plan mode is invalid")
    _identifier(plan_id, "plan id", _ID)
    if mode == "main_only":
        if set(arguments) != {"mode", "plan_id"}:
            raise AgentCapabilityError("main-only agent plan shape is invalid")
        return
    if set(arguments) != {"mode", "plan_id", "cluster_id", "assignments"}:
        raise AgentCapabilityError("cluster agent plan shape is invalid")
    _identifier(arguments.get("cluster_id"), "cluster id", _ID)
    assignments = arguments.get("assignments")
    if not isinstance(assignments, list) or not 1 <= len(assignments) <= 8:
        raise AgentCapabilityError("agent plan assignments are invalid")
    seen: set[str] = set()
    for assignment in assignments:
        payload = _mapping(assignment, "agent plan assignment")
        if set(payload) - {'division'} != {"assignment_id", "profile_id", "profile_revision", "task", "budget", "capability_ids", "expert", "skill"}:
            raise AgentCapabilityError("agent plan assignment shape is invalid")
        assignment_id = _identifier(payload["assignment_id"], "assignment id", _ID)
        if assignment_id in seen:
            raise AgentCapabilityError("agent plan assignment ids must be unique")
        seen.add(assignment_id)
        _identifier(payload["profile_id"], "assignment profile id", _ID)
        if not isinstance(payload["profile_revision"], int) or isinstance(payload["profile_revision"], bool) or payload["profile_revision"] < 1:
            raise AgentCapabilityError("agent plan profile revision is invalid")
        if not isinstance(payload["task"], str) or not payload["task"].strip() or len(payload["task"]) > 16_000:
            raise AgentCapabilityError("agent plan task is invalid")
        _budget(payload["budget"])
        _plan_capability_ids(payload["capability_ids"])
        _plan_expert(payload["expert"])
        _plan_skill(payload["skill"])
    if any('division' in item for item in assignments):
        from backend.shared.task_division_graph import validate_divisions
        validate_divisions(assignments, {item['assignment_id']:item['assignment_id'] for item in assignments})


def _plan_capability_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > 128:
        raise AgentCapabilityError("agent plan capabilities are invalid")
    values = tuple(_identifier(item, "agent plan capability", _ID) for item in value)
    if len(values) != len(set(values)):
        raise AgentCapabilityError("agent plan capabilities must be unique")
    return values


def _plan_expert(value: object) -> None:
    if value is None: return
    expert = _mapping(value, "agent plan expert")
    if set(expert) != {"expert_id", "task_intents", "budget"}:
        raise AgentCapabilityError("agent plan expert shape is invalid")
    _identifier(expert["expert_id"], "agent plan expert id", _ID)
    intents = expert["task_intents"]
    if not isinstance(intents, list) or not 1 <= len(intents) <= 16:
        raise AgentCapabilityError("agent plan expert intents are invalid")
    normalized = tuple(_identifier(item, "agent plan expert intent", _ID) for item in intents)
    if len(normalized) != len(set(normalized)):
        raise AgentCapabilityError("agent plan expert intents must be unique")
    _identifier(expert["budget"], "agent plan expert budget", _ID)


def _plan_skill(value: object) -> None:
    if value is None: return
    skill = _mapping(value, "agent plan skill")
    if set(skill) != {"skill_ids"}:
        raise AgentCapabilityError("agent plan skill shape is invalid")
    ids = skill["skill_ids"]
    if not isinstance(ids, list) or not 1 <= len(ids) <= 16:
        raise AgentCapabilityError("agent plan skills are invalid")
    normalized = tuple(_identifier(item, "agent plan skill id", _ID) for item in ids)
    if len(normalized) != len(set(normalized)):
        raise AgentCapabilityError("agent plan skills must be unique")


def _public_result(capability_id: str, request: _ProviderRequest, outcome: Mapping[str, object]) -> Mapping[str, object]:
    safe = _safe_projection(outcome)
    summary = safe.pop("summary", None)
    if not isinstance(summary, str) or not summary.strip():
        summary = f"{capability_id} completed"
    result = {key: value for key, value in safe.items() if key not in {"receipt_ref", "evidence_refs"}}
    refs = safe.get("evidence_refs", ())
    evidence_refs = [item for item in refs if isinstance(item, str) and _CRP_REF.fullmatch(item)] if isinstance(refs, Sequence) and not isinstance(refs, str) else []
    payload: dict[str, object] = {"summary": summary[:512], "result": result, "evidence_refs": evidence_refs}
    if capability_id in _MUTATING:
        payload["operation_receipt"] = {
            "schema_version": "1.0.0", "kind": "agent.operation.receipt.v1",
            "capability_id": capability_id, "operation_id": request.operation_id,
            "tool_call_id": request.tool_call_id, "turn_id": request.turn_id,
            "project_id": request.project_id, "status": "completed",
        }
    return payload


def _safe_projection(value: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or _SENSITIVE.search(key):
            continue
        safe = _safe_value(item)
        if safe is not _UNSAFE:
            result[key] = safe
    return result


_UNSAFE = object()


def _safe_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > 4096 or "\\" in value or value.startswith(("/", "~")):
            return _UNSAFE
        return value
    if isinstance(value, Mapping):
        return _safe_projection(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        values = [_safe_value(item) for item in value]
        return [item for item in values if item is not _UNSAFE][:128]
    return _UNSAFE


def _reject_sensitive(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or _SENSITIVE.search(key):
                raise AgentCapabilityError("agent arguments contain an unsafe field")
            _reject_sensitive(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _reject_sensitive(item)


def _reject_plan_sensitive(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = key.casefold().replace("-", "_") if isinstance(key, str) else ""
            if (
                not normalized
                or normalized in _PLAN_SENSITIVE_FIELDS
                or normalized.endswith(("_secret", "_token", "_endpoint", "_path", "_context"))
            ):
                raise AgentCapabilityError("agent plan contains an unsafe field")
            _reject_plan_sensitive(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _reject_plan_sensitive(item)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise AgentCapabilityError(f"agent {label} is invalid")
    return value


def _identifier(value: object, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise AgentCapabilityError(f"agent {label} is invalid")
    return value
