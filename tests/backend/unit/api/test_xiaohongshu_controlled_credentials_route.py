from __future__ import annotations

from pathlib import Path
import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes.xiaohongshu_controlled_credentials import router
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.secrets import InMemorySecretStore


_COOKIE = "xhs-route-cookie-must-never-leak"
_EXPIRY = "2030-01-02T03:04:05Z"
_PATH = "/api/ai/projects/project-a/xiaohongshu-controlled-credentials"


def _app(tmp_path: Path, *, secret_store: object | None = None) -> FastAPI:
    app = FastAPI()
    container = SimpleNamespace(root_dir=tmp_path)
    if secret_store is not None:
        container.secret_store = secret_store
    app.state.container = container
    app.include_router(router)
    return app


def _grant_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "subject": "account-a", "cookie_value": _COOKIE, "expires_at": _EXPIRY,
        "expected_authorization_revision": 0, "command_id": "grant-1", "confirm": True,
    }
    body.update(overrides)
    return body


def test_grant_current_rotate_revoke_are_local_no_store_and_never_return_cookie(tmp_path: Path) -> None:
    secrets = InMemorySecretStore()
    with TestClient(_app(tmp_path, secret_store=secrets)) as client:
        granted = client.post(f"{_PATH}/grant", json=_grant_body())
        assert granted.status_code == 200
        assert granted.headers["cache-control"] == "no-store"
        public = granted.json()["authorization"]
        assert public["boundary_profile_id"] == "project-boundary-project-a"
        assert public["boundary_revision"] == 1
        assert public["authorization_revision"] == 1
        assert "secret_key" not in public
        assert _COOKIE not in granted.text

        current = client.get(f"{_PATH}/current", params={"subject": "account-a"})
        assert current.status_code == 200
        assert current.headers["cache-control"] == "no-store"
        assert current.json()["authorization"] == public
        assert _COOKIE not in current.text

        replay = client.post(f"{_PATH}/grant", json=_grant_body(cookie_value="different-cookie-still-hidden"))
        assert replay.status_code == 200
        assert replay.json() == granted.json()
        assert "different-cookie-still-hidden" not in replay.text

        rotated = client.post(f"{_PATH}/rotate", json=_grant_body(
            cookie_value="xhs-rotated-cookie-must-never-leak",
            expected_authorization_revision=1, command_id="rotate-1",
        ))
        assert rotated.status_code == 200
        assert rotated.json()["authorization"]["authorization_revision"] == 2
        assert rotated.json()["authorization"]["secret_generation"] == 2
        assert "xhs-rotated-cookie-must-never-leak" not in rotated.text

        revoked = client.post(f"{_PATH}/revoke", json={
            "subject": "account-a", "expected_authorization_revision": 2,
            "command_id": "revoke-1", "confirm": True,
        })
        assert revoked.status_code == 200
        assert revoked.json()["authorization"]["state"] == "revoked"
        assert _COOKIE not in revoked.text


def test_boundary_is_server_derived_and_rotate_rejects_boundary_drift_without_cookie_leak(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path, secret_store=InMemorySecretStore())) as client:
        granted = client.post(f"{_PATH}/grant", json=_grant_body()).json()["authorization"]
        # A Boundary change is not client-input; it is derived at the server
        # and makes the stale authorization fail closed on the next mutation.
        ProjectBoundaryProfileStore(tmp_path).set_mode(
            "project-a", mode="sealed", remote_default="deny", expected_revision=1,
        )
        body = _grant_body(
            cookie_value="boundary-drift-cookie", expected_authorization_revision=granted["authorization_revision"],
            command_id="rotate-1",
        )
        rejected = client.post(f"{_PATH}/rotate", json=body)
    assert rejected.status_code == 409
    assert "boundary-drift-cookie" not in rejected.text
    assert "boundary_profile_id" not in rejected.text


def test_exact_request_fields_confirm_local_guard_and_missing_live_secret_store(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path, secret_store=InMemorySecretStore())) as client:
        rejected = client.post(f"{_PATH}/grant", json=_grant_body(boundary_revision=99))
        assert rejected.status_code == 400
        rejected = client.post(f"{_PATH}/grant", json=_grant_body(confirm=False))
        assert rejected.status_code == 400
        missing_subject = client.get(f"{_PATH}/current")
        assert missing_subject.status_code == 400

    with TestClient(_app(tmp_path)) as client:
        unavailable = client.post(f"{_PATH}/grant", json=_grant_body())
        assert unavailable.status_code == 503
        assert _COOKIE not in unavailable.text
        current = client.get(f"{_PATH}/current", params={"subject": "account-a"})
        assert current.status_code == 503


def test_remote_client_is_rejected_before_cookie_value_reaches_authority(tmp_path: Path) -> None:
    with TestClient(
        _app(tmp_path, secret_store=InMemorySecretStore()), client=("198.51.100.42", 51000),
    ) as client:
        rejected = client.post(f"{_PATH}/grant", json=_grant_body())
    assert rejected.status_code == 403
    assert _COOKIE not in rejected.text
    assert not (tmp_path / ".rebuild-data" / "security" / "xiaohongshu-controlled-credentials.sqlite3").exists()


def test_indeterminate_reconciliation_is_explicit_local_quarantine(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path, secret_store=InMemorySecretStore())) as client:
        assert client.post(f"{_PATH}/grant", json=_grant_body()).status_code == 200
        database = tmp_path / ".rebuild-data" / "security" / "xiaohongshu-controlled-credentials.sqlite3"
        with sqlite3.connect(database) as conn:
            conn.execute(
                "INSERT INTO xhs_controlled_credential_command_intents "
                "(command_id, operation, semantic, project_id, credential_subject_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("rotate-crashed", "rotate", "{}", "project-a", "account-a", "2026-08-27T00:00:00Z"),
            )
        response = client.post(f"{_PATH}/reconcile-indeterminate", json={
            "subject": "account-a", "pending_command_id": "rotate-crashed",
            "reconciliation_command_id": "reconcile-rotate-1", "confirm_quarantine": True,
        })
        assert response.status_code == 200
        assert response.json()["outcome"] == "quarantined"
        assert response.json()["authorization"]["state"] == "revoked"
        assert _COOKIE not in response.text
