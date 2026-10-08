from __future__ import annotations

import pytest

from backend.security import InMemorySecretStore, SecretEgressBroker, SecretEgressError


def _broker(store, revisions, clock):
    return SecretEgressBroker(
        store, boundary_revision_reader=lambda project: revisions[project], clock=lambda: clock[0],
    )


def test_header_injection_binds_project_host_boundary_revision_and_ttl() -> None:
    store = InMemorySecretStore({"provider:openai": "canary-secret"})
    revisions = {"project-a": "boundary-r1"}
    clock = [100.0]
    broker = _broker(store, revisions, clock)
    lease = broker.grant(
        project_id="project-a", secret_ref="provider:openai", purpose="model_call",
        allowed_hosts=("api.openai.com",), boundary_revision="boundary-r1", ttl_seconds=10,
    )
    assert broker.inject_header(
        lease, url="https://api.openai.com/v1/responses",
        project_id="project-a", purpose="model_call", boundary_revision="boundary-r1",
        header_name="Authorization", prefix="Bearer ",
    ) == {"Authorization": "Bearer canary-secret"}
    with pytest.raises(SecretEgressError, match="host_denied"):
        broker.inject_header(
            lease, project_id="project-a", purpose="model_call", boundary_revision="boundary-r1",
            url="https://evil.example/v1", header_name="Authorization",
        )
    clock[0] = 111.0
    with pytest.raises(SecretEgressError, match="expired"):
        broker.inject_header(
            lease, project_id="project-a", purpose="model_call", boundary_revision="boundary-r1",
            url="https://api.openai.com/v1", header_name="Authorization",
        )


def test_rotation_revocation_and_boundary_drift_fail_before_wire() -> None:
    store = InMemorySecretStore({"mcp:server:token": "first"})
    revisions = {"project-a": "boundary-r1"}
    broker = _broker(store, revisions, [100.0])
    lease = broker.grant(
        project_id="project-a", secret_ref="mcp:server:token", purpose="mcp_call",
        allowed_hosts=("mcp.example",), boundary_revision="boundary-r1",
    )
    store.set("mcp:server:token", "second")
    with pytest.raises(SecretEgressError, match="revision_drift"):
        broker.inject_header(
            lease, project_id="project-a", purpose="mcp_call", boundary_revision="boundary-r1",
            url="https://mcp.example/", header_name="Authorization",
        )
    fresh = broker.grant(
        project_id="project-a", secret_ref="mcp:server:token", purpose="mcp_call",
        allowed_hosts=("mcp.example",), boundary_revision="boundary-r1",
    )
    revisions["project-a"] = "boundary-r2"
    with pytest.raises(SecretEgressError, match="boundary_drift"):
        broker.inject_header(
            fresh, project_id="project-a", purpose="mcp_call", boundary_revision="boundary-r1",
            url="https://mcp.example/", header_name="Authorization",
        )
    broker.revoke(fresh.lease_id)
    with pytest.raises(SecretEgressError, match="revoked"):
        broker.inject_header(
            fresh, project_id="project-a", purpose="mcp_call", boundary_revision="boundary-r1",
            url="https://mcp.example/", header_name="Authorization",
        )


def test_cross_project_purpose_and_boundary_contexts_fail_without_disclosure() -> None:
    canary = "cross-project-canary-must-not-leak"
    store = InMemorySecretStore({"provider:one": canary})
    broker = _broker(store, {"project-a": "boundary-r1", "project-b": "boundary-r1"}, [100.0])
    lease = broker.grant(
        project_id="project-a", secret_ref="provider:one", purpose="model_call",
        allowed_hosts=("api.example",), boundary_revision="boundary-r1",
    )
    for context in (
        {"project_id": "project-b", "purpose": "model_call", "boundary_revision": "boundary-r1"},
        {"project_id": "project-a", "purpose": "model_probe", "boundary_revision": "boundary-r1"},
        {"project_id": "project-a", "purpose": "model_call", "boundary_revision": "boundary-r2"},
    ):
        with pytest.raises(SecretEgressError, match="context_denied") as failure:
            broker.materialize_for_sdk(lease, url="https://api.example/v1", **context)
        assert canary not in str(failure.value)
    assert canary not in repr(lease)


def test_deleted_secret_is_not_present_even_when_generation_is_a_tombstone() -> None:
    store = InMemorySecretStore({"provider:one": "temporary"})
    store.delete("provider:one")
    assert store.get_generation("provider:one") > 0
    assert store.has_secret("provider:one") is False
