from __future__ import annotations

import hmac
import hashlib
import os
import re
import threading
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


DESKTOP_MODE_ENV = "CHRIPTMAS_DESKTOP_SESSION_MODE"
DESKTOP_SECRET_ENV = "CHRIPTMAS_DESKTOP_SESSION_SECRET"
DESKTOP_INSTANCE_ENV = "CHRIPTMAS_DESKTOP_INSTANCE_ID"
DESKTOP_NONCE_ENV = "CHRIPTMAS_DESKTOP_NONCE"
DESKTOP_PROTOCOL_ENV = "CHRIPTMAS_DESKTOP_PROTOCOL_VERSION"
DESKTOP_EXPIRES_ENV = "CHRIPTMAS_DESKTOP_SESSION_EXPIRES_AT"
DESKTOP_ALLOWED_ORIGIN_ENV = "CHRIPTMAS_DESKTOP_ALLOWED_ORIGIN"
DESKTOP_SESSION_HEADER = "X-Chriptmas-Desktop-Session"
DESKTOP_PROTOCOL_VERSION = "desktop-loopback/1"
DESKTOP_ROTATION_VERSION = "desktop-session-rotation/1"
_ROTATION_GRACE = timedelta(seconds=60)
_MAX_SESSION_LIFETIME = timedelta(hours=8)
_SECRET_PATTERN = re.compile(r"[A-Za-z0-9_-]{43,128}\Z")
_ROTATION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")


@dataclass(frozen=True)
class DesktopSession:
    secret: str
    instance_id: str
    nonce: str
    expires_at: str
    allowed_origin: str


@dataclass
class _SessionState:
    environment: DesktopSession
    current: DesktopSession
    previous: DesktopSession | None = None
    previous_until: datetime | None = None
    rotation_id: str | None = None


_lock = threading.RLock()
_state: _SessionState | None = None
_request_session: ContextVar[DesktopSession | None] = ContextVar(
    "desktop_request_session", default=None,
)


def _configured_session() -> DesktopSession | None:
    if os.environ.get(DESKTOP_MODE_ENV) != "desktop_production":
        return None
    values = {
        "secret": os.environ.get(DESKTOP_SECRET_ENV, ""),
        "instance_id": os.environ.get(DESKTOP_INSTANCE_ENV, ""),
        "nonce": os.environ.get(DESKTOP_NONCE_ENV, ""),
        "expires_at": os.environ.get(DESKTOP_EXPIRES_ENV, ""),
        "allowed_origin": os.environ.get(DESKTOP_ALLOWED_ORIGIN_ENV, ""),
    }
    if (
        os.environ.get(DESKTOP_PROTOCOL_ENV) != DESKTOP_PROTOCOL_VERSION
        or len(values["secret"]) < 43
        or len(values["nonce"]) < 43
        or not all(values.values())
        or not values["allowed_origin"].startswith("http://127.0.0.1:")
    ):
        raise RuntimeError("desktop_session_config_invalid")
    _expires_at(values["expires_at"])
    return DesktopSession(**values)


def _active_state() -> _SessionState | None:
    global _state
    configured = _configured_session()
    if configured is None:
        return None
    with _lock:
        if _state is None or _state.environment != configured:
            _state = _SessionState(environment=configured, current=configured)
        return _state


def desktop_session() -> DesktopSession | None:
    snapshot = _request_session.get()
    if snapshot is not None:
        return snapshot
    state = _active_state()
    return state.current if state is not None else None


def current_desktop_session() -> DesktopSession | None:
    """Return the live session, including within a request using an older snapshot."""
    state = _active_state()
    return state.current if state is not None else None


def desktop_session_for_header(header: str | None) -> DesktopSession | None:
    if not isinstance(header, str):
        return None
    state = _active_state()
    if state is None:
        return None
    now = datetime.now(timezone.utc)
    with _lock:
        current = state.current
        if _expires_at(current.expires_at) > now and hmac.compare_digest(header, current.secret):
            return current
        previous = state.previous
        if (
            previous is not None
            and state.previous_until is not None
            and state.previous_until > now
            and _expires_at(previous.expires_at) > now
            and hmac.compare_digest(header, previous.secret)
        ):
            return previous
    return None


def bind_desktop_request_session(session: DesktopSession) -> Token[DesktopSession | None]:
    return _request_session.set(session)


def reset_desktop_request_session(token: Token[DesktopSession | None]) -> None:
    _request_session.reset(token)


def desktop_session_authorized(header: str | None) -> bool:
    snapshot = _request_session.get()
    if snapshot is not None and isinstance(header, str) and hmac.compare_digest(header, snapshot.secret):
        return True
    return desktop_session_for_header(header) is not None


def rotate_desktop_session(payload: object, header: str | None) -> tuple[int, dict[str, str]]:
    """Rotate only with a main-process HMAC, atomically and idempotently."""
    state = _active_state()
    if state is None:
        return 403, {"detail": "desktop_session_unauthorized"}
    if not isinstance(payload, dict):
        return 400, {"detail": "desktop_session_rotation_invalid"}
    fields = ("version", "instance_id", "rotation_id", "next_secret", "next_expires_at", "signature")
    if set(payload) != set(fields) or any(not isinstance(payload.get(key), str) for key in fields):
        return 400, {"detail": "desktop_session_rotation_invalid"}
    version, instance_id, rotation_id, next_secret, next_expires_at, signature = (
        payload[key] for key in fields
    )
    if (
        version != DESKTOP_ROTATION_VERSION
        or instance_id != state.current.instance_id
        or not _ROTATION_ID_PATTERN.fullmatch(rotation_id)
        or not _SECRET_PATTERN.fullmatch(next_secret)
        or not re.fullmatch(r"[0-9a-f]{64}", signature)
        or len(next_expires_at) > 64
        or "\n" in next_expires_at
    ):
        return 400, {"detail": "desktop_session_rotation_invalid"}
    try:
        requested_expiry = _expires_at(next_expires_at)
    except RuntimeError:
        return 400, {"detail": "desktop_session_rotation_invalid"}
    signed = "\n".join((version, instance_id, rotation_id, next_secret, next_expires_at))
    with _lock:
        now = datetime.now(timezone.utc)
        previous = state.previous
        if (
            previous is not None
            and state.rotation_id == rotation_id
            and state.previous_until is not None
            and now < state.previous_until
            and now < _expires_at(previous.expires_at)
            and isinstance(header, str)
            and hmac.compare_digest(header, previous.secret)
            and hmac.compare_digest(
                signature,
                hmac.new(previous.secret.encode(), signed.encode(), hashlib.sha256).hexdigest(),
            )
            and state.current.secret == next_secret
            and state.current.expires_at == next_expires_at
        ):
            return 200, _rotation_result(state.current, rotation_id)
        current = state.current
        if (
            not isinstance(header, str)
            or not hmac.compare_digest(header, current.secret)
            or now >= _expires_at(current.expires_at)
            or not hmac.compare_digest(
                signature,
                hmac.new(current.secret.encode(), signed.encode(), hashlib.sha256).hexdigest(),
            )
        ):
            return 403, {"detail": "desktop_session_rotation_unauthorized"}
        if (
            hmac.compare_digest(next_secret, current.secret)
            or requested_expiry <= _expires_at(current.expires_at)
            or requested_expiry <= now
            or requested_expiry > now + _MAX_SESSION_LIFETIME
        ):
            return 400, {"detail": "desktop_session_rotation_invalid"}
        state.previous = current
        state.previous_until = min(now + _ROTATION_GRACE, _expires_at(current.expires_at))
        state.current = DesktopSession(
            secret=next_secret,
            instance_id=current.instance_id,
            nonce=current.nonce,
            expires_at=next_expires_at,
            allowed_origin=current.allowed_origin,
        )
        state.rotation_id = rotation_id
        return 200, _rotation_result(state.current, rotation_id)


def _rotation_result(session: DesktopSession, rotation_id: str) -> dict[str, str]:
    return {
        "status": "rotated",
        "instance_id": session.instance_id,
        "rotation_id": rotation_id,
        "session_expires_at": session.expires_at,
    }


def desktop_health_payload() -> dict[str, object] | None:
    session = current_desktop_session()
    if session is None:
        return None
    return {
        "kind": "desktop_loopback_health",
        "schema_version": "1.0.0",
        "protocol_version": DESKTOP_PROTOCOL_VERSION,
        "instance_id": session.instance_id,
        "nonce": session.nonce,
        "child_pid": os.getpid(),
        "status": "ready",
        "auth_required": True,
        "renderer_secret_access": False,
        "session_fingerprint": hashlib.sha256(session.secret.encode("utf-8")).hexdigest(),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "session_expires_at": session.expires_at,
    }


def _expires_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise RuntimeError("desktop_session_config_invalid") from error
    if parsed.tzinfo is None:
        raise RuntimeError("desktop_session_config_invalid")
    return parsed.astimezone(timezone.utc)
