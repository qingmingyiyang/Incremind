from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import os
from pathlib import Path

from core.effect_log import Effect, EffectReceipt, EffectRunner, EffectState

from core.product_core.retention import (
    RetentionCandidate,
    RetentionDryRunReport,
    RetentionDryRunItem,
    RetentionBackupEvidence,
    RetentionRecord,
)

from .ports import ObjectStorePort
from .retention_effect_contract import build_retention_effect, retention_effect_receipt


_LEGACY_OPERATION_COLLECTION = "source_retention_purge_operations"
_INTENT_COLLECTION = "source_retention_purge_intents"
_STEP_COLLECTION = "source_retention_purge_step_facts"
_RECEIPT_COLLECTION = "source_retention_purge_receipts"
_INTENT_SCHEMA = "source-retention-purge-intent-v2"
_RECEIPT_KIND = "source-retention-purge-receipt"
_RECEIPT_SCHEMA = "source-retention-purge-receipt-v2"
_REPARSE_POINT = 0x0400


class SourceRetentionPurgeError(ValueError):
    """Raised when a physical Source purge cannot be proven safe."""


@dataclass(frozen=True, slots=True)
class SourceRetentionPurgeResult:
    operation_id: str
    source_id: str
    status: str
    deleted_count: int
    total_count: int
    idempotent: bool


class ExecuteSourceRetentionPurge:
    """Physically remove one eligible Source aggregate through a resumable saga."""

    def __init__(
        self,
        store: ObjectStorePort,
        *,
        active_fingerprint: Callable[[], str],
        effect_runner: EffectRunner | None = None,
        clock: Callable[[], datetime] | None = None,
        after_delete: Callable[[RetentionRecord, int], None] | None = None,
        owned_media_roots: tuple[Path, ...] = (),
    ) -> None:
        self._store = store
        self._active_fingerprint = active_fingerprint
        self._runner = effect_runner
        self._clock = clock or (lambda: datetime.now(UTC))
        self._after_delete = after_delete
        self._media_roots = tuple(
            root.expanduser().resolve(strict=False) for root in owned_media_roots
        )

    def preflight_owned_records(
        self,
        candidate: RetentionCandidate,
    ) -> None:
        """Validate the exact aggregate and owned derivative paths without mutation."""

        self._preflight_records(_deletion_order(candidate))

    def verify_effect(self, operation_id: str) -> tuple[EffectState, str | None]:
        """Probe durable facts only; Core Reaper owns retry scheduling."""

        receipt = self._store.read(_RECEIPT_COLLECTION, operation_id)
        if receipt is not None:
            if receipt.get("kind") != "source_retention_purge_receipt":
                raise SourceRetentionPurgeError("Source purge terminal Receipt drifted")
            return EffectState.SETTLED_OK, f"receipt:source-retention-purge/{operation_id}"
        if self._store.read(_INTENT_COLLECTION, operation_id) is not None:
            return EffectState.PLANNED, f"intent:source-retention-purge/{operation_id}"
        return EffectState.UNKNOWN, "source_retention_purge_intent_missing"

    def execute(
        self,
        *,
        candidate: RetentionCandidate,
        report: RetentionDryRunReport,
        plan_id: str,
        expected_source_revision: int,
        confirm: bool,
    ) -> SourceRetentionPurgeResult:
        if self._runner is None:
            raise SourceRetentionPurgeError("Source purge requires Core EffectRunner")
        effect_intent, gate_fact = build_retention_effect(
            session_id="source-retention-purge",
            root_id=candidate.object_id,
            step_key="purge-owned-records",
            kind="source_retention_purge",
            intent_ref_prefix="intent:source-retention-purge",
            gate_decision_id=f"gate:source-retention-purge-confirmed/{plan_id}",
            payload={"source_id": candidate.object_id, "plan_id": plan_id},
            policy_revision="source-retention-policy-v2",
            boundary_revision=f"source-revision-{candidate.revision}",
            workflow_revision="source-retention-workflow-v2",
            intent_schema_version=_INTENT_SCHEMA,
            receipt_kind=_RECEIPT_KIND,
            receipt_schema_version=_RECEIPT_SCHEMA,
        )
        operation_id = effect_intent.operation_id
        receipt = self._store.read(_RECEIPT_COLLECTION, operation_id)
        intent = self._store.read(_INTENT_COLLECTION, operation_id)
        legacy = self._store.read(_LEGACY_OPERATION_COLLECTION, operation_id)
        self._validate_request(
            candidate=candidate,
            report=report,
            plan_id=plan_id,
            expected_source_revision=expected_source_revision,
            confirm=confirm,
            resume=intent is not None or legacy is not None,
        )
        records = _deletion_order(candidate)
        replayed = receipt is not None
        if receipt is not None:
            _validate_terminal_receipt(receipt, operation_id, candidate, report)
        if intent is None:
            if legacy is None:
                self._preflight_records(records)
            intent = {
            "schema_version": _INTENT_SCHEMA,
            "id": operation_id,
            "kind": "source_retention_purge",
            "source_id": candidate.object_id,
            "plan_id": plan_id,
            "snapshot_id": report.backup_evidence.snapshot_id,
            "snapshot_fingerprint": report.backup_evidence.snapshot_fingerprint,
            "source_revision": expected_source_revision,
            "records": [
                {
                    "authority": record.authority,
                    "collection": record.collection,
                    "object_id": record.object_id,
                    "revision": record.revision,
                }
                for record in records
            ],
            "created_at": _iso(self._clock()),
            }
            self._store.write(_INTENT_COLLECTION, operation_id, intent, expected_revision=0)
        _validate_intent(intent, operation_id, candidate, report)
        self._runner.log.plan_v2(
            effect_intent,
            gate_decision_id=effect_intent.gate_decision_id,
            gate_fact=gate_fact,
            now=int(self._clock().timestamp()),
        )
        outcome = self._runner.execute_planned(
            operation_id,
            lambda _effect: self._execute_claimed(
                operation_id=operation_id,
                candidate=candidate,
                report=report,
                intent=intent,
            ),
            now=int(self._clock().timestamp()),
        )
        if outcome.state is not EffectState.SETTLED_OK:
            raise SourceRetentionPurgeError("Source purge Effect is not settled")
        terminal = self._store.read(_RECEIPT_COLLECTION, operation_id)
        if terminal is None:
            raise SourceRetentionPurgeError("Source purge terminal Receipt is unavailable")
        _validate_terminal_receipt(terminal, operation_id, candidate, report)
        return self._result(operation_id, candidate, records, idempotent=replayed)

    def handle_effect(self, effect: Effect) -> EffectReceipt:
        """Resume one v2 purge from its immutable domain Intent only."""

        intent = self._store.read(_INTENT_COLLECTION, effect.operation_id)
        if intent is None:
            raise SourceRetentionPurgeError("Source purge intent is unavailable")
        candidate, report = _recovery_objects(intent, effect.operation_id)
        return self._execute_claimed(
            operation_id=effect.operation_id,
            candidate=candidate,
            report=report,
            intent=intent,
        )

    def _validate_request(
        self,
        *,
        candidate: RetentionCandidate,
        report: RetentionDryRunReport,
        plan_id: str,
        expected_source_revision: int,
        confirm: bool,
        resume: bool,
    ) -> None:
        if confirm is not True:
            raise SourceRetentionPurgeError("Source purge requires explicit confirmation")
        if not plan_id or plan_id != report.plan_id:
            raise SourceRetentionPurgeError("Source purge plan identity mismatch")
        if candidate.aggregate_type != "source" or candidate.authority != "json_object_store":
            raise SourceRetentionPurgeError("Source purge requires JSON Source authority")
        if (
            not isinstance(expected_source_revision, int)
            or isinstance(expected_source_revision, bool)
            or expected_source_revision < 1
            or expected_source_revision != candidate.revision
        ):
            raise SourceRetentionPurgeError("Source purge revision mismatch")
        matching = [
            item
            for item in report.items
            if item.aggregate_type == "source" and item.object_id == candidate.object_id
        ]
        if len(matching) != 1 or matching[0].eligible is not True:
            raise SourceRetentionPurgeError("Source purge requires one eligible dry-run item")
        if matching[0].revision != candidate.revision:
            raise SourceRetentionPurgeError("Source purge dry-run revision drifted")
        if _record_identity(matching[0].owned_records) != _record_identity(
            candidate.owned_records
        ):
            raise SourceRetentionPurgeError("Source purge dry-run inventory drifted")
        if matching[0].inbound_references or candidate.inbound_references:
            raise SourceRetentionPurgeError("Source purge is blocked by inbound references")
        if not candidate.inventory_complete:
            raise SourceRetentionPurgeError("Source purge reference catalog is incomplete")
        backup = report.backup_evidence
        if (
            backup.status != "verified"
            or not backup.snapshot_id
            or not backup.snapshot_fingerprint
            or backup.snapshot_fingerprint != backup.active_fingerprint
            or backup.active_fingerprint != candidate.observed_vault_fingerprint
        ):
            raise SourceRetentionPurgeError("Source purge backup proof is invalid")
        if resume:
            return
        if self._active_fingerprint() != candidate.observed_vault_fingerprint:
            raise SourceRetentionPurgeError("Source purge active Vault drifted")
        source = self._store.read_including_deleted("sources", candidate.object_id)
        if not isinstance(source, Mapping):
            raise SourceRetentionPurgeError("Source purge authority is unavailable")
        lifecycle = source.get("library_lifecycle")
        if not isinstance(lifecycle, Mapping) or lifecycle.get("status") != "deleted":
            raise SourceRetentionPurgeError("Source purge authority is not deleted")
        if self._store.revision("sources", candidate.object_id) != expected_source_revision:
            raise SourceRetentionPurgeError("Source purge authority revision drifted")

    def _preflight_records(self, records: tuple[RetentionRecord, ...]) -> None:
        if not records or records[-1].collection != "sources":
            raise SourceRetentionPurgeError("Source purge inventory lacks terminal Source record")
        for record in records:
            if record.authority != "json_object_store":
                raise SourceRetentionPurgeError("Source purge cannot delete another authority")
            current = self._store.read_including_deleted(record.collection, record.object_id)
            if current is None:
                raise SourceRetentionPurgeError("Source purge owned record is unavailable")
            if record.revision is None or self._store.revision(
                record.collection, record.object_id
            ) != record.revision:
                raise SourceRetentionPurgeError("Source purge owned record revision drifted")
            if record.collection == "audio_asset_refs":
                self._verify_audio_derivative(current, allow_missing=False)

    def _execute_claimed(
        self,
        *,
        operation_id: str,
        candidate: RetentionCandidate,
        report: RetentionDryRunReport,
        intent: Mapping[str, object],
    ) -> EffectReceipt:
        _validate_intent(intent, operation_id, candidate, report)
        terminal = self._store.read(_RECEIPT_COLLECTION, operation_id)
        if terminal is not None:
            _validate_terminal_receipt(terminal, operation_id, candidate, report)
            return _effect_receipt(operation_id)
        records = _intent_records(intent)
        for index, record in enumerate(records):
            step_id = f"{operation_id}-step-{index:04d}"
            existing_step = self._store.read(_STEP_COLLECTION, step_id)
            if existing_step is not None:
                _validate_step(existing_step, operation_id, index, record)
                continue
            record = records[index]
            current = self._store.read_including_deleted(
                record.collection, record.object_id
            )
            if current is not None:
                if record.revision is None or self._store.revision(
                    record.collection, record.object_id
                ) != record.revision:
                    raise SourceRetentionPurgeError(
                        "Source purge owned record revision drifted during resume"
                    )
                if record.collection == "audio_asset_refs":
                    self._purge_audio_derivative(
                        current,
                        operation_id=operation_id,
                    )
                self._store.delete(record.collection, record.object_id)
            step = {
                "schema_version": "1.0.0", "id": step_id,
                "operation_id": operation_id, "index": index,
                "authority": record.authority, "collection": record.collection,
                "object_id": record.object_id, "revision": record.revision,
                "deleted_at": _iso(self._clock()),
            }
            self._store.write(_STEP_COLLECTION, step_id, step, expected_revision=0)
            if self._after_delete is not None:
                self._after_delete(record, index)
        receipt = {
            "schema_version": "1.0.0", "id": operation_id,
            "kind": "source_retention_purge_receipt",
            "source_id": candidate.object_id, "plan_id": report.plan_id,
            "snapshot_id": report.backup_evidence.snapshot_id,
            "snapshot_fingerprint": report.backup_evidence.snapshot_fingerprint,
            "source_revision": candidate.revision,
            "deleted_count": len(records), "completed_at": _iso(self._clock()),
        }
        existing_receipt = self._store.read(_RECEIPT_COLLECTION, operation_id)
        if existing_receipt is None:
            self._store.write(_RECEIPT_COLLECTION, operation_id, receipt, expected_revision=0)
        elif existing_receipt != receipt:
            raise SourceRetentionPurgeError("Source purge terminal Receipt drifted")
        return _effect_receipt(operation_id)

    @staticmethod
    def _result(
        operation_id: str,
        candidate: RetentionCandidate,
        records: tuple[RetentionRecord, ...],
        *,
        idempotent: bool,
    ) -> SourceRetentionPurgeResult:
        return SourceRetentionPurgeResult(
            operation_id=operation_id,
            source_id=candidate.object_id,
            status="completed",
            deleted_count=len(records),
            total_count=len(records),
            idempotent=idempotent,
        )

    def _verify_audio_derivative(
        self,
        payload: Mapping[str, object],
        *,
        allow_missing: bool,
    ) -> Path | None:
        if payload.get("path_scope") != "local_generated_audio_track":
            raise SourceRetentionPurgeError(
                "Source purge audio derivative path scope is not owned"
            )
        raw = payload.get("path")
        if not isinstance(raw, str) or not raw:
            raise SourceRetentionPurgeError(
                "Source purge audio derivative path is unavailable"
            )
        path = Path(raw).expanduser()
        if not path.is_absolute() or path.is_symlink():
            raise SourceRetentionPurgeError(
                "Source purge audio derivative path is unsafe"
            )
        lexical = Path(os.path.abspath(path))
        lexical_root = next(
            (item for item in self._media_roots if _is_relative_to(lexical, item)),
            None,
        )
        if lexical_root is None:
            raise SourceRetentionPurgeError(
                "Source purge audio derivative root is not configured"
            )
        _reject_reparse_chain(lexical.parent, lexical_root)
        resolved = path.resolve(strict=False)
        root = next(
            (item for item in self._media_roots if _is_relative_to(resolved, item)),
            None,
        )
        if root is None:
            raise SourceRetentionPurgeError(
                "Source purge audio derivative root is not configured"
            )
        _reject_reparse_chain(resolved.parent, root)
        if not resolved.exists():
            if allow_missing:
                return None
            raise SourceRetentionPurgeError(
                "Source purge audio derivative file is unavailable"
            )
        if not resolved.is_file() or _is_reparse(resolved):
            raise SourceRetentionPurgeError(
                "Source purge audio derivative is not a regular file"
            )
        expected = payload.get("size_bytes")
        if (
            not isinstance(expected, int)
            or isinstance(expected, bool)
            or expected < 0
            or resolved.stat().st_size != expected
        ):
            raise SourceRetentionPurgeError(
                "Source purge audio derivative byte count drifted"
            )
        return resolved

    def _purge_audio_derivative(
        self,
        payload: Mapping[str, object],
        *,
        operation_id: str,
    ) -> None:
        raw = payload.get("path")
        if not isinstance(raw, str) or not raw:
            raise SourceRetentionPurgeError(
                "Source purge audio derivative path is unavailable"
            )
        original = Path(raw).expanduser()
        digest = hashlib.sha256(
            f"{operation_id}\0{raw}".encode("utf-8")
        ).hexdigest()[:24]
        quarantine = original.with_name(f".crp-source-purge-{digest}.quarantine")
        if quarantine.exists() or quarantine.is_symlink():
            if original.exists():
                raise SourceRetentionPurgeError(
                    "Source purge audio derivative quarantine conflicts"
                )
            if not quarantine.is_file() or quarantine.is_symlink() or _is_reparse(quarantine):
                raise SourceRetentionPurgeError(
                    "Source purge audio derivative quarantine is unsafe"
                )
            quarantine.unlink()
            return
        path = self._verify_audio_derivative(payload, allow_missing=True)
        if path is None:
            return
        os.replace(path, quarantine)
        if not quarantine.is_file() or quarantine.is_symlink() or _is_reparse(quarantine):
            raise SourceRetentionPurgeError(
                "Source purge audio derivative quarantine is unsafe"
            )
        quarantine.unlink()

def _deletion_order(candidate: RetentionCandidate) -> tuple[RetentionRecord, ...]:
    unique = {
        (item.authority, item.collection, item.object_id): item
        for item in candidate.owned_records
    }
    source_key = ("json_object_store", "sources", candidate.object_id)
    if source_key not in unique:
        raise SourceRetentionPurgeError("Source purge inventory lacks Source authority")
    ordered = sorted(
        unique.values(),
        key=lambda item: (
            item.collection == "sources",
            item.collection,
            item.object_id,
        ),
    )
    return tuple(ordered)


def _operation_id(plan_id: str, source_id: str) -> str:
    digest = hashlib.sha256(f"{plan_id}\0{source_id}".encode("utf-8")).hexdigest()
    return f"source-retention-purge-{digest[:32]}"


def _effect_receipt(operation_id: str) -> EffectReceipt:
    return retention_effect_receipt(
        operation_id,
        receipt_ref_prefix="receipt:source-retention-purge",
        receipt_kind=_RECEIPT_KIND,
        receipt_schema_version=_RECEIPT_SCHEMA,
        intent_schema_version=_INTENT_SCHEMA,
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_reparse(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & _REPARSE_POINT)


def _reject_reparse_chain(path: Path, root: Path) -> None:
    current = path
    while True:
        if current.exists() and _is_reparse(current):
            raise SourceRetentionPurgeError(
                "Source purge audio derivative path contains a reparse directory"
            )
        if current == root:
            return
        if current.parent == current or not _is_relative_to(current, root):
            raise SourceRetentionPurgeError(
                "Source purge audio derivative path escapes configured root"
            )
        current = current.parent


def _record_identity(
    records: tuple[RetentionRecord, ...],
) -> tuple[tuple[str, str, str, int | None], ...]:
    return tuple(
        sorted(
            (
                record.authority,
                record.collection,
                record.object_id,
                record.revision,
            )
            for record in records
        )
    )


def _validate_intent(
    intent: Mapping[str, object],
    operation_id: str,
    candidate: RetentionCandidate,
    report: RetentionDryRunReport,
) -> None:
    if (
        intent.get("schema_version") != _INTENT_SCHEMA
        or intent.get("id") != operation_id
        or intent.get("kind") != "source_retention_purge"
        or intent.get("source_id") != candidate.object_id
        or intent.get("plan_id") != report.plan_id
        or intent.get("source_revision") != candidate.revision
        or intent.get("snapshot_id") != report.backup_evidence.snapshot_id
        or intent.get("snapshot_fingerprint")
        != report.backup_evidence.snapshot_fingerprint
    ):
        raise SourceRetentionPurgeError("Source purge intent identity drifted")
    _intent_records(intent)


def _recovery_objects(
    intent: Mapping[str, object], operation_id: str,
) -> tuple[RetentionCandidate, RetentionDryRunReport]:
    records = _intent_records(intent)
    if intent.get("schema_version") != _INTENT_SCHEMA or intent.get("id") != operation_id:
        raise SourceRetentionPurgeError("Source purge recovery Intent drifted")
    source_id = intent.get("source_id")
    plan_id = intent.get("plan_id")
    revision = intent.get("source_revision")
    snapshot_id = intent.get("snapshot_id")
    fingerprint = intent.get("snapshot_fingerprint")
    created_at = intent.get("created_at")
    if (
        not isinstance(source_id, str) or not source_id
        or not isinstance(plan_id, str) or not plan_id
        or not isinstance(revision, int) or isinstance(revision, bool) or revision < 1
        or not isinstance(snapshot_id, str) or not snapshot_id
        or not isinstance(fingerprint, str) or not fingerprint
        or not isinstance(created_at, str) or not created_at
    ):
        raise SourceRetentionPurgeError("Source purge recovery Intent is invalid")
    candidate = RetentionCandidate(
        aggregate_type="source",
        object_id=source_id,
        authority="json_object_store",
        revision=revision,
        lifecycle_status="deleted",
        lifecycle_at=None,
        undo_expires_at=None,
        observed_vault_fingerprint=fingerprint,
        inventory_complete=True,
        owned_records=records,
        inbound_references=(),
    )
    backup = RetentionBackupEvidence(
        status="verified",
        snapshot_id=snapshot_id,
        snapshot_fingerprint=fingerprint,
        active_fingerprint=fingerprint,
        file_count=1,
    )
    report = RetentionDryRunReport(
        schema_version="1.0.0",
        plan_id=plan_id,
        evaluated_at=created_at,
        execution_supported=False,
        approval_token=None,
        backup_evidence=backup,
        items=(RetentionDryRunItem(
            aggregate_type="source",
            object_id=source_id,
            authority="json_object_store",
            revision=revision,
            eligible=True,
            eligible_after=None,
            blockers=(),
            owned_records=records,
            inbound_references=(),
        ),),
    )
    _validate_intent(intent, operation_id, candidate, report)
    return candidate, report


def _intent_records(intent: Mapping[str, object]) -> tuple[RetentionRecord, ...]:
    raw = intent.get("records")
    if not isinstance(raw, list) or not raw:
        raise SourceRetentionPurgeError("Source purge intent records are invalid")
    records: list[RetentionRecord] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise SourceRetentionPurgeError("Source purge intent record is invalid")
        authority = item.get("authority")
        collection = item.get("collection")
        object_id = item.get("object_id")
        revision = item.get("revision")
        if (
            authority != "json_object_store"
            or not isinstance(collection, str)
            or not collection
            or not isinstance(object_id, str)
            or not object_id
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
        ):
            raise SourceRetentionPurgeError("Source purge intent record is invalid")
        records.append(
            RetentionRecord(
                authority=authority,
                collection=collection,
                object_id=object_id,
                revision=revision,
            )
        )
    if records[-1].collection != "sources":
        raise SourceRetentionPurgeError("Source purge intent must delete Source last")
    return tuple(records)


def _validate_step(
    payload: Mapping[str, object],
    operation_id: str,
    index: int,
    record: RetentionRecord,
) -> None:
    if (
        payload.get("operation_id") != operation_id
        or payload.get("index") != index
        or payload.get("authority") != record.authority
        or payload.get("collection") != record.collection
        or payload.get("object_id") != record.object_id
        or payload.get("revision") != record.revision
    ):
        raise SourceRetentionPurgeError("Source purge step fact drifted")


def _validate_terminal_receipt(
    receipt: Mapping[str, object],
    operation_id: str,
    candidate: RetentionCandidate,
    report: RetentionDryRunReport,
) -> None:
    if (
        receipt.get("id") != operation_id
        or receipt.get("kind") != "source_retention_purge_receipt"
        or receipt.get("source_id") != candidate.object_id
        or receipt.get("plan_id") != report.plan_id
        or receipt.get("source_revision") != candidate.revision
        or receipt.get("snapshot_id") != report.backup_evidence.snapshot_id
        or receipt.get("snapshot_fingerprint") != report.backup_evidence.snapshot_fingerprint
        or receipt.get("deleted_count") != len(_deletion_order(candidate))
    ):
        raise SourceRetentionPurgeError("Source purge terminal Receipt drifted")


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise SourceRetentionPurgeError("Source purge clock must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
