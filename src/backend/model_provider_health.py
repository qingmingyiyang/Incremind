"""Passive, metadata-only health projection for model providers.

The store is deliberately not an egress gateway.  It records the outcome of a
single wire attempt after the caller has made it, so it cannot trigger retries
or retain prompts, credentials, endpoints, or provider response bodies.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
import re
import sqlite3
import time
from typing import Callable

from backend.shared.interprocess_lock import interprocess_file_lock


_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,159}$")
_ROUTE_KEY = re.compile(r"^[a-z][a-z0-9_.-]{2,79}$")
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_LOCAL_ABSOLUTE_PATH = re.compile(r"(?i)(?:^[A-Z]:[\\/]|^\\\\|^file:/|^/)")


class ModelProviderHealthError(ValueError):
    """The caller attempted to persist data outside the health contract."""


class ModelProviderHealthConflict(ModelProviderHealthError):
    """The durable health projection is temporarily unavailable."""


class ProviderHealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OPEN = "open"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class ProviderFailureClass(StrEnum):
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    AUTH = "auth"
    QUOTA = "quota"
    CLIENT_ERROR = "client_error"
    UNKNOWN = "unknown"


_TRANSIENT_FAILURES = frozenset({
    ProviderFailureClass.TIMEOUT,
    ProviderFailureClass.CONNECTION,
    ProviderFailureClass.RATE_LIMITED,
    ProviderFailureClass.SERVER_ERROR,
})
_BLOCKING_FAILURES = frozenset({ProviderFailureClass.AUTH, ProviderFailureClass.QUOTA})


@dataclass(frozen=True, slots=True)
class ProviderHealthScope:
    """The revision-scoped identity of a provider route; never request content."""

    project_id: str
    boundary_profile_id: str
    boundary_revision: int
    route_key: str
    provider_id: str
    provider_revision: str
    model_name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", _identity(self.project_id, "project id"))
        object.__setattr__(self, "boundary_profile_id", _identity(self.boundary_profile_id, "boundary profile id"))
        object.__setattr__(self, "route_key", _route_key(self.route_key))
        object.__setattr__(self, "provider_id", _identity(self.provider_id, "provider id"))
        object.__setattr__(self, "model_name", _model_name(self.model_name))
        object.__setattr__(self, "boundary_revision", _positive(self.boundary_revision, "boundary revision"))
        object.__setattr__(self, "provider_revision", _revision(self.provider_revision))


@dataclass(frozen=True, slots=True)
class ProviderHealthRecord:
    scope: ProviderHealthScope
    state: ProviderHealthState
    failure_class: ProviderFailureClass | None
    observed_at: str | None
    expires_at: str | None

    @property
    def is_unavailable(self) -> bool:
        return self.state in {ProviderHealthState.OPEN, ProviderHealthState.BLOCKED}


class ModelProviderHealthStore:
    """SQLite-backed passive health projection safe for threads and processes.

    ``open`` only represents uncertain/transient failures and expires after the
    configured TTL.  ``blocked`` is deliberately non-expiring, but its primary
    key includes both Boundary and provider revisions, so changed authority or
    provider identity naturally receives a fresh, unknown health state.
    """

    def __init__(
        self, root_dir: Path, *, transient_open_ttl_seconds: float = 60.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(transient_open_ttl_seconds, (int, float)) or isinstance(transient_open_ttl_seconds, bool) or transient_open_ttl_seconds <= 0:
            raise ModelProviderHealthError("transient open TTL must be positive")
        self._path = Path(root_dir) / ".rebuild-data" / "model-provider-health.sqlite3"
        self._ttl = float(transient_open_ttl_seconds)
        self._clock = clock or time.time

    def get(self, scope: ProviderHealthScope) -> ProviderHealthRecord:
        scope = _scope(scope)
        now = self._clock()
        if not self._path.exists():
            return ProviderHealthRecord(
                scope, ProviderHealthState.UNKNOWN, None, None, None,
            )
        try:
            with interprocess_file_lock(self._path):
                with closing(self._connection()) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    row = conn.execute(
                        """SELECT state, failure_class, observed_at, expires_at
                           FROM model_provider_health WHERE project_id=? AND boundary_profile_id=?
                           AND boundary_revision=? AND route_key=? AND provider_id=?
                           AND provider_revision=? AND model_name=?""",
                        _scope_values(scope),
                    ).fetchone()
                    if row is not None and row[3] is not None and row[3] <= now:
                        conn.execute(
                            "DELETE FROM model_provider_health WHERE project_id=? AND boundary_profile_id=? "
                            "AND boundary_revision=? AND route_key=? AND provider_id=? "
                            "AND provider_revision=? AND model_name=?", _scope_values(scope),
                        )
                        row = None
                    conn.commit()
        except (sqlite3.Error, TimeoutError) as error:
            raise ModelProviderHealthConflict("model provider health authority is busy") from error
        return _record(scope, row)

    status = get

    def observe_success(self, scope: ProviderHealthScope) -> ProviderHealthRecord:
        return self._observe(scope, ProviderHealthState.HEALTHY, None)

    record_success = observe_success

    def observe_failure(
        self, scope: ProviderHealthScope, failure: ProviderFailureClass | str | BaseException,
    ) -> ProviderHealthRecord:
        failure_class = classify_provider_failure(failure)
        if failure_class in _TRANSIENT_FAILURES:
            state = ProviderHealthState.OPEN
        elif failure_class in _BLOCKING_FAILURES:
            state = ProviderHealthState.BLOCKED
        else:
            state = ProviderHealthState.DEGRADED
        return self._observe(scope, state, failure_class)

    record_failure = observe_failure

    def invalidate_provider(self, provider_id: str) -> None:
        """Forget advisory state after a local credential authority change."""
        provider = _identity(provider_id, "provider id")
        if not self._path.exists():
            return
        try:
            with interprocess_file_lock(self._path):
                with closing(self._connection()) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "DELETE FROM model_provider_health WHERE provider_id=?",
                        (provider,),
                    )
                    conn.commit()
        except (sqlite3.Error, TimeoutError) as error:
            raise ModelProviderHealthConflict(
                "model provider health authority is busy"
            ) from error

    def _observe(
        self, scope: ProviderHealthScope, state: ProviderHealthState,
        failure_class: ProviderFailureClass | None,
    ) -> ProviderHealthRecord:
        scope = _scope(scope)
        now = self._clock()
        expires_at = now + self._ttl if state is ProviderHealthState.OPEN else None
        self._initialize()
        try:
            with interprocess_file_lock(self._path):
                with closing(self._connection()) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        """INSERT INTO model_provider_health (
                            project_id, boundary_profile_id, boundary_revision, route_key,
                            provider_id, provider_revision, model_name, state, failure_class,
                            observed_at, expires_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(project_id, boundary_profile_id, boundary_revision, route_key,
                                    provider_id, provider_revision, model_name)
                        DO UPDATE SET state=excluded.state, failure_class=excluded.failure_class,
                                      observed_at=excluded.observed_at, expires_at=excluded.expires_at""",
                        (*_scope_values(scope), state.value,
                         None if failure_class is None else failure_class.value, now, expires_at),
                    )
                    conn.commit()
        except (sqlite3.Error, TimeoutError) as error:
            raise ModelProviderHealthConflict("model provider health authority is busy") from error
        return ProviderHealthRecord(scope, state, failure_class, _timestamp(now), _timestamp(expires_at))

    def _initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with interprocess_file_lock(self._path):
                with closing(self._connection()) as conn:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA busy_timeout=5000")
                    conn.execute(
                        """CREATE TABLE IF NOT EXISTS model_provider_health (
                            project_id TEXT NOT NULL, boundary_profile_id TEXT NOT NULL,
                            boundary_revision INTEGER NOT NULL, route_key TEXT NOT NULL,
                            provider_id TEXT NOT NULL, provider_revision TEXT NOT NULL,
                            model_name TEXT NOT NULL, state TEXT NOT NULL,
                            failure_class TEXT, observed_at REAL NOT NULL, expires_at REAL,
                            PRIMARY KEY (project_id, boundary_profile_id, boundary_revision,
                                         route_key, provider_id, provider_revision, model_name)
                        )"""
                    )
                    conn.commit()
        except (sqlite3.Error, TimeoutError) as error:
            raise ModelProviderHealthConflict("model provider health authority is busy") from error

    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn


ProviderHealthStore = ModelProviderHealthStore


def classify_provider_failure(value: ProviderFailureClass | str | BaseException) -> ProviderFailureClass:
    """Return a stable, body-free failure class without retaining the exception."""
    if isinstance(value, ProviderFailureClass):
        return value
    if isinstance(value, str):
        try:
            return ProviderFailureClass(value)
        except ValueError as error:
            raise ModelProviderHealthError("provider failure class is invalid") from error
    # Transport adapters such as LiteLLM wrap httpx/socket failures and may
    # even expose a synthetic 500 at the outer layer.  Inspect a bounded,
    # body-free exception chain before status so the underlying transport
    # class wins without coupling this module to a particular SDK.
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = value
    while current is not None and len(chain) < 8 and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    if any(isinstance(error, TimeoutError) or "timeout" in type(error).__name__.lower()
           for error in chain):
        return ProviderFailureClass.TIMEOUT
    if any(isinstance(error, ConnectionError) or "connection" in type(error).__name__.lower()
           for error in chain):
        return ProviderFailureClass.CONNECTION
    name = type(value).__name__.lower()
    status = getattr(value, "status_code", None)
    if status in {401, 403}:
        return ProviderFailureClass.AUTH
    if status == 429:
        return ProviderFailureClass.RATE_LIMITED
    if isinstance(status, int) and 500 <= status <= 599:
        return ProviderFailureClass.SERVER_ERROR
    if "quota" in name or "insufficientquota" in name:
        return ProviderFailureClass.QUOTA
    return ProviderFailureClass.UNKNOWN


def _scope(value: object) -> ProviderHealthScope:
    if not isinstance(value, ProviderHealthScope):
        raise ModelProviderHealthError("provider health scope is invalid")
    return value


def _scope_values(scope: ProviderHealthScope) -> tuple[object, ...]:
    return (scope.project_id, scope.boundary_profile_id, scope.boundary_revision,
            scope.route_key, scope.provider_id, scope.provider_revision, scope.model_name)


def _record(scope: ProviderHealthScope, row: sqlite3.Row | tuple[object, ...] | None) -> ProviderHealthRecord:
    if row is None:
        return ProviderHealthRecord(scope, ProviderHealthState.UNKNOWN, None, None, None)
    state = ProviderHealthState(str(row[0]))
    failure = None if row[1] is None else ProviderFailureClass(str(row[1]))
    return ProviderHealthRecord(scope, state, failure, _timestamp(float(row[2])), _timestamp(None if row[3] is None else float(row[3])))


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise ModelProviderHealthError(f"provider health {label} is invalid")
    return value


def _route_key(value: object) -> str:
    if not isinstance(value, str) or not _ROUTE_KEY.fullmatch(value):
        raise ModelProviderHealthError("provider health route key is invalid")
    return value


def _revision(value: object) -> str:
    if not isinstance(value, str) or not _REVISION.fullmatch(value):
        raise ModelProviderHealthError("provider health provider revision is invalid")
    return value


def _model_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not _MODEL_NAME.fullmatch(value)
        or _LOCAL_ABSOLUTE_PATH.search(value)
    ):
        raise ModelProviderHealthError("provider health model name is invalid")
    return value


def _positive(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ModelProviderHealthError(f"provider health {label} must be positive")
    return value


def _timestamp(value: float | None) -> str | None:
    return None if value is None else datetime.fromtimestamp(value, UTC).isoformat()
