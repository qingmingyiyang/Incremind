from __future__ import annotations

import os
import re
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from typing import Protocol
from urllib.parse import urlsplit

from backend.security.secrets import SecretSnapshot, SecretStore


_TOKEN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}\Z")


class SecretEgressError(ValueError):
    pass


class BoundaryRevisionReader(Protocol):
    def __call__(self, project_id: str) -> str: ...


@dataclass(frozen=True, slots=True)
class SecretLease:
    lease_id: str
    project_id: str
    secret_ref: str
    secret_revision: int
    purpose: str
    allowed_hosts: tuple[str, ...]
    boundary_revision: str
    expires_at: float


class SecretEgressBroker:
    """The only component allowed to materialize SecretStore values at a wire boundary."""

    def __init__(
        self, store: SecretStore, *, boundary_revision_reader: BoundaryRevisionReader,
        clock=monotonic,
    ) -> None:
        self._store = store
        self._boundary_revision_reader = boundary_revision_reader
        self._clock = clock
        self._lock = RLock()
        self._active: dict[str, SecretLease] = {}

    def has_secret(self, secret_ref: str) -> bool:
        return self._store.has_secret(_identity(secret_ref, "secret_ref"))

    def grant(
        self, *, project_id: str, secret_ref: str, purpose: str,
        allowed_hosts: tuple[str, ...], boundary_revision: str, ttl_seconds: int = 300,
    ) -> SecretLease:
        project = _identity(project_id, "project_id")
        reference = _identity(secret_ref, "secret_ref")
        normalized_purpose = _identity(purpose, "purpose")
        revision = _identity(boundary_revision, "boundary_revision")
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or not 1 <= ttl_seconds <= 300:
            raise SecretEgressError("secret_lease_ttl_invalid")
        hosts = tuple(sorted({_host(item) for item in allowed_hosts}))
        if not hosts:
            raise SecretEgressError("secret_lease_hosts_required")
        if self._boundary_revision_reader(project) != revision:
            raise SecretEgressError("secret_lease_boundary_drift")
        generation = self._store.get_generation(reference)
        if generation < 1:
            raise SecretEgressError("secret_unavailable")
        lease = SecretLease(
            lease_id="secret-lease-" + os.urandom(24).hex(), project_id=project,
            secret_ref=reference, secret_revision=generation, purpose=normalized_purpose,
            allowed_hosts=hosts, boundary_revision=revision,
            expires_at=self._clock() + ttl_seconds,
        )
        with self._lock:
            self._active[lease.lease_id] = lease
        return lease

    def revoke(self, lease_id: str) -> None:
        with self._lock:
            self._active.pop(lease_id, None)

    def inject_header(
        self, lease: SecretLease, *, project_id: str, purpose: str,
        boundary_revision: str, url: str, header_name: str, prefix: str = "",
    ) -> dict[str, str]:
        if not isinstance(header_name, str) or not header_name or any(char in header_name for char in "\r\n\x00"):
            raise SecretEgressError("secret_header_invalid")
        value = self._materialize(
            lease, project_id=project_id, purpose=purpose,
            boundary_revision=boundary_revision, host=_url_host(url),
        )
        return {header_name: f"{prefix}{value}"}

    def materialize_for_sdk(
        self, lease: SecretLease, *, project_id: str, purpose: str,
        boundary_revision: str, url: str,
    ) -> str:
        """Materialize only inside an SDK's immediate wire-call adapter."""
        return self._materialize(
            lease, project_id=project_id, purpose=purpose,
            boundary_revision=boundary_revision, host=_url_host(url),
        )

    def _materialize(
        self, lease: SecretLease, *, project_id: str, purpose: str,
        boundary_revision: str, host: str,
    ) -> str:
        if not isinstance(lease, SecretLease):
            raise SecretEgressError("secret_lease_invalid")
        with self._lock:
            current = self._active.get(lease.lease_id)
        if current != lease:
            raise SecretEgressError("secret_lease_revoked")
        if (
            _identity(project_id, "project_id") != lease.project_id
            or _identity(purpose, "purpose") != lease.purpose
            or _identity(boundary_revision, "boundary_revision") != lease.boundary_revision
        ):
            raise SecretEgressError("secret_lease_context_denied")
        if self._clock() >= lease.expires_at:
            self.revoke(lease.lease_id)
            raise SecretEgressError("secret_lease_expired")
        if _host(host) not in lease.allowed_hosts:
            raise SecretEgressError("secret_lease_host_denied")
        if self._boundary_revision_reader(lease.project_id) != lease.boundary_revision:
            raise SecretEgressError("secret_lease_boundary_drift")
        snapshot = self._store.get_snapshot(lease.secret_ref)
        if (
            not isinstance(snapshot, SecretSnapshot) or snapshot.generation != lease.secret_revision
            or not snapshot.value
        ):
            raise SecretEgressError("secret_lease_revision_drift")
        return snapshot.value


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise SecretEgressError(f"{label}_invalid")
    return value


def _host(value: object) -> str:
    if not isinstance(value, str) or not value or any(char in value for char in "\r\n\x00"):
        raise SecretEgressError("secret_lease_host_invalid")
    normalized = value.encode("idna").decode("ascii").casefold().rstrip(".")
    if not normalized or "/" in normalized or ":" in normalized:
        raise SecretEgressError("secret_lease_host_invalid")
    return normalized


def _url_host(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise SecretEgressError("secret_lease_url_invalid") from exc
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise SecretEgressError("secret_lease_url_invalid")
    return _host(parsed.hostname)
