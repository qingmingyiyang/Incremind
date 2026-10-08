from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.memory_projection_effect_runtime import (
    register_memory_projection_rebuild_effect_runtime,
)
from backend.api.routes.automation_memory_projection import router
from backend.security.secrets import InMemorySecretStore
from core.effect_log import build_effect_runtime


_BASE = "/api/rebuild/automations/memory-projection-rebuild"


class _InterruptedRuntime:
    def __init__(self, runtime) -> None:
        self._runtime = runtime
        self.log = runtime.log

    def dispatch_operation(self, operation_id: str, *, now: int):
        inflight, claimed = self._runtime.runner.claim_planned(operation_id, now=now)
        assert claimed
        self._runtime.runner.mark_unknown(
            inflight,
            error_ref="error:simulated-sidecar-dispatch-interruption",
            now=now,
        )
        raise RuntimeError("simulated sidecar dispatch interruption")


def _client(tmp_path, effect_runtime) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.container = SimpleNamespace(
        root_dir=tmp_path,
        secret_store=InMemorySecretStore(),
    )
    app.state.effect_runtime = effect_runtime
    return TestClient(app)


def _create_grant(client: TestClient, preview: dict[str, object]) -> dict[str, object]:
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    response = client.post(f"{_BASE}/grants", json={
        "project_id": preview["project_id"],
        "authority_fingerprint": preview["authority_fingerprint"],
        "expires_at": expires_at,
        "command_id": "memory-projection-ui-confirm-1",
    })
    assert response.status_code == 201, response.text
    return response.json()


def test_preview_cancel_creates_no_grant(tmp_path) -> None:
    runtime = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="test")
    with _client(tmp_path, runtime) as client:
        preview = client.post(f"{_BASE}/preview", json={"project_id": "project-1"})

    assert preview.status_code == 200
    payload = preview.json()
    assert payload["requires_confirmation"] is True
    assert payload["binding"]["effect_kind"] == "memory_projection_rebuild"
    assert not (tmp_path / ".rebuild-data" / "security" / "automation-grants.sqlite3").exists()


def test_unknown_execution_remains_queryable_after_one_grant_claim(tmp_path) -> None:
    raw_runtime = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="test")
    with _client(tmp_path, _InterruptedRuntime(raw_runtime)) as client:
        preview = client.post(f"{_BASE}/preview", json={"project_id": "project-1"}).json()
        grant = _create_grant(client, preview)
        execution = client.post(
            f"{_BASE}/grants/{grant['grant_id']}/execute",
            json={
                "project_id": preview["project_id"],
                "authority_fingerprint": preview["authority_fingerprint"],
                "expected_grant_revision": grant["revision"],
            },
        )
        queried = client.get(
            f"{_BASE}/grants/{grant['grant_id']}",
            params={"project_id": "project-1"},
        )

    assert execution.status_code == 202
    assert execution.json()["status"] == "accepted"
    assert execution.json()["effect"]["status"] == "unknown"
    assert queried.status_code == 200
    assert queried.json()["grant"]["state"] == "exhausted"
    assert queried.json()["effect"]["operation_id"] == execution.json()["operation_id"]
    assert queried.json()["effect"]["receipt_ref"] is None
    assert queried.json()["effect"]["status"] == "unknown"
    assert queried.json()["effect"]["error_recorded"] is True


def test_grant_status_is_fail_closed_outside_its_project(tmp_path) -> None:
    runtime = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="test")
    with _client(tmp_path, runtime) as client:
        preview = client.post(
            f"{_BASE}/preview", json={"project_id": "project-1"},
        ).json()
        grant = _create_grant(client, preview)
        missing_scope = client.get(f"{_BASE}/grants/{grant['grant_id']}")
        other_project = client.get(
            f"{_BASE}/grants/{grant['grant_id']}",
            params={"project_id": "project-2"},
        )
        matching_project = client.get(
            f"{_BASE}/grants/{grant['grant_id']}",
            params={"project_id": "project-1"},
        )

    assert missing_scope.status_code == 422
    assert other_project.status_code == 404
    assert other_project.json()["detail"] == "automation_grant_unavailable"
    assert matching_project.status_code == 200


def test_create_rejects_an_expiry_beyond_the_preview_policy(tmp_path) -> None:
    runtime = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="test")
    with _client(tmp_path, runtime) as client:
        preview = client.post(f"{_BASE}/preview", json={"project_id": "project-1"}).json()
        response = client.post(f"{_BASE}/grants", json={
            "project_id": preview["project_id"],
            "authority_fingerprint": preview["authority_fingerprint"],
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=11)).isoformat(),
            "command_id": "memory-projection-expiry-rejected",
        })

    assert response.status_code == 409
    assert response.json()["detail"] == "automation_grant_rejected"
    assert not (tmp_path / ".rebuild-data" / "security" / "automation-grants.sqlite3").exists()


def test_execution_returns_terminal_receipt_and_completed_grant(tmp_path) -> None:
    runtime = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="test")
    register_memory_projection_rebuild_effect_runtime(tmp_path, runtime)
    with _client(tmp_path, runtime) as client:
        preview = client.post(f"{_BASE}/preview", json={"project_id": "project-1"}).json()
        grant = _create_grant(client, preview)
        execution = client.post(
            f"{_BASE}/grants/{grant['grant_id']}/execute",
            json={
                "project_id": preview["project_id"],
                "authority_fingerprint": preview["authority_fingerprint"],
                "expected_grant_revision": grant["revision"],
            },
        )

    assert execution.status_code == 200, execution.text
    payload = execution.json()
    assert payload["status"] == "completed"
    assert payload["effect"]["status"] == "completed"
    assert payload["receipt_ref"].startswith("receipt:memory-projection-rebuild/")
    assert payload["grant"]["receipt_ref"] == payload["receipt_ref"]
