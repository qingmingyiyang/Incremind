from __future__ import annotations

import hashlib
import hmac
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event, Thread

from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from backend.api.app import create_worker_auth_middleware
from backend.api import desktop_session as sessions
from backend.api.desktop_session import (
    DESKTOP_ALLOWED_ORIGIN_ENV,
    DESKTOP_EXPIRES_ENV,
    DESKTOP_INSTANCE_ENV,
    DESKTOP_MODE_ENV,
    DESKTOP_NONCE_ENV,
    DESKTOP_PROTOCOL_ENV,
    DESKTOP_PROTOCOL_VERSION,
    DESKTOP_ROTATION_VERSION,
    DESKTOP_SECRET_ENV,
    DESKTOP_SESSION_HEADER,
)
from backend.api.routes.health import rotate_desktop_session_route
from backend.api.routes import realtime_asr


OLD = "a" * 43
NEW = "b" * 43
INSTANCE = "desktop-session-rotation-test"


def _configure(monkeypatch) -> str:
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    monkeypatch.setenv(DESKTOP_MODE_ENV, "desktop_production")
    monkeypatch.setenv(DESKTOP_SECRET_ENV, OLD)
    monkeypatch.setenv(DESKTOP_INSTANCE_ENV, INSTANCE)
    monkeypatch.setenv(DESKTOP_NONCE_ENV, "n" * 43)
    monkeypatch.setenv(DESKTOP_PROTOCOL_ENV, DESKTOP_PROTOCOL_VERSION)
    monkeypatch.setenv(DESKTOP_EXPIRES_ENV, expires)
    monkeypatch.setenv(DESKTOP_ALLOWED_ORIGIN_ENV, "http://127.0.0.1:49231")
    monkeypatch.setattr(sessions, "_state", None)
    return expires


def _payload(*, secret: str = OLD, next_secret: str = NEW, rotation_id: str = "rotation_001", expires: str | None = None):
    next_expires_at = expires or (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    payload = {
        "version": DESKTOP_ROTATION_VERSION,
        "instance_id": INSTANCE,
        "rotation_id": rotation_id,
        "next_secret": next_secret,
        "next_expires_at": next_expires_at,
    }
    signed = "\n".join(payload.values())
    payload["signature"] = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
    return payload


def _app() -> FastAPI:
    app = FastAPI()
    app.add_api_route("/api/desktop/session/rotate", rotate_desktop_session_route, methods=["POST"])

    @app.get("/probe")
    def probe():
        return {"session_secret": sessions.desktop_session().secret}

    create_worker_auth_middleware(app)
    return app


def test_rotation_requires_main_signature_and_current_header(monkeypatch) -> None:
    _configure(monkeypatch)
    app = _app()
    payload = _payload()
    with TestClient(app) as client:
        unsigned = client.post("/api/desktop/session/rotate", json={**payload, "signature": "0" * 64}, headers={DESKTOP_SESSION_HEADER: OLD})
        wrong_header = client.post("/api/desktop/session/rotate", json=payload, headers={DESKTOP_SESSION_HEADER: NEW})
        non_desktop = client.post("/api/desktop/session/rotate", json=payload)
        forged_instance = client.post("/api/desktop/session/rotate", json={**payload, "instance_id": "other"}, headers={DESKTOP_SESSION_HEADER: OLD})
        assert unsigned.status_code == 403
        assert wrong_header.status_code == non_desktop.status_code == 403
        assert forged_instance.status_code == 400
        assert client.get("/probe", headers={DESKTOP_SESSION_HEADER: OLD}).status_code == 200


def test_rotation_updates_health_and_keeps_short_old_grace(monkeypatch) -> None:
    _configure(monkeypatch)
    payload = _payload()
    with TestClient(_app()) as client:
        rotated = client.post("/api/desktop/session/rotate", json=payload, headers={DESKTOP_SESSION_HEADER: OLD})
        assert rotated.status_code == 200
        assert rotated.json() == {
            "status": "rotated", "instance_id": INSTANCE,
            "rotation_id": payload["rotation_id"],
            "session_expires_at": payload["next_expires_at"],
        }
        assert sessions.desktop_health_payload()["session_fingerprint"] == hashlib.sha256(NEW.encode()).hexdigest()
        assert client.get("/probe", headers={DESKTOP_SESSION_HEADER: NEW}).json()["session_secret"] == NEW
        assert client.get("/probe", headers={DESKTOP_SESSION_HEADER: OLD}).json()["session_secret"] == OLD
        retry = client.post("/api/desktop/session/rotate", json=payload, headers={DESKTOP_SESSION_HEADER: OLD})
        assert retry.status_code == 200 and retry.json() == rotated.json()
        assert client.post("/api/desktop/session/rotate", json={**payload, "next_secret": "c" * 43}, headers={DESKTOP_SESSION_HEADER: OLD}).status_code == 403
        sessions._state.previous_until = datetime.now(UTC) - timedelta(seconds=1)
        assert client.get("/probe", headers={DESKTOP_SESSION_HEADER: OLD}).status_code == 403
        assert client.post("/api/desktop/session/rotate", json=payload, headers={DESKTOP_SESSION_HEADER: OLD}).status_code == 403
        assert client.get("/probe", headers={DESKTOP_SESSION_HEADER: NEW}).status_code == 200


def test_rotation_rejects_expired_or_overlong_successor(monkeypatch) -> None:
    _configure(monkeypatch)
    with TestClient(_app()) as client:
        too_long = _payload(expires=(datetime.now(UTC) + timedelta(hours=9)).isoformat())
        assert client.post("/api/desktop/session/rotate", json=too_long, headers={DESKTOP_SESSION_HEADER: OLD}).status_code == 400
        expired = _payload(expires=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
        assert client.post("/api/desktop/session/rotate", json=expired, headers={DESKTOP_SESSION_HEADER: OLD}).status_code == 400
        assert client.get("/probe", headers={DESKTOP_SESSION_HEADER: OLD}).status_code == 200


def test_concurrent_rotations_have_one_winner(monkeypatch) -> None:
    _configure(monkeypatch)
    first = _payload(rotation_id="rotation_first", next_secret="b" * 43)
    second = _payload(rotation_id="rotation_second", next_secret="c" * 43)
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda payload: sessions.rotate_desktop_session(payload, OLD)[0], (first, second)))
    assert sorted(statuses) == [200, 403]


def test_in_flight_request_keeps_its_authenticated_signature_snapshot(monkeypatch) -> None:
    _configure(monkeypatch)
    entered, proceed = Event(), Event()
    app = _app()

    @app.get("/in-flight")
    async def in_flight():
        entered.set()
        assert await asyncio.to_thread(proceed.wait, 5)
        return {"session_secret": sessions.desktop_session().secret}

    response_holder = []
    with TestClient(app) as client:
        thread = Thread(target=lambda: response_holder.append(client.get("/in-flight", headers={DESKTOP_SESSION_HEADER: OLD})))
        thread.start()
        assert entered.wait(timeout=5)
        try:
            rotated = client.post("/api/desktop/session/rotate", json=_payload(), headers={DESKTOP_SESSION_HEADER: OLD})
            assert rotated.status_code == 200
        finally:
            proceed.set()
            thread.join(timeout=5)
        assert not thread.is_alive()
        assert response_holder[0].json()["session_secret"] == OLD


def test_new_websocket_rejects_a_ticket_after_desktop_session_expiry(monkeypatch) -> None:
    _configure(monkeypatch)
    app = FastAPI()

    @app.websocket("/socket")
    async def socket(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_json({"authorized": realtime_asr._websocket_authorized(websocket)})

    create_worker_auth_middleware(app)
    with TestClient(app) as client:
        before = realtime_asr._TICKET_AUTHORITY.issue(subject=INSTANCE)
        with client.websocket_connect(f"/socket?ticket={before}") as socket:
            assert socket.receive_json() == {"authorized": True}
        after = realtime_asr._TICKET_AUTHORITY.issue(subject=INSTANCE)
        monkeypatch.setenv(DESKTOP_EXPIRES_ENV, (datetime.now(UTC) - timedelta(seconds=1)).isoformat())
        with client.websocket_connect(f"/socket?ticket={after}") as socket:
            assert socket.receive_json() == {"authorized": False}
