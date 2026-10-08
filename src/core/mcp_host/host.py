from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from time import monotonic
from typing import Callable, Iterator, Literal, Protocol

from core.ai_kernel import (
    CapabilityDefinition,
    CapabilityProviderPort,
    CapabilityRegistrationPort,
    CapabilityRegistryPort,
    TurnPayloadStorePort,
    ToolProviderFailure,
    ToolDispatchCancelled,
    ToolDispatchDeadlineExceeded,
)
from core.ai_tooling import (
    ToolConnectionIdentity,
    ToolDefinition,
    ToolRetryPolicy,
    tool_matches_contract_identity,
)
from jsonschema import Draft202012Validator

from .header_projection import EMPTY_HEADER_PROJECTION, HeaderProjectionPlan, compile_header_projection


MCP_PROTOCOL_VERSION = "2025-11-25"
MCP_STATELESS_PROTOCOL_VERSION = "2026-07-28"
MCP_LEGACY_PROFILE = "legacy_2025_11_25"
MCP_STATELESS_PROFILE = "stateless_2026_07_28"
_PROTOCOL_BY_PROFILE = {
    MCP_LEGACY_PROFILE: MCP_PROTOCOL_VERSION,
    MCP_STATELESS_PROFILE: MCP_STATELESS_PROTOCOL_VERSION,
}
_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REMOTE_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


class MCPHostError(ValueError):
    """A local host control failure, never a remote tool result."""


class MCPCredentialGenerationChanged(MCPHostError):
    """A captured local credential epoch no longer matches its Secret Store."""


class MCPFatalTransportError(ConnectionError):
    """A transport session that cannot safely serve another request."""


class MCPInputRequiredError(MCPFatalTransportError):
    """A stateless server requested a bounded opaque continuation state.

    The bytes are deliberately kept on the exception boundary: callers must
    persist them as an opaque immutable Turn payload and must never render or
    log them.  A legacy caller that only needs the old fail-closed behaviour
    may still construct this exception with a message.
    """

    def __init__(self, message: str = "MCP stateless input is required", *, request_state: bytes | None = None) -> None:
        super().__init__(message)
        self.request_state = request_state


class MCPTransportPort(Protocol):
    """Injected transport seam; stdio and Streamable HTTP belong behind it."""

    def initialize(self, request: Mapping[str, object]) -> Mapping[str, object]: ...

    def notify_initialized(self) -> None: ...

    def server_discover(self) -> Mapping[str, object]: ...

    def list_tools(self, cursor: str | None = None) -> Mapping[str, object]: ...

    def ping(self, *, timeout_ms: int) -> None: ...

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        timeout_ms: int,
        execution_control: object,
        parameter_headers: Mapping[str, str] | None = None,
        invocation_envelope: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]: ...

    def continue_tool(
        self,
        name: str,
        arguments: Mapping[str, object],
        request_state: bytes,
        *,
        timeout_ms: int,
        execution_control: object,
        parameter_headers: Mapping[str, str] | None = None,
        invocation_envelope: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]: ...

    def close(self) -> None: ...


class MCPReceiptStorePort(Protocol):
    """Stores a metadata-only MCP receipt and returns an opaque receipt ref."""

    def write_metadata(self, receipt: Mapping[str, object]) -> str: ...

    def completed_invocation(
        self, *, turn_id: str, invocation_id: str
    ) -> tuple[str, Mapping[str, object]] | None: ...

    def reserve_side_effect(self, intent: Mapping[str, object]) -> bool: ...

    def side_effect_reserved(self, intent: Mapping[str, object]) -> bool: ...

    def mark_side_effect_unknown(
        self, intent: Mapping[str, object], *, error_code: str,
    ) -> None: ...

    def reserve_verified_none_replay(self, claim: Mapping[str, object]) -> bool: ...

    def record_verified_none_probe(self, claim: Mapping[str, object]) -> str: ...


class TurnPayloadMCPReceiptStore:
    """Persists MCP metadata without making it a generic Tool result."""

    def __init__(self, payloads: TurnPayloadStorePort) -> None:
        self._payloads = payloads

    def write_metadata(self, receipt: Mapping[str, object]) -> str:
        turn_id = receipt.get("turn_id")
        invocation_id = receipt.get("invocation_id")
        if (
            not isinstance(turn_id, str) or not turn_id.strip()
            or not isinstance(invocation_id, str) or not invocation_id.strip()
        ):
            raise MCPHostError("MCP receipt turn identity is invalid")
        settle = getattr(self._payloads, "settle_mcp_side_effect", None)
        if receipt.get("effect_certainty") == "confirmed_applied" and callable(settle):
            return settle(dict(receipt))
        return self._payloads.get_or_create_immutable_payload(
            turn_id, f"mcp-call-receipt-{invocation_id}", dict(receipt)
        )

    def completed_invocation(
        self, *, turn_id: str, invocation_id: str
    ) -> tuple[str, Mapping[str, object]] | None:
        existing = self._payloads.get_immutable_payload(
            turn_id, f"mcp-call-receipt-{invocation_id}"
        )
        if existing is None:
            return None
        ref, payload = existing
        if not isinstance(payload, Mapping):
            raise MCPHostError("MCP receipt payload is invalid")
        return ref, dict(payload)

    def reserve_side_effect(self, intent: Mapping[str, object]) -> bool:
        turn_id = intent.get("turn_id")
        invocation_id = intent.get("invocation_id")
        if (
            not isinstance(turn_id, str) or not turn_id.strip()
            or not isinstance(invocation_id, str) or not invocation_id.strip()
        ):
            raise MCPHostError("MCP side-effect intent identity is invalid")
        reserve = getattr(self._payloads, "reserve_mcp_side_effect", None)
        if callable(reserve):
            _ref, created = reserve(dict(intent))
            return bool(created)
        kind = f"mcp-side-effect-intent-{invocation_id}"
        _ref, created = self._payloads.reserve_immutable_payload(
            turn_id, kind, dict(intent)
        )
        return created

    def side_effect_reserved(self, intent: Mapping[str, object]) -> bool:
        turn_id = intent.get("turn_id")
        invocation_id = intent.get("invocation_id")
        if not isinstance(turn_id, str) or not isinstance(invocation_id, str):
            raise MCPHostError("MCP side-effect intent identity is invalid")
        existing = self._payloads.get_immutable_payload(
            turn_id, f"mcp-side-effect-intent-{invocation_id}"
        )
        if existing is None:
            return False
        _ref, payload = existing
        if payload != intent:
            raise MCPHostError("MCP side-effect intent identity conflicts")
        return True

    def mark_side_effect_unknown(
        self, intent: Mapping[str, object], *, error_code: str,
    ) -> None:
        marker = getattr(self._payloads, "mark_mcp_side_effect_unknown", None)
        if callable(marker):
            marker(dict(intent), error_code=error_code)

    def reserve_verified_none_replay(self, claim: Mapping[str, object]) -> bool:
        turn_id = claim.get("turn_id")
        invocation_id = claim.get("invocation_id")
        if (
            not isinstance(turn_id, str) or not turn_id.strip()
            or not isinstance(invocation_id, str) or not invocation_id.strip()
        ):
            raise MCPHostError("MCP verified-none replay identity is invalid")
        reserve = getattr(self._payloads, "reserve_mcp_verified_none_replay", None)
        if callable(reserve):
            _ref, created = reserve(dict(claim))
            return bool(created)
        _ref, created = self._payloads.reserve_immutable_payload(
            turn_id, f"mcp-verified-none-replay-{invocation_id}", dict(claim)
        )
        return created

    def record_verified_none_probe(self, claim: Mapping[str, object]) -> str:
        turn_id = claim.get("turn_id")
        invocation_id = claim.get("invocation_id")
        if (
            not isinstance(turn_id, str) or not turn_id.strip()
            or not isinstance(invocation_id, str) or not invocation_id.strip()
        ):
            raise MCPHostError("MCP verified-none probe identity is invalid")
        return self._payloads.get_or_create_immutable_payload(
            turn_id, f"mcp-verified-none-probe-{invocation_id}", dict(claim),
        )


@dataclass(frozen=True, slots=True)
class MCPHostLimits:
    max_pages: int = 16
    max_tools: int = 128
    max_descriptor_bytes: int = 16 * 1024
    max_catalog_bytes: int = 256 * 1024
    max_description_bytes: int = 4096
    max_catalog_ttl_ms: int = 300_000

    def __post_init__(self) -> None:
        if not 1 <= self.max_pages <= 64:
            raise MCPHostError("MCP page limit is outside the supported range")
        if not 1 <= self.max_tools <= 512:
            raise MCPHostError("MCP tool limit is outside the supported range")
        if not 256 <= self.max_descriptor_bytes <= 262_144:
            raise MCPHostError("MCP descriptor limit is outside the supported range")
        if self.max_catalog_bytes < self.max_descriptor_bytes:
            raise MCPHostError("MCP catalog limit must cover one descriptor")
        if not 32 <= self.max_description_bytes <= self.max_descriptor_bytes:
            raise MCPHostError("MCP description limit is outside the supported range")
        if not 1_000 <= self.max_catalog_ttl_ms <= 86_400_000:
            raise MCPHostError("MCP catalog TTL limit is outside the supported range")


@dataclass(frozen=True, slots=True)
class MCPHostConnectionConfig:
    server_id: str
    manifest_revision: int
    endpoint_identity: str
    credential_subject_id: str
    transport_generation: int
    catalog_revision: int
    protocol_profile: str = MCP_LEGACY_PROFILE

    def __post_init__(self) -> None:
        for label, value in (
            ("server_id", self.server_id),
            ("endpoint_identity", self.endpoint_identity),
            ("credential_subject_id", self.credential_subject_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise MCPHostError(f"MCP {label} must be non-empty")
        for label, value in (
            ("manifest_revision", self.manifest_revision),
            ("transport_generation", self.transport_generation),
            ("catalog_revision", self.catalog_revision),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise MCPHostError(f"MCP {label} must be positive")
        if self.protocol_profile not in _PROTOCOL_BY_PROFILE:
            raise MCPHostError("MCP protocol profile is invalid")

    @property
    def protocol_version(self) -> str:
        return _PROTOCOL_BY_PROFILE[self.protocol_profile]


@dataclass(frozen=True, slots=True)
class MCPToolPolicy:
    """Local authority for one remote descriptor name.

    Server annotations and descriptions intentionally cannot alter any member
    that is mapped into ``ToolDefinition``.
    """

    tool_name: str
    tool_id: str
    version: int
    display_name: str
    description: str
    effect: Literal["read", "write", "external", "platform", "delete"]
    data_classes: tuple[str, ...]
    input_schema_uri: str
    output_schema_uri: str
    receipt_schema_uri: str | None
    operation_semantics: Literal["none", "read_only", "receipt_required"]
    execution_mode: Literal["parallel", "exclusive"]
    resource_locks: tuple[str, ...]
    idempotency: Literal["idempotent", "verify_before_retry", "never_retry"]
    retry_policy: ToolRetryPolicy
    verification_tool_id: str | None
    compensation_tool_id: str | None
    mutability: Literal["read_only", "reversible", "irreversible"]
    egress_class: Literal["none", "local", "remote"]
    network_scope: tuple[str, ...]
    data_egress_scope: tuple[str, ...]
    timeout_ms: int
    required_scopes: tuple[str, ...]
    boundary_requirements: tuple[str, ...]
    requires_approval: bool
    tool_schema_revision: int
    reviewed_input_schema: Mapping[str, object]
    reviewed_output_schema: Mapping[str, object] | None
    available: bool = True
    remote_receipt_field: str | None = None
    reviewed_receipt_schema: Mapping[str, object] | None = None
    _input_schema: Mapping[str, object] = field(init=False, repr=False, compare=False)
    _output_schema: Mapping[str, object] | None = field(init=False, repr=False, compare=False)
    _receipt_schema: Mapping[str, object] | None = field(init=False, repr=False, compare=False)
    _input_signature: str = field(init=False, repr=False, compare=False)
    _output_signature: str | None = field(init=False, repr=False, compare=False)
    parameter_header_plan: HeaderProjectionPlan | None = field(
        default=None, repr=False, compare=False,
    )
    _parameter_header_plan: HeaderProjectionPlan = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not _TOOL_NAME.fullmatch(self.tool_name):
            raise MCPHostError("MCP policy tool name is invalid")
        if not isinstance(self.requires_approval, bool):
            raise MCPHostError("MCP policy approval requirement is invalid")
        if self.effect in {"write", "external", "platform", "delete"} and not self.requires_approval:
            raise MCPHostError("side-effecting MCP policy requires approval")
        if self.effect in {"write", "external", "platform", "delete"}:
            if not isinstance(self.remote_receipt_field, str) or not _TOOL_NAME.fullmatch(self.remote_receipt_field):
                raise MCPHostError("side-effecting MCP policy requires a remote receipt field")
            if self.reviewed_receipt_schema is None:
                raise MCPHostError("side-effecting MCP policy requires a reviewed receipt schema")
        elif self.remote_receipt_field is not None and (
            not isinstance(self.remote_receipt_field, str)
            or not _TOOL_NAME.fullmatch(self.remote_receipt_field)
        ):
            raise MCPHostError("MCP remote receipt field is invalid")
        if not isinstance(self.tool_schema_revision, int) or isinstance(self.tool_schema_revision, bool) or self.tool_schema_revision < 1:
            raise MCPHostError("MCP policy tool schema revision must be positive")
        input_schema = _reviewed_schema(self.reviewed_input_schema, "input")
        output_schema = (
            _reviewed_schema(self.reviewed_output_schema, "output")
            if self.reviewed_output_schema is not None else None
        )
        receipt_schema = (
            _reviewed_schema(self.reviewed_receipt_schema, "receipt")
            if self.reviewed_receipt_schema is not None else None
        )
        object.__setattr__(self, "_input_schema", input_schema)
        object.__setattr__(self, "_output_schema", output_schema)
        object.__setattr__(self, "_receipt_schema", receipt_schema)
        object.__setattr__(self, "_input_signature", _canonical_json(input_schema))
        object.__setattr__(self, "_output_signature", _canonical_json(output_schema) if output_schema is not None else None)
        plan = self.parameter_header_plan or compile_header_projection(input_schema)
        object.__setattr__(self, "_parameter_header_plan", plan)
        # Let the canonical Tool contract validate the complete local policy.
        self.to_tool(_policy_identity(self.tool_schema_revision))

    def accepts(self, descriptor: "_DiscoveredTool") -> bool:
        return (
            descriptor.input_signature == self._input_signature
            and descriptor.output_signature == self._output_signature
        )

    def validate_arguments(self, arguments: Mapping[str, object]) -> bool:
        return not tuple(Draft202012Validator(self._input_schema).iter_errors(dict(arguments)))

    def validate_output(self, output: object) -> bool:
        return (
            self._output_schema is not None
            and isinstance(output, Mapping)
            and not tuple(Draft202012Validator(self._output_schema).iter_errors(dict(output)))
        )

    def parameter_headers(self, arguments: Mapping[str, object]) -> Mapping[str, str]:
        return self._parameter_header_plan.project(arguments)

    def validate_remote_receipt(self, receipt: object) -> bool:
        return (
            self._receipt_schema is not None
            and isinstance(receipt, Mapping)
            and not tuple(Draft202012Validator(self._receipt_schema).iter_errors(dict(receipt)))
        )

    def to_tool(self, connection_identity: ToolConnectionIdentity) -> ToolDefinition:
        return ToolDefinition(
            tool_id=self.tool_id,
            version=self.version,
            display_name=self.display_name,
            description=self.description,
            source="mcp",
            owner_id=connection_identity.server_id,
            effect=self.effect,
            data_classes=self.data_classes,
            destination="mcp",
            input_schema_uri=self.input_schema_uri,
            output_schema_uri=self.output_schema_uri,
            receipt_schema_uri=self.receipt_schema_uri,
            operation_semantics=self.operation_semantics,
            execution_mode=self.execution_mode,
            resource_locks=self.resource_locks,
            idempotency=self.idempotency,
            retry_policy=self.retry_policy,
            verification_tool_id=self.verification_tool_id,
            compensation_tool_id=self.compensation_tool_id,
            mutability=self.mutability,
            egress_class=self.egress_class,
            network_scope=self.network_scope,
            data_egress_scope=self.data_egress_scope,
            timeout_ms=self.timeout_ms,
            required_scopes=self.required_scopes,
            boundary_requirements=self.boundary_requirements,
            available=self.available,
            connection_identity=connection_identity,
        )


@dataclass(frozen=True, slots=True)
class _DiscoveredTool:
    name: str
    signature: str = field(repr=False)
    input_signature: str = field(repr=False)
    output_signature: str | None = field(repr=False)


class _ConnectionGuard:
    def __init__(self) -> None:
        self._lock = RLock()
        self._active: frozenset[ToolConnectionIdentity] = frozenset()

    def activate(self, identities: Sequence[ToolConnectionIdentity]) -> None:
        with self._lock:
            self._active = frozenset(identities)

    def invalidate(self) -> None:
        with self._lock:
            self._active = frozenset()

    @contextmanager
    def authorize(self, identity: ToolConnectionIdentity | None) -> Iterator[None]:
        with self._lock:
            if identity is None or identity not in self._active:
                raise ToolProviderFailure("mcp.connection_revoked", effect_certainty="confirmed_none")
            # The guard is an invocation lease, not a point-in-time check.  A
            # lifecycle transition must wait until an already-authorized
            # remote call reaches its post-call checkpoint before it can close
            # or replace the transport.
            yield


class MCPHostConnection:
    """Owns one initialized MCP session and its revocable registry leases."""

    def __init__(
        self,
        *,
        config: MCPHostConnectionConfig,
        transport: MCPTransportPort,
        registry: CapabilityRegistryPort,
        policies: Sequence[MCPToolPolicy],
        receipt_store: MCPReceiptStorePort,
        request_state_continuation_allowed: bool = False,
        credential_generation_current: Callable[[], bool] | None = None,
        limits: MCPHostLimits | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        self._config = config
        self._transport = transport
        self._registry = registry
        self._receipt_store = receipt_store
        self._request_state_continuation_allowed = request_state_continuation_allowed is True
        self._credential_generation_current = credential_generation_current or (lambda: True)
        self._limits = limits or MCPHostLimits()
        self._policies = _policy_map(policies)
        self._leases: tuple[CapabilityRegistrationPort, ...] = ()
        self._capability_ids: tuple[str, ...] = ()
        self._catalog: tuple[_DiscoveredTool, ...] = ()
        self._catalog_missing_count = 0
        self._catalog_expires_at: float | None = None
        self._catalog_revision = config.catalog_revision
        self._connected = False
        self._lifecycle = RLock()
        self._guard = _ConnectionGuard()
        self._monotonic_clock = monotonic_clock

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def catalog_revision(self) -> int:
        return self._catalog_revision

    @property
    def installation_reason_counts(self) -> tuple[tuple[str, int], ...]:
        """Return only bounded, anonymous local installation facts.

        This read never discovers, refreshes, or otherwise contacts a server.
        The count intentionally cannot be joined to a remote Tool descriptor.
        """
        with self._lifecycle:
            if self._catalog_missing_count < 1:
                return ()
            return (("catalog_missing", self._catalog_missing_count),)

    def connect(self) -> tuple[CapabilityDefinition, ...]:
        with self._lifecycle:
            if self._connected:
                raise MCPHostError("MCP connection is already initialized")
            try:
                self._ensure_credential_generation_current()
                if self._config.protocol_profile == MCP_LEGACY_PROFILE:
                    response = self._transport.initialize({
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "chriptmas-os", "version": "1"},
                    })
                    _validate_initialize_response(response)
                    self._transport.notify_initialized()
                else:
                    _validate_discover_response(self._transport.server_discover())
                catalog, ttl_ms = self._discover_catalog()
                self._set_catalog_expiry(ttl_ms)
                definitions, leases = self._install(catalog, self._catalog_revision)
            except Exception:
                self._catalog_expires_at = None
                self._transport.close()
                raise
            self._catalog = catalog
            self._leases = leases
            self._capability_ids = tuple(item.capability_id for item in definitions)
            self._guard.activate(_definition_identities(definitions))
            self._connected = True
            return definitions

    def refresh(self, *, catalog_revision: int) -> tuple[CapabilityDefinition, ...]:
        """Replace leases only when the caller supplies a new catalog revision."""
        with self._lifecycle:
            if not self._connected:
                raise MCPHostError("MCP connection is not initialized")
            if not isinstance(catalog_revision, int) or isinstance(catalog_revision, bool) or catalog_revision < 1:
                raise MCPHostError("MCP catalog revision must be positive")
            self._guard.invalidate()
            try:
                self._ensure_credential_generation_current()
                catalog, ttl_ms = self._discover_catalog()
            except Exception:
                self.close()
                raise
            if catalog == self._catalog:
                definitions = self.capabilities()
                try:
                    self._set_catalog_expiry(ttl_ms)
                except Exception:
                    self.close()
                    raise
                self._guard.activate(_definition_identities(definitions))
                return definitions
            if catalog_revision == self._catalog_revision:
                self.close()
                raise MCPHostError("MCP catalog drift requires a new catalog revision")
            old_leases = self._leases
            self._leases = ()
            self._capability_ids = ()
            self._catalog = ()
            self._catalog_missing_count = 0
            for lease in reversed(old_leases):
                lease.close()
            leases: tuple[CapabilityRegistrationPort, ...] = ()
            try:
                definitions, leases = self._install(catalog, catalog_revision)
                self._set_catalog_expiry(ttl_ms)
            except Exception:
                for lease in reversed(leases):
                    lease.close()
                self._catalog_expires_at = None
                self._connected = False
                self._transport.close()
                raise
            self._catalog = catalog
            self._catalog_revision = catalog_revision
            self._leases = leases
            self._capability_ids = tuple(item.capability_id for item in definitions)
            self._guard.activate(_definition_identities(definitions))
            return definitions

    def capabilities(self) -> tuple[CapabilityDefinition, ...]:
        if not self._connected:
            return ()
        definitions: list[CapabilityDefinition] = []
        for capability_id in self._capability_ids:
            resolved = self._registry.resolve(capability_id)
            if resolved is not None:
                definitions.append(resolved[0])
        return tuple(definitions)

    def probe(self, *, timeout_ms: int) -> None:
        """Verify the negotiated 2025-11-25 protocol session without invoking a Tool."""
        with self._lifecycle:
            if not self._connected:
                raise MCPHostError("MCP connection is not initialized")
            try:
                self._ensure_credential_generation_current()
                if self._config.protocol_profile == MCP_LEGACY_PROFILE:
                    self._transport.ping(timeout_ms=timeout_ms)
                else:
                    _validate_discover_response(self._transport.server_discover())
            except (MCPFatalTransportError, ConnectionError, TimeoutError, OSError):
                self._fail_connection()
                raise

    def close(self) -> None:
        with self._lifecycle:
            self._guard.invalidate()
            for lease in reversed(self._leases):
                lease.close()
            self._leases = ()
            self._capability_ids = ()
            self._catalog = ()
            self._catalog_missing_count = 0
            self._catalog_expires_at = None
            self._connected = False
            self._transport.close()

    @contextmanager
    def _authorize(self, identity: ToolConnectionIdentity | None) -> Iterator[None]:
        """Refresh a stateless catalog before granting the invocation lease."""
        with self._lifecycle:
            try:
                self._ensure_credential_generation_current()
                if self._connected:
                    self._ensure_catalog_fresh()
            except MCPCredentialGenerationChanged as error:
                raise ToolProviderFailure(
                    "mcp.credential_generation_changed", effect_certainty="confirmed_none"
                ) from error
            except ToolProviderFailure:
                raise
            except Exception as error:
                if self._connected:
                    self.close()
                raise ToolProviderFailure(
                    "mcp.catalog_unavailable", effect_certainty="confirmed_none"
                ) from error
            with self._guard.authorize(identity):
                yield

    def _ensure_catalog_fresh(self) -> None:
        if self._config.protocol_profile != MCP_STATELESS_PROFILE:
            return
        if not self._connected:
            raise MCPHostError("MCP connection is not initialized")
        expires_at = self._catalog_expires_at
        if expires_at is None:
            self.close()
            raise MCPHostError("MCP stateless catalog expiry is unavailable")
        if self._clock_now() < expires_at:
            return
        self._guard.invalidate()
        try:
            catalog, ttl_ms = self._discover_catalog()
        except Exception:
            self.close()
            raise
        if catalog != self._catalog:
            self.close()
            raise MCPHostError("MCP catalog drift requires a new catalog revision")
        self._set_catalog_expiry(ttl_ms)
        self._guard.activate(_definition_identities(self.capabilities()))

    def _ensure_credential_generation_current(self) -> None:
        """Fence every pre-wire boundary against a rotated local credential.

        The callback only compares local epochs captured during composition; it
        never returns a credential value or makes a network request.  Drift
        revokes all capability leases before the caller can reach a transport.
        """
        try:
            current = self._credential_generation_current()
        except Exception:
            current = False
        if current is True:
            return
        self._fail_connection(local_only=True)
        raise MCPCredentialGenerationChanged("MCP credential generation changed")

    def _set_catalog_expiry(self, ttl_ms: int | None) -> None:
        self._catalog_expires_at = (
            None if ttl_ms is None else self._clock_now() + (ttl_ms / 1000)
        )

    def _clock_now(self) -> float:
        value = self._monotonic_clock()
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            raise MCPHostError("MCP monotonic clock is invalid")
        return float(value)

    def _discover_catalog(self) -> tuple[tuple[_DiscoveredTool, ...], int | None]:
        cursor: str | None = None
        cursors: set[str] = set()
        names: set[str] = set()
        catalog: list[_DiscoveredTool] = []
        catalog_bytes = 0
        ttl_ms: int | None = None
        for _page in range(self._limits.max_pages):
            self._ensure_credential_generation_current()
            page = self._transport.list_tools(cursor)
            if self._config.protocol_profile == MCP_STATELESS_PROFILE:
                page, page_ttl_ms = _validated_cacheable_result(page, "MCP tools/list")
                page_ttl_ms = min(page_ttl_ms, self._limits.max_catalog_ttl_ms)
                ttl_ms = page_ttl_ms if ttl_ms is None else min(ttl_ms, page_ttl_ms)
            if not isinstance(page, Mapping):
                raise MCPHostError("MCP tools/list response is invalid")
            tools = page.get("tools")
            if not isinstance(tools, Sequence) or isinstance(tools, (str, bytes)):
                raise MCPHostError("MCP tools/list response is missing tools")
            for descriptor in tools:
                item = _discover_descriptor(descriptor, self._limits)
                if item.name in names:
                    raise MCPHostError("MCP catalog contains duplicate tool names")
                names.add(item.name)
                catalog.append(item)
                catalog_bytes += len(item.signature.encode("utf-8"))
                if len(catalog) > self._limits.max_tools:
                    raise MCPHostError("MCP catalog exceeds the tool limit")
                if catalog_bytes > self._limits.max_catalog_bytes:
                    raise MCPHostError("MCP catalog exceeds the descriptor budget")
            next_cursor = page.get("nextCursor")
            if next_cursor is None:
                return tuple(sorted(catalog, key=lambda item: item.name)), ttl_ms
            if not isinstance(next_cursor, str) or not next_cursor.strip():
                raise MCPHostError("MCP tools/list cursor is invalid")
            if next_cursor in cursors:
                raise MCPHostError("MCP tools/list cursor repeated")
            cursors.add(next_cursor)
            cursor = next_cursor
        raise MCPHostError("MCP tools/list exceeded the page limit")

    def _install(
        self,
        catalog: tuple[_DiscoveredTool, ...],
        catalog_revision: int,
    ) -> tuple[tuple[CapabilityDefinition, ...], tuple[CapabilityRegistrationPort, ...]]:
        discovered = {item.name: item for item in catalog}
        # This is deliberately a count rather than a list: a status reader
        # must not learn which locally reviewed policy was absent remotely.
        self._catalog_missing_count = sum(
            1 for name in self._policies if name not in discovered
        )
        verification_ids = {
            policy.verification_tool_id
            for policy in self._policies.values()
            if policy.verification_tool_id is not None
        }
        policies_by_id = {policy.tool_id: policy for policy in self._policies.values()}
        for verification_id in verification_ids:
            verification = policies_by_id[verification_id]
            descriptor = discovered.get(verification.tool_name)
            if descriptor is None or not verification.accepts(descriptor):
                raise MCPHostError("MCP verification tool catalog contract is unavailable")
        definitions: list[CapabilityDefinition] = []
        leases: list[CapabilityRegistrationPort] = []
        try:
            for name, policy in sorted(self._policies.items()):
                descriptor = discovered.get(name)
                if descriptor is None:
                    continue
                if not policy.accepts(descriptor):
                    raise MCPHostError("MCP reviewed tool schema drifted")
                # Verification tools are reviewed members of the same Approved
                # Server catalog, but they are Host-internal recovery controls.
                # They must never become model-visible Capabilities.
                if policy.tool_id in verification_ids:
                    continue
                identity = ToolConnectionIdentity(
                    protocol="mcp",
                    server_id=self._config.server_id,
                    protocol_version=self._config.protocol_version,
                    manifest_revision=self._config.manifest_revision,
                    endpoint_identity=self._config.endpoint_identity,
                    credential_subject_id=self._config.credential_subject_id,
                    transport_generation=self._config.transport_generation,
                    catalog_revision=catalog_revision,
                    tool_schema_revision=policy.tool_schema_revision,
                )
                tool = policy.to_tool(identity)
                definition = CapabilityDefinition(
                    capability_id=tool.tool_id,
                    version=tool.version,
                    mode=tool.effect,
                    requires_approval=policy.requires_approval,
                    operation_semantics=tool.operation_semantics,
                    input_schema_uri=tool.input_schema_uri,
                    output_schema_uri=tool.output_schema_uri,
                    tool_definition=tool,
                )
                provider = _MCPToolProvider(
                    transport=self._transport,
                    receipt_store=self._receipt_store,
                    tool_name=name,
                    tool=tool,
                    remote_receipt_field=policy.remote_receipt_field,
                    policy=policy,
                    verification_policy=(
                        policies_by_id.get(policy.verification_tool_id)
                        if policy.verification_tool_id is not None else None
                    ),
                    authorize=self._authorize,
                    pre_wire_fence=self._tool_pre_wire_fence,
                    on_fatal=self._fail_connection,
                    request_state_continuation_allowed=self._request_state_continuation_allowed,
                )
                leases.append(self._registry.register(definition, provider))
                definitions.append(definition)
        except Exception:
            for lease in reversed(leases):
                lease.close()
            raise
        return tuple(definitions), tuple(leases)

    def _tool_pre_wire_fence(self) -> None:
        """Repeat the epoch test immediately before a Tool transport call."""
        with self._lifecycle:
            try:
                self._ensure_credential_generation_current()
            except MCPCredentialGenerationChanged as error:
                raise ToolProviderFailure(
                    "mcp.credential_generation_changed", effect_certainty="confirmed_none"
                ) from error

    def _fail_connection(self, *, local_only: bool = False) -> None:
        """Revoke a failed session after the active invocation releases its guard."""
        with self._lifecycle:
            self._guard.invalidate()
            for lease in reversed(self._leases):
                lease.close()
            self._leases = ()
            self._capability_ids = ()
            self._catalog = ()
            self._catalog_missing_count = 0
            self._catalog_expires_at = None
            self._connected = False
            if local_only:
                abort_local = getattr(self._transport, "abort_local", None)
                if callable(abort_local):
                    abort_local()
                    return
            self._transport.close()


class _MCPToolProvider(CapabilityProviderPort):
    def __init__(
        self,
        *,
        transport: MCPTransportPort,
        receipt_store: MCPReceiptStorePort,
        tool_name: str,
        tool: ToolDefinition,
        remote_receipt_field: str | None,
        policy: MCPToolPolicy,
        verification_policy: MCPToolPolicy | None,
        authorize: Callable[[ToolConnectionIdentity | None], Iterator[None]],
        pre_wire_fence: Callable[[], None],
        on_fatal: Callable[[], None],
        request_state_continuation_allowed: bool,
    ) -> None:
        self._transport = transport
        self._receipt_store = receipt_store
        self._tool_name = tool_name
        self._tool = tool
        self._remote_receipt_field = remote_receipt_field
        self._policy = policy
        self._verification_policy = verification_policy
        self._authorize = authorize
        self._pre_wire_fence = pre_wire_fence
        self._on_fatal = on_fatal
        self._request_state_continuation_allowed = request_state_continuation_allowed is True

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        try:
            return self._invoke_guarded(request)
        except MCPCredentialGenerationChanged as error:
            self._on_fatal()
            raise ToolProviderFailure(
                "mcp.credential_generation_changed", effect_certainty="confirmed_none"
            ) from error
        except MCPInputRequiredError as error:
            self._on_fatal()
            if not self._continuation_eligible():
                # Credentialed, mutable and non-2026 calls retain the historic
                # unknown-effect quarantine. Nothing becomes resumable merely
                # because a remote response happened to contain requestState.
                raise ToolProviderFailure("mcp.input_required", effect_certainty="unknown") from error
            raise ToolProviderFailure(
                "mcp.input_required", effect_certainty="unknown",
                continuation_state=error.request_state,
            ) from error
        except (ToolDispatchCancelled, ToolDispatchDeadlineExceeded):
            if getattr(self._transport, "failed_closed", False) is True:
                self._on_fatal()
            raise
        except MCPFatalTransportError as error:
            self._on_fatal()
            raise ToolProviderFailure(
                "mcp.transport_unconfirmed", effect_certainty="unknown"
            ) from error

    def recover_completed_invocation(
        self, request: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        """Recover locally or query one reviewed remote dedupe/status Tool."""
        with self._authorize(self._tool.connection_identity):
            if self._tool.effect == "read":
                return None
            if not tool_matches_contract_identity(self._tool, request.get("tool_contract")):
                raise ToolProviderFailure(
                    "mcp.tool_contract_drift", effect_certainty="confirmed_none"
                )
            turn_id, invocation_id, operation_id = _side_effect_request_identity(request)
            recovered = self._recover_completed(
                turn_id=turn_id,
                invocation_id=invocation_id,
                operation_id=operation_id,
            )
            if recovered is not None:
                return recovered
            if self._verification_policy is None:
                return None
            return self._recover_remote_status(
                request,
                turn_id=turn_id,
                invocation_id=invocation_id,
                operation_id=operation_id,
            )

    def probe_completed_invocation(
        self, request: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        """Query completion status without ever replaying the target Tool."""
        with self._authorize(self._tool.connection_identity):
            if self._tool.effect == "read":
                return None
            if not tool_matches_contract_identity(self._tool, request.get("tool_contract")):
                raise ToolProviderFailure(
                    "mcp.tool_contract_drift", effect_certainty="confirmed_none"
                )
            turn_id, invocation_id, operation_id = _side_effect_request_identity(request)
            recovered = self._recover_completed(
                turn_id=turn_id,
                invocation_id=invocation_id,
                operation_id=operation_id,
            )
            if recovered is not None:
                return recovered
            if self._verification_policy is None:
                return None
            return self._recover_remote_status(
                request,
                turn_id=turn_id,
                invocation_id=invocation_id,
                operation_id=operation_id,
                replay_confirmed_none=False,
            )

    def _recover_remote_status(
        self,
        request: Mapping[str, object],
        *,
        turn_id: str,
        invocation_id: str,
        operation_id: str,
        replay_confirmed_none: bool = True,
    ) -> Mapping[str, object] | None:
        context = request.get("execution_context")
        checkpoint = getattr(context, "checkpoint", None)
        remaining_timeout_ms = getattr(context, "remaining_timeout_ms", None)
        if not callable(checkpoint) or not isinstance(remaining_timeout_ms, int) or remaining_timeout_ms < 1:
            raise ToolProviderFailure("mcp.execution_control_missing", effect_certainty="confirmed_none")
        verification = self._verification_policy
        if verification is None:
            return None
        status_arguments = {
            "schema_version": "1.0.0",
            "server_id": self._tool.owner_id,
            "tool_name": self._tool_name,
            "tool_id": self._tool.tool_id,
            "turn_id": turn_id,
            "invocation_id": invocation_id,
            "operation_id": operation_id,
            "idempotency_key": _request_identity(request, "idempotency_key"),
        }
        if not verification.validate_arguments(status_arguments):
            raise ToolProviderFailure("mcp.verification_contract_invalid", effect_certainty="unknown")
        try:
            checkpoint()
            self._pre_wire_fence()
            parameter_headers = (
                verification.parameter_headers(status_arguments)
                if self._tool.connection_identity is not None
                and self._tool.connection_identity.protocol_version == MCP_STATELESS_PROTOCOL_VERSION
                else EMPTY_HEADER_PROJECTION.project(status_arguments)
            )
            result = self._transport.call_tool(
                verification.tool_name,
                status_arguments,
                timeout_ms=min(remaining_timeout_ms, verification.timeout_ms),
                execution_control=context,
                parameter_headers=parameter_headers,
            )
            checkpoint()
        except (ToolDispatchCancelled, ToolDispatchDeadlineExceeded):
            raise
        except MCPCredentialGenerationChanged:
            raise
        except MCPFatalTransportError as error:
            self._on_fatal()
            raise ToolProviderFailure("mcp.verification_unconfirmed", effect_certainty="unknown") from error
        except Exception as error:
            raise ToolProviderFailure("mcp.verification_unconfirmed", effect_certainty="unknown") from error
        if not isinstance(result, Mapping) or result.get("isError") is True:
            raise ToolProviderFailure("mcp.verification_unconfirmed", effect_certainty="unknown")
        status = result.get("structuredContent")
        if not verification.validate_output(status) or not isinstance(status, Mapping):
            raise ToolProviderFailure("mcp.verification_response_invalid", effect_certainty="unknown")
        expected = {
            "schema_version": "1.0.0",
            "server_id": self._tool.owner_id,
            "tool_name": self._tool_name,
            "tool_id": self._tool.tool_id,
            "turn_id": turn_id,
            "invocation_id": invocation_id,
            "operation_id": operation_id,
            "idempotency_key": _request_identity(request, "idempotency_key"),
        }
        if any(status.get(key) != value for key, value in expected.items()):
            raise ToolProviderFailure("mcp.verification_identity_drift", effect_certainty="unknown")
        certainty = status.get("effect_certainty")
        if certainty == "confirmed_applied":
            remote_receipt = status.get("remote_receipt")
            if not self._policy.validate_remote_receipt(remote_receipt):
                raise ToolProviderFailure("mcp.remote_receipt_unconfirmed", effect_certainty="unknown")
            return self._complete_result(
                request,
                {"structuredContent": dict(remote_receipt)},
                started_at=_now(),
                summary="MCP tool completion recovered from reviewed remote status",
                side_effect_intent=self._side_effect_intent(
                    request, remaining_timeout_ms=remaining_timeout_ms,
                ),
            )
        if certainty == "confirmed_none":
            # The status proof is scoped to the exact immutable identity above.
            # Re-enter the ordinary call with the same attempt/envelope while
            # bypassing only the already-durable local reservation check.
            replay_claim = {
                "schema_version": "1.0.0",
                **expected,
                "effect_certainty": "confirmed_none",
                "attempt": 1,
            }
            if not replay_confirmed_none:
                probe_ref = self._receipt_store.record_verified_none_probe(replay_claim)
                return {
                    "summary": "MCP remote status confirmed no target effect",
                    "effect_certainty": "confirmed_none",
                    "probe_ref": probe_ref,
                }
            if not self._receipt_store.reserve_verified_none_replay(replay_claim):
                return None
            mark_started = getattr(context, "mark_provider_started", None)
            if callable(mark_started):
                mark_started()
            return self._invoke_guarded(request, verified_none_replay=True)
        if certainty == "unknown":
            return None
        raise ToolProviderFailure("mcp.verification_response_invalid", effect_certainty="unknown")

    def continue_request_state(self, request: Mapping[str, object], request_state: bytes) -> Mapping[str, object]:
        """Issue the one explicit 2026 continuation RPC on a fresh provider.

        This intentionally shares all of the ordinary provider validation and
        receipt handling, but never routes through ``call_tool`` again.
        """
        if not isinstance(request_state, bytes) or not request_state or len(request_state) > 4096:
            raise ToolProviderFailure("mcp.continuation_invalid", effect_certainty="confirmed_none")
        # Reconnection rebuilds this frozen local gate from the current
        # Approved Server record.  Check it before entering the transport so
        # credential/secret drift can never produce a continuation POST.
        if not self._continuation_eligible():
            raise ToolProviderFailure("mcp.continuation_denied", effect_certainty="confirmed_none")
        try:
            return self._invoke_guarded(request, request_state=request_state)
        except MCPCredentialGenerationChanged as error:
            self._on_fatal()
            raise ToolProviderFailure(
                "mcp.credential_generation_changed", effect_certainty="confirmed_none"
            ) from error
        except MCPInputRequiredError as error:
            self._on_fatal()
            if not self._continuation_eligible():
                raise ToolProviderFailure("mcp.input_required", effect_certainty="unknown") from error
            raise ToolProviderFailure(
                "mcp.input_required", effect_certainty="unknown",
                continuation_state=error.request_state,
            ) from error
        except MCPFatalTransportError as error:
            self._on_fatal()
            raise ToolProviderFailure("mcp.transport_unconfirmed", effect_certainty="unknown") from error

    def _continuation_eligible(self) -> bool:
        identity = self._tool.connection_identity
        return (
            self._request_state_continuation_allowed
            and self._tool.effect == "read"
            and self._tool.operation_semantics == "read_only"
            and identity is not None
            and identity.protocol_version == MCP_STATELESS_PROTOCOL_VERSION
        )

    def _side_effect_intent(
        self, request: Mapping[str, object], *, remaining_timeout_ms: int,
    ) -> dict[str, object]:
        turn_id, invocation_id, operation_id = _side_effect_request_identity(request)
        identity = self._tool.connection_identity
        if identity is None:
            raise ToolProviderFailure(
                "mcp.connection_identity_missing", effect_certainty="confirmed_none",
            )
        return {
            "schema_version": "1.0.0",
            "turn_id": turn_id,
            "invocation_id": invocation_id,
            "operation_id": operation_id,
            "idempotency_key": _request_identity(request, "idempotency_key"),
            "attempt": request.get("attempt"),
            "server_id": self._tool.owner_id,
            "tool_id": self._tool.tool_id,
            "tool_name": self._tool_name,
            "protocol_version": identity.protocol_version,
            # Freeze every authority input needed by a later Core Reaper
            # status-only probe.  These are references and revisions, never
            # Secret values or model-visible payloads.
            "capability_id": request.get("capability_id"),
            "capability_version": request.get("capability_version"),
            "authorization_facts_ref": request.get("authorization_facts_ref"),
            "authorization_facts_revision": request.get("authorization_facts_revision"),
            "approval_fact_ref": request.get("approval_fact_ref"),
            "execution_mode": request.get("execution_mode"),
            "resource_locks": list(request.get("resource_locks") or ()),
            "timeout_ms": request.get("timeout_ms"),
            "tool_contract": dict(request.get("tool_contract") or {}),
            # Freeze the wire deadline into the execution authority contract.
            # The small grace window covers local Receipt settlement after the
            # transport deadline without allowing the Effect lease to expire
            # while the provider can still be running.
            "lease_ttl_seconds": max(1, (remaining_timeout_ms + 999) // 1000) + 5,
        }

    def _mark_side_effect_unknown(
        self, intent: Mapping[str, object] | None, *, error_code: str,
    ) -> None:
        if intent is None:
            return
        self._receipt_store.mark_side_effect_unknown(
            intent, error_code=error_code,
        )

    def _unknown_failure(
        self, intent: Mapping[str, object] | None, error_code: str,
    ) -> ToolProviderFailure:
        self._mark_side_effect_unknown(intent, error_code=error_code)
        return ToolProviderFailure(error_code, effect_certainty="unknown")

    def _invoke_guarded(
        self,
        request: Mapping[str, object],
        *,
        request_state: bytes | None = None,
        verified_none_replay: bool = False,
    ) -> Mapping[str, object]:
        with self._authorize(self._tool.connection_identity):
            context = request.get("execution_context")
            checkpoint = getattr(context, "checkpoint", None)
            remaining_timeout_ms = getattr(context, "remaining_timeout_ms", None)
            if not callable(checkpoint) or not isinstance(remaining_timeout_ms, int) or remaining_timeout_ms < 1:
                raise ToolProviderFailure("mcp.execution_control_missing", effect_certainty="confirmed_none")
            checkpoint()
            if not tool_matches_contract_identity(self._tool, request.get("tool_contract")):
                raise ToolProviderFailure("mcp.tool_contract_drift", effect_certainty="confirmed_none")
            arguments = request.get("arguments")
            if not isinstance(arguments, Mapping) or not self._policy.validate_arguments(arguments):
                raise ToolProviderFailure("mcp.invalid_arguments", effect_certainty="confirmed_none")
            try:
                parameter_headers = (
                    self._policy.parameter_headers(arguments)
                    if self._tool.connection_identity is not None
                    and self._tool.connection_identity.protocol_version == MCP_STATELESS_PROTOCOL_VERSION
                    else EMPTY_HEADER_PROJECTION.project(arguments)
                )
            except Exception as error:
                raise ToolProviderFailure("mcp.parameter_header_invalid", effect_certainty="confirmed_none") from error
            side_effect_intent = None
            invocation_envelope = None
            if self._tool.effect != "read":
                turn_id, invocation_id, operation_id = _side_effect_request_identity(request)
                idempotency_key = _request_identity(request, "idempotency_key")
                attempt = request.get("attempt")
                recovered = self._recover_completed(
                    turn_id=turn_id,
                    invocation_id=invocation_id,
                    operation_id=operation_id,
                )
                if recovered is not None:
                    return recovered
                side_effect_intent = self._side_effect_intent(
                    request, remaining_timeout_ms=remaining_timeout_ms,
                )
                invocation_envelope = {
                    "turn_id": turn_id,
                    "invocation_id": invocation_id,
                    "operation_id": operation_id,
                    "idempotency_key": idempotency_key,
                    "attempt": attempt,
                }
                if self._receipt_store.side_effect_reserved(side_effect_intent) and not verified_none_replay:
                    raise ToolProviderFailure(
                        "mcp.side_effect_indeterminate", effect_certainty="unknown"
                    )
            self._pre_wire_fence()
            if side_effect_intent is not None and not verified_none_replay and not self._receipt_store.reserve_side_effect(
                side_effect_intent
            ):
                raise ToolProviderFailure(
                    "mcp.side_effect_indeterminate", effect_certainty="unknown"
                )
            started_at = _now()
            try:
                if request_state is None:
                    if side_effect_intent is None:
                        result = self._transport.call_tool(
                            self._tool_name, dict(arguments), timeout_ms=remaining_timeout_ms,
                            execution_control=context, parameter_headers=parameter_headers,
                        )
                    else:
                        result = self._transport.call_tool(
                            self._tool_name, dict(arguments), timeout_ms=remaining_timeout_ms,
                            execution_control=context, parameter_headers=parameter_headers,
                            invocation_envelope=invocation_envelope,
                        )
                else:
                    if side_effect_intent is None:
                        result = self._transport.continue_tool(
                            self._tool_name, dict(arguments), request_state,
                            timeout_ms=remaining_timeout_ms, execution_control=context,
                            parameter_headers=parameter_headers,
                        )
                    else:
                        result = self._transport.continue_tool(
                            self._tool_name, dict(arguments), request_state,
                            timeout_ms=remaining_timeout_ms, execution_control=context,
                            parameter_headers=parameter_headers,
                            invocation_envelope=invocation_envelope,
                        )
            except (ToolDispatchCancelled, ToolDispatchDeadlineExceeded):
                self._mark_side_effect_unknown(
                    side_effect_intent, error_code="mcp.dispatch_interrupted",
                )
                raise
            except MCPCredentialGenerationChanged:
                self._mark_side_effect_unknown(
                    side_effect_intent, error_code="mcp.credential_generation_changed",
                )
                raise
            except MCPFatalTransportError:
                self._mark_side_effect_unknown(
                    side_effect_intent, error_code="mcp.fatal_transport",
                )
                raise
            except (ConnectionError, TimeoutError, OSError) as error:
                raise self._unknown_failure(side_effect_intent, "mcp.transport_unconfirmed") from error
            except Exception as error:
                raise self._unknown_failure(side_effect_intent, "mcp.call_unconfirmed") from error
            checkpoint()
        return self._complete_result(
            request, result, started_at=started_at,
            side_effect_intent=side_effect_intent,
        )

    def _complete_result(
        self,
        request: Mapping[str, object],
        result: object,
        *,
        started_at: str,
        summary: str = "MCP tool completed; remote output is isolated",
        side_effect_intent: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        if not isinstance(result, Mapping):
            raise self._unknown_failure(side_effect_intent, "mcp.invalid_response")
        if result.get("isError") is True:
            raise self._unknown_failure(side_effect_intent, "mcp.remote_error")
        remote_operation_id = None
        if self._tool.effect != "read":
            structured = result.get("structuredContent")
            if not self._policy.validate_remote_receipt(structured):
                raise self._unknown_failure(side_effect_intent, "mcp.remote_receipt_unconfirmed")
            remote_operation_id = (
                structured.get(self._remote_receipt_field)
                if isinstance(structured, Mapping) and self._remote_receipt_field is not None
                else None
            )
            if not isinstance(remote_operation_id, str) or not _REMOTE_OPERATION_ID.fullmatch(remote_operation_id):
                raise self._unknown_failure(side_effect_intent, "mcp.remote_receipt_unconfirmed")
            if (
                structured.get("invocation_id") != _invocation_id(request)
                or structured.get("operation_id") != _request_identity(request, "operation_id")
                or structured.get("tool_name") != self._tool_name
                or structured.get("server_id") != self._tool.owner_id
            ):
                raise self._unknown_failure(side_effect_intent, "mcp.remote_receipt_identity_drift")
        receipt = {
            "schema_version": "1.0.0",
            "receipt_id": f"mcp-tool-receipt-{_invocation_id(request)}",
            "server_id": self._tool.owner_id,
            "protocol_version": self._tool.connection_identity.protocol_version if self._tool.connection_identity else None,
            "manifest_revision": self._tool.connection_identity.manifest_revision if self._tool.connection_identity else None,
            "transport_generation": self._tool.connection_identity.transport_generation if self._tool.connection_identity else None,
            "catalog_revision": self._tool.connection_identity.catalog_revision if self._tool.connection_identity else None,
            "tool_schema_revision": self._tool.connection_identity.tool_schema_revision if self._tool.connection_identity else None,
            "tool_id": self._tool.tool_id,
            "tool_name": self._tool_name,
            "invocation_id": _invocation_id(request),
            "turn_id": _request_identity(request, "turn_id"),
            "operation_id": _request_identity(request, "operation_id"),
            "idempotency_key": request.get("idempotency_key"),
            "attempt": request.get("attempt"),
            "status": "completed",
            "effect_certainty": "confirmed_none" if self._tool.effect == "read" else "confirmed_applied",
            "started_at": started_at,
            "finished_at": _now(),
            "remote_operation_id": remote_operation_id,
            "raw_input_recorded": False,
            "raw_output_recorded": False,
        }
        try:
            receipt_ref = self._receipt_store.write_metadata(receipt)
        except Exception as error:
            self._mark_side_effect_unknown(
                side_effect_intent, error_code="mcp.receipt_unavailable",
            )
            raise ToolProviderFailure(
                "mcp.receipt_unavailable", effect_certainty="unknown"
            ) from error
        if not isinstance(receipt_ref, str) or not receipt_ref.strip():
            raise self._unknown_failure(side_effect_intent, "mcp.receipt_invalid")
        # Do not return the remote body, content blocks, annotations, or data to
        # the Runtime payload store.  A later explicit governed extractor may do so.
        return {
            "summary": summary,
            "receipt_ref": receipt_ref,
            "evidence_refs": [],
        }

    def _recover_completed(
        self, *, turn_id: str, invocation_id: str, operation_id: str,
    ) -> Mapping[str, object] | None:
        completed = self._receipt_store.completed_invocation(
            turn_id=turn_id, invocation_id=invocation_id
        )
        if completed is None:
            return None
        receipt_ref, receipt = completed
        if not _completed_receipt_matches(
            receipt,
            tool=self._tool,
            tool_name=self._tool_name,
            turn_id=turn_id,
            invocation_id=invocation_id,
            operation_id=operation_id,
        ):
            raise ToolProviderFailure(
                "mcp.receipt_identity_drift", effect_certainty="unknown"
            )
        return {
            "summary": "MCP tool completion recovered from immutable receipt",
            "receipt_ref": receipt_ref,
            "evidence_refs": [],
        }


def _policy_map(policies: Sequence[MCPToolPolicy]) -> dict[str, MCPToolPolicy]:
    mapped: dict[str, MCPToolPolicy] = {}
    tool_ids: set[str] = set()
    for policy in policies:
        if not isinstance(policy, MCPToolPolicy):
            raise MCPHostError("MCP tool policy is invalid")
        if policy.tool_name in mapped or policy.tool_id in tool_ids:
            raise MCPHostError("MCP policy identity is duplicated")
        mapped[policy.tool_name] = policy
        tool_ids.add(policy.tool_id)
    by_id = {policy.tool_id: policy for policy in mapped.values()}
    verification_ids: set[str] = set()
    for policy in mapped.values():
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
            raise MCPHostError("MCP verification tool policy is invalid")
        verification_ids.add(verification.tool_id)
    if any(policy.tool_id in verification_ids and policy.effect != "read" for policy in mapped.values()):
        raise MCPHostError("MCP verification tool policy is invalid")
    return mapped


def _validate_initialize_response(response: Mapping[str, object]) -> None:
    if not isinstance(response, Mapping):
        raise MCPHostError("MCP initialize response is invalid")
    if response.get("protocolVersion") != MCP_PROTOCOL_VERSION:
        raise MCPHostError("MCP protocol version was not negotiated")
    capabilities = response.get("capabilities")
    if not isinstance(capabilities, Mapping) or not isinstance(capabilities.get("tools"), Mapping):
        raise MCPHostError("MCP server did not negotiate tools capability")


def _validate_discover_response(response: Mapping[str, object]) -> None:
    response, _ttl_ms = _validated_cacheable_result(response, "MCP server discovery")
    if not isinstance(response, Mapping):
        raise MCPHostError("MCP server discovery response is invalid")
    versions = response.get("supportedVersions")
    capabilities = response.get("capabilities")
    if (
        not isinstance(versions, Sequence) or isinstance(versions, (str, bytes))
        or MCP_STATELESS_PROTOCOL_VERSION not in versions
        or not isinstance(capabilities, Mapping) or not isinstance(capabilities.get("tools"), Mapping)
    ):
        raise MCPHostError("MCP server does not support the approved stateless profile")


def _validated_cacheable_result(
    value: Mapping[str, object], label: str,
) -> tuple[Mapping[str, object], int]:
    if not isinstance(value, Mapping):
        raise MCPHostError(f"{label} response is invalid")
    ttl_ms = value.get("ttlMs")
    cache_scope = value.get("cacheScope")
    if (
        not isinstance(ttl_ms, int) or isinstance(ttl_ms, bool) or ttl_ms < 0
        or cache_scope not in {"private", "public"}
    ):
        raise MCPHostError(f"{label} cache metadata is invalid")
    return (
        {key: item for key, item in value.items() if key not in {"ttlMs", "cacheScope"}},
        ttl_ms,
    )


def _discover_descriptor(value: object, limits: MCPHostLimits) -> _DiscoveredTool:
    if not isinstance(value, Mapping):
        raise MCPHostError("MCP tool descriptor is invalid")
    try:
        signature = json.dumps(dict(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise MCPHostError("MCP tool descriptor is not serializable") from error
    if len(signature.encode("utf-8")) > limits.max_descriptor_bytes:
        raise MCPHostError("MCP tool descriptor exceeds the size limit")
    name = value.get("name")
    if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
        raise MCPHostError("MCP tool descriptor name is invalid")
    description = value.get("description", "")
    if not isinstance(description, str) or len(description.encode("utf-8")) > limits.max_description_bytes:
        raise MCPHostError("MCP tool descriptor description is invalid")
    input_schema = value.get("inputSchema")
    if not isinstance(input_schema, Mapping) or input_schema.get("type") != "object":
        raise MCPHostError("MCP tool descriptor input schema is invalid")
    output_schema = value.get("outputSchema")
    if output_schema is not None and (
        not isinstance(output_schema, Mapping) or output_schema.get("type") != "object"
    ):
        raise MCPHostError("MCP tool descriptor output schema is invalid")
    return _DiscoveredTool(
        name=name,
        signature=signature,
        input_signature=_canonical_json(input_schema),
        output_signature=_canonical_json(output_schema) if output_schema is not None else None,
    )


def _invocation_id(request: Mapping[str, object]) -> str:
    value = request.get("tool_call_id")
    if isinstance(value, str) and value.strip():
        return value
    raise ToolProviderFailure("mcp.invalid_invocation", effect_certainty="confirmed_none")


def _side_effect_request_identity(
    request: Mapping[str, object],
) -> tuple[str, str, str]:
    turn_id = _request_identity(request, "turn_id")
    invocation_id = _invocation_id(request)
    operation_id = _request_identity(request, "operation_id")
    idempotency_key = _request_identity(request, "idempotency_key")
    if idempotency_key != f"{operation_id}:{invocation_id}":
        raise ToolProviderFailure(
            "mcp.idempotency_identity_drift", effect_certainty="confirmed_none"
        )
    if request.get("attempt") != 1:
        raise ToolProviderFailure(
            "mcp.side_effect_attempt_invalid", effect_certainty="confirmed_none"
        )
    return turn_id, invocation_id, operation_id


def _completed_receipt_matches(
    receipt: Mapping[str, object],
    *,
    tool: ToolDefinition,
    tool_name: str,
    turn_id: str,
    invocation_id: str,
    operation_id: str,
) -> bool:
    identity = tool.connection_identity
    if identity is None:
        return False
    expected = {
        "schema_version": "1.0.0",
        "server_id": tool.owner_id,
        "protocol_version": identity.protocol_version,
        "manifest_revision": identity.manifest_revision,
        "transport_generation": identity.transport_generation,
        "catalog_revision": identity.catalog_revision,
        "tool_schema_revision": identity.tool_schema_revision,
        "tool_id": tool.tool_id,
        "tool_name": tool_name,
        "invocation_id": invocation_id,
        "turn_id": turn_id,
        "operation_id": operation_id,
        "idempotency_key": f"{operation_id}:{invocation_id}",
        "attempt": 1,
        "status": "completed",
        "effect_certainty": "confirmed_applied",
        "raw_input_recorded": False,
        "raw_output_recorded": False,
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        return False
    remote_operation_id = receipt.get("remote_operation_id")
    receipt_id = receipt.get("receipt_id")
    return (
        isinstance(remote_operation_id, str)
        and _REMOTE_OPERATION_ID.fullmatch(remote_operation_id) is not None
        and isinstance(receipt_id, str)
        and bool(receipt_id.strip())
    )


def _request_identity(request: Mapping[str, object], field: str) -> str:
    value = request.get(field)
    if isinstance(value, str) and value.strip():
        return value
    raise ToolProviderFailure(f"mcp.invalid_{field}", effect_certainty="confirmed_none")


def _policy_identity(schema_revision: int) -> ToolConnectionIdentity:
    """A synthetic identity used solely for policy validation before connect."""
    return ToolConnectionIdentity(
        protocol="mcp",
        server_id="policy-validation",
        protocol_version=MCP_PROTOCOL_VERSION,
        manifest_revision=1,
        endpoint_identity="policy-validation",
        credential_subject_id="policy-validation",
        transport_generation=1,
        catalog_revision=1,
        tool_schema_revision=schema_revision,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _reviewed_schema(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or value.get("type") != "object":
        raise MCPHostError(f"MCP reviewed {label} schema is invalid")
    try:
        normalized = json.loads(_canonical_json(value))
        Draft202012Validator.check_schema(normalized)
    except Exception as error:
        raise MCPHostError(f"MCP reviewed {label} schema is invalid") from error
    return normalized


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise MCPHostError("MCP schema is not serializable") from error


def _definition_identities(
    definitions: Sequence[CapabilityDefinition],
) -> tuple[ToolConnectionIdentity, ...]:
    identities: list[ToolConnectionIdentity] = []
    for definition in definitions:
        tool = definition.tool_definition
        if tool is None or tool.connection_identity is None:
            raise MCPHostError("MCP capability connection identity is unavailable")
        identities.append(tool.connection_identity)
    return tuple(identities)
