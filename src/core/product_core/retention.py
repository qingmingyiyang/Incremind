from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta


_SUPPORTED_AGGREGATES = {"source", "document"}
_SUPPORTED_AUTHORITIES = {
    "source": {"json_object_store"},
    "document": {"json_document", "sqlite_document"},
}


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    document_archive_min_age_days: int = 30

    def __post_init__(self) -> None:
        if (
            not isinstance(self.document_archive_min_age_days, int)
            or isinstance(self.document_archive_min_age_days, bool)
            or self.document_archive_min_age_days < 30
        ):
            raise ValueError("Document archive retention must be at least 30 days")


@dataclass(frozen=True, slots=True)
class RetentionRecord:
    authority: str
    collection: str
    object_id: str
    revision: int | None = None


@dataclass(frozen=True, slots=True)
class RetentionReference:
    authority: str
    collection: str
    object_id: str
    field_path: str


@dataclass(frozen=True, slots=True)
class RetentionCandidate:
    aggregate_type: str
    object_id: str
    authority: str
    revision: int
    lifecycle_status: str
    lifecycle_at: str | None
    undo_expires_at: str | None
    observed_vault_fingerprint: str
    inventory_complete: bool
    owned_records: tuple[RetentionRecord, ...]
    inbound_references: tuple[RetentionReference, ...]


@dataclass(frozen=True, slots=True)
class RetentionBackupEvidence:
    status: str
    snapshot_id: str | None
    snapshot_fingerprint: str | None
    active_fingerprint: str | None
    file_count: int | None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class RetentionDryRunItem:
    aggregate_type: str
    object_id: str
    authority: str
    revision: int
    eligible: bool
    eligible_after: str | None
    blockers: tuple[str, ...]
    owned_records: tuple[RetentionRecord, ...]
    inbound_references: tuple[RetentionReference, ...]


@dataclass(frozen=True, slots=True)
class RetentionDryRunReport:
    schema_version: str
    plan_id: str
    evaluated_at: str
    execution_supported: bool
    approval_token: None
    backup_evidence: RetentionBackupEvidence
    items: tuple[RetentionDryRunItem, ...]


class BuildRetentionDryRun:
    """Build a deterministic, non-executable purge inventory."""

    def __init__(self, *, policy: RetentionPolicy | None = None) -> None:
        self._policy = policy or RetentionPolicy()

    def execute(
        self,
        *,
        candidates: Sequence[RetentionCandidate],
        backup_evidence: RetentionBackupEvidence,
        evaluated_at: datetime,
    ) -> RetentionDryRunReport:
        now = _utc(evaluated_at)
        ordered = sorted(candidates, key=lambda item: (item.aggregate_type, item.object_id))
        identities = [(item.aggregate_type, item.object_id) for item in ordered]
        if len(identities) != len(set(identities)):
            raise ValueError("retention candidates must have unique aggregate identities")
        items = tuple(self._evaluate(item, backup_evidence, now) for item in ordered)
        evaluated = _iso(now)
        plan_payload = {
            "schema_version": "1.0.0",
            "evaluated_at": evaluated,
            "execution_supported": False,
            "backup_evidence": asdict(backup_evidence),
            "items": [asdict(item) for item in items],
        }
        digest = hashlib.sha256(
            json.dumps(
                plan_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return RetentionDryRunReport(
            schema_version="1.0.0",
            plan_id=f"retention-dry-run-{digest}",
            evaluated_at=evaluated,
            execution_supported=False,
            approval_token=None,
            backup_evidence=backup_evidence,
            items=items,
        )

    def _evaluate(
        self,
        candidate: RetentionCandidate,
        backup: RetentionBackupEvidence,
        now: datetime,
    ) -> RetentionDryRunItem:
        blockers: set[str] = set()
        eligible_after: datetime | None = None
        if candidate.aggregate_type not in _SUPPORTED_AGGREGATES:
            blockers.add("unsupported_aggregate")
        elif candidate.authority not in _SUPPORTED_AUTHORITIES[candidate.aggregate_type]:
            blockers.add("authority_invalid")
        if not candidate.inventory_complete:
            blockers.add("reference_catalog_incomplete")
        if not isinstance(candidate.revision, int) or isinstance(candidate.revision, bool) or candidate.revision < 1:
            blockers.add("revision_invalid")
        if candidate.inbound_references:
            blockers.add("inbound_references_present")
        if candidate.aggregate_type == "source":
            if candidate.lifecycle_status != "deleted":
                blockers.add("source_not_deleted")
            eligible_after = _parse(candidate.undo_expires_at)
        elif candidate.aggregate_type == "document":
            if candidate.lifecycle_status != "archived":
                blockers.add("document_not_archived")
            archived_at = _parse(candidate.lifecycle_at)
            if archived_at is not None:
                eligible_after = archived_at + timedelta(
                    days=self._policy.document_archive_min_age_days
                )
        if eligible_after is None:
            blockers.add("retention_time_invalid")
        elif now < eligible_after:
            blockers.add("retention_not_elapsed")
        if backup.status != "verified":
            blockers.add("backup_not_verified")
        elif not _verified_backup_evidence_is_well_formed(backup):
            blockers.add("backup_evidence_invalid")
        elif (
            backup.snapshot_fingerprint != backup.active_fingerprint
        ):
            blockers.add("backup_active_drift")
        elif candidate.observed_vault_fingerprint != backup.active_fingerprint:
            blockers.add("inventory_authority_drift")
        return RetentionDryRunItem(
            aggregate_type=candidate.aggregate_type,
            object_id=candidate.object_id,
            authority=candidate.authority,
            revision=candidate.revision,
            eligible=not blockers,
            eligible_after=_iso(eligible_after) if eligible_after is not None else None,
            blockers=tuple(sorted(blockers)),
            owned_records=tuple(sorted(candidate.owned_records, key=_record_key)),
            inbound_references=tuple(
                sorted(candidate.inbound_references, key=_reference_key)
            ),
        )


def serialize_retention_dry_run(report: RetentionDryRunReport) -> dict[str, object]:
    return asdict(report)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("retention evaluation time must be timezone-aware")
    return value.astimezone(UTC)


def _parse(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _record_key(record: RetentionRecord) -> tuple[str, str, str]:
    return record.authority, record.collection, record.object_id


def _reference_key(reference: RetentionReference) -> tuple[str, str, str, str]:
    return (
        reference.authority,
        reference.collection,
        reference.object_id,
        reference.field_path,
    )


def _verified_backup_evidence_is_well_formed(
    backup: RetentionBackupEvidence,
) -> bool:
    return (
        isinstance(backup.snapshot_id, str)
        and bool(backup.snapshot_id)
        and isinstance(backup.snapshot_fingerprint, str)
        and bool(backup.snapshot_fingerprint)
        and isinstance(backup.active_fingerprint, str)
        and bool(backup.active_fingerprint)
        and isinstance(backup.file_count, int)
        and not isinstance(backup.file_count, bool)
        and backup.file_count > 0
        and backup.error_code is None
    )
