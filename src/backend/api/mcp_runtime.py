from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event, RLock, Thread, current_thread
from time import monotonic
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

from backend.security.mcp_approved_servers import (
    JsonMCPApprovedServerStore,
    MCPApprovedServerStoreError,
    MCPApprovedServer,
    MCPApprovedServerSnapshot,
)
from backend.security.secrets import SecretStore
from backend.security.secret_egress import SecretEgressBroker
from backend.security.network_adapter import SafeJsonHttpAdapter
from core.ai_kernel import CapabilityRegistryPort, ScopedCapabilityRegistry, TurnPayloadStorePort
from core.ai_tooling import tool_contract_identity
from core.mcp_host.streamable_http_transport import MCPHttpRequesterPort
from core.mcp_host import (
    MCPHostConnection,
    MCPStdioLaunchAuthority,
    MCPStdioTransport,
    MCPStreamableHTTPAuthority,
    MCPStreamableHTTPTransport,
    TurnPayloadMCPReceiptStore,
)


HttpRequesterFactory = Callable[[], MCPHttpRequesterPort]


@dataclass(frozen=True, slots=True)
class MCPConnectionStatus:
    server_id: str | None
    state: str
    error_code: str | None


@dataclass(frozen=True, slots=True)
class MCPMigrationRevocationReceipt:
    """In-process proof that a manager revoked one exact server set.

    The private manager/proof fields intentionally make a structurally similar
    object insufficient.  The migration authority must ask the originating
    manager to validate this receipt before it advances its durable pointer.
    """

    server_ids: tuple[str, ...]
    epoch: int
    _manager_identity: object
    _proof: object


_PUBLIC_CONNECTION_STATES = frozenset({"connected", "unavailable", "closed"})
_PUBLIC_ERROR_CODES = frozenset({
    "mcp.authority_invalid", "mcp.connection_failed", "mcp.session_failed",
    "mcp.migration_in_progress",
})
_PUBLIC_REASON_CODES = frozenset({
    "approved_server_missing", "catalog_missing", "header_policy_rejected",
})
_MAX_PUBLIC_REASON_COUNT = 1024


class _MCPSecretInjector:
    def __init__(
        self, broker: SecretEgressBroker | None, store: SecretStore | None,
        server_id: str, approval_revision: int,
    ) -> None:
        self._broker = broker
        self._store = store
        self._prefix = f"mcp:{server_id}:"
        self._project_id = f"mcp:{server_id}"
        self._boundary_revision = f"approval:{approval_revision}"
        self._generations: dict[str, int] = {}

    def capture_all(self, secret_refs: object) -> None:
        if not isinstance(secret_refs, (tuple, list, set, frozenset)):
            raise ValueError("MCP secret references are invalid")
        for secret_ref in secret_refs:
            if not isinstance(secret_ref, str):
                raise ValueError("MCP secret reference identity is invalid")
            if not secret_ref.startswith(self._prefix) or self._store is None:
                raise ValueError("MCP secret reference identity is invalid")
            generation = self._store.get_generation(secret_ref)
            if generation < 1:
                raise ValueError("MCP secret is unavailable")
            self._generations[secret_ref] = generation

    def headers_for_wire(self, *, url: str, secret_header_refs: object, purpose: str) -> dict[str, str]:
        if not isinstance(secret_header_refs, dict) and not hasattr(secret_header_refs, "items"):
            raise ValueError("MCP secret header references are invalid")
        if self._broker is None:
            if secret_header_refs:
                raise ValueError("MCP secret broker is unavailable")
            return {}
        host = urlsplit(url).hostname
        if not host:
            raise ValueError("MCP endpoint host is invalid")
        headers: dict[str, str] = {}
        for name, secret_ref in secret_header_refs.items():
            if not isinstance(name, str) or not isinstance(secret_ref, str) or not secret_ref.startswith(self._prefix):
                raise ValueError("MCP secret reference identity is invalid")
            lease = self._broker.grant(
                project_id=self._project_id, secret_ref=secret_ref, purpose=purpose,
                allowed_hosts=(host,), boundary_revision=self._boundary_revision, ttl_seconds=30,
            )
            try:
                headers.update(self._broker.inject_header(
                    lease, project_id=self._project_id, purpose=purpose,
                    boundary_revision=self._boundary_revision, url=url, header_name=name,
                ))
            finally:
                self._broker.revoke(lease.lease_id)
        return headers

    def generations_current(self) -> bool:
        """Check captured local epochs without disclosing or re-reading values."""
        if self._store is None:
            return not self._generations
        getter = getattr(self._store, "get_generation", None)
        if not callable(getter):
            return False
        try:
            return all(getter(secret_ref) == generation for secret_ref, generation in self._generations.items())
        except Exception:
            return False


class MCPConnectionManager:
    """Owns production MCP connection lifetimes for one AI Runtime composition."""

    def __init__(
        self,
        connections: dict[str, MCPHostConnection] | None = None,
        statuses: tuple[MCPConnectionStatus, ...] = (),
        *,
        authority_loader: Callable[[], MCPApprovedServerSnapshot] | None = None,
        connector: Callable[[MCPApprovedServer], MCPHostConnection] | None = None,
        poll_interval_seconds: float = 2.0,
        max_backoff_seconds: float = 30.0,
        health_interval_seconds: float = 15.0,
        health_timeout_ms: int = 1_000,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._connections = dict(connections or {})
        self._statuses = {status.server_id: status for status in statuses}
        self._authority_records: dict[str, MCPApprovedServer] = {}
        self._failures: dict[str, int] = {}
        self._retry_at: dict[str, float] = {}
        self._authority_loader = authority_loader
        self._connector = connector
        self._poll_interval = max(0.05, poll_interval_seconds)
        self._max_backoff = max(0.1, max_backoff_seconds)
        self._health_interval = max(self._poll_interval, health_interval_seconds)
        self._health_timeout_ms = max(100, health_timeout_ms)
        self._next_probe: dict[str, float] = {}
        self._migration_blocked: set[str] = set()
        self._migration_receipts: dict[frozenset[str], MCPMigrationRevocationReceipt] = {}
        self._migration_pending_closes: dict[frozenset[str], tuple[MCPHostConnection, ...]] = {}
        self._migration_epoch = 0
        self._migration_manager_identity = object()
        self._migration_receipt_proof = object()
        self._clock = clock
        self._lock = RLock()
        self._reconcile_lock = RLock()
        self._closed = False
        self._stop = Event()
        self._thread: Thread | None = None

    @property
    def statuses(self) -> tuple[MCPConnectionStatus, ...]:
        with self._lock:
            current: list[MCPConnectionStatus] = []
            for server_id in sorted(self._statuses, key=lambda value: value or ""):
                status = self._statuses[server_id]
                connection = self._connections.get(status.server_id) if status.server_id else None
                if status.state == "connected" and self._closed:
                    current.append(MCPConnectionStatus(status.server_id, "closed", None))
                elif status.state == "connected" and (connection is None or not connection.connected):
                    current.append(MCPConnectionStatus(
                        status.server_id, "unavailable", "mcp.session_failed",
                    ))
                else:
                    current.append(status)
            return tuple(current)

    @property
    def connected_server_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(
                server_id for server_id, connection in self._connections.items()
                if connection.connected
            ))

    @property
    def capability_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted({
                definition.capability_id
                for connection in self._connections.values()
                for definition in connection.capabilities()
            }))

    def bounded_audit_status(
        self, *, enabled_server_ids: tuple[str, ...],
    ) -> dict[str, object]:
        """Project existing local MCP facts without reconciling or connecting.

        This method is intentionally a snapshot under the manager lock.  It
        never calls the authority loader, connector, Host discovery or probe
        methods, so a status GET cannot instantiate an MCP runtime side effect.
        """
        selected = tuple(sorted({
            server_id for server_id in enabled_server_ids
            if isinstance(server_id, str) and server_id
        }))
        with self._lock:
            authority_invalid = self._statuses.get(None)
            if authority_invalid is not None:
                return {
                    "servers": [],
                    "global": [{
                        "state": "unavailable",
                        "error_code": "mcp.authority_invalid",
                    }],
                    "anonymous_reason_counts": [],
                }
            records = dict(self._authority_records)
            statuses = dict(self._statuses)
            connections = dict(self._connections)
            manager_closed = self._closed
        reasons: dict[str, int] = {}
        servers: list[dict[str, object]] = []
        for server_id in selected:
            record = records.get(server_id)
            if record is None:
                _add_public_reason(reasons, "approved_server_missing", 1)
                continue
            status = statuses.get(server_id)
            connection = connections.get(server_id)
            state, error_code = _public_connection_status(
                status, connection, manager_closed=manager_closed,
            )
            servers.append({
                "server_id": record.server_id,
                "approved_revision": record.approval_revision,
                "protocol_profile": record.host_connection.protocol_profile,
                "transport": record.transport_kind,
                "connection_state": state,
                "error_code": error_code,
            })
            _add_public_reason(
                reasons, "header_policy_rejected",
                getattr(record, "header_policy_rejected_count", 0),
            )
            if connection is not None:
                try:
                    counts = connection.installation_reason_counts
                except Exception:
                    counts = ()
                for reason, count in counts:
                    _add_public_reason(reasons, reason, count)
        return {
            "servers": servers,
            "global": [],
            "anonymous_reason_counts": [
                {"reason": reason, "count": reasons[reason]}
                for reason in sorted(reasons)
            ],
        }

    def close_all(self) -> None:
        with self._reconcile_lock:
            with self._lock:
                if self._closed:
                    return
                self._closed = True
                self._stop.set()
                thread, self._thread = self._thread, None
                connections, self._connections = self._connections, {}
        if thread is not None and thread is not current_thread():
            thread.join(timeout=max(1.0, self._poll_interval * 2))
        for server_id in reversed(sorted(connections)):
            try:
                connections[server_id].close()
            except Exception:
                # Shutdown is best-effort across every connection.  Individual
                # transports already perform bounded fail-closed cleanup.
                continue

    def start(self) -> None:
        if self._authority_loader is None or self._connector is None:
            return
        self.reconcile_once()
        with self._lock:
            if self._closed or self._thread is not None:
                return
            self._thread = Thread(target=self._run, name="mcp-authority-reconcile", daemon=True)
            self._thread.start()

    def reconcile_once(self) -> None:
        if self._authority_loader is None or self._connector is None:
            return
        with self._reconcile_lock:
            try:
                snapshot = self._authority_loader()
            except MCPApprovedServerStoreError:
                self._replace_authority({}, authority_invalid=True)
                return
            self._replace_authority(
                {record.server_id: record for record in snapshot.enabled_servers},
                authority_invalid=False,
            )

    def reconnect_for_continuation(self, server_id: str) -> None:
        """Synchronously replace a revoked stateless provider for one user action."""
        if not isinstance(server_id, str) or not server_id:
            raise ValueError("MCP continuation server identity is invalid")
        if self._authority_loader is None or self._connector is None:
            raise RuntimeError("MCP continuation connector is unavailable")
        self.reconcile_once()
        with self._lock:
            if server_id in self._migration_blocked:
                raise RuntimeError("MCP continuation server is migrating")
            record = self._authority_records.get(server_id)
            old = self._connections.pop(server_id, None)
            self._retry_at.pop(server_id, None)
            self._failures.pop(server_id, None)
        if record is None:
            raise RuntimeError("MCP continuation approved server is unavailable")
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        self._connect(server_id, record, self._clock())
        with self._lock:
            if server_id not in self._connections:
                raise RuntimeError("MCP continuation fresh provider is unavailable")

    def revoke_for_migration(
        self, server_ids: tuple[str, ...] | list[str] | set[str] | frozenset[str],
    ) -> MCPMigrationRevocationReceipt:
        """Atomically fence a migration and close its old runtime sessions.

        Reconciliation is serialized with revocation.  The block is installed
        before a connection leaves the manager, so neither the poller nor a
        continuation can re-register an old lease while ``close`` waits for
        an active Host invocation guard to reach its post-call checkpoint.
        Retrying the same cutover receives its original receipt.
        """
        selected = _migration_server_ids(server_ids)
        selected_set = frozenset(selected)
        with self._reconcile_lock:
            with self._lock:
                if self._closed:
                    raise RuntimeError("MCP connection manager is closed")
                existing = self._migration_receipts.get(selected_set)
                if existing is not None:
                    return existing
                if self._migration_blocked and self._migration_blocked != set(selected):
                    raise RuntimeError("another MCP migration is already blocked")
                if not self._migration_blocked:
                    self._migration_blocked.update(selected_set)
                    self._migration_pending_closes[selected_set] = tuple(
                        self._connections.pop(server_id)
                        for server_id in selected
                        if server_id in self._connections
                    )
                    for server_id in selected:
                        self._failures.pop(server_id, None)
                        self._retry_at.pop(server_id, None)
                        self._next_probe.pop(server_id, None)
                        self._statuses[server_id] = MCPConnectionStatus(
                            server_id, "unavailable", "mcp.migration_in_progress",
                        )
                to_close = self._migration_pending_closes.get(selected_set, ())
            close_failed = False
            for connection in to_close:
                try:
                    connection.close()
                except Exception:
                    # The Host invalidates registry leases before transport
                    # cleanup. Keep the block even if an adapter close reports
                    # an error, rather than reconnecting an indeterminate old
                    # session during a pointer transition.
                    close_failed = True
            if close_failed:
                raise RuntimeError("MCP migration revocation did not close every connection")
            with self._lock:
                # A successful retry may be completing an earlier adapter
                # close failure. Only now is the receipt usable for a pointer
                # advance.
                self._migration_pending_closes.pop(selected_set, None)
                self._migration_epoch += 1
                receipt = MCPMigrationRevocationReceipt(
                    server_ids=selected,
                    epoch=self._migration_epoch,
                    _manager_identity=self._migration_manager_identity,
                    _proof=self._migration_receipt_proof,
                )
                self._migration_receipts[selected_set] = receipt
                return receipt

    def validate_migration_revocation(
        self, receipt: object, server_ids: tuple[str, ...] | list[str] | set[str] | frozenset[str],
    ) -> bool:
        """Return true only for this manager's live, exact migration receipt."""
        try:
            selected = _migration_server_ids(server_ids)
        except ValueError:
            return False
        if not isinstance(receipt, MCPMigrationRevocationReceipt):
            return False
        selected_set = frozenset(selected)
        with self._lock:
            return (
                receipt.server_ids == selected
                and receipt._manager_identity is self._migration_manager_identity
                and receipt._proof is self._migration_receipt_proof
                and self._migration_receipts.get(selected_set) is receipt
                and self._migration_blocked == set(selected)
                and not self._closed
            )

    def release_migration_block(
        self, server_ids: tuple[str, ...] | list[str] | set[str] | frozenset[str],
    ) -> None:
        """Release an exact completed fence and reconcile only afterwards."""
        selected = _migration_server_ids(server_ids)
        selected_set = frozenset(selected)
        with self._reconcile_lock:
            with self._lock:
                receipt = self._migration_receipts.get(selected_set)
                if receipt is None or self._migration_blocked != set(selected):
                    raise RuntimeError("MCP migration block identity is invalid")
                self._migration_blocked.clear()
                self._migration_receipts.pop(selected_set, None)
                self._migration_pending_closes.pop(selected_set, None)
            # Do not reconnect until the caller has already committed its
            # pointer. Reconciliation remains serialized with release.
            if self._authority_loader is not None and self._connector is not None:
                try:
                    snapshot = self._authority_loader()
                except MCPApprovedServerStoreError:
                    self._replace_authority({}, authority_invalid=True)
                else:
                    self._replace_authority(
                        {record.server_id: record for record in snapshot.enabled_servers},
                        authority_invalid=False,
                    )

    def _replace_authority(
        self,
        desired: dict[str, MCPApprovedServer],
        *,
        authority_invalid: bool,
    ) -> None:
        to_close: list[MCPHostConnection] = []
        now = self._clock()
        with self._lock:
            blocked = frozenset(self._migration_blocked)
        if blocked:
            desired = {
                server_id: record for server_id, record in desired.items()
                if server_id not in blocked
            }
        self._probe_connections(desired, now)
        with self._lock:
            if self._closed:
                return
            previous = dict(self._authority_records)
            for server_id, connection in tuple(self._connections.items()):
                unhealthy = not connection.connected
                if server_id not in desired or self._authority_records.get(server_id) != desired[server_id] or unhealthy:
                    to_close.append(self._connections.pop(server_id))
                    if unhealthy and server_id in desired:
                        failures = self._failures.get(server_id, 0) + 1
                        self._failures[server_id] = failures
                        delay = min(self._max_backoff, self._poll_interval * (2 ** min(failures - 1, 10)))
                        self._retry_at[server_id] = now + delay
                        self._statuses[server_id] = MCPConnectionStatus(
                            server_id, "unavailable", "mcp.session_failed"
                        )
            self._authority_records = dict(desired)
            if authority_invalid:
                self._statuses = {None: MCPConnectionStatus(None, "unavailable", "mcp.authority_invalid")}
            else:
                self._statuses.pop(None, None)
                for server_id in tuple(self._statuses):
                    if server_id not in desired:
                        if server_id in self._migration_blocked:
                            continue
                        self._statuses.pop(server_id, None)
                        self._failures.pop(server_id, None)
                        self._retry_at.pop(server_id, None)
                        self._next_probe.pop(server_id, None)
                for server_id, record in desired.items():
                    if previous.get(server_id) != record:
                        self._failures.pop(server_id, None)
                        self._retry_at.pop(server_id, None)
        for connection in to_close:
            try:
                connection.close()
            except Exception:
                pass
        if authority_invalid:
            return
        for server_id in sorted(desired):
            with self._lock:
                if self._closed or server_id in self._connections:
                    continue
                retry_at = self._retry_at.get(server_id, 0.0)
                if now < retry_at:
                    error_code = self._statuses.get(
                        server_id, MCPConnectionStatus(server_id, "unavailable", "mcp.connection_failed")
                    ).error_code
                    self._statuses[server_id] = MCPConnectionStatus(server_id, "unavailable", error_code)
                    continue
            self._connect(server_id, desired[server_id], now)

    def _connect(self, server_id: str, record: MCPApprovedServer, now: float) -> None:
        assert self._connector is not None
        try:
            connection = self._connector(record)
        except Exception:
            with self._lock:
                failures = self._failures.get(server_id, 0) + 1
                self._failures[server_id] = failures
                delay = min(self._max_backoff, self._poll_interval * (2 ** min(failures - 1, 10)))
                self._retry_at[server_id] = now + delay
                self._statuses[server_id] = MCPConnectionStatus(server_id, "unavailable", "mcp.connection_failed")
            return
        with self._lock:
            if (
                self._closed
                or server_id in self._migration_blocked
                or self._authority_records.get(server_id) != record
            ):
                reject = True
            else:
                reject = False
                self._connections[server_id] = connection
                self._failures.pop(server_id, None)
                self._retry_at.pop(server_id, None)
                self._statuses[server_id] = MCPConnectionStatus(server_id, "connected", None)
                self._next_probe[server_id] = now + self._health_interval
        if reject:
            try:
                connection.close()
            except Exception:
                pass

    def _probe_connections(self, desired: dict[str, MCPApprovedServer], now: float) -> None:
        with self._lock:
            candidates = tuple(
                (server_id, connection)
                for server_id, connection in self._connections.items()
                if self._authority_records.get(server_id) == desired.get(server_id)
                and now >= self._next_probe.get(server_id, now + self._health_interval)
            )
        for server_id, connection in candidates:
            try:
                connection.probe(timeout_ms=self._health_timeout_ms)
            except Exception:
                try:
                    connection.close()
                except Exception:
                    pass
                with self._lock:
                    if self._connections.get(server_id) is connection:
                        self._connections.pop(server_id, None)
                        failures = self._failures.get(server_id, 0) + 1
                        self._failures[server_id] = failures
                        delay = min(
                            self._max_backoff,
                            self._poll_interval * (2 ** min(failures - 1, 10)),
                        )
                        self._retry_at[server_id] = now + delay
                        self._statuses[server_id] = MCPConnectionStatus(
                            server_id, "unavailable", "mcp.session_failed"
                        )
                    self._next_probe.pop(server_id, None)
                continue
            with self._lock:
                if self._connections.get(server_id) is connection:
                    self._next_probe[server_id] = now + self._health_interval

    def _run(self) -> None:
        while not self._stop.wait(self._poll_interval):
            self.reconcile_once()


class MCPRemoteStatusRecoverySession:
    """Open one approved MCP session for one status-only recovery probe.

    The temporary registry is private to this call.  No capability is
    published to the AI Runtime and no connection manager thread is started.
    """

    def __init__(
        self,
        *,
        root_dir: Path,
        turn_store: TurnPayloadStorePort,
        secret_store: SecretStore | None,
        http_requester_factory: HttpRequesterFactory = SafeJsonHttpAdapter,
    ) -> None:
        self._store = JsonMCPApprovedServerStore(root_dir)
        self._receipts = TurnPayloadMCPReceiptStore(turn_store)
        self._secret_store = secret_store
        self._http_requester_factory = http_requester_factory
        self._broker = _build_mcp_secret_broker(self._store, secret_store)

    def probe(
        self,
        frozen_intent: Mapping[str, object],
        *,
        execution_control: object,
    ) -> Mapping[str, object] | None:
        server_id = frozen_intent.get("server_id")
        tool_id = frozen_intent.get("tool_id")
        frozen_contract = frozen_intent.get("tool_contract")
        if (
            not isinstance(server_id, str) or not server_id
            or not isinstance(tool_id, str) or not tool_id
            or not isinstance(frozen_contract, Mapping)
        ):
            raise ValueError("MCP recovery authority is incomplete")
        snapshot = self._store.snapshot()
        matches = [
            record for record in snapshot.enabled_servers
            if record.server_id == server_id
        ]
        if len(matches) != 1:
            raise MCPApprovedServerStoreError("MCP recovery approval is unavailable")
        registry = ScopedCapabilityRegistry()
        connection = _build_approved_mcp_connection(
            snapshot=snapshot,
            record=matches[0],
            registry=registry,
            receipt_store=self._receipts,
            secret_store=self._secret_store,
            broker=self._broker,
            http_requester_factory=self._http_requester_factory,
        )
        try:
            resolved = registry.resolve(tool_id)
            if resolved is None:
                raise MCPApprovedServerStoreError("MCP recovery Tool is unavailable")
            definition, provider = resolved
            if tool_contract_identity(definition.tool_definition) != dict(frozen_contract):
                raise MCPApprovedServerStoreError("MCP recovery Tool contract drifted")
            probe = getattr(provider, "probe_completed_invocation", None)
            if not callable(probe):
                raise MCPApprovedServerStoreError("MCP recovery probe is unavailable")
            return probe({
                "tool_call_id": frozen_intent.get("invocation_id"),
                "turn_id": frozen_intent.get("turn_id"),
                "operation_id": frozen_intent.get("operation_id"),
                "idempotency_key": frozen_intent.get("idempotency_key"),
                "attempt": frozen_intent.get("attempt"),
                "arguments": {},
                "execution_context": execution_control,
                "tool_contract": dict(frozen_contract),
            })
        finally:
            connection.close()


def build_mcp_connection_manager(
    *,
    root_dir: Path,
    registry: CapabilityRegistryPort,
    turn_store: TurnPayloadStorePort,
    secret_store: SecretStore | None,
    http_requester_factory: HttpRequesterFactory = SafeJsonHttpAdapter,
) -> MCPConnectionManager:
    store = JsonMCPApprovedServerStore(root_dir)
    receipt_store = TurnPayloadMCPReceiptStore(turn_store)
    broker = _build_mcp_secret_broker(store, secret_store)

    def connect(record: MCPApprovedServer) -> MCPHostConnection:
        return _build_approved_mcp_connection(
            snapshot=store.snapshot(),
            record=record,
            registry=registry,
            receipt_store=receipt_store,
            secret_store=secret_store,
            broker=broker,
            http_requester_factory=http_requester_factory,
        )

    manager = MCPConnectionManager(authority_loader=store.snapshot, connector=connect)
    manager.start()
    return manager


def _build_mcp_secret_broker(
    store: JsonMCPApprovedServerStore,
    secret_store: SecretStore | None,
) -> SecretEgressBroker | None:
    def current_boundary(project_id: str) -> str:
        if not project_id.startswith("mcp:"):
            raise ValueError("MCP secret project is invalid")
        server_id = project_id[4:]
        record = next((item for item in store.snapshot().servers if item.server_id == server_id), None)
        if record is None or not record.enabled:
            raise ValueError("MCP approval is unavailable")
        return f"approval:{record.approval_revision}"
    return (
        SecretEgressBroker(secret_store, boundary_revision_reader=current_boundary)
        if secret_store else None
    )


def _build_approved_mcp_connection(
    *,
    snapshot: MCPApprovedServerSnapshot,
    record: MCPApprovedServer,
    registry: CapabilityRegistryPort,
    receipt_store: TurnPayloadMCPReceiptStore,
    secret_store: SecretStore | None,
    broker: SecretEgressBroker | None,
    http_requester_factory: HttpRequesterFactory,
) -> MCPHostConnection:
    transport: MCPStdioTransport | MCPStreamableHTTPTransport | None = None
    try:
        injector = _MCPSecretInjector(
            broker, secret_store, record.server_id, record.approval_revision,
        )
        manifest = record.connection_manifest
        secret_refs = getattr(manifest, "secret_env_refs", None)
        if secret_refs is None:
            secret_refs = getattr(manifest, "secret_header_refs", {})
        injector.capture_all(tuple(dict(secret_refs or {}).values()))
        if record.transport_kind == "stdio":
            if secret_refs:
                raise MCPApprovedServerStoreError("MCP stdio secret environment is prohibited")
            launch = MCPStdioLaunchAuthority(snapshot).resolve(record.host_connection)
            transport = MCPStdioTransport(
                launch,
                host_connection=record.host_connection,
                credential_generation_current=injector.generations_current,
            )
        elif record.transport_kind == "streamable_http":
            http = MCPStreamableHTTPAuthority(snapshot).resolve(record.host_connection)
            transport = MCPStreamableHTTPTransport(
                http,
                secret_injector=injector,
                requester=http_requester_factory(),
                credential_generation_current=injector.generations_current,
            )
        else:
            raise MCPApprovedServerStoreError("MCP approved transport kind is invalid")
        connection = MCPHostConnection(
            config=record.host_connection,
            transport=transport,
            registry=registry,
            policies=record.tool_policies,
            receipt_store=receipt_store,
            credential_generation_current=injector.generations_current,
            request_state_continuation_allowed=(
                record.host_connection.protocol_profile == "stateless_2026_07_28"
                and record.host_connection.credential_subject_id == "anonymous"
                and all(
                    policy.effect == "read" and policy.operation_semantics == "read_only"
                    for policy in record.tool_policies
                )
                and not getattr(record.connection_manifest, "secret_header_refs", {})
                and not getattr(record.connection_manifest, "secret_env_refs", {})
            ),
        )
        connection.connect()
        return connection
    except Exception:
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass
        raise


def shutdown_ai_mcp_runtime(application: object) -> None:
    state = getattr(application, "state", None)
    manager = getattr(state, "ai_mcp_connection_manager", None)
    if isinstance(manager, MCPConnectionManager):
        manager.close_all()


def _add_public_reason(reasons: dict[str, int], reason: object, count: object) -> None:
    if reason not in _PUBLIC_REASON_CODES:
        return
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        return
    reasons[reason] = min(_MAX_PUBLIC_REASON_COUNT, reasons.get(reason, 0) + count)


def _migration_server_ids(
    server_ids: tuple[str, ...] | list[str] | set[str] | frozenset[str],
) -> tuple[str, ...]:
    if not isinstance(server_ids, (tuple, list, set, frozenset)):
        raise ValueError("MCP migration server identities are invalid")
    if not server_ids or any(not isinstance(server_id, str) or not server_id for server_id in server_ids):
        raise ValueError("MCP migration server identities are invalid")
    selected = tuple(sorted(server_ids))
    if len(selected) != len(set(selected)):
        raise ValueError("MCP migration server identities are invalid")
    return selected


def _public_connection_status(
    status: MCPConnectionStatus | None,
    connection: MCPHostConnection | None,
    *,
    manager_closed: bool = False,
) -> tuple[str, str | None]:
    if manager_closed:
        return "closed", None
    if status is None:
        if connection is not None and connection.connected:
            return "connected", None
        return "unavailable", "mcp.connection_failed"
    state = status.state if status.state in _PUBLIC_CONNECTION_STATES else "unavailable"
    error_code = status.error_code if status.error_code in _PUBLIC_ERROR_CODES else None
    if state == "connected" and (connection is None or not connection.connected):
        return "unavailable", "mcp.session_failed"
    if state == "connected":
        return "connected", None
    return state, error_code or "mcp.connection_failed"
