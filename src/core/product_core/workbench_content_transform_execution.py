"""Core Effect-v2 Handler and Probe for local Workbench content transforms."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from core.effect_log import EFFECT_V2, Effect, EffectHandlerAbandoned, EffectReceipt, EffectState
from core.effect_log.runtime import EffectExecutionCancelled
from core.job_runner.job_projection import JobProjectionBuilder, initialize_job_projection_schema

from .workbench_content_transform_admission import exact_transform_items
from .workbench_content_transform_effect_contract import (
    EFFECT_KIND,
    INTENT_SCHEMA,
    RECEIPT_KIND,
    RECEIPT_SCHEMA,
RECEIPT_TABLE,
)


FAILURE_TABLE = "workbench_content_transform_failure"


class WorkbenchContentTransformExecutionError(ValueError):
    pass


TransformExecutor = Callable[[Effect, Mapping[str, object], Callable[[], None]], Mapping[str, object]]
TransformVerifier = Callable[[Effect, Mapping[str, object], Mapping[str, object]], None]


@dataclass(frozen=True, slots=True)
class WorkbenchContentTransformEffectHandler:
    database: Path | str
    execute_item: TransformExecutor
    verify_output: TransformVerifier
    checkpoint_factory: Callable[[Effect], Callable[[], None]]
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
            job = _read_job(connection, effect)
            receipt = _validated_receipt(connection, effect, job, self.verify_output)
            if receipt is not None:
                connection.commit()
                return _effect_receipt(effect)
            connection.commit()

        checkpoint = self.checkpoint_factory(effect)
        if not callable(checkpoint):
            raise WorkbenchContentTransformExecutionError("core checkpoint is unavailable")
        outputs: list[dict[str, object]] = []
        try:
            for item in exact_transform_items(job.get("transform_items")):
                checkpoint()
                output = _exact_output(self.execute_item(effect, item, checkpoint), item)
                self.verify_output(effect, item, output)
                outputs.append(output)
            checkpoint()
        except EffectExecutionCancelled as error:
            raise EffectHandlerAbandoned(
                f"workbench_content_transform.user_cancelled:{effect.operation_id}",
            ) from error
        except EffectHandlerAbandoned:
            raise
        except Exception:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                failed_job = _read_job(connection, effect)
                failure = _validated_failure(connection, effect, failed_job)
                if failure is None:
                    failure = _failure_payload(effect, failed_job)
                    connection.execute(
                        f"INSERT INTO {FAILURE_TABLE}(operation_id,job_id,failure_ref,failure_json,recorded_at) "
                        "VALUES(?,?,?,?,?)",
                        (effect.operation_id, failed_job["id"], failure["failure_ref"], _canonical(failure), effect.recorded_at),
                    )
                    self.projection_builder.append_fact_in_connection(
                        connection,
                        job_id=str(failed_job["id"]),
                        effect_operation_id=effect.operation_id,
                        payload=_failure_job_fact(failed_job, failure),
                        recorded_at=_now_iso(),
                    )
                    self.projection_builder.rebuild_in_connection(
                        connection, job_id=str(failed_job["id"]), rebuilt_at=_now_iso(),
                    )
                connection.commit()
            raise
        if self.after_domain_write is not None:
            self.after_domain_write()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = _read_job(connection, effect)
            receipt = _validated_receipt(connection, effect, job, self.verify_output)
            if receipt is None:
                receipt = _receipt_payload(effect, job, outputs)
                connection.execute(
                    f"INSERT INTO {RECEIPT_TABLE}(operation_id,job_id,receipt_ref,receipt_json,recorded_at) "
                    "VALUES(?,?,?,?,?)",
                    (effect.operation_id, job["id"], receipt["receipt_ref"], _canonical(receipt), effect.recorded_at),
                )
                self.projection_builder.append_fact_in_connection(
                    connection,
                    job_id=str(job["id"]),
                    effect_operation_id=effect.operation_id,
                    payload=_output_job_fact(job, outputs),
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
class WorkbenchContentTransformEffectProbe:
    database: Path | str
    verify_output: TransformVerifier

    def __post_init__(self) -> None:
        object.__setattr__(self, "database", Path(self.database))
        _ensure_schema(Path(self.database))

    def __call__(self, effect: Effect) -> tuple[EffectState, str | None]:
        return self.probe(effect)

    def probe(self, effect: Effect) -> tuple[EffectState, str | None]:
        try:
            with sqlite3.connect(self.database) as connection:
                connection.row_factory = sqlite3.Row
                job = _read_job(connection, effect)
                receipt = _validated_receipt(connection, effect, job, self.verify_output)
                if receipt is not None:
                    return EffectState.SETTLED_OK, str(receipt["receipt_ref"])
                failure = _validated_failure(connection, effect, job)
                if failure is not None:
                    return EffectState.SETTLED_ERR, str(failure["failure_ref"])
                return EffectState.PLANNED, f"facts:workbench-content-transform/not-completed/{effect.operation_id}"
        except WorkbenchContentTransformExecutionError:
            return EffectState.UNKNOWN, "error:workbench-content-transform-evidence-drift"
        except (TypeError, ValueError, sqlite3.DatabaseError):
            return EffectState.SETTLED_ERR, "error:workbench-content-transform-invalid"


def read_workbench_transform_receipt(database: Path | str, job_id: str) -> dict[str, object] | None:
    path = Path(database)
    if not path.exists():
        return None
    with sqlite3.connect(f"{path.resolve(strict=False).as_uri()}?mode=ro", uri=True) as connection:
        row = connection.execute(
            f"SELECT receipt_json FROM {RECEIPT_TABLE} WHERE job_id=?", (job_id,),
        ).fetchone()
    if row is None:
        return None
    value = json.loads(str(row[0]))
    if not isinstance(value, dict):
        raise WorkbenchContentTransformExecutionError("stored transform receipt is invalid")
    return value


def _read_job(connection: sqlite3.Connection, effect: Effect) -> dict[str, object]:
    if (
        effect.contract_version != EFFECT_V2
        or effect.kind != EFFECT_KIND
        or effect.intent_schema_version != INTENT_SCHEMA
        or effect.expected_receipt_kind != RECEIPT_KIND
        or effect.expected_receipt_schema_version != RECEIPT_SCHEMA
    ):
        raise WorkbenchContentTransformExecutionError("workbench transform Effect contract drifted")
    intent_row = connection.execute(
        "SELECT intent_ref,intent_digest,payload_json,schema_version FROM effect_intent_fact WHERE operation_id=?",
        (effect.operation_id,),
    ).fetchone()
    fact_row = connection.execute(
        "SELECT payload_json FROM job_effect_fact WHERE job_id=? AND effect_operation_id=? "
        "ORDER BY sequence ASC LIMIT 1",
        (effect.root_id, effect.operation_id),
    ).fetchone()
    if intent_row is None or fact_row is None or tuple(intent_row[:2]) != (effect.intent_ref, effect.intent_digest):
        raise WorkbenchContentTransformExecutionError("immutable transform inputs are unavailable")
    if intent_row[3] != INTENT_SCHEMA:
        raise WorkbenchContentTransformExecutionError("transform intent schema drifted")
    try:
        intent = json.loads(str(intent_row[2]))
        job = json.loads(str(fact_row[0]))
    except json.JSONDecodeError as error:
        raise WorkbenchContentTransformExecutionError("transform input JSON is invalid") from error
    if not isinstance(job, dict) or job.get("id") != effect.root_id:
        raise WorkbenchContentTransformExecutionError("transform Job identity drifted")
    if job.get("job_type") != "workbench_content_transform" or job.get("execution_version") != EFFECT_V2:
        raise WorkbenchContentTransformExecutionError("transform Job is not executable evidence")
    exact_transform_items(job.get("transform_items"))
    admission_ref = f"facts:workbench-content-transform/{effect.root_id}/attempt-0"
    if intent != {"job_ref": admission_ref, "admission_ref": admission_ref, "mode": "admit", "attempt_index": 0}:
        raise WorkbenchContentTransformExecutionError("transform intent and Job fact drifted")
    return job


def _validated_receipt(
    connection: sqlite3.Connection,
    effect: Effect,
    job: Mapping[str, object],
    verify_output: TransformVerifier,
) -> dict[str, object] | None:
    row = connection.execute(
        f"SELECT job_id,receipt_ref,receipt_json FROM {RECEIPT_TABLE} WHERE operation_id=?",
        (effect.operation_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        receipt = json.loads(str(row[2]))
    except json.JSONDecodeError as error:
        raise WorkbenchContentTransformExecutionError("transform receipt is invalid") from error
    if not isinstance(receipt, dict) or row[0] != job["id"] or row[1] != receipt.get("receipt_ref"):
        raise WorkbenchContentTransformExecutionError("transform receipt identity drifted")
    items = exact_transform_items(job.get("transform_items"))
    outputs = receipt.get("outputs")
    if not isinstance(outputs, list) or len(outputs) != len(items):
        raise WorkbenchContentTransformExecutionError("transform receipt outputs drifted")
    normalized = [_exact_output(output, item) for item, output in zip(items, outputs)]
    expected = _receipt_payload(effect, job, normalized)
    if receipt != expected:
        raise WorkbenchContentTransformExecutionError("transform receipt payload drifted")
    for item, output in zip(items, normalized):
        verify_output(effect, item, output)
    return receipt


def _validated_failure(
    connection: sqlite3.Connection,
    effect: Effect,
    job: Mapping[str, object],
) -> dict[str, object] | None:
    row = connection.execute(
        f"SELECT job_id,failure_ref,failure_json FROM {FAILURE_TABLE} WHERE operation_id=?",
        (effect.operation_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        failure = json.loads(str(row[2]))
    except json.JSONDecodeError as error:
        raise WorkbenchContentTransformExecutionError("transform failure evidence is invalid") from error
    expected = _failure_payload(effect, job)
    if not isinstance(failure, dict) or row[0] != job["id"] or row[1] != failure.get("failure_ref") or failure != expected:
        raise WorkbenchContentTransformExecutionError("transform failure evidence drifted")
    return failure


def _exact_output(value: object, item: Mapping[str, object]) -> dict[str, object]:
    fields = {
        "source_id", "pipeline", "document_id", "document_revision", "markdown_uri",
        "summary_ref", "candidate_id", "candidate_status", "execution_ref",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise WorkbenchContentTransformExecutionError("transform output fields are not exact")
    output = dict(value)
    if output.get("source_id") != item.get("source_id") or output.get("pipeline") != item.get("pipeline"):
        raise WorkbenchContentTransformExecutionError("transform output source drifted")
    for output_field in fields - {"document_revision"}:
        if not isinstance(output.get(output_field), str) or not output[output_field]:
            raise WorkbenchContentTransformExecutionError(f"transform output {output_field} is invalid")
    if output.get("document_revision") != 1 or output.get("candidate_status") != "pending_review":
        raise WorkbenchContentTransformExecutionError("transform output authority state is invalid")
    return output


def _receipt_payload(effect: Effect, job: Mapping[str, object], outputs: list[dict[str, object]]) -> dict[str, object]:
    return {
        "operation_id": effect.operation_id,
        "job_id": job["id"],
        "receipt_ref": _receipt_ref(effect.operation_id),
        "receipt_kind": RECEIPT_KIND,
        "receipt_schema_version": RECEIPT_SCHEMA,
        "intent_schema_version": INTENT_SCHEMA,
        "outputs": outputs,
    }


def _output_job_fact(job: Mapping[str, object], outputs: list[dict[str, object]]) -> dict[str, object]:
    updated = dict(job)
    updated["published_outputs"] = [
        {
            "kind": "document",
            "object_id": output["document_id"],
            "uri": output["markdown_uri"],
            "status": "published",
        }
        for output in outputs
    ]
    updated["transform_outputs"] = outputs
    updated["updated_at"] = _now_iso()
    return updated


def _failure_payload(effect: Effect, job: Mapping[str, object]) -> dict[str, object]:
    pipelines = {str(item["pipeline"]) for item in exact_transform_items(job.get("transform_items"))}
    code = "local_video_transform_failed" if "local_video" in pipelines else "document_transform_failed"
    message = (
        "视频转化未完成，请检查本地音视频与转写设置后重新建立任务。"
        if "local_video" in pipelines
        else "文档转化未完成，请检查本地文档解析设置后重新建立任务。"
    )
    return {
        "operation_id": effect.operation_id,
        "job_id": job["id"],
        "failure_ref": f"error:workbench-content-transform/{effect.operation_id}/{code}",
        "code": code,
        "message": message,
    }


def _failure_job_fact(job: Mapping[str, object], failure: Mapping[str, object]) -> dict[str, object]:
    updated = dict(job)
    updated["error"] = {"code": failure["code"], "message": failure["message"]}
    updated["updated_at"] = _now_iso()
    return updated


def _ensure_schema(database: Path) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        initialize_job_projection_schema(connection)
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {RECEIPT_TABLE}("
            "operation_id TEXT PRIMARY KEY,job_id TEXT NOT NULL UNIQUE,receipt_ref TEXT NOT NULL UNIQUE,"
            "receipt_json TEXT NOT NULL,recorded_at INTEGER NOT NULL,"
            "FOREIGN KEY(operation_id) REFERENCES effect(operation_id))"
        )
        connection.execute(
            f"CREATE TRIGGER IF NOT EXISTS {RECEIPT_TABLE}_deny_update BEFORE UPDATE ON {RECEIPT_TABLE} "
            "BEGIN SELECT RAISE(ABORT,'workbench transform receipt is immutable'); END"
        )
        connection.execute(
            f"CREATE TRIGGER IF NOT EXISTS {RECEIPT_TABLE}_deny_delete BEFORE DELETE ON {RECEIPT_TABLE} "
            "BEGIN SELECT RAISE(ABORT,'workbench transform receipt is immutable'); END"
        )
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {FAILURE_TABLE}("
            "operation_id TEXT PRIMARY KEY,job_id TEXT NOT NULL UNIQUE,failure_ref TEXT NOT NULL UNIQUE,"
            "failure_json TEXT NOT NULL,recorded_at INTEGER NOT NULL,"
            "FOREIGN KEY(operation_id) REFERENCES effect(operation_id))"
        )
        connection.execute(
            f"CREATE TRIGGER IF NOT EXISTS {FAILURE_TABLE}_deny_update BEFORE UPDATE ON {FAILURE_TABLE} "
            "BEGIN SELECT RAISE(ABORT,'workbench transform failure is immutable'); END"
        )
        connection.execute(
            f"CREATE TRIGGER IF NOT EXISTS {FAILURE_TABLE}_deny_delete BEFORE DELETE ON {FAILURE_TABLE} "
            "BEGIN SELECT RAISE(ABORT,'workbench transform failure is immutable'); END"
        )


def _receipt_ref(operation_id: str) -> str:
    return f"receipt:workbench-content-transform/{operation_id}"


def _effect_receipt(effect: Effect) -> EffectReceipt:
    return EffectReceipt(_receipt_ref(effect.operation_id), RECEIPT_KIND, RECEIPT_SCHEMA, INTENT_SCHEMA)


def _canonical(value: Mapping[str, object]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
