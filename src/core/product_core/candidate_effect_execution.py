"""Effect-v2 execution boundary for source-read Memory Candidates.

This module deliberately does not import the Job lifecycle or worker.  A Job is
only a rebuildable query projection here; the immutable Effect facts and the
candidate-domain receipt are the execution evidence.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from core.effect_log import EFFECT_V2, Effect, EffectReceipt, EffectState
from core.job_runner.job_projection import JobProjectionBuilder, initialize_job_projection_schema

from .candidate_effect_contract import (
    EFFECT_KIND,
    INTENT_SCHEMA,
    RECEIPT_KIND,
    RECEIPT_SCHEMA,
    RECEIPT_TABLE,
    candidate_evidence_revision,
)
from .candidate_memory_job_handler import CandidateMemoryJobHandler
from .source_output_memory_candidate import CreateMemoryCandidateFromSourceOutput


class CandidateEffectExecutionError(ValueError):
    """The candidate domain cannot prove a safe v2 execution outcome."""


@dataclass(frozen=True, slots=True)
class CandidateEffectExecutionHandler:
    """Execute one v2 candidate intent and persist its domain receipt.

    ``creator`` must expose an explicit ``execution_ref`` input and durable
    read-back evidence carrying the same reference.  An unbound creator is
    rejected instead of falling back to the Job worker.
    """

    database: Path | str
    creator: CreateMemoryCandidateFromSourceOutput | object
    projection_builder: JobProjectionBuilder = field(default_factory=JobProjectionBuilder)
    after_domain_write: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "database", Path(self.database))
        _ensure_schema(Path(self.database))

    def __call__(self, effect: Effect) -> EffectReceipt:
        return self.handle(effect)

    def handle(self, effect: Effect) -> EffectReceipt:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            intent, job = _read_and_validate_inputs(connection, effect)
            existing = _validated_receipt(connection, effect, job, self.creator)
            if existing is not None:
                connection.commit()
                return _effect_receipt(effect)
            _assert_execution_is_safe(self.creator, effect, job)
            connection.commit()

        result = _execute_creator(self.creator, effect, intent, job)
        if self.after_domain_write is not None:
            self.after_domain_write()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            intent, job = _read_and_validate_inputs(connection, effect)
            existing = _validated_receipt(connection, effect, job, self.creator)
            if existing is None:
                candidate_id = _candidate_id(result)
                _assert_candidate_binding(
                    self.creator, candidate_id, effect.operation_id, job,
                )
                receipt = _receipt_payload(effect, job_id=str(job["id"]), candidate_id=candidate_id)
                _write_receipt(connection, receipt)
                self.projection_builder.append_fact_in_connection(
                    connection,
                    job_id=str(job["id"]),
                    effect_operation_id=effect.operation_id,
                    payload=_output_job_fact(job, receipt),
                    recorded_at=_now_iso(),
                )
                self.projection_builder.rebuild_in_connection(
                    connection, job_id=str(job["id"]), rebuilt_at=_now_iso(),
                )
            connection.commit()
        return _effect_receipt(effect)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


@dataclass(frozen=True, slots=True)
class CandidateEffectExecutionProbe:
    """Recover only from immutable candidate evidence; never inspect Job state."""

    database: Path | str
    creator: CreateMemoryCandidateFromSourceOutput | object

    def __post_init__(self) -> None:
        object.__setattr__(self, "database", Path(self.database))
        _ensure_schema(Path(self.database))

    def __call__(self, effect: Effect) -> tuple[EffectState, str | None]:
        return self.probe(effect)

    def probe(self, effect: Effect) -> tuple[EffectState, str | None]:
        try:
            with sqlite3.connect(self.database) as connection:
                connection.row_factory = sqlite3.Row
                _intent, job = _read_and_validate_inputs(connection, effect)
                if _validated_receipt(connection, effect, job, self.creator) is not None:
                    return EffectState.SETTLED_OK, _receipt_ref(effect.operation_id)
                # Evidence is complete but no receipt exists.  A future creator
                # seam may prove an already-created candidate here and safely
                # materialize the receipt.  Without that proof, only re-plan.
                _assert_execution_is_safe(self.creator, effect, job)
                return EffectState.PLANNED, _retry_evidence_ref(effect.operation_id)
        except CandidateEffectExecutionError:
            return EffectState.UNKNOWN, "error:candidate-effect-evidence-drift"
        except (TypeError, ValueError, sqlite3.DatabaseError):
            return EffectState.SETTLED_ERR, "error:candidate-effect-invalid"


def candidate_effect_handler(
    database: Path | str, creator: CreateMemoryCandidateFromSourceOutput | object,
) -> CandidateEffectExecutionHandler:
    return CandidateEffectExecutionHandler(database, creator)


def candidate_effect_probe(
    database: Path | str, creator: CreateMemoryCandidateFromSourceOutput | object,
) -> CandidateEffectExecutionProbe:
    return CandidateEffectExecutionProbe(database, creator)


def _read_and_validate_inputs(
    connection: sqlite3.Connection, effect: Effect,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    if effect.contract_version != EFFECT_V2 or effect.kind != EFFECT_KIND:
        raise CandidateEffectExecutionError("candidate Effect requires its frozen v2 contract")
    row = connection.execute(
        "SELECT intent_ref,intent_digest,payload_json,schema_version FROM effect_intent_fact WHERE operation_id=?",
        (effect.operation_id,),
    ).fetchone()
    if row is None or row[0] != effect.intent_ref or row[1] != effect.intent_digest or row[3] != INTENT_SCHEMA:
        raise CandidateEffectExecutionError("candidate immutable intent fact drifted")
    try:
        intent = json.loads(str(row[2]))
    except json.JSONDecodeError as error:
        raise CandidateEffectExecutionError("candidate immutable intent fact is invalid") from error
    if not isinstance(intent, Mapping):
        raise CandidateEffectExecutionError("candidate immutable intent fact is invalid")
    job_id = effect.root_id
    fact = connection.execute(
        "SELECT payload_json FROM job_effect_fact "
        "WHERE job_id=? AND effect_operation_id=? ORDER BY sequence ASC LIMIT 1",
        (job_id, effect.operation_id),
    ).fetchone()
    if fact is None:
        raise CandidateEffectExecutionError("candidate Job fact is unavailable")
    try:
        job = json.loads(str(fact[0]))
    except json.JSONDecodeError as error:
        raise CandidateEffectExecutionError("candidate Job fact is invalid") from error
    if not isinstance(job, Mapping) or job.get("id") != job_id:
        raise CandidateEffectExecutionError("candidate Job fact identity drifted")
    required = (
        "parent_job_id", "source_id", "project_id", "evidence_id", "created_at",
    )
    if (job.get("job_type") != CandidateMemoryJobHandler.job_type
            or job.get("execution_version") != EFFECT_V2
            or job.get("evidence_kind") != "source_content_read"
            or any(not isinstance(job.get(key), str) or not job.get(key) for key in required)):
        raise CandidateEffectExecutionError("candidate Job fact is not executable evidence")
    admission_ref = f"facts:candidate-job-admission/{job_id}/{job['evidence_id']}"
    expected_intent = {
        "job_ref": admission_ref,
        "admission_ref": admission_ref,
        "parent_job_ref": f"facts:candidate-job/parents/{job['parent_job_id']}",
        "source_ref": f"crp://candidate-job/sources/{job['source_id']}",
        "evidence_ref": (
            f"crp://candidate-job/source-content-reads/{job['evidence_id']}"
        ),
        "mode": "admit",
        "attempt_index": 0,
    }
    if dict(intent) != expected_intent:
        raise CandidateEffectExecutionError("candidate intent and Job fact drifted")
    return intent, job


def _execute_creator(
    creator: object, effect: Effect, intent: Mapping[str, object], job: Mapping[str, object],
) -> object:
    _require_creator_seam(creator)
    execute = getattr(creator, "execute_from_content_read")
    return execute(
        source_id=str(job["source_id"]), project_id=str(job["project_id"]),
        content_read_id=str(job["evidence_id"]), target_layer="atom", candidate_type="other",
        created_at=str(job["created_at"]), execution_ref=_execution_ref(effect.operation_id),
    )


def _require_creator_seam(creator: object) -> None:
    execute = getattr(creator, "execute_from_content_read", None)
    if not callable(execute):
        raise CandidateEffectExecutionError("candidate creator is unavailable")
    try:
        parameters = inspect.signature(execute).parameters
    except (TypeError, ValueError) as error:
        raise CandidateEffectExecutionError("candidate creator execution seam is not inspectable") from error
    if "execution_ref" not in parameters:
        raise CandidateEffectExecutionError("candidate creator lacks required execution_ref seam")


def _candidate_id(result: object) -> str:
    candidate_id = getattr(result, "candidate_id", None)
    if not isinstance(candidate_id, str) or not candidate_id:
        raise CandidateEffectExecutionError("candidate creator returned no candidate identity")
    return candidate_id


def _assert_candidate_binding(
    creator: object, candidate_id: str, operation_id: str, job: Mapping[str, object],
) -> None:
    """Read back durable candidate-domain evidence, never a Job projection."""
    read_candidate = getattr(creator, "read_candidate", None)
    execution_ref = _execution_ref(operation_id)
    if callable(read_candidate):
        candidate = read_candidate(candidate_id)
        if not isinstance(candidate, Mapping) or candidate.get("id") != candidate_id:
            raise CandidateEffectExecutionError("candidate read-back identity drifted")
        if candidate.get("execution_ref") != execution_ref:
            raise CandidateEffectExecutionError("candidate execution binding drifted")
        return
    # The production creator persists its operation binding on the completed
    # source-read record.  Keeping this fallback here avoids a hidden Job
    # lifecycle dependency while the candidate repository gains a public reader.
    objects = getattr(creator, "_object_store", None)
    read = getattr(objects, "read", None)
    if not callable(read):
        raise CandidateEffectExecutionError("candidate object store reader is unavailable")
    evidence_id = str(job["evidence_id"])
    source_id = str(job["source_id"])
    record = read("source_content_reads", evidence_id)
    candidate = read("memory_candidates", candidate_id)
    source = read("sources", source_id)
    event = read(
        "activity_events", f"event-memory-candidate-created-{source_id}-{evidence_id}",
    )
    provenance = candidate.get("provenance") if isinstance(candidate, Mapping) else None
    review = candidate.get("review") if isinstance(candidate, Mapping) else None
    source_metadata = source.get("metadata") if isinstance(source, Mapping) else None
    source_read = source_metadata.get("content_read") if isinstance(source_metadata, Mapping) else None
    event_details = event.get("details") if isinstance(event, Mapping) else None
    if (
        not isinstance(candidate, Mapping)
        or candidate.get("id") != candidate_id
        or candidate.get("project_id") != job["project_id"]
        or candidate.get("status") != "pending_review"
        or not isinstance(provenance, Mapping)
        or provenance.get("source_content_read_id") != evidence_id
        or not isinstance(review, Mapping)
        or review.get("requires_user_confirmation") is not True
        or review.get("auto_promote_allowed") is not False
        or not isinstance(record, Mapping)
        or record.get("memory_candidate_id") != candidate_id
        or record.get("memory_candidate_execution_ref") != execution_ref
        or not isinstance(source_read, Mapping)
        or source_read.get("memory_candidate_id") != candidate_id
        or source_read.get("memory_candidate_execution_ref") != execution_ref
        or not isinstance(event_details, Mapping)
        or event_details.get("candidate_id") != candidate_id
        or event_details.get("execution_ref") != execution_ref
    ):
        raise CandidateEffectExecutionError("candidate execution binding drifted")


def _assert_execution_is_safe(
    creator: object, effect: Effect, job: Mapping[str, object],
) -> None:
    """Prove a first execution or an operation-owned idempotent repair."""

    _require_creator_seam(creator)
    objects = getattr(creator, "_object_store", None)
    read = getattr(objects, "read", None)
    if not callable(read):
        # Test doubles prove ownership through their explicit candidate reader.
        if callable(getattr(creator, "read_candidate", None)):
            return
        raise CandidateEffectExecutionError("candidate evidence reader is unavailable")
    source_id = str(job["source_id"])
    evidence_id = str(job["evidence_id"])
    source = read("sources", source_id)
    evidence = read("source_content_reads", evidence_id)
    if (
        not isinstance(source, Mapping)
        or source.get("id") != source_id
        or (
            source.get("project_id") is not None
            and source.get("project_id") != job["project_id"]
        )
        or not isinstance(evidence, Mapping)
        or evidence.get("id") != evidence_id
        or evidence.get("source_id") != source_id
        or evidence.get("status") != "completed"
    ):
        raise CandidateEffectExecutionError("candidate admission evidence is unavailable")
    try:
        current_context = candidate_evidence_revision(
            "candidate-context",
            source_id=source_id,
            content_read_id=evidence_id,
            text_sha256=str(evidence.get("text_sha256", "")),
        )
    except ValueError as error:
        raise CandidateEffectExecutionError(
            "candidate admission evidence identity is invalid"
        ) from error
    if effect.rev_set.get("context_manifest") != current_context:
        raise CandidateEffectExecutionError("candidate admission evidence revision drifted")
    execution_ref = evidence.get("memory_candidate_execution_ref")
    candidate_id = evidence.get("memory_candidate_id")
    if execution_ref is not None or candidate_id is not None:
        if execution_ref != _execution_ref(effect.operation_id) or not isinstance(candidate_id, str):
            raise CandidateEffectExecutionError("candidate partial evidence belongs to another execution")
        # A same-operation marker makes an idempotent repair safe even when
        # later Source/event markers were not written before a crash.
        candidate = read("memory_candidates", candidate_id)
        if not isinstance(candidate, Mapping) or candidate.get("id") != candidate_id:
            raise CandidateEffectExecutionError("candidate partial evidence is ambiguous")
        return


def _initialize_receipt_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {RECEIPT_TABLE} ("
        "operation_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, candidate_id TEXT NOT NULL, "
        "receipt_ref TEXT NOT NULL UNIQUE, receipt_json TEXT NOT NULL, recorded_at TEXT NOT NULL, "
        "FOREIGN KEY(operation_id) REFERENCES effect(operation_id))"
    )


def _ensure_schema(database: Path) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        initialize_job_projection_schema(connection)
        _initialize_receipt_schema(connection)
        connection.commit()


def _validated_receipt(
    connection: sqlite3.Connection, effect: Effect, job: Mapping[str, object], creator: object,
) -> Mapping[str, object] | None:
    row = connection.execute(
        f"SELECT job_id,candidate_id,receipt_ref,receipt_json FROM {RECEIPT_TABLE} "
        "WHERE operation_id=?",
        (effect.operation_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        receipt = json.loads(str(row[3]))
    except json.JSONDecodeError as error:
        raise CandidateEffectExecutionError("candidate receipt is invalid") from error
    if not isinstance(receipt, Mapping):
        raise CandidateEffectExecutionError("candidate receipt is invalid")
    expected = _receipt_payload(
        effect,
        job_id=str(job["id"]),
        candidate_id=str(receipt.get("candidate_id", "")),
    )
    if (
        dict(receipt) != expected
        or tuple(row[:3]) != (
            str(job["id"]), str(receipt.get("candidate_id", "")), expected["receipt_ref"],
        )
    ):
        raise CandidateEffectExecutionError("candidate receipt drifted")
    _assert_candidate_binding(
        creator, str(receipt["candidate_id"]), effect.operation_id, job,
    )
    return receipt


def _write_receipt(connection: sqlite3.Connection, receipt: Mapping[str, object]) -> None:
    encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    connection.execute(
        f"INSERT INTO {RECEIPT_TABLE}"
        "(operation_id,job_id,candidate_id,receipt_ref,receipt_json,recorded_at) "
        "VALUES(?,?,?,?,?,?)",
        (
            receipt["operation_id"], receipt["job_id"], receipt["candidate_id"],
            receipt["receipt_ref"], encoded, _now_iso(),
        ),
    )


def _output_job_fact(job: Mapping[str, object], receipt: Mapping[str, object]) -> dict[str, object]:
    output = dict(job)
    output["staged_outputs"] = [{
        "kind": "memory_candidate", "object_id": receipt["candidate_id"],
        "ref": _receipt_ref(str(receipt["operation_id"])), "status": "pending_review",
        "publication_state": "candidate_created_not_published",
    }]
    output["published_outputs"] = []
    return output


def _receipt_payload(effect: Effect, *, job_id: str, candidate_id: str) -> dict[str, object]:
    if not candidate_id:
        raise CandidateEffectExecutionError("candidate receipt requires candidate identity")
    return {
        "operation_id": effect.operation_id, "job_id": job_id, "candidate_id": candidate_id,
        "execution_ref": _execution_ref(effect.operation_id),
        "receipt_ref": _receipt_ref(effect.operation_id),
        "receipt_kind": RECEIPT_KIND, "receipt_schema_version": RECEIPT_SCHEMA,
        "intent_schema_version": INTENT_SCHEMA,
    }


def _effect_receipt(effect: Effect) -> EffectReceipt:
    return EffectReceipt(_receipt_ref(effect.operation_id), RECEIPT_KIND, RECEIPT_SCHEMA, INTENT_SCHEMA)


def _receipt_ref(operation_id: str) -> str:
    return f"receipt:memory-candidate/{operation_id}"


def _execution_ref(operation_id: str) -> str:
    return f"facts:effect/{operation_id}"


def _retry_evidence_ref(operation_id: str) -> str:
    return f"facts:candidate-effect-retry/{operation_id}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
