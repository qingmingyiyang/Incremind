from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .aggregate_repository_factory import (
    AUTHORITY_DATABASE_NAME,
    STRUCTURED_DATABASE_NAME,
    TARGET_IDENTITY,
)
from .document_engine.migration_executor import (
    _source_records as _document_source_records,
    _validate_target as _validate_document_target,
)
from .document_engine.migration_inventory import scan_document_migration_inventory
from .project_skill_core.migration_executor import (
    _source_records as _skill_source_records,
    _validate_target as _validate_skill_target,
)
from .project_skill_core.migration_inventory import scan_project_skill_migration_inventory
from .storage_provider import (
    AggregateAuthorityTransition,
    SQLiteAggregateAuthorityStore,
    SQLiteStructuredRecordStore,
)


_SAFE_EVIDENCE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class AggregateActivationError(ValueError):
    """Raised when staged aggregates cannot be activated without ambiguity."""


@dataclass(frozen=True, slots=True)
class AggregateActivationResult:
    evidence_id: str
    document_authority_revision: int
    project_skill_authority_revision: int


def activate_document_skill_fixture(
    *,
    runtime_root: Path,
    namespace_id: str,
    evidence_id: str,
) -> AggregateActivationResult:
    """Activate two staged fixture aggregates after local evidence preflight."""

    if not _SAFE_EVIDENCE_ID.fullmatch(evidence_id):
        raise AggregateActivationError("activation evidence id is invalid")
    root = runtime_root.expanduser().resolve(strict=False)
    rebuild_root = root / ".rebuild-data"
    target_path = rebuild_root / STRUCTURED_DATABASE_NAME
    authority_path = rebuild_root / AUTHORITY_DATABASE_NAME
    if not target_path.exists() or not authority_path.exists():
        raise AggregateActivationError("activation target or authority store is missing")
    if Path(f"{target_path}-wal").exists() or Path(f"{target_path}-shm").exists():
        raise AggregateActivationError("activation target has active SQLite artifacts")
    authority = SQLiteAggregateAuthorityStore(authority_path)
    document_authority = _staged(authority, namespace_id, "documents")
    skill_authority = _staged(authority, namespace_id, "project_skills")
    records = SQLiteStructuredRecordStore(target_path)
    evidence_record = records.read("aggregate_activation_evidence", evidence_id)
    if evidence_record is None:
        raise AggregateActivationError("verified local activation evidence is missing")
    _validate_activation_evidence(
        evidence_record.payload,
        evidence_id,
        namespace_id,
        document_authority,
        skill_authority,
    )
    document_inventory = scan_document_migration_inventory(
        rebuild_root,
        namespace_id=namespace_id,
    )
    skill_inventory = scan_project_skill_migration_inventory(
        rebuild_root,
        namespace_id=namespace_id,
    )
    if document_inventory.issues or skill_inventory.issues:
        raise AggregateActivationError("activation source inventory is inconsistent")
    if document_inventory.inventory.fingerprint != document_authority.evidence.source_fingerprint:
        raise AggregateActivationError("Document source fingerprint changed after staging")
    if skill_inventory.inventory.fingerprint != skill_authority.evidence.source_fingerprint:
        raise AggregateActivationError("Project Skill source fingerprint changed after staging")
    _validate_marker(records, namespace_id, "documents", document_authority.evidence)
    _validate_marker(records, namespace_id, "project_skills", skill_authority.evidence)
    _validate_document_target(
        records,
        _document_source_records(rebuild_root, namespace_id),
    )
    _validate_skill_target(
        records,
        _skill_source_records(rebuild_root, namespace_id),
    )
    try:
        activated = authority.transition_many(
            (
                AggregateAuthorityTransition(
                    namespace_id,
                    "documents",
                    document_authority.revision,
                    "sqlite_active",
                    f"activation evidence {evidence_id} verified",
                    document_authority.evidence,
                ),
                AggregateAuthorityTransition(
                    namespace_id,
                    "project_skills",
                    skill_authority.revision,
                    "sqlite_active",
                    f"activation evidence {evidence_id} verified",
                    skill_authority.evidence,
                ),
            )
        )
    except Exception as exc:
        raise AggregateActivationError(f"atomic authority activation failed: {exc}") from exc
    return AggregateActivationResult(
        evidence_id,
        activated[0].revision,
        activated[1].revision,
    )


def _staged(store, namespace_id: str, aggregate: str):
    record = store.get(namespace_id, aggregate)
    if record is None or record.state != "sqlite_staged" or record.evidence is None:
        raise AggregateActivationError(f"{aggregate} authority is not staged")
    return record


def _validate_activation_evidence(
    payload,
    evidence_id,
    namespace_id,
    documents,
    skills,
) -> None:
    if payload.get("id") != evidence_id or payload.get("namespace_id") != namespace_id:
        raise AggregateActivationError("activation evidence identity mismatch")
    backup_pointer = payload.get("backup_pointer")
    rollback_manifest = payload.get("rollback_manifest_id")
    verified_at = payload.get("verified_at")
    verified_by = payload.get("verified_by")
    if not isinstance(backup_pointer, str) or not backup_pointer.startswith("snapshot:"):
        raise AggregateActivationError("activation evidence requires backup pointer")
    if not isinstance(rollback_manifest, str) or not rollback_manifest.startswith("rollback:"):
        raise AggregateActivationError("activation evidence requires rollback manifest")
    if not isinstance(verified_at, str) or not verified_at:
        raise AggregateActivationError("activation evidence requires verified_at")
    if not isinstance(verified_by, str) or not verified_by.startswith("local-verifier:"):
        raise AggregateActivationError("activation evidence requires local verifier")
    expected = {
        "documents_source_fingerprint": documents.evidence.source_fingerprint,
        "documents_target_fingerprint": documents.evidence.target_fingerprint,
        "project_skills_source_fingerprint": skills.evidence.source_fingerprint,
        "project_skills_target_fingerprint": skills.evidence.target_fingerprint,
        "target_identity": TARGET_IDENTITY,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise AggregateActivationError("activation evidence fingerprints do not match staged authority")


def _validate_marker(records, namespace_id, aggregate, evidence) -> None:
    marker = records.read(
        "aggregate_authority_targets",
        f"{namespace_id}~{aggregate}",
    )
    if marker is None:
        raise AggregateActivationError(f"{aggregate} target marker is missing")
    expected = {
        "namespace_id": namespace_id,
        "aggregate": aggregate,
        "migration_id": evidence.migration_id,
        "source_fingerprint": evidence.source_fingerprint,
        "target_fingerprint": evidence.target_fingerprint,
        "target_identity": evidence.target_identity,
    }
    if dict(marker.payload) != expected:
        raise AggregateActivationError(f"{aggregate} target marker mismatch")
