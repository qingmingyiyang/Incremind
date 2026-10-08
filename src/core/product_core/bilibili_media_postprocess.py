"""Receipt-bound Effect-v2 post-processing for completed Bilibili Documents."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

from core.effect_log import (
    EFFECT_V2,
    NOT_APPLICABLE,
    V2_REVISION_KEYS,
    Effect,
    EffectClass,
    EffectIntent,
    EffectReceipt,
    EffectState,
    GateDecision,
    GateDecisionFact,
)
from core.job_runner import JobAdmissionAuthorization, JobAdmissionCommandKind
from core.job_runner.job_projection import JobProjectionBuilder, initialize_job_projection_schema


EFFECT_KIND = "bilibili_media_postprocess"
INTENT_SCHEMA = "bilibili-media-postprocess-intent-v1"
RECEIPT_KIND = "bilibili-media-postprocess.receipt"
RECEIPT_SCHEMA = "bilibili-media-postprocess-receipt-v1"
RECEIPT_TABLE = "bilibili_media_postprocess_receipt"
FAILURE_TABLE = "bilibili_media_postprocess_failure"
_POLICY = "bilibili-receipt-local-postprocess-v2"

_INPUT_FIELDS = {
    "parent_job_id", "parent_operation_id", "parent_receipt_ref", "source_id",
    "project_id", "manifest_ref", "manifest_revision", "document_id",
    "document_revision", "document_uri",
}
_OUTPUT_FIELDS = {
    "transcript_output_id", "content_transcript_output_id", "summary_output_id", "candidate_id",
    "candidate_status", "execution_ref",
}


@dataclass(frozen=True, slots=True)
class BilibiliPostprocessAdmission:
    authorization: JobAdmissionAuthorization
    intent: EffectIntent


class BilibiliPostprocessAdmissionFactory:
    def __init__(self, *, admitted_at: int) -> None:
        self._admitted_at = admitted_at

    def build(self, *, job_payload: Mapping[str, object]) -> BilibiliPostprocessAdmission:
        job_id = _required(job_payload, "id")
        if (
            job_payload.get("job_type") != EFFECT_KIND
            or job_payload.get("execution_version") != EFFECT_V2
            or job_payload.get("attempt") != 0
        ):
            raise ValueError("Bilibili postprocess requires Effect-v2 attempt zero")
        item = exact_input(job_payload.get("postprocess_input"))
        revisions = {key: NOT_APPLICABLE for key in V2_REVISION_KEYS}
        revisions.update({
            "policy": _POLICY,
            "boundary": "verified-media-hands-receipt-v1",
            "capability": "local-bilibili-summary-candidate-v1",
            "context_manifest": str(item["manifest_revision"]),
            "provider": "builtin-local-extractive-summary-ad-filter-v2",
            "bundle": "bilibili-media-postprocess-bundle-v2",
            "handler": "bilibili-media-postprocess-handler-v2",
            "budget": "one-document-local-postprocess-v1",
            "workflow": "bilibili-media-postprocess-workflow-v2",
        })
        admission_ref = f"facts:bilibili-media-postprocess/{job_id}/attempt-0"
        gate_id = f"gate:bilibili-media-postprocess/{job_id}/attempt-0"
        intent_ref = f"intent:bilibili-media-postprocess/{job_id}/attempt-0"
        gate = GateDecisionFact(
            decision=GateDecision.ALLOW,
            rule_ref="rule:verified-bilibili-receipt-local-postprocess-v2",
            scope_ref=(
                f"scope:bilibili-media-postprocess/{item['project_id']}/{job_id}"
            ),
            budget_after={
                "parent_receipt_ref": item["parent_receipt_ref"],
                "document_ref": item["document_uri"],
                "remote_processing_count": 0,
                "memory_auto_publish_count": 0,
            },
            secret_scope="scope:bilibili-media-postprocess-secret/not-applicable",
            policy_revision=_POLICY,
        )
        authorization = JobAdmissionAuthorization(
            job_id=job_id,
            admission_ref=admission_ref,
            command_kind=JobAdmissionCommandKind.ADMIT,
            gate_decision_id=gate_id,
            gate_fact=gate,
            revision_set=MappingProxyType(revisions),
            intent_refs={EFFECT_KIND: intent_ref},
            admitted_at=self._admitted_at,
        )
        intent = EffectIntent(
            session_id=f"bilibili-media-postprocess:{item['project_id']}",
            root_id=job_id,
            step_key="postprocess",
            kind=EFFECT_KIND,
            effect_class=EffectClass.QUERYABLE,
            intent_ref=intent_ref,
            gate_decision_id=gate_id,
            rev_set=revisions,
            payload={
                "job_ref": admission_ref,
                "admission_ref": admission_ref,
                "mode": "admit",
                "attempt_index": 0,
            },
            contract_version=EFFECT_V2,
            intent_schema_version=INTENT_SCHEMA,
            expected_receipt_kind=RECEIPT_KIND,
            expected_receipt_schema_version=RECEIPT_SCHEMA,
        )
        authorization.validate_for_intent(intent)
        return BilibiliPostprocessAdmission(authorization, intent)


ExecutePostprocess = Callable[[Effect, Mapping[str, object], Callable[[], None]], Mapping[str, object]]
VerifyPostprocess = Callable[[Effect, Mapping[str, object], Mapping[str, object]], None]


@dataclass(frozen=True, slots=True)
class BilibiliPostprocessEffectHandler:
    database: Path | str
    execute: ExecutePostprocess
    verify: VerifyPostprocess
    checkpoint_factory: Callable[[Effect], Callable[[], None]]
    projection: JobProjectionBuilder = field(default_factory=JobProjectionBuilder)

    def __post_init__(self) -> None:
        object.__setattr__(self, "database", Path(self.database))
        _ensure_schema(Path(self.database))

    def __call__(self, effect: Effect) -> EffectReceipt:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = _read_job(connection, effect)
            if _validated_receipt(connection, effect, job, self.verify) is not None:
                connection.commit()
                return _effect_receipt(effect)
            connection.commit()
        checkpoint = self.checkpoint_factory(effect)
        try:
            checkpoint()
            output = exact_output(self.execute(effect, exact_input(job["postprocess_input"]), checkpoint))
            self.verify(effect, exact_input(job["postprocess_input"]), output)
            checkpoint()
        except Exception:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = _read_job(connection, effect)
                if _read_failure(connection, effect, current) is None:
                    failure = _failure(effect, current)
                    connection.execute(
                        f"INSERT INTO {FAILURE_TABLE}(operation_id,job_id,failure_ref,failure_json,recorded_at) VALUES(?,?,?,?,?)",
                        (effect.operation_id, current["id"], failure["failure_ref"], _json(failure), _now()),
                    )
                    self.projection.append_fact_in_connection(
                        connection, job_id=str(current["id"]), effect_operation_id=effect.operation_id,
                        payload={**current, "error": {"code": "postprocess_failed", "retryable": True}},
                        recorded_at=_now(),
                    )
                    self.projection.rebuild_in_connection(
                        connection, job_id=str(current["id"]), rebuilt_at=_now(),
                    )
                connection.commit()
            raise
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = _read_job(connection, effect)
            if _validated_receipt(connection, effect, current, self.verify) is None:
                receipt = _receipt(effect, current, output)
                connection.execute(
                    f"INSERT INTO {RECEIPT_TABLE}(operation_id,job_id,receipt_ref,receipt_json,recorded_at) VALUES(?,?,?,?,?)",
                    (effect.operation_id, current["id"], receipt["receipt_ref"], _json(receipt), _now()),
                )
                self.projection.append_fact_in_connection(
                    connection, job_id=str(current["id"]), effect_operation_id=effect.operation_id,
                    payload={
                        **current,
                        "published_outputs": [{
                            "kind": "memory_candidate",
                            "object_id": output["candidate_id"],
                            "uri": f"crp://memory-candidates/{output['candidate_id']}",
                            "status": "pending_review",
                        }],
                    },
                    recorded_at=_now(),
                )
                self.projection.rebuild_in_connection(
                    connection, job_id=str(current["id"]), rebuilt_at=_now(),
                )
            connection.commit()
        return _effect_receipt(effect)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


@dataclass(frozen=True, slots=True)
class BilibiliPostprocessEffectProbe:
    database: Path | str
    verify: VerifyPostprocess

    def __post_init__(self) -> None:
        object.__setattr__(self, "database", Path(self.database))
        _ensure_schema(Path(self.database))

    def __call__(self, effect: Effect) -> tuple[EffectState, str | None]:
        try:
            with sqlite3.connect(self.database) as connection:
                connection.row_factory = sqlite3.Row
                job = _read_job(connection, effect)
                receipt = _validated_receipt(connection, effect, job, self.verify)
                if receipt is not None:
                    return EffectState.SETTLED_OK, str(receipt["receipt_ref"])
                failure = _read_failure(connection, effect, job)
                if failure is not None:
                    return EffectState.SETTLED_ERR, str(failure["failure_ref"])
                return EffectState.PLANNED, f"facts:bilibili-media-postprocess/pending/{effect.operation_id}"
        except (TypeError, ValueError, sqlite3.DatabaseError):
            return EffectState.UNKNOWN, "error:bilibili-media-postprocess-evidence-drift"


def exact_input(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _INPUT_FIELDS:
        raise ValueError("Bilibili postprocess input fields are not exact")
    result = dict(value)
    for key in _INPUT_FIELDS - {"document_revision"}:
        _required(result, key)
    if result.get("document_revision") != 1:
        raise ValueError("Bilibili postprocess requires Document revision one")
    return result


def exact_output(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _OUTPUT_FIELDS:
        raise ValueError("Bilibili postprocess output fields are not exact")
    result = dict(value)
    for key in _OUTPUT_FIELDS:
        _required(result, key)
    if result["candidate_status"] != "pending_review":
        raise ValueError("Bilibili postprocess cannot publish memory")
    return result


def _read_job(connection: sqlite3.Connection, effect: Effect) -> dict[str, object]:
    if (
        effect.kind != EFFECT_KIND
        or effect.contract_version != EFFECT_V2
        or effect.intent_schema_version != INTENT_SCHEMA
    ):
        raise ValueError("Bilibili postprocess Effect contract drifted")
    row = connection.execute(
        "SELECT payload_json FROM job_effect_fact WHERE job_id=? AND effect_operation_id=? ORDER BY sequence LIMIT 1",
        (effect.root_id, effect.operation_id),
    ).fetchone()
    if row is None:
        raise ValueError("Bilibili postprocess Job fact is unavailable")
    job = json.loads(str(row[0]))
    if (
        not isinstance(job, dict)
        or job.get("id") != effect.root_id
        or job.get("job_type") != EFFECT_KIND
        or job.get("execution_version") != EFFECT_V2
    ):
        raise ValueError("Bilibili postprocess Job identity drifted")
    exact_input(job.get("postprocess_input"))
    return job


def _validated_receipt(connection, effect, job, verify):
    row = connection.execute(
        f"SELECT receipt_json FROM {RECEIPT_TABLE} WHERE operation_id=?", (effect.operation_id,),
    ).fetchone()
    if row is None:
        return None
    receipt = json.loads(str(row[0]))
    output = exact_output(receipt.get("output") if isinstance(receipt, Mapping) else None)
    if receipt != _receipt(effect, job, output):
        raise ValueError("Bilibili postprocess receipt drifted")
    verify(effect, exact_input(job["postprocess_input"]), output)
    return receipt


def _read_failure(connection, effect, job):
    row = connection.execute(
        f"SELECT failure_json FROM {FAILURE_TABLE} WHERE operation_id=?", (effect.operation_id,),
    ).fetchone()
    if row is None:
        return None
    failure = json.loads(str(row[0]))
    if failure != _failure(effect, job):
        raise ValueError("Bilibili postprocess failure drifted")
    return failure


def _receipt(effect, job, output):
    return {
        "operation_id": effect.operation_id,
        "job_id": job["id"],
        "receipt_ref": f"receipt:bilibili-media-postprocess/{effect.operation_id}",
        "receipt_kind": RECEIPT_KIND,
        "receipt_schema_version": RECEIPT_SCHEMA,
        "intent_schema_version": INTENT_SCHEMA,
        "output": dict(output),
    }


def _failure(effect, job):
    return {
        "operation_id": effect.operation_id,
        "job_id": job["id"],
        "failure_ref": f"failure:bilibili-media-postprocess/{effect.operation_id}",
        "failure_kind": "bilibili-media-postprocess.failed",
    }


def _effect_receipt(effect):
    return EffectReceipt(
        f"receipt:bilibili-media-postprocess/{effect.operation_id}",
        RECEIPT_KIND, RECEIPT_SCHEMA, INTENT_SCHEMA,
    )


def _ensure_schema(database: Path) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        initialize_job_projection_schema(connection)
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {RECEIPT_TABLE}(operation_id TEXT PRIMARY KEY,job_id TEXT NOT NULL,receipt_ref TEXT NOT NULL UNIQUE,receipt_json TEXT NOT NULL,recorded_at TEXT NOT NULL)"
        )
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {FAILURE_TABLE}(operation_id TEXT PRIMARY KEY,job_id TEXT NOT NULL,failure_ref TEXT NOT NULL UNIQUE,failure_json TEXT NOT NULL,recorded_at TEXT NOT NULL)"
        )


def _required(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"{key} must be non-empty")
    return item


def _json(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
