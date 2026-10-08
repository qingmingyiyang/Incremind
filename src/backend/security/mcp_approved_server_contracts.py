from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from core.ai_tooling import ToolRetryPolicy
from core.mcp_host.host import MCPHostConnectionConfig, MCPToolPolicy
from core.mcp_host.header_projection import (
    EMPTY_HEADER_PROJECTION,
    HeaderProjectionError,
    HeaderProjectionPlan,
    compile_header_projection,
)
from core.mcp_host.stdio_config import (
    MCPStdioApprovedLaunchManifestStorePort,
    MCPStdioLaunchManifest,
)
from core.mcp_host.streamable_http_config import (
    MCPStreamableHTTPApprovedManifestStorePort,
    MCPStreamableHTTPManifest,
)


_MAX_FILE_BYTES = 512 * 1024
_MAX_SERVERS = 16
_MAX_TOOLS_PER_SERVER = 64
_ROOT_FIELDS = frozenset({"schema_version", "servers"})
_LEGACY_RECORD_FIELDS = frozenset({
    "server_id", "enabled", "approval_status", "approval_revision",
    "host_connection", "launch_manifest", "tool_policies",
})
_RECORD_FIELDS = frozenset({
    "server_id", "enabled", "approval_status", "approval_revision",
    "transport_kind", "host_connection", "connection_manifest", "tool_policies",
})
_PROFILED_RECORD_FIELDS = _RECORD_FIELDS | frozenset({"protocol_profile"})
_HOST_FIELDS = frozenset({
    "server_id", "manifest_revision", "endpoint_identity", "credential_subject_id",
    "transport_generation", "catalog_revision",
})
_PROFILED_HOST_FIELDS = _HOST_FIELDS | frozenset({"protocol_profile"})
_LAUNCH_FIELDS = frozenset({
    "server_id", "manifest_revision", "endpoint_identity", "credential_subject_id",
    "transport_generation", "approval_revision", "approval_status", "executable",
    "argv", "cwd", "environment", "secret_env_refs",
})
_PROFILED_LAUNCH_FIELDS = _LAUNCH_FIELDS | frozenset({"protocol_profile"})
_HTTP_FIELDS = frozenset({
    "server_id", "manifest_revision", "endpoint_identity", "credential_subject_id",
    "transport_generation", "approval_revision", "approval_status", "endpoint_url",
    "headers", "secret_header_refs", "timeout_seconds", "max_response_bytes", "max_sse_events",
})
_PROFILED_HTTP_FIELDS = _HTTP_FIELDS | frozenset({"protocol_profile"})
_POLICY_FIELDS = frozenset({
    "tool_name", "tool_id", "version", "display_name", "description", "effect",
    "data_classes", "input_schema_uri", "output_schema_uri", "receipt_schema_uri",
    "operation_semantics", "execution_mode", "resource_locks", "idempotency",
    "retry_policy", "verification_tool_id", "compensation_tool_id", "mutability",
    "egress_class", "network_scope", "data_egress_scope", "timeout_ms",
    "required_scopes", "boundary_requirements", "requires_approval",
    "tool_schema_revision", "reviewed_input_schema", "reviewed_output_schema",
    "available", "remote_receipt_field", "reviewed_receipt_schema",
})
_RETRY_FIELDS = frozenset({"max_attempts", "backoff_ms", "retryable_error_codes"})


class MCPApprovedServerStoreError(ValueError):
    """Safe persistent-authority failure without deployment or secret details."""


@dataclass(frozen=True, slots=True)
class MCPApprovedServer:
    server_id: str
    enabled: bool
    approval_revision: int
    host_connection: MCPHostConnectionConfig
    transport_kind: str
    connection_manifest: MCPStdioLaunchManifest | MCPStreamableHTTPManifest
    tool_policies: tuple[MCPToolPolicy, ...]
    # A rejected parameter-header annotation must remain local to its reviewed
    # policy.  Retain only a bounded count so diagnostics cannot turn the
    # rejected Tool identity, schema, or header name into an API disclosure.
    header_policy_rejected_count: int = 0

    @property
    def launch_manifest(self) -> MCPStdioLaunchManifest:
        if not isinstance(self.connection_manifest, MCPStdioLaunchManifest):
            raise MCPApprovedServerStoreError("MCP approved server is not stdio")
        return self.connection_manifest


@dataclass(frozen=True, slots=True)
class MCPApprovedServerSnapshot(MCPStdioApprovedLaunchManifestStorePort, MCPStreamableHTTPApprovedManifestStorePort):
    _records: Mapping[str, MCPApprovedServer]

    def get_approved(self, server_id: str) -> MCPStdioLaunchManifest | None:
        record = self._records.get(server_id)
        if record is None or not record.enabled:
            return None
        return record.connection_manifest if isinstance(record.connection_manifest, MCPStdioLaunchManifest) else None

    def get_approved_http(self, server_id: str) -> MCPStreamableHTTPManifest | None:
        record = self._records.get(server_id)
        if record is None or not record.enabled:
            return None
        return record.connection_manifest if isinstance(record.connection_manifest, MCPStreamableHTTPManifest) else None

    @property
    def enabled_servers(self) -> tuple[MCPApprovedServer, ...]:
        return tuple(record for record in self._records.values() if record.enabled)

    @property
    def servers(self) -> tuple[MCPApprovedServer, ...]:
        return tuple(self._records.values())


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MCPApprovedServerStoreError(
                "MCP approved server authority contains duplicate fields"
            )
        result[key] = value
    return result


def parse_mcp_approved_server_payload(raw: bytes) -> MCPApprovedServerSnapshot:
    """Parse the one reviewed authority shape without exposing its contents.

    Durable migration snapshots and the legacy JSON reader share this parser;
    no migration path is allowed to have a more permissive schema.
    """
    if not isinstance(raw, bytes) or len(raw) > _MAX_FILE_BYTES:
        raise MCPApprovedServerStoreError("MCP approved server authority exceeds limits")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
        records = _records(value)
    except MCPApprovedServerStoreError:
        raise
    except Exception:
        raise MCPApprovedServerStoreError("MCP approved server authority is invalid") from None
    return MCPApprovedServerSnapshot(MappingProxyType(records))


def canonical_mcp_approved_server_payload(value: object) -> tuple[bytes, MCPApprovedServerSnapshot]:
    """Validate then canonicalize a candidate for immutable SQLite storage."""
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        raise MCPApprovedServerStoreError("MCP approved server authority is invalid") from None
    snapshot = parse_mcp_approved_server_payload(raw)
    return raw, snapshot


def _records(value: object) -> dict[str, MCPApprovedServer]:
    root = _strict_mapping(value, _ROOT_FIELDS)
    schema_version = root["schema_version"]
    if schema_version not in {"1.0.0", "1.1.0", "1.2.0"}:
        raise MCPApprovedServerStoreError("MCP approved server authority version is invalid")
    items = _sequence(root["servers"], _MAX_SERVERS)
    records: dict[str, MCPApprovedServer] = {}
    for item in items:
        record = _record(item, schema_version=schema_version)
        if record.server_id in records:
            raise MCPApprovedServerStoreError("MCP approved server authority contains duplicates")
        records[record.server_id] = record
    return dict(sorted(records.items()))


def _record(value: object, *, schema_version: object) -> MCPApprovedServer:
    legacy = schema_version == "1.0.0"
    profiled = schema_version == "1.2.0"
    raw = _strict_mapping(value, _LEGACY_RECORD_FIELDS if legacy else (_PROFILED_RECORD_FIELDS if profiled else _RECORD_FIELDS))
    server_id = _text(raw["server_id"])
    enabled = raw["enabled"]
    revision = _positive(raw["approval_revision"])
    if not server_id or not isinstance(enabled, bool) or raw["approval_status"] != "approved":
        raise MCPApprovedServerStoreError("MCP approved server record is invalid")
    protocol_profile = raw.get("protocol_profile", "legacy_2025_11_25")
    if protocol_profile not in {"legacy_2025_11_25", "stateless_2026_07_28"}:
        raise MCPApprovedServerStoreError("MCP approved protocol profile is invalid")
    host = _host(raw["host_connection"], profiled=profiled, protocol_profile=protocol_profile)
    transport_kind = "stdio" if legacy else raw["transport_kind"]
    manifest_value = raw["launch_manifest"] if legacy else raw["connection_manifest"]
    if transport_kind == "stdio":
        manifest: MCPStdioLaunchManifest | MCPStreamableHTTPManifest = _launch(manifest_value, profiled=profiled, protocol_profile=protocol_profile)
        secret_refs = manifest.secret_env_refs
    elif transport_kind == "streamable_http":
        manifest = _http_manifest(manifest_value, profiled=profiled, protocol_profile=protocol_profile)
        secret_refs = manifest.secret_header_refs
    else:
        raise MCPApprovedServerStoreError("MCP approved transport kind is invalid")
    secret_prefix = f"mcp:{server_id}:"
    if any(
        not secret_ref.startswith(secret_prefix) or len(secret_ref) == len(secret_prefix)
        for secret_ref in secret_refs.values()
    ):
        raise MCPApprovedServerStoreError("MCP approved secret reference identity drifted")
    if (
        host.server_id != server_id or manifest.server_id != server_id
        or manifest.approval_revision != revision or manifest.approval_status != "approved"
        or host.manifest_revision != manifest.manifest_revision
        or host.endpoint_identity != manifest.endpoint_identity
        or host.credential_subject_id != manifest.credential_subject_id
        or host.transport_generation != manifest.transport_generation
        or host.protocol_profile != protocol_profile
        or manifest.protocol_profile != protocol_profile
    ):
        raise MCPApprovedServerStoreError("MCP approved server identity drifted")
    raw_policies = _sequence(raw["tool_policies"], _MAX_TOOLS_PER_SERVER)
    policies = tuple(_policy(item, defer_parameter_headers=True) for item in raw_policies)
    if len({policy.tool_name for policy in policies}) != len(policies) or len({policy.tool_id for policy in policies}) != len(policies):
        raise MCPApprovedServerStoreError("MCP approved server tool policies are duplicated")
    _validate_verification_links(policies)
    header_policy_rejected_count = 0
    if transport_kind == "streamable_http" and protocol_profile == "stateless_2026_07_28":
        accepted: list[MCPToolPolicy] = []
        for policy in policies:
            try:
                plan = compile_header_projection(policy.reviewed_input_schema)
            except HeaderProjectionError:
                # Annotation compilation is policy-local.  A bad projection
                # must not make unrelated reviewed tools unavailable.
                header_policy_rejected_count += 1
                continue
            accepted.append(_with_parameter_header_plan(policy, plan))
        policies = tuple(accepted)
        # A rejected header policy cannot silently remove an internal status
        # Tool while leaving its mutable target enabled.
        _validate_verification_links(policies)
    if not enabled:
        # Persisted but disabled records are retained for audit and can never be
        # resolved by the approved launch protocol.
        return MCPApprovedServer(
            server_id, False, revision, host, str(transport_kind), manifest,
            policies, header_policy_rejected_count,
        )
    return MCPApprovedServer(
        server_id, True, revision, host, str(transport_kind), manifest,
        policies, header_policy_rejected_count,
    )


def _host(value: object, *, profiled: bool, protocol_profile: str) -> MCPHostConnectionConfig:
    raw = _strict_mapping(value, _PROFILED_HOST_FIELDS if profiled else _HOST_FIELDS)
    if profiled and raw.get("protocol_profile") != protocol_profile:
        raise MCPApprovedServerStoreError("MCP approved Host protocol profile drifted")
    raw.setdefault("protocol_profile", protocol_profile)
    try:
        return MCPHostConnectionConfig(**raw)  # type: ignore[arg-type]
    except Exception:
        raise MCPApprovedServerStoreError("MCP approved Host connection is invalid") from None


def _launch(value: object, *, profiled: bool, protocol_profile: str) -> MCPStdioLaunchManifest:
    raw = _strict_mapping(value, _PROFILED_LAUNCH_FIELDS if profiled else _LAUNCH_FIELDS)
    if profiled and raw.get("protocol_profile") != protocol_profile:
        raise MCPApprovedServerStoreError("MCP approved launch protocol profile drifted")
    raw.setdefault("protocol_profile", protocol_profile)
    for name in ("environment", "secret_env_refs"):
        if raw[name] is not None and not isinstance(raw[name], Mapping):
            raise MCPApprovedServerStoreError("MCP approved launch environment is invalid")
    try:
        return MCPStdioLaunchManifest(
            server_id=raw["server_id"], manifest_revision=raw["manifest_revision"],
            endpoint_identity=raw["endpoint_identity"], credential_subject_id=raw["credential_subject_id"],
            transport_generation=raw["transport_generation"], approval_revision=raw["approval_revision"],
            approval_status=raw["approval_status"], executable=raw["executable"],
            argv=tuple(_sequence(raw["argv"], 64)), cwd=raw["cwd"],
            environment=dict(raw["environment"]) if isinstance(raw["environment"], Mapping) else None,
            secret_env_refs=dict(raw["secret_env_refs"]) if isinstance(raw["secret_env_refs"], Mapping) else None,
            protocol_profile=raw["protocol_profile"],
        )
    except Exception:
        raise MCPApprovedServerStoreError("MCP approved launch manifest is invalid") from None


def _http_manifest(value: object, *, profiled: bool, protocol_profile: str) -> MCPStreamableHTTPManifest:
    raw = _strict_mapping(value, _PROFILED_HTTP_FIELDS if profiled else _HTTP_FIELDS)
    if profiled and raw.get("protocol_profile") != protocol_profile:
        raise MCPApprovedServerStoreError("MCP approved HTTP protocol profile drifted")
    raw.setdefault("protocol_profile", protocol_profile)
    for name in ("headers", "secret_header_refs"):
        if raw[name] is not None and not isinstance(raw[name], Mapping):
            raise MCPApprovedServerStoreError("MCP approved HTTP headers are invalid")
    try:
        return MCPStreamableHTTPManifest(
            server_id=raw["server_id"], manifest_revision=raw["manifest_revision"],
            endpoint_identity=raw["endpoint_identity"], credential_subject_id=raw["credential_subject_id"],
            transport_generation=raw["transport_generation"], approval_revision=raw["approval_revision"],
            approval_status=raw["approval_status"], endpoint_url=raw["endpoint_url"],
            headers=dict(raw["headers"]) if isinstance(raw["headers"], Mapping) else None,
            secret_header_refs=dict(raw["secret_header_refs"]) if isinstance(raw["secret_header_refs"], Mapping) else None,
            timeout_seconds=raw["timeout_seconds"], max_response_bytes=raw["max_response_bytes"],
            max_sse_events=raw["max_sse_events"],
            protocol_profile=raw["protocol_profile"],
        )
    except Exception:
        raise MCPApprovedServerStoreError("MCP approved HTTP manifest is invalid") from None


def _policy(value: object, *, defer_parameter_headers: bool = False) -> MCPToolPolicy:
    raw = _strict_mapping(value, _POLICY_FIELDS)
    retry = _strict_mapping(raw["retry_policy"], _RETRY_FIELDS)
    try:
        return MCPToolPolicy(
            tool_name=raw["tool_name"], tool_id=raw["tool_id"], version=raw["version"],
            display_name=raw["display_name"], description=raw["description"], effect=raw["effect"],
            data_classes=_strings(raw["data_classes"]), input_schema_uri=raw["input_schema_uri"],
            output_schema_uri=raw["output_schema_uri"], receipt_schema_uri=raw["receipt_schema_uri"],
            operation_semantics=raw["operation_semantics"], execution_mode=raw["execution_mode"],
            resource_locks=_strings(raw["resource_locks"]), idempotency=raw["idempotency"],
            retry_policy=ToolRetryPolicy(
                max_attempts=retry["max_attempts"], backoff_ms=retry["backoff_ms"],
                retryable_error_codes=_strings(retry["retryable_error_codes"]),
            ),
            verification_tool_id=raw["verification_tool_id"], compensation_tool_id=raw["compensation_tool_id"],
            mutability=raw["mutability"], egress_class=raw["egress_class"],
            network_scope=_strings(raw["network_scope"]), data_egress_scope=_strings(raw["data_egress_scope"]),
            timeout_ms=raw["timeout_ms"], required_scopes=_strings(raw["required_scopes"]),
            boundary_requirements=_strings(raw["boundary_requirements"]),
            requires_approval=raw["requires_approval"], tool_schema_revision=raw["tool_schema_revision"],
            reviewed_input_schema=_schema(raw["reviewed_input_schema"]),
            reviewed_output_schema=_optional_schema(raw["reviewed_output_schema"]),
            available=raw["available"], remote_receipt_field=raw["remote_receipt_field"],
            reviewed_receipt_schema=_optional_schema(raw["reviewed_receipt_schema"]),
            parameter_header_plan=EMPTY_HEADER_PROJECTION if defer_parameter_headers else None,
        )
    except Exception:
        raise MCPApprovedServerStoreError("MCP approved tool policy is invalid") from None


def _validate_verification_links(policies: tuple[MCPToolPolicy, ...]) -> None:
    by_id = {policy.tool_id: policy for policy in policies}
    for policy in policies:
        if policy.verification_tool_id is None:
            continue
        verification = by_id.get(policy.verification_tool_id)
        if (
            policy.idempotency != "verify_before_retry"
            or verification is None
            or verification.effect != "read"
            or verification.operation_semantics != "read_only"
            or verification.requires_approval
            or verification.reviewed_output_schema is None
            or verification.idempotency != "idempotent"
            or verification.verification_tool_id is not None
        ):
            raise MCPApprovedServerStoreError(
                "MCP approved verification tool policy is invalid"
            )


def _strict_mapping(value: object, fields: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise MCPApprovedServerStoreError("MCP approved server authority shape is invalid")
    return dict(value)


def _sequence(value: object, maximum: int) -> tuple[object, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) > maximum:
        raise MCPApprovedServerStoreError("MCP approved server authority sequence is invalid")
    return tuple(value)


def _strings(value: object) -> tuple[str, ...]:
    items = _sequence(value, 128)
    if any(not isinstance(item, str) for item in items):
        raise MCPApprovedServerStoreError("MCP approved server authority strings are invalid")
    return tuple(items)  # type: ignore[return-value]


def _schema(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise MCPApprovedServerStoreError("MCP approved schema is invalid")
    return dict(value)


def _optional_schema(value: object) -> Mapping[str, object] | None:
    return None if value is None else _schema(value)


def _with_parameter_header_plan(policy: MCPToolPolicy, plan: HeaderProjectionPlan) -> MCPToolPolicy:
    # MCPToolPolicy is frozen so a compiled plan cannot later be replaced by
    # remote discovery.  Reconstruct through its canonical constructor.
    try:
        return MCPToolPolicy(
            tool_name=policy.tool_name, tool_id=policy.tool_id, version=policy.version,
            display_name=policy.display_name, description=policy.description, effect=policy.effect,
            data_classes=policy.data_classes, input_schema_uri=policy.input_schema_uri,
            output_schema_uri=policy.output_schema_uri, receipt_schema_uri=policy.receipt_schema_uri,
            operation_semantics=policy.operation_semantics, execution_mode=policy.execution_mode,
            resource_locks=policy.resource_locks, idempotency=policy.idempotency,
            retry_policy=policy.retry_policy, verification_tool_id=policy.verification_tool_id,
            compensation_tool_id=policy.compensation_tool_id, mutability=policy.mutability,
            egress_class=policy.egress_class, network_scope=policy.network_scope,
            data_egress_scope=policy.data_egress_scope, timeout_ms=policy.timeout_ms,
            required_scopes=policy.required_scopes, boundary_requirements=policy.boundary_requirements,
            requires_approval=policy.requires_approval, tool_schema_revision=policy.tool_schema_revision,
            reviewed_input_schema=policy.reviewed_input_schema,
            reviewed_output_schema=policy.reviewed_output_schema, available=policy.available,
            remote_receipt_field=policy.remote_receipt_field,
            reviewed_receipt_schema=policy.reviewed_receipt_schema,
            parameter_header_plan=plan,
        )
    except Exception:
        raise MCPApprovedServerStoreError("MCP parameter header plan is invalid") from None


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _positive(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise MCPApprovedServerStoreError("MCP approved server revision is invalid")
    return value
