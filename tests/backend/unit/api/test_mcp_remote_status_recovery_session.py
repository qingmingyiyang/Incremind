from __future__ import annotations

from types import SimpleNamespace

import pytest

import backend.api.mcp_runtime as runtime_module
from backend.api.mcp_runtime import MCPRemoteStatusRecoverySession
from backend.security.mcp_approved_servers import MCPApprovedServerStoreError


class _Store:
    def __init__(self, records: tuple[object, ...]) -> None:
        self.records = records
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        return SimpleNamespace(enabled_servers=self.records)


class _Connection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Provider:
    def __init__(self, result: object) -> None:
        self.result = result
        self.requests: list[dict[str, object]] = []

    def probe_completed_invocation(self, request):
        self.requests.append(dict(request))
        return self.result


def _session(store: _Store) -> MCPRemoteStatusRecoverySession:
    session = object.__new__(MCPRemoteStatusRecoverySession)
    session._store = store
    session._receipts = object()
    session._secret_store = None
    session._http_requester_factory = lambda: object()
    session._broker = None
    return session


def _intent(contract: object | None = None) -> dict[str, object]:
    return {
        "server_id": "calendar-server", "tool_id": "calendar.create",
        "tool_contract": {"frozen": "contract"} if contract is None else contract,
        "invocation_id": "call-1", "turn_id": "turn-1",
        "operation_id": "operation-1", "idempotency_key": "operation-1:call-1",
        "attempt": 1,
    }


def test_recovery_session_uses_private_registry_and_closes_one_shot_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = SimpleNamespace(server_id="calendar-server")
    store = _Store((record,))
    session = _session(store)
    connection = _Connection()
    provider = _Provider({"effect_certainty": "confirmed_none", "probe_ref": "facts:probe"})
    captured: dict[str, object] = {}

    def build_connection(**kwargs):
        captured.update(kwargs)
        kwargs["registry"]._entries["calendar.create"] = (
            1, SimpleNamespace(tool_definition=object()), provider,
        )
        return connection

    monkeypatch.setattr(runtime_module, "_build_approved_mcp_connection", build_connection)
    monkeypatch.setattr(
        runtime_module, "tool_contract_identity", lambda _tool: {"frozen": "contract"},
    )
    control = object()

    result = session.probe(_intent(), execution_control=control)

    assert result == {"effect_certainty": "confirmed_none", "probe_ref": "facts:probe"}
    assert captured["record"] is record
    assert captured["registry"].__class__.__name__ == "ScopedCapabilityRegistry"
    assert connection.closed is True
    assert provider.requests == [{
        "tool_call_id": "call-1", "turn_id": "turn-1",
        "operation_id": "operation-1", "idempotency_key": "operation-1:call-1",
        "attempt": 1, "arguments": {}, "execution_context": control,
        "tool_contract": {"frozen": "contract"},
    }]


def test_recovery_session_fails_closed_on_contract_drift_and_still_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = SimpleNamespace(server_id="calendar-server")
    session = _session(_Store((record,)))
    connection = _Connection()
    provider = _Provider(None)

    def build_connection(**kwargs):
        kwargs["registry"]._entries["calendar.create"] = (
            1, SimpleNamespace(tool_definition=object()), provider,
        )
        return connection

    monkeypatch.setattr(runtime_module, "_build_approved_mcp_connection", build_connection)
    monkeypatch.setattr(
        runtime_module, "tool_contract_identity", lambda _tool: {"current": "drifted"},
    )

    with pytest.raises(MCPApprovedServerStoreError, match="contract drifted"):
        session.probe(_intent(), execution_control=object())

    assert connection.closed is True
    assert provider.requests == []


def test_recovery_session_does_not_connect_without_exact_enabled_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session(_Store(()))
    called = False

    def build_connection(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("connection must not be built")

    monkeypatch.setattr(runtime_module, "_build_approved_mcp_connection", build_connection)

    with pytest.raises(MCPApprovedServerStoreError, match="approval is unavailable"):
        session.probe(_intent(), execution_control=object())

    assert called is False

