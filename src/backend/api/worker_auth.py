"""Bound, short-lived authentication for the local Python worker."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path

from fastapi import Request


WORKER_SECRET_ENV = "CHRIPTMAS_WORKER_SECRET"
WORKER_INSTANCE_ENV = "CHRIPTMAS_WORKER_INSTANCE_ID"
WORKER_PORT_ENV = "CHRIPTMAS_WORKER_PORT"
WORKER_SECRET_HEADER = "X-Worker-Secret"
WORKER_CHALLENGE_HEADER = "X-Worker-Challenge"
WORKER_IDENTITY_HEADER = "X-Worker-Identity"
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_CLOCK_SKEW_SECONDS = 30
_MAX_REPLAY_ENTRIES = 4096


def worker_config() -> tuple[str, str, int] | None:
    secret = os.environ.get(WORKER_SECRET_ENV, "")
    if not secret:
        return None
    instance = os.environ.get(WORKER_INSTANCE_ENV, "")
    port_text = os.environ.get(WORKER_PORT_ENV, "")
    if not _HEX32.fullmatch(instance) or not port_text or not port_text.isascii():
        raise RuntimeError("worker_auth_config_invalid")
    try:
        port = int(port_text)
    except ValueError as error:
        raise RuntimeError("worker_auth_config_invalid") from error
    if port != 8001:
        raise RuntimeError("worker_auth_config_invalid")
    return secret, instance, port


def worker_health_identity(
    challenge: str | None, *, container_root: object = None, recognition_root: object = None,
) -> str | None:
    config = worker_config()
    if config is None or challenge is None:
        return None
    if not _HEX32.fullmatch(challenge):
        raise ValueError("worker_challenge_invalid")
    root = _health_root(container_root)
    if root != _health_root(recognition_root):
        raise RuntimeError("worker_health_root_invalid")
    root_hex = str(root).encode("utf-8").hex()
    secret, instance, port = config
    pid = os.getpid()
    payload = f"chriptmas-worker-health/v2\n{challenge}\n{instance}\n{pid}\n{port}\n{root_hex}"
    proof = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"v2:{instance}:{pid}:{port}:{root_hex}:{proof}"


def _health_root(value: object) -> Path:
    try:
        root = Path(value).expanduser()
        if not root.is_absolute():
            raise RuntimeError("worker_health_root_invalid")
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise RuntimeError("worker_health_root_invalid")
        return root
    except (OSError, TypeError, ValueError) as error:
        raise RuntimeError("worker_health_root_invalid") from error


class WorkerRequestAuthenticator:
    """Verify signatures and reject duplicate nonces within a bounded window."""

    def __init__(self, secret: str, instance_id: str) -> None:
        self._key = secret.encode("utf-8")
        self._instance_id = instance_id
        self._seen: OrderedDict[str, int] = OrderedDict()
        self._lock = threading.Lock()

    def verify(self, request: Request, header: str | None) -> bool:
        if not isinstance(header, str) or len(header) > 128:
            return False
        pieces = header.split(":")
        if len(pieces) != 4 or pieces[0] != "v1":
            return False
        _, seconds_text, nonce, supplied_proof = pieces
        if (
            not seconds_text.isascii()
            or not seconds_text.isdecimal()
            or not _HEX32.fullmatch(nonce)
            or not _HEX64.fullmatch(supplied_proof)
        ):
            return False
        now = int(time.time())
        seconds = int(seconds_text)
        if abs(now - seconds) > _MAX_CLOCK_SKEW_SECONDS:
            return False
        raw_path = request.scope.get("raw_path", b"")
        query = request.scope.get("query_string", b"")
        if not isinstance(raw_path, bytes) or not isinstance(query, bytes):
            return False
        try:
            target = raw_path.decode("ascii")
            if query:
                target += "?" + query.decode("ascii")
        except UnicodeDecodeError:
            return False
        payload = (
            f"chriptmas-worker-request/v1\n{self._instance_id}\n"
            f"{request.method}\n{target}\n{seconds_text}\n{nonce}"
        )
        expected = hmac.new(self._key, payload.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied_proof):
            return False
        with self._lock:
            # A future-dated proof can remain valid for twice the skew window.
            cutoff = now - 2 * _MAX_CLOCK_SKEW_SECONDS
            while self._seen and next(iter(self._seen.values())) < cutoff:
                self._seen.popitem(last=False)
            if nonce in self._seen:
                return False
            if len(self._seen) >= _MAX_REPLAY_ENTRIES:
                return False
            self._seen[nonce] = now
        return True
