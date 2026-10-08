"""Core-Effect admission boundary for frozen Bilibili favorite batches.

HTTP only records a command and plans this Effect.  The existing Core reaper
owns execution and crash recovery; this module deliberately creates neither a
thread nor a second queue.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

from backend.api.bilibili_favorite_batch import BilibiliFavoriteBatchRepository
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.effect_log import (
    EFFECT_V2, NOT_APPLICABLE, V2_REVISION_KEYS, Effect, EffectClass,
    EffectHandlerRegistration, EffectIntent, EffectReceipt,
    EffectRecoveryRegistration, EffectState, GateDecision, GateDecisionFact,
)


from .bilibili_favorite_batch_admission import (
    EFFECT_KIND, INTENT_SCHEMA, RECEIPT_KIND, RECEIPT_SCHEMA,
    admit_bilibili_favorite_batch_command, _intent_for, _processing_status, _now,
)
_RECEIPT_TABLE = "bilibili_favorite_batch_effect_receipt"
_FAILURE_TABLE = "bilibili_favorite_batch_effect_failure"


def register_bilibili_favorite_batch_v2_handler(application, runtime_root: Path, effect_runtime) -> None:
    """Register this partition in the already composed Core Effect Runtime."""
    root = Path(runtime_root)
    from core.effect_log.runtime import EffectLeaseCheckpoint
    handler = BilibiliFavoriteBatchEffectHandler(
        root,
        effect_runtime.log.database,
        application,
        lambda effect: EffectLeaseCheckpoint.for_claim(effect_runtime, effect).checkpoint,
    )
    probe = BilibiliFavoriteBatchEffectProbe(root, effect_runtime.log.database)
    effect_runtime.handlers.register(EffectHandlerRegistration(
        kind=EFFECT_KIND, effect_class=EffectClass.QUERYABLE, handler=handler, probe=probe,
        contract_version=EFFECT_V2, intent_schema_version=INTENT_SCHEMA,
        receipt_kind=RECEIPT_KIND, receipt_schema_version=RECEIPT_SCHEMA,
    ))
    effect_runtime.recoveries.register(EffectRecoveryRegistration(
        kind=EFFECT_KIND, effect_class=EffectClass.QUERYABLE, probe=probe, verify=probe,
        contract_version=EFFECT_V2,
    ))


@dataclass(frozen=True, slots=True)
class BilibiliFavoriteBatchEffectHandler:
    runtime_root: Path
    database: Path
    application: object
    checkpoint_factory: object

    def __post_init__(self) -> None:
        object.__setattr__(self, "runtime_root", Path(self.runtime_root))
        object.__setattr__(self, "database", Path(self.database))
        _ensure_schema(Path(self.database))

    def __call__(self, effect: Effect) -> EffectReceipt:
        batch_id = _effect_batch_id(effect)
        with sqlite3.connect(self.database) as connection:
            row = connection.execute(
                f"SELECT receipt_json FROM {_RECEIPT_TABLE} WHERE operation_id=?", (effect.operation_id,),
            ).fetchone()
        if row is not None:
            _validate_receipt(json.loads(str(row[0])), effect, batch_id)
            return _receipt(effect, batch_id)
        store, settings = build_rebuild_object_store(self.runtime_root)
        repository = BilibiliFavoriteBatchRepository(store, namespace_id=settings.namespace_id)
        batch = repository.get(batch_id)
        if batch is None:
            raise ValueError("favorite batch is unavailable")
        command = batch.payload.get("command")
        if not isinstance(command, Mapping) or command.get("operation_id") != effect.operation_id:
            raise ValueError("favorite batch command evidence drifted")
        # Reuse the existing governed resolver, permission and Media Hands
        # admission boundary.  Each call persists at most ten item transitions.
        from backend.api.routes.bilibili_media_ingress import _process_favorite_batch
        request = SimpleNamespace(app=self.application)
        current = batch.payload
        checkpoint = self.checkpoint_factory(effect)
        while True:
            checkpoint()
            result = _process_favorite_batch(
                request, getattr(self.application.state, "container"), store,
                settings.namespace_id, repository, current,
                bool(command.get("retry_failed")),
            )
            current_batch = repository.get(batch_id)
            if current_batch is None:
                raise ValueError("favorite batch disappeared during processing")
            current = current_batch.payload
            # retry-failed applies only once; later chunks must not reopen an
            # already failed sibling.
            command = dict(current["command"])
            if command.get("retry_failed") is True:
                command["retry_failed"] = False
                current = dict(current, command=command)
                current = repository.save(current).payload
            if not result.get("has_pending"):
                break
        checkpoint()
        final = dict(current)
        final["admission_status"] = "partial_failure" if final["status"] == "partial_failure" else "complete"
        final["processing_status"] = _processing_status(final)
        final["updated_at"] = _now()
        final = repository.save(final).payload
        receipt = _receipt_payload(effect, batch_id, final)
        with sqlite3.connect(self.database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"INSERT OR IGNORE INTO {_RECEIPT_TABLE}(operation_id,batch_id,receipt_ref,receipt_json,recorded_at) VALUES(?,?,?,?,?)",
                (effect.operation_id, batch_id, receipt["receipt_ref"], _json(receipt), _now()),
            )
            connection.commit()
        return _receipt(effect, batch_id)


@dataclass(frozen=True, slots=True)
class BilibiliFavoriteBatchEffectProbe:
    runtime_root: Path
    database: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "runtime_root", Path(self.runtime_root))
        object.__setattr__(self, "database", Path(self.database))
        _ensure_schema(Path(self.database))

    def __call__(self, effect: Effect) -> tuple[EffectState, str | None]:
        try:
            batch_id = _effect_batch_id(effect)
            with sqlite3.connect(self.database) as connection:
                row = connection.execute(
                    f"SELECT receipt_json FROM {_RECEIPT_TABLE} WHERE operation_id=?", (effect.operation_id,),
                ).fetchone()
            if row is not None:
                _validate_receipt(json.loads(str(row[0])), effect, batch_id)
                return EffectState.SETTLED_OK, _receipt(effect, batch_id).receipt_ref
            store, settings = build_rebuild_object_store(self.runtime_root)
            batch = BilibiliFavoriteBatchRepository(store, namespace_id=settings.namespace_id).get(batch_id)
            if batch is None:
                return EffectState.UNKNOWN, "error:bilibili-favorite-batch-missing"
            command = batch.payload.get("command")
            if not isinstance(command, Mapping) or command.get("operation_id") != effect.operation_id:
                return EffectState.UNKNOWN, "error:bilibili-favorite-batch-command-drift"
            return EffectState.PLANNED, f"facts:bilibili-favorite-batch/pending/{batch_id}"
        except (TypeError, ValueError, sqlite3.DatabaseError):
            return EffectState.UNKNOWN, "error:bilibili-favorite-batch-evidence-drift"


def _effect_batch_id(effect: Effect) -> str:
    if effect.kind != EFFECT_KIND or effect.contract_version != EFFECT_V2 or effect.intent_schema_version != INTENT_SCHEMA:
        raise ValueError("favorite batch Effect contract drifted")
    return effect.root_id


def _receipt(effect: Effect, batch_id: str) -> EffectReceipt:
    return EffectReceipt(f"receipt:bilibili-favorite-batch/{effect.operation_id}", RECEIPT_KIND, RECEIPT_SCHEMA, INTENT_SCHEMA)


def _receipt_payload(effect: Effect, batch_id: str, payload: Mapping[str, object]) -> dict[str, object]:
    return {"operation_id": effect.operation_id, "batch_id": batch_id, "receipt_ref": _receipt(effect, batch_id).receipt_ref, "receipt_kind": RECEIPT_KIND, "receipt_schema_version": RECEIPT_SCHEMA, "intent_schema_version": INTENT_SCHEMA, "admission_status": payload["admission_status"]}


def _validate_receipt(value: object, effect: Effect, batch_id: str) -> None:
    if not isinstance(value, Mapping) or value != _receipt_payload(effect, batch_id, {"admission_status": value.get("admission_status")}):
        raise ValueError("favorite batch receipt drifted")


def _ensure_schema(database: Path) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute(f"CREATE TABLE IF NOT EXISTS {_RECEIPT_TABLE}(operation_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,receipt_ref TEXT NOT NULL UNIQUE,receipt_json TEXT NOT NULL,recorded_at TEXT NOT NULL)")


def _json(value: Mapping[str, object]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
