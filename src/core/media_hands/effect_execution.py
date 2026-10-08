"""Effect-v2 execution boundary for Media Hands operations.

This is deliberately a domain adapter, not another Job runtime.  It consumes
the immutable Effect intent and Job fact, asks the existing Media Hands handler
to preflight and execute, then records one immutable receipt fact.  Core owns
the Effect transition and recovery scheduling.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.effect_log import EFFECT_V2, Effect, EffectReceipt, EffectState
from core.job_runner import JobStepBlockedError
from core.job_runner.job_projection import initialize_job_projection_schema

from .handler import MediaHandsOperationHandler, MediaOperationStepResult
from .effect_contract import (
    EFFECT_KIND,
    INTENT_SCHEMA,
    RECEIPT_KIND,
    RECEIPT_SCHEMA,
    provider_revision_identity,
)


RECEIPT_TABLE = "media_hands_effect_domain_receipt"
RESERVATION_TABLE = "media_hands_effect_provider_reservation"


class MediaHandsEffectExecutionError(ValueError):
    """The Media Hands domain cannot prove a safe Effect-v2 outcome."""


class MediaHandsExecutionAuthorityDrift(MediaHandsEffectExecutionError):
    """A live policy or ingress authority no longer matches frozen evidence."""


@dataclass(frozen=True, slots=True)
class MediaHandsEffectExecutionHandler:
    """Execute one immutable Media Hands v2 Effect.

    ``after_receipt_write`` exists solely as a crash-injection seam.  Raising
    from it rolls back the receipt transaction; it never settles the Effect.
    """

    database: Path | str
    handler: MediaHandsOperationHandler
    execution_authorizer: Callable[[Effect, Mapping[str, object]], None] | None = None
    execution_checkpoint_factory: Callable[[Effect], Callable[[], None]] | None = None
    after_reservation_write: Callable[[], None] | None = None
    after_domain_write: Callable[[], None] | None = None
    after_receipt_write: Callable[[], None] | None = None
    receipt_committer: Callable[
        [sqlite3.Connection, Effect, Mapping[str, object], Mapping[str, object]], None
    ] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "database", Path(self.database))
        if self.execution_authorizer is not None and not callable(self.execution_authorizer):
            raise TypeError("Media Hands execution authorizer must be callable")
        if (
            self.execution_checkpoint_factory is not None
            and not callable(self.execution_checkpoint_factory)
        ):
            raise TypeError("Media Hands execution checkpoint factory must be callable")
        if self.receipt_committer is not None and not callable(self.receipt_committer):
            raise TypeError("Media Hands receipt committer must be callable")
        _ensure_schema(Path(self.database))

    def __call__(self, effect: Effect) -> EffectReceipt:
        return self.handle(effect)

    def handle(self, effect: Effect) -> EffectReceipt:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _intent, job = _read_and_validate_inputs(connection, effect)
            _assert_frozen_provider_identity(effect, self.handler)
            if _validated_receipt(connection, effect, job, self.handler) is not None:
                connection.commit()
                return _effect_receipt(effect)
            if _validated_reservation(connection, effect, job) is not None:
                raise MediaHandsEffectExecutionError(
                    "Media Hands provider attempt is already reserved without a receipt"
                )
            connection.commit()

            # Live policy and selection authorities may use their own SQLite
            # read transaction.  Evaluate them without holding the domain
            # reservation write lock, then re-open the reservation transaction
            # and revalidate immutable evidence before writing the fence.
            self._authorize(effect, job)
            request = self.handler.preflight_step("execute_operation", job)
            connection.execute("BEGIN IMMEDIATE")
            _intent, job = _read_and_validate_inputs(connection, effect)
            frozen_provider_revision_identity = _assert_frozen_provider_identity(
                effect, self.handler,
            )
            if _validated_receipt(connection, effect, job, self.handler) is not None:
                connection.commit()
                return _effect_receipt(effect)
            if _validated_reservation(connection, effect, job) is not None:
                raise MediaHandsEffectExecutionError(
                    "Media Hands provider attempt is already reserved without a receipt"
                )
            _write_reservation(connection, effect, job)
            connection.commit()

        # A committed reservation is a one-way recovery fence: a crash here
        # must never cause a second provider call without human resolution.
        if self.after_reservation_write is not None:
            self.after_reservation_write()

        if self.execution_checkpoint_factory is not None:
            request = self.handler.attach_execution_control(
                request,
                self.execution_checkpoint_factory(effect),
                recipe_control=None,
            )

        result = self.handler.execute_prepared_step(
            request,
            expected_provider_revision_identity=frozen_provider_revision_identity,
            execution_authorizer=lambda: self._authorize(effect, job),
        )
        if self.after_domain_write is not None:
            self.after_domain_write()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _intent, job = _read_and_validate_inputs(connection, effect)
            _assert_frozen_provider_identity(effect, self.handler)
            existing = _validated_receipt(connection, effect, job, self.handler)
            if existing is None:
                if _validated_reservation(connection, effect, job) is None:
                    raise MediaHandsEffectExecutionError(
                        "Media Hands provider result has no immutable reservation"
                    )
                receipt = _receipt_payload(effect, job, result)
                _write_receipt(connection, receipt)
                if self.receipt_committer is not None:
                    self.receipt_committer(connection, effect, job, receipt)
                if self.after_receipt_write is not None:
                    self.after_receipt_write()
            connection.commit()
        return _effect_receipt(effect)

    def _authorize(self, effect: Effect, job: Mapping[str, object]) -> None:
        if self.execution_authorizer is not None:
            self.execution_authorizer(effect, job)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


@dataclass(frozen=True, slots=True)
class MediaHandsEffectExecutionProbe:
    """Probe immutable receipt and canonical output evidence only."""

    database: Path | str
    handler: MediaHandsOperationHandler
    execution_authorizer: Callable[[Effect, Mapping[str, object]], None] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "database", Path(self.database))
        if self.execution_authorizer is not None and not callable(self.execution_authorizer):
            raise TypeError("Media Hands execution authorizer must be callable")
        _ensure_schema(Path(self.database))

    def __call__(self, effect: Effect) -> tuple[EffectState, str | None]:
        return self.probe(effect)

    def probe(self, effect: Effect) -> tuple[EffectState, str | None]:
        try:
            with sqlite3.connect(self.database) as connection:
                connection.row_factory = sqlite3.Row
                _intent, job = _read_and_validate_inputs(connection, effect)
                _assert_frozen_provider_identity(effect, self.handler)
                receipt = _validated_receipt(connection, effect, job, self.handler)
                if receipt is not None:
                    return EffectState.SETTLED_OK, str(receipt["receipt_ref"])
                if _validated_reservation(connection, effect, job) is not None:
                    return EffectState.UNKNOWN, "error:media-hands-provider-attempt-reserved"
                if self.execution_authorizer is not None:
                    self.execution_authorizer(effect, job)
                self.handler.preflight_step("execute_operation", job)
                # No receipt is proof of neither terminal state nor failure.
                # Do not infer status from the Job projection or lease.
                return EffectState.PLANNED, _retry_evidence_ref(effect.operation_id)
        except MediaHandsExecutionAuthorityDrift:
            return EffectState.UNKNOWN, "error:media-hands-authority-revision-drift"
        except JobStepBlockedError:
            return EffectState.UNKNOWN, "error:media-hands-execution-authorization-invalid"
        except MediaHandsEffectExecutionError:
            return EffectState.UNKNOWN, "error:media-hands-effect-evidence-drift"
        except (TypeError, ValueError, sqlite3.DatabaseError):
            return EffectState.UNKNOWN, "error:media-hands-effect-invalid"


def media_hands_effect_handler(
    database: Path | str, handler: MediaHandsOperationHandler,
    execution_authorizer: Callable[[Effect, Mapping[str, object]], None] | None = None,
) -> MediaHandsEffectExecutionHandler:
    return MediaHandsEffectExecutionHandler(database, handler, execution_authorizer)


def media_hands_effect_probe(
    database: Path | str, handler: MediaHandsOperationHandler,
    execution_authorizer: Callable[[Effect, Mapping[str, object]], None] | None = None,
) -> MediaHandsEffectExecutionProbe:
    return MediaHandsEffectExecutionProbe(database, handler, execution_authorizer)


def _read_and_validate_inputs(
    connection: sqlite3.Connection, effect: Effect,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    if (
        effect.contract_version != EFFECT_V2
        or effect.kind != EFFECT_KIND
        or effect.intent_schema_version != INTENT_SCHEMA
    ):
        raise MediaHandsEffectExecutionError("Media Hands Effect requires its frozen v2 contract")
    row = connection.execute(
        "SELECT intent_ref,intent_digest,payload_json,schema_version "
        "FROM effect_intent_fact WHERE operation_id=?",
        (effect.operation_id,),
    ).fetchone()
    if (
        row is None
        or row[0] != effect.intent_ref
        or row[1] != effect.intent_digest
        or row[3] != INTENT_SCHEMA
    ):
        raise MediaHandsEffectExecutionError("Media Hands immutable intent fact drifted")
    try:
        intent = json.loads(str(row[2]))
    except json.JSONDecodeError as error:
        raise MediaHandsEffectExecutionError("Media Hands immutable intent fact is invalid") from error
    if not isinstance(intent, Mapping):
        raise MediaHandsEffectExecutionError("Media Hands immutable intent fact is invalid")
    job_id = effect.root_id
    fact = connection.execute(
        "SELECT payload_json FROM job_effect_fact "
        "WHERE job_id=? AND effect_operation_id=? ORDER BY sequence ASC LIMIT 1",
        (job_id, effect.operation_id),
    ).fetchone()
    if fact is None:
        raise MediaHandsEffectExecutionError("Media Hands Job fact is unavailable")
    try:
        job = json.loads(str(fact[0]))
    except json.JSONDecodeError as error:
        raise MediaHandsEffectExecutionError("Media Hands Job fact is invalid") from error
    if not isinstance(job, Mapping) or job.get("id") != job_id:
        raise MediaHandsEffectExecutionError("Media Hands Job fact identity drifted")
    media = job.get("media_hands")
    if (
        job.get("job_type") != "media_hands"
        or job.get("execution_version") != EFFECT_V2
        or not isinstance(job.get("source_id"), str)
        or not isinstance(media, Mapping)
    ):
        raise MediaHandsEffectExecutionError("Media Hands Job fact is not executable evidence")
    selection = media.get("selection")
    policy = media.get("policy")
    if (
        not isinstance(selection, Mapping)
        or set(selection) != {"ref", "revision", "mode"}
        or selection.get("mode") != "hands"
        or not isinstance(selection.get("ref"), str)
        or not isinstance(selection.get("revision"), str)
        or not isinstance(policy, Mapping)
        or not isinstance(policy.get("revision"), str)
        or policy.get("revision") != effect.rev_set.get("policy")
    ):
        raise MediaHandsEffectExecutionError("Media Hands policy or selection fact drifted")
    boundary_revision = effect.rev_set.get("boundary")
    selection_revision = str(selection["revision"])
    if (
        not isinstance(boundary_revision, str)
        or (
            boundary_revision != selection_revision
            and (
                not boundary_revision.startswith(f"{selection_revision}@")
                or boundary_revision.count("@") != 1
            )
        )
    ):
        raise MediaHandsEffectExecutionError("Media Hands selection revision drifted")
    expected = {
        "job_ref": f"facts:media-hands-admission/{job_id}/{_request_id(effect.intent_ref)}",
        "admission_ref": f"facts:media-hands-admission/{job_id}/{_request_id(effect.intent_ref)}",
        "mode": "admit",
        "attempt_index": 0,
    }
    if dict(intent) != expected:
        raise MediaHandsEffectExecutionError("Media Hands intent and Job fact drifted")
    return intent, job


def _request_id(intent_ref: str) -> str:
    prefix = "intent:media-hands-job-execution/"
    if not intent_ref.startswith(prefix):
        raise MediaHandsEffectExecutionError("Media Hands intent reference is invalid")
    request_id = intent_ref.rsplit("/", 1)[-1]
    if not request_id:
        raise MediaHandsEffectExecutionError("Media Hands intent reference is invalid")
    return request_id


def _assert_frozen_provider_identity(
    effect: Effect, handler: MediaHandsOperationHandler,
) -> str:
    """Fail closed before provider invocation if the admitted transport drifted."""

    try:
        provider_id, provider_revision = handler.provider_identity
        current = provider_revision_identity(provider_id, provider_revision)
    except Exception as error:
        raise MediaHandsEffectExecutionError("Media Hands provider identity is unavailable") from error
    if effect.rev_set.get("provider") != current:
        raise MediaHandsEffectExecutionError("Media Hands provider identity drifted")
    return current


def _validated_receipt(
    connection: sqlite3.Connection,
    effect: Effect,
    job: Mapping[str, object],
    handler: MediaHandsOperationHandler,
) -> Mapping[str, object] | None:
    row = connection.execute(
        f"SELECT job_id,receipt_ref,receipt_json FROM {RECEIPT_TABLE} WHERE operation_id=?",
        (effect.operation_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        receipt = json.loads(str(row[2]))
    except json.JSONDecodeError as error:
        raise MediaHandsEffectExecutionError("Media Hands receipt is invalid") from error
    if not isinstance(receipt, Mapping) or row[0] != job["id"] or row[1] != receipt.get("receipt_ref"):
        raise MediaHandsEffectExecutionError("Media Hands receipt drifted")
    expected = _receipt_payload_from_receipt(effect, job, receipt)
    if dict(receipt) != expected:
        raise MediaHandsEffectExecutionError("Media Hands receipt drifted")
    # Canonical output verification is a read-only proof and deliberately
    # happens in Probe as well as after Handler execution.
    try:
        output = receipt["output"]
        if not isinstance(output, Mapping):
            raise TypeError("output is not a mapping")
        handler.verify_published_outputs(job, (output,))
    except Exception as error:
        raise MediaHandsEffectExecutionError("Media Hands canonical output drifted") from error
    return receipt


def _validated_reservation(
    connection: sqlite3.Connection, effect: Effect, job: Mapping[str, object],
) -> Mapping[str, object] | None:
    row = connection.execute(
        f"SELECT job_id,reservation_ref,reservation_json FROM {RESERVATION_TABLE} "
        "WHERE operation_id=?",
        (effect.operation_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        reservation = json.loads(str(row[2]))
    except json.JSONDecodeError as error:
        raise MediaHandsEffectExecutionError("Media Hands provider reservation is invalid") from error
    expected = _reservation_payload(effect, job)
    if (
        not isinstance(reservation, Mapping)
        or row[0] != job["id"]
        or row[1] != expected["reservation_ref"]
        or dict(reservation) != expected
    ):
        raise MediaHandsEffectExecutionError("Media Hands provider reservation drifted")
    return reservation


def _receipt_payload(
    effect: Effect, job: Mapping[str, object], result: MediaOperationStepResult,
) -> dict[str, object]:
    if len(result.published_outputs) != 1 or not isinstance(result.published_outputs[0], Mapping):
        raise MediaHandsEffectExecutionError("Media Hands handler returned no canonical output")
    return {
        "operation_id": effect.operation_id,
        "job_id": str(job["id"]),
        "receipt_ref": _receipt_ref(effect.operation_id),
        "receipt_kind": RECEIPT_KIND,
        "receipt_schema_version": RECEIPT_SCHEMA,
        "intent_schema_version": INTENT_SCHEMA,
        "provider_revision_identity": effect.rev_set["provider"],
        "execution_receipt_ref": result.execution_receipt_ref,
        "output": dict(result.published_outputs[0]),
        "checkpoint": dict(result.checkpoint),
        "consumed": dict(result.resource_consumed),
        "log_refs": list(result.log_refs),
    }


def _receipt_payload_from_receipt(
    effect: Effect, job: Mapping[str, object], receipt: Mapping[str, object],
) -> dict[str, object]:
    required = {
        "output", "checkpoint", "consumed", "log_refs",
        "execution_receipt_ref", "provider_revision_identity",
    }
    if not required.issubset(receipt):
        raise MediaHandsEffectExecutionError("Media Hands receipt is incomplete")
    try:
        result = MediaOperationStepResult(
            published_outputs=(dict(receipt["output"]),),
            checkpoint=dict(receipt["checkpoint"]),
            resource_consumed=dict(receipt["consumed"]),
            log_refs=tuple(receipt["log_refs"]),
            execution_receipt_ref=str(receipt["execution_receipt_ref"]),
        )
    except (TypeError, ValueError) as error:
        raise MediaHandsEffectExecutionError("Media Hands receipt is invalid") from error
    return _receipt_payload(effect, job, result)


def _write_receipt(connection: sqlite3.Connection, receipt: Mapping[str, object]) -> None:
    encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    connection.execute(
        f"INSERT INTO {RECEIPT_TABLE}(operation_id,job_id,receipt_ref,receipt_json,recorded_at) "
        "VALUES(?,?,?,?,?)",
        (receipt["operation_id"], receipt["job_id"], receipt["receipt_ref"], encoded, _now_iso()),
    )


def _reservation_payload(effect: Effect, job: Mapping[str, object]) -> dict[str, object]:
    """Immutable proof that this operation may already have reached Provider."""

    return {
        "operation_id": effect.operation_id,
        "job_id": str(job["id"]),
        "reservation_ref": f"facts:media-hands-provider-attempt/{effect.operation_id}",
        "intent_ref": effect.intent_ref,
        "intent_digest": effect.intent_digest,
        "contract_version": EFFECT_V2,
        "intent_schema_version": INTENT_SCHEMA,
    }


def _write_reservation(connection: sqlite3.Connection, effect: Effect, job: Mapping[str, object]) -> None:
    reservation = _reservation_payload(effect, job)
    encoded = json.dumps(reservation, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    connection.execute(
        f"INSERT INTO {RESERVATION_TABLE}"
        "(operation_id,job_id,reservation_ref,reservation_json,recorded_at) VALUES(?,?,?,?,?)",
        (
            reservation["operation_id"], reservation["job_id"], reservation["reservation_ref"],
            encoded, _now_iso(),
        ),
    )


def _effect_receipt(effect: Effect) -> EffectReceipt:
    return EffectReceipt(_receipt_ref(effect.operation_id), RECEIPT_KIND, RECEIPT_SCHEMA, INTENT_SCHEMA)


def _ensure_schema(database: Path) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        initialize_job_projection_schema(connection)
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {RECEIPT_TABLE} ("
            "operation_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, receipt_ref TEXT NOT NULL UNIQUE, "
            "receipt_json TEXT NOT NULL, recorded_at TEXT NOT NULL, "
            "FOREIGN KEY(operation_id) REFERENCES effect(operation_id))"
        )
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {RESERVATION_TABLE} ("
            "operation_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, reservation_ref TEXT NOT NULL UNIQUE, "
            "reservation_json TEXT NOT NULL, recorded_at TEXT NOT NULL, "
            "FOREIGN KEY(operation_id) REFERENCES effect(operation_id))"
        )
        connection.commit()


def _receipt_ref(operation_id: str) -> str:
    return f"receipt:media-hands/{operation_id}"


def _retry_evidence_ref(operation_id: str) -> str:
    return f"facts:media-hands-effect-retry/{operation_id}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
