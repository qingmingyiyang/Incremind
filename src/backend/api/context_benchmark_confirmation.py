"""Durable, Core-owned confirmation facts for governed context benchmarks.

This module records the user's confirmation before a benchmark plan can be
prepared.  It contains only immutable scope, consent references, and frozen
revision identifiers.  It deliberately has no model, provider, Secret, retry,
or recovery responsibilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import re

from core.context_graph import FrozenContextRevisions
from core.storage_provider import (
    SQLiteStructuredRecordStore,
    SQLiteUnitOfWorkConflict,
    SQLiteUnitOfWorkError,
)


class ContextBenchmarkConfirmationError(ValueError):
    """A benchmark confirmation fact is malformed or unavailable."""


class ContextBenchmarkConfirmationConflict(ContextBenchmarkConfirmationError):
    """A confirmation identity is already bound to different authority."""


_SCHEMA_VERSION = "1.0.0"
_COLLECTION = "context_benchmark_confirmations"
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")


@dataclass(frozen=True, slots=True)
class ContextBenchmarkConfirmationCreateRequest:
    """Server-derived confirmation input, without client-authored references."""

    run_id: str
    suite_run_id: str
    project_id: str
    session_id: str
    actor_id: str
    capability_id: str
    replicate_index: int
    consent_refs: tuple[str, ...]
    confirmed_at: str
    revisions: FrozenContextRevisions


@dataclass(frozen=True, slots=True)
class ContextBenchmarkConfirmationFact:
    schema_version: str
    confirmation_ref: str
    run_id: str
    suite_run_id: str
    project_id: str
    session_id: str
    actor_id: str
    capability_id: str
    replicate_index: int
    consent_refs: tuple[str, ...]
    confirmed_at: str
    revisions: FrozenContextRevisions


class ContextBenchmarkConfirmationRepository:
    """Append-only SQLite confirmation facts with CAS idempotency."""

    def __init__(self, records: SQLiteStructuredRecordStore) -> None:
        if not isinstance(records, SQLiteStructuredRecordStore):
            raise ContextBenchmarkConfirmationError("benchmark confirmation store is invalid")
        self._records = records

    def create(
        self, request: ContextBenchmarkConfirmationCreateRequest,
    ) -> ContextBenchmarkConfirmationFact:
        _validate_create_request(request)
        fact = ContextBenchmarkConfirmationFact(
            schema_version=_SCHEMA_VERSION,
            confirmation_ref=_confirmation_ref(request.project_id, request.run_id),
            run_id=request.run_id,
            suite_run_id=request.suite_run_id,
            project_id=request.project_id,
            session_id=request.session_id,
            actor_id=request.actor_id,
            capability_id=request.capability_id,
            replicate_index=request.replicate_index,
            consent_refs=request.consent_refs,
            confirmed_at=request.confirmed_at,
            revisions=request.revisions,
        )
        payload = _payload(fact)
        try:
            with self._records.begin() as uow:
                uow.put(_COLLECTION, fact.run_id, payload, expected_revision=0)
                uow.commit()
        except SQLiteUnitOfWorkConflict as error:
            existing = self._read_by_run_id(fact.run_id)
            if existing is not None and _canonical(_payload(existing)) == _canonical(payload):
                return existing
            raise ContextBenchmarkConfirmationConflict(
                "benchmark confirmation identity drifted"
            ) from error
        except SQLiteUnitOfWorkError as error:
            raise ContextBenchmarkConfirmationError(
                "benchmark confirmation persistence is unavailable"
            ) from error
        return fact

    def get(self, *, project_id: str, run_id: str) -> ContextBenchmarkConfirmationFact | None:
        project = _identity(project_id, "project id")
        run = _identity(run_id, "run id")
        fact = self._read_by_run_id(run)
        if fact is None:
            return None
        if fact.project_id != project:
            raise ContextBenchmarkConfirmationConflict("benchmark confirmation project drifted")
        return fact

    def verify(
        self,
        confirmation_ref: str,
        *,
        run_id: str,
        suite_run_id: str,
        project_id: str,
        session_id: str,
        actor_id: str,
        capability_id: str,
        replicate_index: int,
        consent_refs: tuple[str, ...],
        revisions: FrozenContextRevisions,
    ) -> ContextBenchmarkConfirmationFact:
        expected = ContextBenchmarkConfirmationCreateRequest(
            run_id=run_id,
            suite_run_id=suite_run_id,
            project_id=project_id,
            session_id=session_id,
            actor_id=actor_id,
            capability_id=capability_id,
            replicate_index=replicate_index,
            consent_refs=consent_refs,
            confirmed_at="2000-01-01T00:00:00Z",
            revisions=revisions,
        )
        _validate_create_request(expected)
        ref = _text(confirmation_ref, "reference")
        expected_ref = _confirmation_ref(expected.project_id, expected.run_id)
        if ref != expected_ref:
            raise ContextBenchmarkConfirmationConflict("benchmark confirmation reference drifted")
        fact = self.get(project_id=expected.project_id, run_id=expected.run_id)
        if fact is None:
            raise ContextBenchmarkConfirmationError("benchmark confirmation is unavailable")
        if (
            fact.confirmation_ref != expected_ref
            or fact.suite_run_id != expected.suite_run_id
            or fact.session_id != expected.session_id
            or fact.actor_id != expected.actor_id
            or fact.capability_id != expected.capability_id
            or fact.replicate_index != expected.replicate_index
            or fact.consent_refs != expected.consent_refs
            or fact.revisions != expected.revisions
        ):
            raise ContextBenchmarkConfirmationConflict("benchmark confirmation authority drifted")
        return fact

    def _read_by_run_id(self, run_id: str) -> ContextBenchmarkConfirmationFact | None:
        try:
            record = self._records.read(_COLLECTION, run_id)
        except SQLiteUnitOfWorkError as error:
            raise ContextBenchmarkConfirmationError(
                "benchmark confirmation read is unavailable"
            ) from error
        if record is None:
            return None
        fact = _fact_from_payload(record.payload)
        if fact.run_id != run_id:
            raise ContextBenchmarkConfirmationError(
                "benchmark confirmation record identity drifted"
            )
        return fact


def _payload(fact: ContextBenchmarkConfirmationFact) -> dict[str, object]:
    _validate_fact(fact)
    return {
        "schema_version": fact.schema_version,
        "confirmation_ref": fact.confirmation_ref,
        "run_id": fact.run_id,
        "suite_run_id": fact.suite_run_id,
        "project_id": fact.project_id,
        "session_id": fact.session_id,
        "actor_id": fact.actor_id,
        "capability_id": fact.capability_id,
        "replicate_index": fact.replicate_index,
        "consent_refs": list(fact.consent_refs),
        "confirmed_at": fact.confirmed_at,
        "revisions": _revisions_payload(fact.revisions),
    }


def _fact_from_payload(payload: object) -> ContextBenchmarkConfirmationFact:
    fields = {
        "schema_version", "confirmation_ref", "run_id", "suite_run_id", "project_id",
        "session_id", "actor_id", "capability_id", "replicate_index", "consent_refs",
        "confirmed_at", "revisions",
    }
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ContextBenchmarkConfirmationError("benchmark confirmation record is invalid")
    refs = payload.get("consent_refs")
    if not isinstance(refs, list):
        raise ContextBenchmarkConfirmationError("benchmark confirmation record is invalid")
    fact = ContextBenchmarkConfirmationFact(
        schema_version=_text(payload.get("schema_version"), "schema version"),
        confirmation_ref=_text(payload.get("confirmation_ref"), "reference"),
        run_id=_identity(payload.get("run_id"), "run id"),
        suite_run_id=_identity(payload.get("suite_run_id"), "suite run id"),
        project_id=_identity(payload.get("project_id"), "project id"),
        session_id=_identity(payload.get("session_id"), "session id"),
        actor_id=_identity(payload.get("actor_id"), "actor id"),
        capability_id=_identity(payload.get("capability_id"), "capability id"),
        replicate_index=_replicate_index(payload.get("replicate_index")),
        consent_refs=tuple(_text(item, "consent ref") for item in refs),
        confirmed_at=_timestamp(payload.get("confirmed_at")),
        revisions=_revisions_from_payload(payload.get("revisions")),
    )
    _validate_fact(fact)
    return fact


def _validate_create_request(value: object) -> None:
    if not isinstance(value, ContextBenchmarkConfirmationCreateRequest):
        raise ContextBenchmarkConfirmationError("benchmark confirmation request is invalid")
    for item, label in (
        (value.run_id, "run id"), (value.suite_run_id, "suite run id"),
        (value.project_id, "project id"), (value.session_id, "session id"),
        (value.actor_id, "actor id"), (value.capability_id, "capability id"),
    ):
        _identity(item, label)
    _replicate_index(value.replicate_index)
    _consent_refs(value.consent_refs)
    _timestamp(value.confirmed_at)
    if not isinstance(value.revisions, FrozenContextRevisions):
        raise ContextBenchmarkConfirmationError("benchmark confirmation revisions are invalid")


def _validate_fact(fact: ContextBenchmarkConfirmationFact) -> None:
    if fact.schema_version != _SCHEMA_VERSION:
        raise ContextBenchmarkConfirmationError("benchmark confirmation schema is invalid")
    _validate_create_request(ContextBenchmarkConfirmationCreateRequest(
        fact.run_id, fact.suite_run_id, fact.project_id, fact.session_id,
        fact.actor_id, fact.capability_id, fact.replicate_index, fact.consent_refs,
        fact.confirmed_at, fact.revisions,
    ))
    if fact.confirmation_ref != _confirmation_ref(fact.project_id, fact.run_id):
        raise ContextBenchmarkConfirmationError("benchmark confirmation reference is invalid")


def _revisions_payload(value: FrozenContextRevisions) -> dict[str, str]:
    return {
        "capability_revision": value.capability_revision,
        "boundary_revision": value.boundary_revision,
        "provider_revision": value.provider_revision,
        "model_route_revision": value.model_route_revision,
        "compiler_revision": value.compiler_revision,
    }


def _revisions_from_payload(value: object) -> FrozenContextRevisions:
    fields = {
        "capability_revision", "boundary_revision", "provider_revision",
        "model_route_revision", "compiler_revision",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ContextBenchmarkConfirmationError("benchmark confirmation revisions are invalid")
    try:
        return FrozenContextRevisions(**{
            name: _text(value.get(name), name.replace("_", " ")) for name in fields
        })
    except (TypeError, ValueError) as error:
        raise ContextBenchmarkConfirmationError(
            "benchmark confirmation revisions are invalid"
        ) from error


def _consent_refs(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, tuple)
        or not value
        or len(value) > 16
        or len(set(value)) != len(value)
    ):
        raise ContextBenchmarkConfirmationError("benchmark confirmation consent refs are invalid")
    return tuple(_text(item, "consent ref") for item in value)


def _confirmation_ref(project_id: str, run_id: str) -> str:
    return f"crp://context-benchmark-confirmations/{project_id}/{run_id}"


def _replicate_index(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 9_999:
        raise ContextBenchmarkConfirmationError(
            "benchmark confirmation replicate index is invalid"
        )
    return value


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ContextBenchmarkConfirmationError(f"benchmark confirmation {label} is invalid")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextBenchmarkConfirmationError(f"benchmark confirmation {label} is invalid")
    return value


def _timestamp(value: object) -> str:
    text = _text(value, "confirmed time")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ContextBenchmarkConfirmationError(
            "benchmark confirmation confirmed time is invalid"
        ) from error
    if parsed.tzinfo is None:
        raise ContextBenchmarkConfirmationError(
            "benchmark confirmation confirmed time is invalid"
        )
    return text


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
