from __future__ import annotations

import pytest

from backend.api.mcp_runtime import _MCPSecretInjector
from backend.security.secret_egress import SecretEgressBroker, SecretEgressError
from backend.security.secrets import InMemorySecretStore


def _fence():
    secrets = InMemorySecretStore()
    secret_ref = "mcp:calendar:token"
    secrets.set(secret_ref, "canary-private-token")
    boundary = {"revision": "approval:1"}
    broker = SecretEgressBroker(
        secrets,
        boundary_revision_reader=lambda project_id: (
            boundary["revision"] if project_id == "mcp:calendar" else "denied"
        ),
        clock=lambda: 100.0,
    )
    injector = _MCPSecretInjector(broker, secrets, "calendar", 1)
    injector.capture_all((secret_ref,))
    return secrets, boundary, injector, secret_ref


def test_recovery_wire_uses_short_lease_and_exposes_generation_drift_to_transport() -> None:
    secrets, _boundary, injector, secret_ref = _fence()

    assert injector.headers_for_wire(
        url="https://calendar.example.test/mcp",
        secret_header_refs={"Authorization": secret_ref},
        purpose="mcp_http_wire",
    ) == {"Authorization": "canary-private-token"}

    secrets.set(secret_ref, "rotated-private-token")
    assert injector.generations_current() is False


def test_recovery_wire_fails_closed_after_approval_boundary_drift() -> None:
    _secrets, boundary, injector, secret_ref = _fence()
    boundary["revision"] = "approval:2"

    with pytest.raises(SecretEgressError, match="boundary_drift"):
        injector.headers_for_wire(
            url="https://calendar.example.test/mcp",
            secret_header_refs={"Authorization": secret_ref},
            purpose="mcp_http_wire",
        )


def test_recovery_wire_fails_closed_after_secret_revocation() -> None:
    secrets, _boundary, injector, secret_ref = _fence()
    secrets.delete(secret_ref)

    assert injector.generations_current() is False
