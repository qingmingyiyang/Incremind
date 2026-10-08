"""Request-scoped wire attempt lifecycle for legacy gateway callers.

The shared gateway already threads a ``WireAttemptSink`` through every real
wire attempt and enforces exactly-one terminal per attempt.  This module
provides the production recorder: one instance per model stage of one
generation request.  It records immutable attempt facts that the caller
persists through its own artifact store; it never authorizes anything and
never touches health state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from time import monotonic
from uuid import uuid4

from backend.shared.llm.litellm_gateway import WireAttemptHandle, WireAttemptSink

_TERMINAL_STATUSES = frozenset(("succeeded", "failed_transport", "consumer_cancelled"))


@dataclass(frozen=True, slots=True)
class WireAttemptRecord:
    attempt_id: str
    request_id: str
    stage: str
    attempt_number: int
    model_identity: str
    started_at: str
    completed_at: str
    duration_ms: int
    status: str
    usage: dict[str, int] | None = None
    cache_observation: dict[str, int] | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "request_id": self.request_id,
            "stage": self.stage,
            "attempt_number": self.attempt_number,
            "model_identity": self.model_identity,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "usage": self.usage,
            "cache_observation": self.cache_observation,
            "error_code": self.error_code,
        }


@dataclass
class _RequestScopedWireAttemptHandle:
    recorder: "RequestScopedWireAttemptRecorder" = field(repr=False)
    attempt_id: str
    attempt_number: int
    started_at: str
    started_monotonic: float
    _finished: bool = field(default=False, init=False)
    _lock: Lock = field(default_factory=Lock, init=False)

    def succeeded(self, *, usage, cache_observation=None) -> None:
        self._finish(
            status="succeeded",
            usage=dict(usage or {}),
            cache_observation=dict(cache_observation) if cache_observation is not None else None,
            error_code=None,
        )

    def failed_transport(self, *, error_code: str) -> None:
        self._finish(status="failed_transport", usage=None, cache_observation=None, error_code=error_code)

    def consumer_cancelled(self) -> None:
        self._finish(status="consumer_cancelled", usage=None, cache_observation=None, error_code=None)

    def _finish(self, *, status: str, usage, cache_observation, error_code: str | None) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
        self.recorder._append(
            WireAttemptRecord(
                attempt_id=self.attempt_id,
                request_id=self.recorder.request_id,
                stage=self.recorder.stage,
                attempt_number=self.attempt_number,
                model_identity=self.recorder.model_identity,
                started_at=self.started_at,
                completed_at=datetime.now(timezone.utc).isoformat(),
                duration_ms=max(0, int((monotonic() - self.started_monotonic) * 1000)),
                status=status,
                usage=usage,
                cache_observation=cache_observation,
                error_code=error_code,
            )
        )


class RequestScopedWireAttemptRecorder:
    """One governed attempt ledger per model stage of one request."""

    def __init__(
        self,
        *,
        request_id: str,
        stage: str,
        model_identity: str,
    ) -> None:
        if not request_id.strip() or not stage.strip():
            raise ValueError("request-scoped wire attempt identity is invalid")
        self.request_id = request_id
        self.stage = stage
        self.model_identity = model_identity or "unknown"
        self._records: list[WireAttemptRecord] = []
        self._next_attempt_number = 1
        self._lock = Lock()

    def begin_model_wire_attempt(self) -> WireAttemptHandle:
        with self._lock:
            attempt_number = self._next_attempt_number
            self._next_attempt_number += 1
        return _RequestScopedWireAttemptHandle(
            recorder=self,
            attempt_id=f"{self.request_id}:{self.stage}:{attempt_number}:{uuid4().hex[:12]}",
            attempt_number=attempt_number,
            started_at=datetime.now(timezone.utc).isoformat(),
            started_monotonic=monotonic(),
        )

    def _append(self, record: WireAttemptRecord) -> None:
        with self._lock:
            self._records.append(record)

    @property
    def records(self) -> tuple[WireAttemptRecord, ...]:
        with self._lock:
            return tuple(self._records)
