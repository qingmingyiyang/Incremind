from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import hashlib
import hmac
import os
import sqlite3
import time

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

from backend.api.app import create_app, create_worker_auth_middleware
from backend.memory_app.app import create_app as create_recognition_app
from backend.api.desktop_session import (
    DESKTOP_ALLOWED_ORIGIN_ENV,
    DESKTOP_EXPIRES_ENV,
    DESKTOP_INSTANCE_ENV,
    DESKTOP_MODE_ENV,
    DESKTOP_NONCE_ENV,
    DESKTOP_PROTOCOL_ENV,
    DESKTOP_PROTOCOL_VERSION,
    DESKTOP_SECRET_ENV,
    DESKTOP_SESSION_HEADER,
)
from backend.api.worker_auth import (
    WORKER_CHALLENGE_HEADER,
    WORKER_IDENTITY_HEADER,
    WORKER_INSTANCE_ENV,
    WORKER_PORT_ENV,
    WORKER_SECRET_HEADER,
)


SECRET = "V7sQ2nL9kR4mX8cD1eF0gHjK3pT6wY5zB2vA9qN7rTu"
NONCE = "A3v_Y8kN5mP2qR7sT4wX9zB6cD1eF0gHjK4lM8nQ2rS"
ORIGIN = "http://127.0.0.1:49231"
WORKER_INSTANCE = "b1a9195e6ca34f308f741cdfcf8d6a02"


def _worker_proof(method: str, target: str, nonce: str, *, at: int | None = None) -> str:
    seconds = str(int(time.time()) if at is None else at)
    payload = f"chriptmas-worker-request/v1\n{WORKER_INSTANCE}\n{method}\n{target}\n{seconds}\n{nonce}"
    signature = hmac.new(b"test-worker-secret", payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"v1:{seconds}:{nonce}:{signature}"


def test_worker_secret_rejects_forged_origin_and_accepts_bridge(monkeypatch) -> None:
    monkeypatch.delenv(DESKTOP_MODE_ENV, raising=False)
    monkeypatch.setenv("CHRIPTMAS_WORKER_SECRET", "test-worker-secret")
    monkeypatch.setenv(WORKER_INSTANCE_ENV, WORKER_INSTANCE)
    monkeypatch.setenv(WORKER_PORT_ENV, "8001")
    app = FastAPI()

    @app.get("/api/protected")
    def protected():
        return {"ok": True}

    @app.get("/api/redirect-external")
    def redirect_external():
        return RedirectResponse("https://elsewhere.example/receive", status_code=302)

    @app.get("/api/redirect-local")
    def redirect_local():
        return RedirectResponse("/api/protected", status_code=307)

    create_worker_auth_middleware(app)
    with TestClient(app) as client:
        forged = [
            client.get("/api/protected", headers={"Origin": origin})
            for origin in (
                "http://127.0.0.1:8001", "http://localhost:8001",
                "http://127.0.0.1:4173", "http://localhost:4173",
                "https://example.test",
            )
        ]
        missing = client.get("/api/protected")
        wrong = client.get("/api/protected", headers={"X-Worker-Secret": "wrong"})
        authorized = client.get("/api/protected", headers={WORKER_SECRET_HEADER: _worker_proof("GET", "/api/protected", "a" * 32)})
        preflight = client.options("/api/protected", headers={"Origin": "http://127.0.0.1:4173"})
        external_redirect = client.get("/api/redirect-external", headers={WORKER_SECRET_HEADER: _worker_proof("GET", "/api/redirect-external", "b" * 32)}, follow_redirects=False)
        local_redirect = client.get("/api/redirect-local", headers={WORKER_SECRET_HEADER: _worker_proof("GET", "/api/redirect-local", "c" * 32)}, follow_redirects=False)

    assert all(response.status_code == 403 for response in forged)
    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert authorized.status_code == 200
    assert preflight.status_code != 403
    assert external_redirect.status_code == 502
    assert external_redirect.json()["detail"] == "worker_external_redirect_blocked"
    assert local_redirect.status_code == 307


def test_worker_proof_binds_method_target_and_nonce(monkeypatch) -> None:
    monkeypatch.delenv(DESKTOP_MODE_ENV, raising=False)
    monkeypatch.setenv("CHRIPTMAS_WORKER_SECRET", "test-worker-secret")
    monkeypatch.setenv(WORKER_INSTANCE_ENV, WORKER_INSTANCE)
    monkeypatch.setenv(WORKER_PORT_ENV, "8001")
    app = FastAPI()

    @app.api_route("/api/protected", methods=["GET", "POST"])
    def protected():
        return {"ok": True}

    create_worker_auth_middleware(app)
    proof = _worker_proof("GET", "/api/protected?q=1", "d" * 32)
    with TestClient(app) as client:
        assert client.get("/api/protected?q=2", headers={WORKER_SECRET_HEADER: proof}).status_code == 403
        assert client.post("/api/protected?q=1", headers={WORKER_SECRET_HEADER: proof}).status_code == 403
        assert client.get("/api/protected?q=1", headers={WORKER_SECRET_HEADER: proof}).status_code == 200
        assert client.get("/api/protected?q=1", headers={WORKER_SECRET_HEADER: proof}).status_code == 403
        assert client.get("/api/protected", headers={WORKER_SECRET_HEADER: "test-worker-secret"}).status_code == 403
        expired = _worker_proof("GET", "/api/protected", "e" * 32, at=int(time.time()) - 31)
        future = _worker_proof("GET", "/api/protected", "f" * 32, at=int(time.time()) + 31)
        assert client.get("/api/protected", headers={WORKER_SECRET_HEADER: expired}).status_code == 403
        assert client.get("/api/protected", headers={WORKER_SECRET_HEADER: future}).status_code == 403


def test_worker_health_challenge_proves_instance_without_exposing_secret(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(DESKTOP_MODE_ENV, raising=False)
    monkeypatch.setenv("CHRIPTMAS_WORKER_SECRET", "test-worker-secret")
    monkeypatch.setenv(WORKER_INSTANCE_ENV, WORKER_INSTANCE)
    monkeypatch.setenv(WORKER_PORT_ENV, "8001")
    root = tmp_path / "资料"
    root.mkdir()
    app = create_recognition_app(runtime_root=root, legacy_app=create_app(SimpleNamespace(root_dir=root)))
    with TestClient(app) as client:
        plain = client.get("/api/health")
        untrusted = client.get("/api/health", headers={WORKER_CHALLENGE_HEADER: "e" * 32})
        malformed = client.get("/api/health", headers={
            WORKER_CHALLENGE_HEADER: "bad", WORKER_SECRET_HEADER: _worker_proof("GET", "/api/health", "a" * 32),
        })
        challenged = client.get("/api/health", headers={
            WORKER_CHALLENGE_HEADER: "e" * 32, WORKER_SECRET_HEADER: _worker_proof("GET", "/api/health", "b" * 32),
        })
        second = client.get("/api/health", headers={
            WORKER_CHALLENGE_HEADER: "f" * 32, WORKER_SECRET_HEADER: _worker_proof("GET", "/api/health", "c" * 32),
        })
    assert plain.status_code == 200
    assert WORKER_IDENTITY_HEADER not in plain.headers
    assert untrusted.status_code == 403
    assert WORKER_IDENTITY_HEADER not in untrusted.headers
    assert malformed.status_code == 400
    assert challenged.status_code == second.status_code == 200
    parts = challenged.headers[WORKER_IDENTITY_HEADER].split(":")
    assert parts[:4] == ["v2", WORKER_INSTANCE, str(os.getpid()), "8001"]
    assert bytes.fromhex(parts[4]).decode("utf-8") == str(root.resolve())
    payload = f"chriptmas-worker-health/v2\n{'e' * 32}\n{WORKER_INSTANCE}\n{os.getpid()}\n8001\n{parts[4]}"
    expected = hmac.new(b"test-worker-secret", payload.encode("ascii"), hashlib.sha256).hexdigest()
    assert parts[5] == expected
    assert second.headers[WORKER_IDENTITY_HEADER] != challenged.headers[WORKER_IDENTITY_HEADER]
    assert "test-worker-secret" not in challenged.text
    assert str(root.resolve()) not in plain.text


def test_worker_health_challenge_rejects_missing_or_mismatched_recognition_root(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(DESKTOP_MODE_ENV, raising=False)
    monkeypatch.setenv("CHRIPTMAS_WORKER_SECRET", "test-worker-secret")
    monkeypatch.setenv(WORKER_INSTANCE_ENV, WORKER_INSTANCE)
    monkeypatch.setenv(WORKER_PORT_ENV, "8001")
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    def challenge(nonce: str) -> dict[str, str]:
        return {
            WORKER_CHALLENGE_HEADER: "e" * 32,
            WORKER_SECRET_HEADER: _worker_proof("GET", "/api/health", nonce * 32),
        }
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        missing = client.get("/api/health", headers=challenge("a"))
        assert missing.status_code == 503
        assert missing.json()["detail"] == "worker_health_root_invalid"
        assert WORKER_IDENTITY_HEADER not in missing.headers
        other = tmp_path / "other"
        other.mkdir()
        app.state.recognition_runtime_root = other
        mismatched = client.get("/api/health", headers=challenge("b"))
        assert mismatched.status_code == 503
        assert mismatched.json()["detail"] == "worker_health_root_invalid"
        assert WORKER_IDENTITY_HEADER not in mismatched.headers
        app.state.recognition_runtime_root = tmp_path / "unavailable"
        unavailable = client.get("/api/health", headers=challenge("c"))
        assert unavailable.status_code == 503
        assert unavailable.json()["detail"] == "worker_health_root_invalid"
        assert WORKER_IDENTITY_HEADER not in unavailable.headers
        app.state.recognition_runtime_root = tmp_path
        app.state.container.root_dir = tmp_path / "missing-container"
        missing_container = client.get("/api/health", headers=challenge("d"))
        assert missing_container.status_code == 503
        assert missing_container.json()["detail"] == "worker_health_root_invalid"
        assert WORKER_IDENTITY_HEADER not in missing_container.headers


def test_worker_secret_without_instance_fails_closed(monkeypatch) -> None:
    monkeypatch.delenv(DESKTOP_MODE_ENV, raising=False)
    monkeypatch.setenv("CHRIPTMAS_WORKER_SECRET", "test-worker-secret")
    monkeypatch.delenv(WORKER_INSTANCE_ENV, raising=False)
    monkeypatch.delenv(WORKER_PORT_ENV, raising=False)
    app = FastAPI()

    @app.get("/api/protected")
    def protected():
        return {"ok": True}

    create_worker_auth_middleware(app)
    with TestClient(app) as client:
        response = client.get("/api/protected")
    assert response.status_code == 503
    assert response.json()["detail"] == "worker_auth_config_invalid"


def _configure_desktop(monkeypatch, *, secret: str = SECRET) -> None:
    monkeypatch.setenv(DESKTOP_MODE_ENV, "desktop_production")
    monkeypatch.setenv(DESKTOP_SECRET_ENV, secret)
    monkeypatch.setenv(DESKTOP_NONCE_ENV, NONCE)
    monkeypatch.setenv(DESKTOP_INSTANCE_ENV, "deskinst_AQ9h6rwYqL3X5nP8cD2vK7")
    monkeypatch.setenv(DESKTOP_PROTOCOL_ENV, DESKTOP_PROTOCOL_VERSION)
    monkeypatch.setenv(
        DESKTOP_EXPIRES_ENV, (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    monkeypatch.setenv(DESKTOP_ALLOWED_ORIGIN_ENV, ORIGIN)


def test_desktop_session_rejects_missing_wrong_and_origin_only_requests(tmp_path, monkeypatch) -> None:
    _configure_desktop(monkeypatch)
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        assert client.get("/api/health").status_code == 403
        assert client.get("/api/health", headers={DESKTOP_SESSION_HEADER: "wrong"}).status_code == 403
        assert client.get("/api/health", headers={"Origin": ORIGIN}).status_code == 403


def test_desktop_session_health_requires_secret_and_returns_bound_identity(tmp_path, monkeypatch) -> None:
    _configure_desktop(monkeypatch)
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        response = client.get("/api/health", headers={DESKTOP_SESSION_HEADER: SECRET})

    assert response.status_code == 200
    payload = response.json()["desktop_session"]
    assert payload["protocol_version"] == DESKTOP_PROTOCOL_VERSION
    assert payload["instance_id"] == "deskinst_AQ9h6rwYqL3X5nP8cD2vK7"
    assert payload["nonce"] == NONCE
    assert payload["auth_required"] is True
    assert SECRET not in response.text
    assert response.json()["storage"]["schema_version"] == "storage-topology-v1"
    assert [item["name"] for item in response.json()["effect_operations"]["partitions"]] == [
        "primary", "ai-turns", "ppt-master",
    ]
    assert str(tmp_path) not in response.text


def test_desktop_session_invalid_configuration_fails_closed(tmp_path, monkeypatch) -> None:
    _configure_desktop(monkeypatch, secret="short")
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        response = client.get("/api/health", headers={DESKTOP_SESSION_HEADER: "short"})

    assert response.status_code == 503
    assert response.json()["detail"] == "desktop_session_config_invalid"


def test_desktop_session_expiry_fails_authorization(tmp_path, monkeypatch) -> None:
    _configure_desktop(monkeypatch)
    monkeypatch.setenv(
        DESKTOP_EXPIRES_ENV, (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        response = client.get(
            "/api/health", headers={DESKTOP_SESSION_HEADER: SECRET},
        )
    assert response.status_code == 403


def test_external_agent_context_requires_desktop_session_before_store_access(
    tmp_path, monkeypatch,
) -> None:
    _configure_desktop(monkeypatch)
    body = {
        "operation_id": "desktop-start-missing",
        "confirm": True,
        "adapter_id": "codex", "adapter_revision": 1,
        "template_revision": "openai-skill-map-v1", "turn_id": "turn-missing",
        "project_id": "project-a", "purpose": "project_assistance",
        "requested_context_bytes": 4096,
    }
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        missing = client.post("/api/ai/external-agents/context-sessions", json=body)
        assert missing.status_code == 403
        connection = sqlite3.connect(tmp_path / ".rebuild-data" / "ai-turns.sqlite3")
        try:
            assert connection.execute(
                "SELECT COUNT(*) FROM ai_external_agent_sessions"
            ).fetchone()[0] == 0
        finally:
            connection.close()
        authorized = client.post(
            "/api/ai/external-agents/context-sessions",
            json=body,
            headers={DESKTOP_SESSION_HEADER: SECRET},
        )
    assert authorized.status_code == 404
    assert SECRET not in authorized.text
