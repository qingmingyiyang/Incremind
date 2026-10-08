from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import include_api_routers
from backend.api.routes.session_placement import router
from backend.security.secrets import InMemorySecretStore
from core.product_core.session_placement import (
    DeviceIdentity,
    HostSigningIdentity,
    PairedDeviceRegistry,
    ResumeBundleService,
)


def _client(tmp_path) -> TestClient:
    application = FastAPI()
    application.state.container = SimpleNamespace(
        root_dir=tmp_path,
        secret_store=InMemorySecretStore(),
    )
    application.include_router(router)
    return TestClient(application)


def test_identity_is_public_stable_and_pairing_requires_explicit_confirmation(tmp_path) -> None:
    client = _client(tmp_path)
    first = client.get("/api/rebuild/session-placement/identity")
    second = client.get("/api/rebuild/session-placement/identity")
    assert first.status_code == 200
    assert first.json() == second.json()
    assert set(first.json()) == {"device_id", "public_key"}

    remote = HostSigningIdentity(device_id="remote-1").public_identity
    rejected = client.post("/api/rebuild/session-placement/pairings", json={
        "device_id": remote.device_id,
        "public_key": remote.public_key,
        "expected_revision": 0,
        "confirm": False,
    })
    assert rejected.status_code == 400
    accepted = client.post("/api/rebuild/session-placement/pairings", json={
        "device_id": remote.device_id,
        "public_key": remote.public_key,
        "expected_revision": 0,
        "confirm": True,
    })
    assert accepted.status_code == 201
    assert accepted.json() == {"device_id": "remote-1", "trust_revision": 1}

    listed = client.get("/api/rebuild/session-placement/pairings")
    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "no-store"
    assert listed.json() == {"items": [{
        "device_id": "remote-1",
        "trust_revision": 1,
        "trusted_at": listed.json()["items"][0]["trusted_at"],
    }]}
    assert "public_key" not in listed.text

    revoked = client.post("/api/rebuild/session-placement/pairings/remote-1/revoke", json={
        "expected_revision": 1,
        "confirm": True,
    })
    assert revoked.status_code == 200
    assert revoked.json() == {
        "device_id": "remote-1", "trust_revision": 2, "trust_state": "revoked",
    }
    assert client.get("/api/rebuild/session-placement/pairings").json() == {"items": []}


def test_session_placement_safe_discovery_lists_are_bounded_and_empty_by_default(tmp_path) -> None:
    client = _client(tmp_path)

    assert client.post("/api/rebuild/session-placement/exports", json={
        "project_id": "project-a", "session_id": "session-1",
        "target_device_id": "remote-1", "expires_at": "2026-09-01T01:00:00Z",
        "workspace_path": str(tmp_path),
    }).status_code == 403

    candidates = client.get("/api/rebuild/session-placement/export-candidates?project_id=project-a&limit=32")
    recoveries = client.get("/api/rebuild/session-placement/recoveries?project_id=project-a&limit=32")

    assert candidates.status_code == 200
    assert candidates.json() == {"items": []}
    assert recoveries.status_code == 200
    assert recoveries.json() == {"items": []}
    assert candidates.headers["cache-control"] == "no-store"
    assert recoveries.headers["cache-control"] == "no-store"

    assert client.get("/api/rebuild/session-placement/export-candidates").status_code == 400
    assert client.get("/api/rebuild/session-placement/recoveries?project_id=project-a&limit=33").status_code == 400


def test_recovery_detail_and_actions_fail_closed_outside_the_project(tmp_path) -> None:
    client = _client(tmp_path)
    local = DeviceIdentity(**client.get("/api/rebuild/session-placement/identity").json())
    remote = HostSigningIdentity(device_id="remote-1")
    paired = client.post("/api/rebuild/session-placement/pairings", json={
        "device_id": remote.device_id,
        "public_key": remote.public_identity.public_key,
        "expected_revision": 0,
        "confirm": True,
    })
    assert paired.status_code == 201

    remote_pairs = PairedDeviceRegistry(tmp_path / "remote")
    remote_pairs.trust(remote.public_identity)
    remote_pairs.trust(local)
    bundle = ResumeBundleService(host=remote, pairs=remote_pairs).create(
        target_device_id=local.device_id,
        project_id="project-a",
        session_ref="session:project-a",
        turn_refs=("turn:1",),
        context_manifest_ref="context:1",
        context_manifest_revision="context-revision:1",
        last_event_cursor="event:1",
        display_summary="项目 A 的只读恢复摘要。",
        workspace_base_manifest_ref="workspace:none",
        workspace_manifest=(),
        capability_descriptors=(),
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    imported = client.post(
        "/api/rebuild/session-placement/imports", json={"bundle": bundle.wire()},
    )
    assert imported.status_code == 201
    assert set(imported.json()) == {
        "bundle_id", "session_id", "project_id", "read_only", "created_at", "source_device_id",
    }

    path = f"/api/rebuild/session-placement/recoveries/{bundle.bundle_id}"
    matching = client.get(path, params={"project_id": "project-a"})
    other = client.get(path, params={"project_id": "project-b"})
    assert matching.status_code == 200
    assert set(matching.json()) == set(imported.json())
    assert other.status_code == 404

    reconcile = client.post(f"{path}/reconcile-plan", headers={
        "X-Chriptmas-Session-Placement-Control": "main-v1",
    }, json={
        "project_id": "project-b", "workspace_path": str(tmp_path),
    })
    authorization = client.post(f"{path}/host-authorization-requests", json={
        "project_id": "project-b",
        "operation_identity": "operation:test",
        "parameter_digest": "a" * 64,
        "confirm": True,
    })
    assert reconcile.status_code == 404
    assert authorization.status_code == 404

    authorized = client.post(f"{path}/host-authorization-requests", json={
        "project_id": "project-a",
        "operation_identity": "operation:test",
        "parameter_digest": "a" * 64,
        "confirm": True,
    })
    assert authorized.status_code == 202
    assert authorized.json()["status"] == "host_authorization_required"
    assert authorized.json()["execution_supported"] is False
    assert authorized.json()["source_host_transport_required"] is True
    assert authorized.json()["parameter_digest"] == "a" * 64

    revoked = client.post("/api/rebuild/session-placement/pairings/remote-1/revoke", json={
        "expected_revision": 1, "confirm": True,
    })
    assert revoked.status_code == 200
    assert client.get(path, params={"project_id": "project-a"}).status_code == 409
    assert client.get("/api/rebuild/session-placement/recoveries", params={
        "project_id": "project-a", "limit": 32,
    }).json() == {"items": []}


def test_shared_router_registry_includes_session_placement_routes() -> None:
    application = FastAPI()
    include_api_routers(application)
    paths = set(application.openapi()["paths"])
    assert "/api/rebuild/session-placement/exports" in paths
    assert "/api/rebuild/session-placement/imports" in paths
    assert "/api/rebuild/session-placement/pairings" in paths
    assert "/api/rebuild/session-placement/export-candidates" in paths
    assert "/api/rebuild/session-placement/recoveries" in paths
    assert "/api/rebuild/session-placement/recoveries/{bundle_id}/reconcile-plan" in paths
