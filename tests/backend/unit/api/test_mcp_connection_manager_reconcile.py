from __future__ import annotations

from types import SimpleNamespace
from threading import Barrier, Event, Thread

import pytest

from backend.api.mcp_runtime import (
    MCPConnectionManager,
    MCPConnectionStatus,
    MCPMigrationRevocationReceipt,
)
from backend.security.mcp_approved_servers import MCPApprovedServerStoreError


class _Connection:
    def __init__(self, server_id: str) -> None:
        self.server_id = server_id
        self.connected = True
        self.closed = 0
        self.probes = 0
        self.probe_error: Exception | None = None

    def close(self) -> None:
        self.connected = False
        self.closed += 1

    def capabilities(self):
        return ()

    def probe(self, *, timeout_ms: int) -> None:
        self.probes += 1
        if self.probe_error is not None:
            self.connected = False
            raise self.probe_error


def _record(server_id: str = "server-a", revision: int = 1, profile: str = "legacy_2025_11_25"):
    return SimpleNamespace(server_id=server_id, approval_revision=revision, protocol_profile=profile)


def _snapshot(*records):
    return SimpleNamespace(enabled_servers=tuple(records))


def test_reconcile_applies_enable_revision_change_and_disable() -> None:
    current = {"snapshot": _snapshot(_record())}
    opened: list[_Connection] = []

    def connect(record):
        connection = _Connection(record.server_id)
        opened.append(connection)
        return connection

    manager = MCPConnectionManager(
        authority_loader=lambda: current["snapshot"], connector=connect,
    )
    manager.reconcile_once()
    assert manager.connected_server_ids == ("server-a",)

    current["snapshot"] = _snapshot(_record(revision=2))
    manager.reconcile_once()
    assert opened[0].closed == 1
    assert len(opened) == 2
    assert manager.connected_server_ids == ("server-a",)

    current["snapshot"] = _snapshot()
    manager.reconcile_once()
    assert opened[1].closed == 1
    assert manager.connected_server_ids == ()
    assert manager.statuses == ()


def test_profile_only_authority_change_closes_old_lease_before_connecting_new_one() -> None:
    current = {"snapshot": _snapshot(_record())}
    opened: list[_Connection] = []

    def connect(record):
        if opened:
            assert opened[0].connected is False
            assert opened[0].closed == 1
        connection = _Connection(record.server_id)
        opened.append(connection)
        return connection

    manager = MCPConnectionManager(authority_loader=lambda: current["snapshot"], connector=connect)
    manager.reconcile_once()
    current["snapshot"] = _snapshot(_record(profile="stateless_2026_07_28"))
    manager.reconcile_once()
    assert len(opened) == 2
    assert opened[0].connected is False
    assert manager.connected_server_ids == ("server-a",)


def test_failed_connection_uses_bounded_retry_and_recovers() -> None:
    now = {"value": 0.0}
    attempts = {"count": 0}
    connection = _Connection("server-a")

    def connect(_record):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError("unavailable")
        return connection

    manager = MCPConnectionManager(
        authority_loader=lambda: _snapshot(_record()),
        connector=connect,
        poll_interval_seconds=2.0,
        max_backoff_seconds=10.0,
        clock=lambda: now["value"],
    )
    manager.reconcile_once()
    assert attempts["count"] == 1
    assert manager.statuses[0].state == "unavailable"

    now["value"] = 1.9
    manager.reconcile_once()
    assert attempts["count"] == 1
    now["value"] = 2.0
    manager.reconcile_once()
    assert attempts["count"] == 2
    now["value"] = 5.9
    manager.reconcile_once()
    assert attempts["count"] == 2
    now["value"] = 6.0
    manager.reconcile_once()
    assert attempts["count"] == 3
    assert manager.connected_server_ids == ("server-a",)
    assert manager.statuses[0].state == "connected"


def test_session_failure_is_closed_before_retry() -> None:
    now = {"value": 0.0}
    opened: list[_Connection] = []

    def connect(record):
        connection = _Connection(record.server_id)
        opened.append(connection)
        return connection

    manager = MCPConnectionManager(
        authority_loader=lambda: _snapshot(_record()), connector=connect,
        poll_interval_seconds=1.0, clock=lambda: now["value"],
    )
    manager.reconcile_once()
    opened[0].connected = False
    manager.reconcile_once()

    assert opened[0].closed == 1
    assert len(opened) == 1
    assert manager.connected_server_ids == ()
    assert manager.statuses[0].error_code == "mcp.session_failed"
    now["value"] = 1.0
    manager.reconcile_once()
    assert len(opened) == 2
    assert manager.connected_server_ids == ("server-a",)


def test_invalid_authority_revokes_every_connection_and_can_recover() -> None:
    invalid = {"value": False}
    opened: list[_Connection] = []

    def load():
        if invalid["value"]:
            raise MCPApprovedServerStoreError("invalid")
        return _snapshot(_record())

    def connect(record):
        connection = _Connection(record.server_id)
        opened.append(connection)
        return connection

    manager = MCPConnectionManager(authority_loader=load, connector=connect)
    manager.reconcile_once()
    invalid["value"] = True
    manager.reconcile_once()
    assert opened[0].closed == 1
    assert manager.connected_server_ids == ()
    assert manager.statuses[0].error_code == "mcp.authority_invalid"

    invalid["value"] = False
    manager.reconcile_once()
    assert manager.connected_server_ids == ("server-a",)


def test_close_all_stops_future_reconcile() -> None:
    attempts = {"count": 0}

    def connect(record):
        attempts["count"] += 1
        return _Connection(record.server_id)

    manager = MCPConnectionManager(
        authority_loader=lambda: _snapshot(_record()), connector=connect,
    )
    manager.reconcile_once()
    manager.close_all()
    manager.reconcile_once()
    assert attempts["count"] == 1
    assert manager.connected_server_ids == ()


def test_parallel_reconcile_never_creates_duplicate_connection() -> None:
    entered = Barrier(2)
    release = Barrier(2)
    opened: list[_Connection] = []

    def connect(record):
        entered.wait(timeout=2)
        release.wait(timeout=2)
        connection = _Connection(record.server_id)
        opened.append(connection)
        return connection

    manager = MCPConnectionManager(
        authority_loader=lambda: _snapshot(_record()), connector=connect,
    )
    first = Thread(target=manager.reconcile_once)
    first.start()
    entered.wait(timeout=2)
    second = Thread(target=manager.reconcile_once)
    second.start()
    release.wait(timeout=2)
    first.join(timeout=2)
    second.join(timeout=2)

    assert len(opened) == 1
    assert manager.connected_server_ids == ("server-a",)


def test_authority_revision_change_clears_old_backoff() -> None:
    now = {"value": 0.0}
    revision = {"value": 1}
    attempts = {"count": 0}

    def connect(record):
        attempts["count"] += 1
        if record.approval_revision == 1:
            raise RuntimeError("old authority cannot connect")
        return _Connection(record.server_id)

    manager = MCPConnectionManager(
        authority_loader=lambda: _snapshot(_record(revision=revision["value"])),
        connector=connect,
        poll_interval_seconds=10.0,
        clock=lambda: now["value"],
    )
    manager.reconcile_once()
    assert attempts["count"] == 1
    revision["value"] = 2
    manager.reconcile_once()

    assert attempts["count"] == 2
    assert manager.connected_server_ids == ("server-a",)


def test_shutdown_waits_for_inflight_connect_then_revokes_it() -> None:
    entered = Event()
    release = Event()
    closed = Event()
    connection = _Connection("server-a")

    def connect(_record):
        entered.set()
        assert release.wait(timeout=2)
        return connection

    manager = MCPConnectionManager(
        authority_loader=lambda: _snapshot(_record()), connector=connect,
    )
    reconcile = Thread(target=manager.reconcile_once)
    reconcile.start()
    assert entered.wait(timeout=2)
    shutdown = Thread(target=lambda: (manager.close_all(), closed.set()))
    shutdown.start()
    assert not closed.wait(timeout=0.05)
    release.set()
    reconcile.join(timeout=2)
    shutdown.join(timeout=2)

    assert closed.is_set()
    assert connection.closed == 1
    assert manager.connected_server_ids == ()


def test_protocol_probe_failure_revokes_then_enters_backoff() -> None:
    now = {"value": 0.0}
    opened: list[_Connection] = []

    def connect(record):
        connection = _Connection(record.server_id)
        opened.append(connection)
        return connection

    manager = MCPConnectionManager(
        authority_loader=lambda: _snapshot(_record()), connector=connect,
        poll_interval_seconds=1.0, health_interval_seconds=5.0,
        health_timeout_ms=250, clock=lambda: now["value"],
    )
    manager.reconcile_once()
    opened[0].probe_error = RuntimeError("private remote failure")
    now["value"] = 5.0
    manager.reconcile_once()

    assert opened[0].probes == 1
    assert opened[0].closed == 1
    assert manager.connected_server_ids == ()
    assert manager.statuses[0].error_code == "mcp.session_failed"
    now["value"] = 6.0
    manager.reconcile_once()
    assert len(opened) == 2
    assert manager.connected_server_ids == ("server-a",)


def test_successful_protocol_probe_respects_interval() -> None:
    now = {"value": 0.0}
    connection = _Connection("server-a")
    manager = MCPConnectionManager(
        authority_loader=lambda: _snapshot(_record()), connector=lambda _record: connection,
        poll_interval_seconds=1.0, health_interval_seconds=5.0,
        clock=lambda: now["value"],
    )
    manager.reconcile_once()
    now["value"] = 4.9
    manager.reconcile_once()
    assert connection.probes == 0
    now["value"] = 5.0
    manager.reconcile_once()
    assert connection.probes == 1
    now["value"] = 9.9
    manager.reconcile_once()
    assert connection.probes == 1
    now["value"] = 10.0
    manager.reconcile_once()
    assert connection.probes == 2


def test_bounded_audit_status_uses_selected_approved_servers_only_without_reconcile() -> None:
    calls = {"load": 0, "connect": 0}
    connection = _Connection("approved-server")
    connection.installation_reason_counts = (("catalog_missing", 2), ("remote-canary", 99))
    record = SimpleNamespace(
        server_id="approved-server", approval_revision=7,
        host_connection=SimpleNamespace(protocol_profile="stateless_2026_07_28"),
        transport_kind="streamable_http", header_policy_rejected_count=3,
    )

    def load():
        calls["load"] += 1
        return _snapshot(record)

    def connect(_record):
        calls["connect"] += 1
        return connection

    manager = MCPConnectionManager(authority_loader=load, connector=connect)
    manager.reconcile_once()
    assert calls == {"load": 1, "connect": 1}

    projected = manager.bounded_audit_status(
        enabled_server_ids=("approved-server", "unapproved-canary"),
    )

    assert calls == {"load": 1, "connect": 1}
    assert projected == {
        "servers": [{
            "server_id": "approved-server", "approved_revision": 7,
            "protocol_profile": "stateless_2026_07_28", "transport": "streamable_http",
            "connection_state": "connected", "error_code": None,
        }],
        "global": [],
        "anonymous_reason_counts": [
            {"reason": "approved_server_missing", "count": 1},
            {"reason": "catalog_missing", "count": 2},
            {"reason": "header_policy_rejected", "count": 3},
        ],
    }
    assert "canary" not in str(projected)


def test_bounded_audit_status_treats_invalid_authority_as_one_global_item() -> None:
    manager = MCPConnectionManager(statuses=(
        MCPConnectionStatus(None, "unavailable", "mcp.authority_invalid"),
    ))

    assert manager.bounded_audit_status(enabled_server_ids=("server-canary",)) == {
        "servers": [],
        "global": [{"state": "unavailable", "error_code": "mcp.authority_invalid"}],
        "anonymous_reason_counts": [],
    }


def test_bounded_audit_status_replaces_untrusted_error_text_with_closed_enum() -> None:
    record = SimpleNamespace(
        server_id="approved-server", approval_revision=1,
        host_connection=SimpleNamespace(protocol_profile="legacy_2025_11_25"),
        transport_kind="stdio", header_policy_rejected_count=0,
    )
    manager = MCPConnectionManager(
        authority_loader=lambda: _snapshot(record),
        connector=lambda _record: _Connection("approved-server"),
    )
    manager.reconcile_once()
    # This simulates an unsafe adapter value crossing the manager seam.  The
    # public projection must not use it as a reason or error code.
    manager._statuses["approved-server"] = MCPConnectionStatus(  # type: ignore[attr-defined]
        "approved-server", "unavailable", "remote-private-canary",
    )

    projected = manager.bounded_audit_status(enabled_server_ids=("approved-server",))

    assert projected["servers"][0]["error_code"] == "mcp.connection_failed"
    assert "canary" not in str(projected)


def test_bounded_audit_status_reports_normal_manager_shutdown_as_closed() -> None:
    record = SimpleNamespace(
        server_id="approved-server", approval_revision=1,
        host_connection=SimpleNamespace(protocol_profile="legacy_2025_11_25"),
        transport_kind="stdio", header_policy_rejected_count=0,
    )
    manager = MCPConnectionManager(
        authority_loader=lambda: _snapshot(record),
        connector=lambda _record: _Connection("approved-server"),
    )
    manager.reconcile_once()
    manager.close_all()

    projected = manager.bounded_audit_status(enabled_server_ids=("approved-server",))

    assert projected["servers"][0]["connection_state"] == "closed"
    assert projected["servers"][0]["error_code"] is None


def test_migration_revocation_blocks_poll_reconnect_until_explicit_release() -> None:
    current = {"snapshot": _snapshot(_record(revision=1))}
    opened: list[_Connection] = []

    def connect(record):
        connection = _Connection(record.server_id)
        opened.append(connection)
        return connection

    manager = MCPConnectionManager(authority_loader=lambda: current["snapshot"], connector=connect)
    manager.reconcile_once()
    receipt = manager.revoke_for_migration(("server-a",))

    assert opened[0].closed == 1
    assert manager.connected_server_ids == ()
    assert manager.validate_migration_revocation(receipt, ("server-a",))
    # A poll that still sees the old pointer cannot recreate its profile.
    manager.reconcile_once()
    assert len(opened) == 1
    assert manager.statuses[0].error_code == "mcp.migration_in_progress"

    current["snapshot"] = _snapshot(_record(revision=2))
    manager.release_migration_block(("server-a",))

    assert len(opened) == 2
    assert opened[0].connected is False
    assert manager.connected_server_ids == ("server-a",)
    assert not manager.validate_migration_revocation(receipt, ("server-a",))


def test_migration_receipt_is_exact_manager_bound_and_retry_safe() -> None:
    first = MCPConnectionManager(
        authority_loader=lambda: _snapshot(_record()), connector=lambda record: _Connection(record.server_id),
    )
    second = MCPConnectionManager(
        authority_loader=lambda: _snapshot(_record()), connector=lambda record: _Connection(record.server_id),
    )
    first.reconcile_once()
    receipt = first.revoke_for_migration(["server-a"])

    assert receipt is first.revoke_for_migration(("server-a",))
    assert first.validate_migration_revocation(receipt, {"server-a"})
    assert not first.validate_migration_revocation(receipt, {"different"})
    assert not second.validate_migration_revocation(receipt, {"server-a"})
    forged = MCPMigrationRevocationReceipt(
        server_ids=("server-a",), epoch=receipt.epoch,
        _manager_identity=object(), _proof=object(),
    )
    assert not first.validate_migration_revocation(forged, {"server-a"})


def test_migration_close_failure_stays_blocked_until_retry_closes_everything() -> None:
    class _FailsOneClose(_Connection):
        def __init__(self, server_id: str) -> None:
            super().__init__(server_id)
            self.fail_once = True

        def close(self) -> None:
            super().close()
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("adapter close failed")

    connection = _FailsOneClose("server-a")
    manager = MCPConnectionManager(
        authority_loader=lambda: _snapshot(_record()), connector=lambda _record: connection,
    )
    manager.reconcile_once()

    with pytest.raises(RuntimeError, match="did not close"):
        manager.revoke_for_migration(("server-a",))
    assert manager.connected_server_ids == ()
    manager.reconcile_once()
    assert connection.closed == 1

    receipt = manager.revoke_for_migration(("server-a",))
    assert connection.closed == 2
    assert manager.validate_migration_revocation(receipt, ("server-a",))


def test_migration_revocation_serializes_with_inflight_reconcile_and_connect() -> None:
    entered = Event()
    release = Event()
    opened: list[_Connection] = []

    def connect(record):
        entered.set()
        assert release.wait(timeout=2)
        connection = _Connection(record.server_id)
        opened.append(connection)
        return connection

    manager = MCPConnectionManager(authority_loader=lambda: _snapshot(_record()), connector=connect)
    reconcile = Thread(target=manager.reconcile_once)
    reconcile.start()
    assert entered.wait(timeout=2)
    revocation_done = Event()
    revocation: list[object] = []

    def revoke() -> None:
        revocation.append(manager.revoke_for_migration(("server-a",)))
        revocation_done.set()

    revoker = Thread(target=revoke)
    revoker.start()
    assert not revocation_done.wait(timeout=0.05)
    release.set()
    reconcile.join(timeout=2)
    revoker.join(timeout=2)

    assert revocation_done.is_set()
    assert len(opened) == 1
    assert opened[0].closed == 1
    assert manager.connected_server_ids == ()
    assert manager.validate_migration_revocation(revocation[0], ("server-a",))
