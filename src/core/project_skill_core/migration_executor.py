from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.storage_provider import (
    MigrationRecord,
    SQLiteMigrationLedger,
    SQLiteStructuredRecordStore,
)

from .migration_inventory import _read_collection, scan_project_skill_migration_inventory
from .publication_inventory import scan_project_skill_publication_fixture_inventory
from .sqlite_runtime import SQLiteProjectSkillRepository


_COLLECTIONS = (
    "project_skill_index",
    "project_skill_json",
    "project_skill_markdown",
    "project_skill_revisions",
    "project_skills",
)
_PUBLICATION_COLLECTIONS = _COLLECTIONS + (
    "memory_publications",
    "memory_transitions",
    "staging_project_skills",
)


class ProjectSkillMigrationExecutionError(ValueError):
    """Raised when a temporary Project Skill migration cannot finish safely."""


@dataclass(frozen=True, slots=True)
class ProjectSkillMigrationExecutionResult:
    project_count: int
    object_count: int
    input_fingerprint: str


def execute_project_skill_fixture_migration(
    *,
    object_store_root: Path,
    target_database_path: Path,
    ledger: SQLiteMigrationLedger,
    dry_run: MigrationRecord,
) -> ProjectSkillMigrationExecutionResult:
    source_root = object_store_root.expanduser().resolve(strict=False)
    target = target_database_path.expanduser().resolve(strict=False)
    _require_target_outside_source(source_root, target)
    artifacts = _sqlite_artifacts(target)
    if any(path.exists() for path in artifacts):
        raise ProjectSkillMigrationExecutionError(
            "Project Skill migration target SQLite artifacts already exist"
        )
    namespace_id = dry_run.inventory.namespace_id
    current = scan_project_skill_migration_inventory(
        source_root,
        namespace_id=namespace_id,
    )
    _validate_dry_run(ledger, dry_run, current)
    source_records = _source_records(source_root, namespace_id)
    if scan_project_skill_migration_inventory(source_root, namespace_id=namespace_id) != current:
        raise ProjectSkillMigrationExecutionError(
            "Project Skill migration source changed while preparing copy"
        )

    records = SQLiteStructuredRecordStore(target)
    try:
        with records.begin() as uow:
            for collection in _COLLECTIONS:
                for object_id, payload in sorted(source_records[collection].items()):
                    uow.put(collection, object_id, payload, expected_revision=0)
            uow.commit()
        _validate_target(records, source_records)
        return ProjectSkillMigrationExecutionResult(
            project_count=len(source_records["project_skill_index"]),
            object_count=sum(len(items) for items in source_records.values()),
            input_fingerprint=current.inventory.fingerprint,
        )
    except Exception as exc:
        _remove_new_target(artifacts)
        if isinstance(exc, ProjectSkillMigrationExecutionError):
            raise
        raise ProjectSkillMigrationExecutionError(str(exc)) from exc


def execute_project_skill_publication_fixture_migration(
    *, object_store_root: Path, target_database_path: Path, ledger: SQLiteMigrationLedger, dry_run: MigrationRecord
) -> ProjectSkillMigrationExecutionResult:
    source_root = object_store_root.expanduser().resolve(strict=False)
    target = target_database_path.expanduser().resolve(strict=False)
    _require_target_outside_source(source_root, target)
    artifacts = _sqlite_artifacts(target)
    if any(path.exists() for path in artifacts):
        raise ProjectSkillMigrationExecutionError("Project Skill publication migration target SQLite artifacts already exist")
    current = scan_project_skill_publication_fixture_inventory(source_root, namespace_id=dry_run.inventory.namespace_id)
    if current.issues:
        codes = ", ".join(sorted({issue.code for issue in current.issues}))
        raise ProjectSkillMigrationExecutionError(f"Project Skill publication inventory is inconsistent: {codes}")
    records_by_id = {record.migration_id: record for record in ledger.list_records()}
    if records_by_id.get(dry_run.migration_id) != dry_run or dry_run.state != "dry_run_ready" or dry_run.input_fingerprint != current.inventory.fingerprint:
        raise ProjectSkillMigrationExecutionError("Project Skill publication migration dry-run fingerprint changed")
    source_records = _publication_source_records(source_root, dry_run.inventory.namespace_id)
    if scan_project_skill_publication_fixture_inventory(source_root, namespace_id=dry_run.inventory.namespace_id) != current:
        raise ProjectSkillMigrationExecutionError("Project Skill publication migration source changed while preparing copy")
    records = SQLiteStructuredRecordStore(target)
    try:
        with records.begin() as uow:
            for collection in _PUBLICATION_COLLECTIONS:
                for object_id, payload in sorted(source_records[collection].items()):
                    uow.put(collection, object_id, payload, expected_revision=0)
            uow.commit()
        _validate_publication_target(records, source_records)
        return ProjectSkillMigrationExecutionResult(
            project_count=len(source_records["project_skill_index"]),
            object_count=sum(len(items) for items in source_records.values()),
            input_fingerprint=current.inventory.fingerprint,
        )
    except Exception as exc:
        _remove_new_target(artifacts)
        if isinstance(exc, ProjectSkillMigrationExecutionError):
            raise
        raise ProjectSkillMigrationExecutionError(str(exc)) from exc


def _validate_dry_run(ledger, dry_run, current) -> None:
    records = {record.migration_id: record for record in ledger.list_records()}
    if records.get(dry_run.migration_id) != dry_run or dry_run.state != "dry_run_ready":
        raise ProjectSkillMigrationExecutionError(
            "Project Skill migration dry-run is not ready"
        )
    if current.issues:
        codes = ", ".join(sorted({issue.code for issue in current.issues}))
        raise ProjectSkillMigrationExecutionError(
            f"Project Skill migration inventory is inconsistent: {codes}"
        )
    if dry_run.input_fingerprint != current.inventory.fingerprint:
        raise ProjectSkillMigrationExecutionError(
            "Project Skill migration input fingerprint changed"
        )


def _source_records(
    source_root: Path,
    namespace_id: str,
) -> dict[str, dict[str, dict[str, object]]]:
    namespace_root = source_root / "objects" / namespace_id
    return {
        collection: _read_collection(namespace_root, collection)[0]
        for collection in _COLLECTIONS
    }


def _publication_source_records(source_root: Path, namespace_id: str) -> dict[str, dict[str, dict[str, object]]]:
    namespace_root = source_root / "objects" / namespace_id
    return {collection: _read_collection(namespace_root, collection)[0] for collection in _PUBLICATION_COLLECTIONS}


def _validate_target(
    records: SQLiteStructuredRecordStore,
    source_records: dict[str, dict[str, dict[str, object]]],
) -> None:
    for collection, objects in source_records.items():
        target_objects = {record.object_id: record for record in records.list(collection)}
        if set(target_objects) != set(objects):
            raise ProjectSkillMigrationExecutionError(
                f"Project Skill target {collection} object set mismatch"
            )
        for object_id, payload in objects.items():
            target = target_objects[object_id]
            if target.revision != 1 or dict(target.payload) != payload:
                raise ProjectSkillMigrationExecutionError(
                    f"Project Skill target {collection}/{object_id} mismatch"
                )


def _validate_publication_target(records: SQLiteStructuredRecordStore, source_records: dict[str, dict[str, dict[str, object]]]) -> None:
    _validate_target(records, {collection: source_records[collection] for collection in _COLLECTIONS})
    for collection in ("memory_publications", "memory_transitions", "staging_project_skills"):
        actual = {record.object_id: dict(record.payload) for record in records.list(collection)}
        if actual != source_records[collection]:
            raise ProjectSkillMigrationExecutionError(f"Project Skill publication target {collection} mismatch")

    repository = SQLiteProjectSkillRepository(records)
    for project_id, index in source_records["project_skill_index"].items():
        skill_id = str(index["skill_id"])
        skill = source_records["project_skills"][skill_id]
        if repository.load(project_id) != skill:
            raise ProjectSkillMigrationExecutionError(
                f"Project Skill current read mismatch: {project_id}"
            )
        revision = int(skill["revision"])
        if len(repository.revisions(project_id)) != revision:
            raise ProjectSkillMigrationExecutionError(
                f"Project Skill revision count mismatch: {project_id}"
            )
        for expected_revision in range(1, revision + 1):
            if repository.markdown(project_id, revision=expected_revision) is None:
                raise ProjectSkillMigrationExecutionError(
                    f"Project Skill Markdown read missing: {project_id} r{expected_revision}"
                )
            if repository.structured(project_id, revision=expected_revision) is None:
                raise ProjectSkillMigrationExecutionError(
                    f"Project Skill JSON read missing: {project_id} r{expected_revision}"
                )


def _sqlite_artifacts(database_path: Path) -> tuple[Path, ...]:
    return database_path, Path(f"{database_path}-wal"), Path(f"{database_path}-shm")


def _require_target_outside_source(source_root: Path, target: Path) -> None:
    try:
        target.relative_to(source_root)
    except ValueError:
        return
    raise ProjectSkillMigrationExecutionError(
        "Project Skill migration target cannot be inside source root"
    )


def _remove_new_target(artifacts: tuple[Path, ...]) -> None:
    for path in reversed(artifacts):
        path.unlink(missing_ok=True)
