from __future__ import annotations

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
    AggregateAuthorityEvidence,
    MigrationRecord,
    SQLiteAggregateAuthorityStore,
    SQLiteMigrationLedger,
    SQLiteStructuredRecordStore,
)


class AggregateCutoverOrchestratorError(ValueError):
    """Raised when combined aggregate staging cannot complete safely."""


@dataclass(frozen=True, slots=True)
class AggregateCutoverStagingResult:
    document_object_count: int
    project_skill_object_count: int
    document_authority_revision: int
    project_skill_authority_revision: int
    target_created: bool


def stage_document_skill_cutover_fixture(
    *,
    runtime_root: Path,
    ledger: SQLiteMigrationLedger,
    document_dry_run: MigrationRecord,
    project_skill_dry_run: MigrationRecord,
) -> AggregateCutoverStagingResult:
    """Stage both aggregates in one controlled target without activating them."""

    root = runtime_root.expanduser().resolve(strict=False)
    rebuild_root = root / ".rebuild-data"
    namespace_id = document_dry_run.inventory.namespace_id
    if project_skill_dry_run.inventory.namespace_id != namespace_id:
        raise AggregateCutoverOrchestratorError(
            "aggregate cutover namespaces do not match"
        )
    document_inventory = scan_document_migration_inventory(
        rebuild_root,
        namespace_id=namespace_id,
    )
    skill_inventory = scan_project_skill_migration_inventory(
        rebuild_root,
        namespace_id=namespace_id,
    )
    _validate_dry_run(ledger, document_dry_run, document_inventory, "documents")
    _validate_dry_run(
        ledger,
        project_skill_dry_run,
        skill_inventory,
        "project_skills",
    )
    documents = _document_source_records(rebuild_root, namespace_id)
    skills = _skill_source_records(rebuild_root, namespace_id)
    if scan_document_migration_inventory(rebuild_root, namespace_id=namespace_id) != document_inventory:
        raise AggregateCutoverOrchestratorError(
            "Document source changed while preparing cutover"
        )
    if scan_project_skill_migration_inventory(rebuild_root, namespace_id=namespace_id) != skill_inventory:
        raise AggregateCutoverOrchestratorError(
            "Project Skill source changed while preparing cutover"
        )

    target_path = rebuild_root / STRUCTURED_DATABASE_NAME
    authority_path = rebuild_root / AUTHORITY_DATABASE_NAME
    target_created = not target_path.exists()
    if Path(f"{target_path}-wal").exists() or Path(f"{target_path}-shm").exists():
        raise AggregateCutoverOrchestratorError(
            "aggregate cutover target has active SQLite artifacts"
        )
    records = SQLiteStructuredRecordStore(target_path)
    document_evidence = _evidence(document_dry_run, document_inventory.inventory.fingerprint)
    skill_evidence = _evidence(
        project_skill_dry_run,
        skill_inventory.inventory.fingerprint,
    )
    markers = {
        "documents": _marker(namespace_id, "documents", document_evidence),
        "project_skills": _marker(
            namespace_id,
            "project_skills",
            skill_evidence,
        ),
    }
    if target_created:
        try:
            _write_new_target(records, documents, skills, markers)
            _validate_combined_target(records, documents, skills, markers)
        except Exception as exc:
            _remove_target(target_path)
            raise AggregateCutoverOrchestratorError(str(exc)) from exc
    else:
        try:
            _validate_combined_target(records, documents, skills, markers)
        except Exception as exc:
            raise AggregateCutoverOrchestratorError(
                f"existing aggregate target does not match staged evidence: {exc}"
            ) from exc

    authority = SQLiteAggregateAuthorityStore(authority_path)
    try:
        document_record = _stage_authority(
            authority,
            namespace_id,
            "documents",
            document_evidence,
        )
        skill_record = _stage_authority(
            authority,
            namespace_id,
            "project_skills",
            skill_evidence,
        )
    except Exception as exc:
        raise AggregateCutoverOrchestratorError(
            f"aggregate target verified but authority staging is incomplete: {exc}"
        ) from exc
    return AggregateCutoverStagingResult(
        document_object_count=sum(len(items) for items in documents.values()),
        project_skill_object_count=sum(len(items) for items in skills.values()),
        document_authority_revision=document_record.revision,
        project_skill_authority_revision=skill_record.revision,
        target_created=target_created,
    )


def _validate_dry_run(ledger, dry_run, current, aggregate: str) -> None:
    records = {record.migration_id: record for record in ledger.list_records()}
    if records.get(dry_run.migration_id) != dry_run or dry_run.state != "dry_run_ready":
        raise AggregateCutoverOrchestratorError(
            f"{aggregate} migration dry-run is not ready"
        )
    if current.issues:
        raise AggregateCutoverOrchestratorError(
            f"{aggregate} inventory is inconsistent"
        )
    if dry_run.input_fingerprint != current.inventory.fingerprint:
        raise AggregateCutoverOrchestratorError(
            f"{aggregate} input fingerprint changed"
        )


def _evidence(dry_run: MigrationRecord, target_fingerprint: str) -> AggregateAuthorityEvidence:
    return AggregateAuthorityEvidence(
        migration_id=dry_run.migration_id,
        source_fingerprint=dry_run.input_fingerprint,
        target_fingerprint=target_fingerprint,
        target_identity=TARGET_IDENTITY,
    )


def _marker(namespace_id: str, aggregate: str, evidence: AggregateAuthorityEvidence) -> dict[str, object]:
    return {
        "namespace_id": namespace_id,
        "aggregate": aggregate,
        "migration_id": evidence.migration_id,
        "source_fingerprint": evidence.source_fingerprint,
        "target_fingerprint": evidence.target_fingerprint,
        "target_identity": evidence.target_identity,
    }


def _write_new_target(records, documents, skills, markers) -> None:
    with records.begin() as uow:
        for source in (documents, skills):
            for collection in sorted(source):
                for object_id, payload in sorted(source[collection].items()):
                    uow.put(collection, object_id, payload, expected_revision=0)
        for aggregate, marker in sorted(markers.items()):
            uow.put(
                "aggregate_authority_targets",
                f"{marker['namespace_id']}~{aggregate}",
                marker,
                expected_revision=0,
            )
        uow.commit()


def _validate_combined_target(records, documents, skills, markers) -> None:
    _validate_document_target(records, documents)
    _validate_skill_target(records, skills)
    for aggregate, expected in markers.items():
        marker_id = f"{expected['namespace_id']}~{aggregate}"
        record = records.read("aggregate_authority_targets", marker_id)
        if record is None or record.revision != 1 or dict(record.payload) != expected:
            raise AggregateCutoverOrchestratorError(
                f"{aggregate} target marker mismatch"
            )


def _stage_authority(store, namespace_id, aggregate, evidence):
    current = store.get(namespace_id, aggregate)
    if current is None:
        current = store.create_json_active(
            namespace_id=namespace_id,
            aggregate=aggregate,
            reason="JSON remains active before verified SQLite staging",
        )
    if current.state == "sqlite_staged":
        if current.evidence != evidence:
            raise AggregateCutoverOrchestratorError(
                f"{aggregate} staged authority evidence mismatch"
            )
        return current
    if current.state != "json_active":
        raise AggregateCutoverOrchestratorError(
            f"{aggregate} authority cannot be staged from {current.state}"
        )
    return store.transition(
        namespace_id=namespace_id,
        aggregate=aggregate,
        expected_revision=current.revision,
        to_state="sqlite_staged",
        evidence=evidence,
        reason="combined target copy and marker verified",
    )


def _remove_target(target_path: Path) -> None:
    for path in (
        Path(f"{target_path}-shm"),
        Path(f"{target_path}-wal"),
        target_path,
    ):
        path.unlink(missing_ok=True)
