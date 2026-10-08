from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal, Protocol


ToolSource = Literal["core", "platform", "plugin", "mcp", "workflow"]
ToolEffect = Literal["read", "write", "external", "platform", "delete"]
ToolDestination = Literal["local", "provider", "mcp", "platform"]
OperationSemantics = Literal["none", "read_only", "receipt_required"]
ToolExecutionMode = Literal["parallel", "exclusive"]
ToolIdempotency = Literal["idempotent", "verify_before_retry", "never_retry"]
ToolMutability = Literal["read_only", "reversible", "irreversible"]
ToolEgressClass = Literal["none", "local", "remote"]

_SOURCES = frozenset({"core", "platform", "plugin", "mcp", "workflow"})
_EFFECTS = frozenset({"read", "write", "external", "platform", "delete"})
_DESTINATIONS = frozenset({"local", "provider", "mcp", "platform"})
_SEMANTICS = frozenset({"none", "read_only", "receipt_required"})
_EXECUTION_MODES = frozenset({"parallel", "exclusive"})
_IDEMPOTENCY = frozenset({"idempotent", "verify_before_retry", "never_retry"})
_MUTABILITY = frozenset({"read_only", "reversible", "irreversible"})
_EGRESS_CLASSES = frozenset({"none", "local", "remote"})
_CONNECTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ToolContractError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ToolConnectionIdentity:
    """Non-secret identity for one reviewed external Tool connection."""

    protocol: Literal["mcp"]
    server_id: str
    protocol_version: str
    manifest_revision: int
    endpoint_identity: str
    credential_subject_id: str
    transport_generation: int
    catalog_revision: int
    tool_schema_revision: int

    def __post_init__(self) -> None:
        if self.protocol != "mcp":
            raise ToolContractError("tool connection protocol is unsupported")
        for label, value in (
            ("server_id", self.server_id),
            ("protocol_version", self.protocol_version),
            ("endpoint_identity", self.endpoint_identity),
            ("credential_subject_id", self.credential_subject_id),
        ):
            if not isinstance(value, str) or not _CONNECTION_ID.fullmatch(value):
                raise ToolContractError(f"tool connection {label} is invalid")
        for label, value in (
            ("manifest_revision", self.manifest_revision),
            ("transport_generation", self.transport_generation),
            ("catalog_revision", self.catalog_revision),
            ("tool_schema_revision", self.tool_schema_revision),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ToolContractError(f"tool connection {label} must be positive")


class LegacyCapabilityView(Protocol):
    capability_id: str
    version: int
    mode: str
    requires_approval: bool
    operation_semantics: str
    input_schema_uri: str
    output_schema_uri: str


@dataclass(frozen=True, slots=True)
class ToolRetryPolicy:
    max_attempts: int
    backoff_ms: int
    retryable_error_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.max_attempts > 10:
            raise ToolContractError("retry max_attempts is outside the supported range")
        if self.backoff_ms < 0 or self.backoff_ms > 300_000:
            raise ToolContractError("retry backoff_ms is outside the supported range")
        _validate_names(self.retryable_error_codes, "retryable error code")
        if self.max_attempts == 1 and self.retryable_error_codes:
            raise ToolContractError("single-attempt policy cannot declare retryable errors")


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    tool_id: str
    version: int
    display_name: str
    description: str
    source: ToolSource
    owner_id: str
    effect: ToolEffect
    data_classes: tuple[str, ...]
    destination: ToolDestination
    input_schema_uri: str
    output_schema_uri: str
    receipt_schema_uri: str | None
    operation_semantics: OperationSemantics
    execution_mode: ToolExecutionMode
    resource_locks: tuple[str, ...]
    idempotency: ToolIdempotency
    retry_policy: ToolRetryPolicy
    verification_tool_id: str | None
    compensation_tool_id: str | None
    mutability: ToolMutability
    egress_class: ToolEgressClass
    network_scope: tuple[str, ...]
    data_egress_scope: tuple[str, ...]
    timeout_ms: int
    required_scopes: tuple[str, ...]
    boundary_requirements: tuple[str, ...]
    available: bool = True
    connection_identity: ToolConnectionIdentity | None = None
    nested_model_handle_budget: int = 1

    def __post_init__(self) -> None:
        for label, value in (
            ("tool_id", self.tool_id),
            ("display_name", self.display_name),
            ("description", self.description),
            ("owner_id", self.owner_id),
            ("input_schema_uri", self.input_schema_uri),
            ("output_schema_uri", self.output_schema_uri),
        ):
            if not _text(value):
                raise ToolContractError(f"{label} must be non-empty")
        if self.version < 1:
            raise ToolContractError("version must be positive")
        if self.source not in _SOURCES:
            raise ToolContractError("source is unsupported")
        if self.effect not in _EFFECTS:
            raise ToolContractError("effect is unsupported")
        if self.destination not in _DESTINATIONS:
            raise ToolContractError("destination is unsupported")
        if self.operation_semantics not in _SEMANTICS:
            raise ToolContractError("operation semantics are unsupported")
        if self.execution_mode not in _EXECUTION_MODES:
            raise ToolContractError("execution mode is unsupported")
        if self.idempotency not in _IDEMPOTENCY:
            raise ToolContractError("idempotency is unsupported")
        if self.mutability not in _MUTABILITY:
            raise ToolContractError("mutability is unsupported")
        if self.egress_class not in _EGRESS_CLASSES:
            raise ToolContractError("egress class is unsupported")
        if self.timeout_ms < 1 or self.timeout_ms > 3_600_000:
            raise ToolContractError("timeout_ms is outside the supported range")
        if (
            not isinstance(self.nested_model_handle_budget, int)
            or isinstance(self.nested_model_handle_budget, bool)
            or not 1 <= self.nested_model_handle_budget <= 16
        ):
            raise ToolContractError("nested model handle budget is outside the supported range")
        _validate_names(self.data_classes, "data class")
        _validate_names(self.resource_locks, "resource lock")
        _validate_names(self.network_scope, "network scope")
        _validate_names(self.data_egress_scope, "data egress scope")
        _validate_names(self.required_scopes, "required scope")
        _validate_names(self.boundary_requirements, "boundary requirement")
        if self.effect == "read" and self.operation_semantics not in {"none", "read_only"}:
            raise ToolContractError("read tool cannot require a mutation receipt")
        if self.effect == "read" and self.mutability != "read_only":
            raise ToolContractError("read tool must be read-only")
        if self.effect in {"write", "external", "platform", "delete"}:
            if self.operation_semantics != "receipt_required" or not _text(self.receipt_schema_uri):
                raise ToolContractError("side-effecting tool requires receipt semantics and schema")
        if self.destination == "local" and self.effect == "external":
            raise ToolContractError("external tool requires a non-local destination")
        if (self.execution_mode == "parallel" and self.mutability == "irreversible"
                and not is_external_task_execution_contract(tool_contract_identity(self))):
            raise ToolContractError("irreversible tool must execute exclusively")
        if self.effect == "delete" and self.execution_mode != "exclusive":
            raise ToolContractError("delete tool must execute exclusively")
        if self.idempotency == "verify_before_retry" and not _text(self.verification_tool_id):
            raise ToolContractError("verify-before-retry tool requires a verification tool")
        if self.idempotency != "verify_before_retry" and self.verification_tool_id is not None:
            raise ToolContractError("verification tool requires verify-before-retry idempotency")
        if self.idempotency == "never_retry" and self.retry_policy.max_attempts != 1:
            raise ToolContractError("never-retry tool must use one attempt")
        if self.egress_class == "remote":
            if self.destination == "local" or not self.network_scope or not self.data_egress_scope:
                raise ToolContractError("remote egress requires destination, network and data scopes")
        elif self.network_scope or self.data_egress_scope:
            raise ToolContractError("network and data egress scopes require remote egress")
        if self.effect == "external" and self.egress_class != "remote":
            raise ToolContractError("external tool requires remote egress")
        if self.compensation_tool_id is not None and self.mutability == "read_only":
            raise ToolContractError("read-only tool cannot declare compensation")
        if self.source == "mcp":
            if self.destination != "mcp" or self.connection_identity is None:
                raise ToolContractError("MCP tool requires an MCP connection identity")
            if self.connection_identity.server_id != self.owner_id:
                raise ToolContractError("MCP tool owner must match its connection server")
        elif self.connection_identity is not None:
            raise ToolContractError("only MCP tools can declare a connection identity")

    @property
    def idempotent(self) -> bool:
        """Compatibility projection for consumers that only understand a boolean."""
        return self.idempotency == "idempotent"


def tool_from_legacy_capability(
    capability: LegacyCapabilityView, *, boundary_requirements: tuple[str, ...] = (),
) -> ToolDefinition:
    """Project the V1 capability into a conservative V2 Tool contract."""
    effect = capability.mode if capability.mode in _EFFECTS else "platform"
    destination: ToolDestination = {
        "external": "provider",
        "platform": "platform",
    }.get(effect, "local")  # type: ignore[assignment]
    mutating = effect in {"write", "external", "platform", "delete"}
    semantics: OperationSemantics = "receipt_required" if mutating else (
        "read_only" if capability.operation_semantics == "read_only" else "none"
    )
    receipt_schema = "crp://schemas/ai/tool-receipt-v1" if mutating else None
    requirements = (("legacy_approval",) if capability.requires_approval else ()) + boundary_requirements
    draft_create_only = 'draft_create_only' in boundary_requirements
    if draft_create_only and effect != 'write':
        raise ToolContractError('draft_create_only requires a local write capability')
    return ToolDefinition(
        tool_id=capability.capability_id,
        version=capability.version,
        display_name=capability.capability_id,
        description=f"Legacy capability {capability.capability_id}",
        source="core",
        owner_id="ai-kernel-v1",
        effect=effect,  # type: ignore[arg-type]
        data_classes=("unclassified",),
        destination=destination,
        input_schema_uri=capability.input_schema_uri,
        output_schema_uri=capability.output_schema_uri,
        receipt_schema_uri=receipt_schema,
        operation_semantics=semantics,
        execution_mode="parallel" if effect == "read" or draft_create_only else "exclusive",
        resource_locks=() if effect == "read" else (f"legacy:{capability.capability_id}",),
        idempotency="idempotent" if effect == "read" else "never_retry",
        retry_policy=ToolRetryPolicy(
            max_attempts=2 if effect == "read" else 1,
            backoff_ms=250 if effect == "read" else 0,
            retryable_error_codes=("timeout", "temporarily_unavailable") if effect == "read" else (),
        ),
        verification_tool_id=None,
        compensation_tool_id=None,
        mutability="read_only" if effect == "read" else ("reversible" if draft_create_only else "irreversible"),
        egress_class="remote" if effect == "external" else ("local" if mutating else "none"),
        network_scope=("configured_provider_endpoint",) if effect == "external" else (),
        data_egress_scope=("unclassified",) if effect == "external" else (),
        timeout_ms=60_000,
        required_scopes=(),
        boundary_requirements=requirements,
    )


def tool_from_capability(capability: LegacyCapabilityView) -> ToolDefinition:
    """Return a validated native Tool contract or the conservative legacy projection."""
    native = getattr(capability, "tool_definition", None)
    if native is None:
        return tool_from_legacy_capability(capability)
    if not isinstance(native, ToolDefinition):
        raise ToolContractError("capability native tool definition is invalid")
    expected = {
        "tool_id": capability.capability_id,
        "version": capability.version,
        "effect": capability.mode,
        "operation_semantics": capability.operation_semantics,
        "input_schema_uri": capability.input_schema_uri,
        "output_schema_uri": capability.output_schema_uri,
    }
    drifted = tuple(
        field for field, value in expected.items()
        if getattr(native, field) != value
    )
    if drifted:
        raise ToolContractError(
            f"capability native tool identity drifted: {', '.join(drifted)}"
        )
    return native


def tool_destination_identity(tool: ToolDefinition, capability_id: str) -> str:
    """Return the non-secret execution destination identity for one Tool.

    The identity is internal Boundary matching material.  Presentation layers
    may compare it, but must not expose the complete value in public DTOs.
    """
    if tool.tool_id != capability_id:
        raise ToolContractError("tool destination capability identity drifted")
    if tool.destination == "local":
        return "local-runtime"
    if tool.source == "mcp":
        identity = tool.connection_identity
        if identity is None:
            raise ToolContractError("MCP tool connection identity is unavailable")
        return (
            f"mcp:{identity.server_id}:p{identity.protocol_version}:"
            f"m{identity.manifest_revision}:e{identity.endpoint_identity}:"
            f"c{identity.credential_subject_id}:g{identity.transport_generation}:"
            f"r{identity.catalog_revision}:s{identity.tool_schema_revision}"
        )
    return f"{tool.destination}-runtime:{capability_id}"


def tool_boundary_target_identity(tool: ToolDefinition, capability_id: str) -> str:
    """Versioned internal identity used by persistent Boundary grants.

    MCP retains its complete connection identity.  Other sources bind enough
    immutable contract coordinates that an old grant cannot authorize a
    replacement Tool sharing only a display or stable capability identifier.
    """
    if tool.tool_id != capability_id:
        raise ToolContractError("tool Boundary target capability identity drifted")
    contract_binding = tool_contract_binding_identity(tool)
    destination = tool_destination_identity(tool, capability_id)
    return f"{tool.tool_id}@{destination}:{contract_binding}"


def tool_contract_binding_identity(tool: ToolDefinition) -> str:
    """Return a non-reversible identity for the complete execution contract."""
    canonical = json.dumps(
        tool_contract_identity(tool), ensure_ascii=True, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"contract-sha256:{hashlib.sha256(canonical).hexdigest()}"


_CONTRACT_IDENTITY_FIELDS = frozenset({
    "tool_id", "version", "effect", "source", "owner_id", "destination", "operation_semantics",
    "receipt_schema_uri", "input_schema_uri", "output_schema_uri",
    "data_classes", "mutability", "egress_class", "network_scope",
    "data_egress_scope", "required_scopes", "boundary_requirements",
    "verification_tool_id", "compensation_tool_id", "available",
    "execution_mode", "resource_locks", "idempotency", "retry_policy",
    "timeout_ms", "connection_identity", "nested_model_handle_budget",
})
_CONTRACT_IDENTITY_TUPLES = frozenset({
    "data_classes", "network_scope", "data_egress_scope", "required_scopes",
    "boundary_requirements", "resource_locks",
})


def tool_contract_identity(tool: ToolDefinition) -> dict[str, object]:
    """Security-relevant immutable metadata, excluding model-facing prose."""
    return {
        "tool_id": tool.tool_id,
        "version": tool.version,
        "effect": tool.effect,
        "source": tool.source,
        "owner_id": tool.owner_id,
        "destination": tool.destination,
        "operation_semantics": tool.operation_semantics,
        "receipt_schema_uri": tool.receipt_schema_uri,
        "input_schema_uri": tool.input_schema_uri,
        "output_schema_uri": tool.output_schema_uri,
        "data_classes": list(tool.data_classes),
        "mutability": tool.mutability,
        "egress_class": tool.egress_class,
        "network_scope": list(tool.network_scope),
        "data_egress_scope": list(tool.data_egress_scope),
        "required_scopes": list(tool.required_scopes),
        "boundary_requirements": list(tool.boundary_requirements),
        "verification_tool_id": tool.verification_tool_id,
        "compensation_tool_id": tool.compensation_tool_id,
        "available": tool.available,
        "execution_mode": tool.execution_mode,
        "resource_locks": list(tool.resource_locks),
        "idempotency": tool.idempotency,
        "retry_policy": {
            "max_attempts": tool.retry_policy.max_attempts,
            "backoff_ms": tool.retry_policy.backoff_ms,
            "retryable_error_codes": list(tool.retry_policy.retryable_error_codes),
        },
        "timeout_ms": tool.timeout_ms,
        "nested_model_handle_budget": tool.nested_model_handle_budget,
        "connection_identity": (
            tool_connection_identity(tool.connection_identity)
            if tool.connection_identity is not None else None
        ),
    }


def is_external_task_execution_contract(identity: Mapping[str, object]) -> bool:
    """只核对注册契约；每用户并发与真实启动资格仍由原宿主核验。"""
    expected = {
        "tool_id": "external.task.execute", "version": 1,
        "source": "core", "owner_id": "external-task-runner",
        "effect": "external", "destination": "provider",
        "operation_semantics": "receipt_required", "execution_mode": "parallel",
        "mutability": "irreversible", "egress_class": "remote", "idempotency": "never_retry",
        "retry_policy": {"max_attempts": 1, "backoff_ms": 0, "retryable_error_codes": []},
        "timeout_ms": 1_230_000, "boundary_requirements": ["external_execute"],
        "available": True, "verification_tool_id": None, "compensation_tool_id": None,
        "connection_identity": None, "nested_model_handle_budget": 1,
    }
    retry = identity.get("retry_policy")
    return (
        all(identity.get(name) == value for name, value in expected.items())
        and type(identity.get("version")) is int and type(identity.get("timeout_ms")) is int
        and identity.get("available") is True
        and isinstance(retry, Mapping)
        and type(retry.get("max_attempts")) is int and type(retry.get("backoff_ms")) is int
        and bool(identity.get("data_classes")) and bool(identity.get("network_scope"))
        and bool(identity.get("data_egress_scope")) and _text(identity.get("receipt_schema_uri"))
    )


def validate_tool_contract_identity(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ToolContractError("tool contract identity must be an object")
    identity = dict(value)
    identity.setdefault("connection_identity", None)
    identity.setdefault("nested_model_handle_budget", 1)
    if set(identity) != _CONTRACT_IDENTITY_FIELDS:
        raise ToolContractError("tool contract identity fields are invalid")
    for field in _CONTRACT_IDENTITY_TUPLES:
        items = identity[field]
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            raise ToolContractError(f"tool contract identity {field} must be an array")
        normalized = tuple(items)
        if any(not isinstance(item, str) or not item.strip() for item in normalized):
            raise ToolContractError(f"tool contract identity {field} is invalid")
        if len(normalized) != len(set(normalized)):
            raise ToolContractError(f"tool contract identity {field} must be unique")
        identity[field] = list(normalized)
    nullable = {"receipt_schema_uri", "verification_tool_id", "compensation_tool_id"}
    special = {"available", "retry_policy", "timeout_ms", "version", "connection_identity", "nested_model_handle_budget"}
    for field in _CONTRACT_IDENTITY_FIELDS - _CONTRACT_IDENTITY_TUPLES - nullable - special:
        if not isinstance(identity[field], str) or not str(identity[field]).strip():
            raise ToolContractError(f"tool contract identity {field} is invalid")
    for field in nullable:
        if identity[field] is not None and (
            not isinstance(identity[field], str) or not str(identity[field]).strip()
        ):
            raise ToolContractError(f"tool contract identity {field} is invalid")
    if not isinstance(identity["available"], bool):
        raise ToolContractError("tool contract identity available is invalid")
    version = identity["version"]
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ToolContractError("tool contract identity version is invalid")
    retry = identity["retry_policy"]
    if not isinstance(retry, Mapping) or set(retry) != {
        "max_attempts", "backoff_ms", "retryable_error_codes",
    }:
        raise ToolContractError("tool contract identity retry policy is invalid")
    retry_codes = retry.get("retryable_error_codes")
    if not isinstance(retry_codes, Sequence) or isinstance(retry_codes, (str, bytes)):
        raise ToolContractError("tool contract identity retry codes are invalid")
    try:
        retry_policy = ToolRetryPolicy(
            max_attempts=retry.get("max_attempts"),  # type: ignore[arg-type]
            backoff_ms=retry.get("backoff_ms"),  # type: ignore[arg-type]
            retryable_error_codes=tuple(retry_codes),  # type: ignore[arg-type]
        )
    except (TypeError, ToolContractError) as error:
        raise ToolContractError("tool contract identity retry policy is invalid") from error
    identity["retry_policy"] = {
        "max_attempts": retry_policy.max_attempts,
        "backoff_ms": retry_policy.backoff_ms,
        "retryable_error_codes": list(retry_policy.retryable_error_codes),
    }
    timeout_ms = identity["timeout_ms"]
    if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or not 1 <= timeout_ms <= 3_600_000:
        raise ToolContractError("tool contract identity timeout is invalid")
    nested_budget = identity["nested_model_handle_budget"]
    if not isinstance(nested_budget, int) or isinstance(nested_budget, bool) or not 1 <= nested_budget <= 16:
        raise ToolContractError("tool contract identity nested model handle budget is invalid")
    enums = {
        "source": _SOURCES,
        "effect": _EFFECTS,
        "destination": _DESTINATIONS,
        "operation_semantics": _SEMANTICS,
        "mutability": _MUTABILITY,
        "egress_class": _EGRESS_CLASSES,
        "execution_mode": _EXECUTION_MODES,
        "idempotency": _IDEMPOTENCY,
    }
    for field, allowed in enums.items():
        if identity[field] not in allowed:
            raise ToolContractError(f"tool contract identity {field} is unsupported")
    connection = identity["connection_identity"]
    if connection is None:
        identity["connection_identity"] = None
    else:
        identity["connection_identity"] = tool_connection_identity(
            validate_tool_connection_identity(connection)
        )
    if identity["source"] == "mcp":
        connection = identity["connection_identity"]
        if not isinstance(connection, Mapping) or connection.get("server_id") != identity["owner_id"]:
            raise ToolContractError("MCP tool contract connection identity is invalid")
    elif identity["connection_identity"] is not None:
        raise ToolContractError("non-MCP tool contract cannot declare a connection identity")
    return identity


def tool_connection_identity(identity: ToolConnectionIdentity) -> dict[str, object]:
    return {
        "protocol": identity.protocol,
        "server_id": identity.server_id,
        "protocol_version": identity.protocol_version,
        "manifest_revision": identity.manifest_revision,
        "endpoint_identity": identity.endpoint_identity,
        "credential_subject_id": identity.credential_subject_id,
        "transport_generation": identity.transport_generation,
        "catalog_revision": identity.catalog_revision,
        "tool_schema_revision": identity.tool_schema_revision,
    }


def validate_tool_connection_identity(value: object) -> ToolConnectionIdentity:
    if not isinstance(value, Mapping):
        raise ToolContractError("tool connection identity must be an object")
    fields = {
        "protocol", "server_id", "protocol_version", "manifest_revision",
        "endpoint_identity", "credential_subject_id", "transport_generation",
        "catalog_revision", "tool_schema_revision",
    }
    if set(value) != fields:
        raise ToolContractError("tool connection identity fields are invalid")
    try:
        return ToolConnectionIdentity(**dict(value))  # type: ignore[arg-type]
    except TypeError as error:
        raise ToolContractError("tool connection identity is invalid") from error


def tool_matches_contract_identity(tool: ToolDefinition, value: object) -> bool:
    try:
        identity = validate_tool_contract_identity(value)
    except ToolContractError:
        return False
    return identity == tool_contract_identity(tool)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _validate_names(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ToolContractError(f"{label} values must be unique")
    if any(not _text(value) for value in values):
        raise ToolContractError(f"{label} values must be non-empty")
