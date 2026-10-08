"""Fixture-only composite migration proof for the Shared Trust Audit."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from core.memory_core.publication_fixture_migration import (
    _COPIED_COLLECTIONS as _MEMORY_COLLECTIONS,
    _STAGING_COLLECTIONS as _MEMORY_STAGING_COLLECTIONS,
    _issues as _memory_issues,
    _read_collection,
)
from core.memory_core.runtime import SQLiteMemoryReader
from core.aggregate_repository_factory import STRUCTURED_DATABASE_NAME
from core.project_skill_core import SQLiteProjectSkillRepository
from core.project_skill_core.publication_inventory import (
    scan_project_skill_publication_fixture_inventory,
)
from core.storage_provider import (
    JsonObjectStoreInventory,
    MigrationRecord,
    SQLiteMigrationLedger,
    SQLiteStructuredRecord,
    SQLiteStructuredRecordStore,
    SharedTrustAuditActivationEvidence,
)

_PROJECT_SKILL_COLLECTIONS = (
    "project_skill_index",
    "project_skill_json",
    "project_skill_markdown",
    "project_skill_revisions",
    "project_skills",
    "staging_project_skills",
)
_SHARED_AUDIT_COLLECTIONS = ("memory_publications", "memory_transitions")
_MEMORY_OBJECT_TYPES = frozenset(("atom", "scenario", "series_memory"))
_PROJECT_SKILL_OBJECT_TYPE = "project_skill"
_AUTHORITY_MEMBERS = (
    "memory_atoms",
    "memory_publications",
    "memory_scenarios",
    "memory_series_memory",
    "memory_transitions",
    "project_skills",
)
_COPIED_COLLECTIONS = tuple(
    dict.fromkeys((*_MEMORY_COLLECTIONS, *_PROJECT_SKILL_COLLECTIONS))
)


class SharedTrustAuditFixtureMigrationError(ValueError):
    """Raised when a composite fixture cannot be proven or copied safely."""


@dataclass(frozen=True, slots=True)
class SharedTrustAuditFixtureInventory:
    inventory: JsonObjectStoreInventory
    issues: tuple[str, ...]

    @property
    def is_migratable(self) -> bool:
        return not self.issues


@dataclass(frozen=True, slots=True)
class SharedTrustAuditFixtureMigrationResult:
    migration_id: str
    member_migrations: dict[str, str]
    source_fingerprint: str
    target_fingerprint: str
    object_count: int

    def activation_evidence(
        self,
        *,
        namespace_id: str,
        activation_id: str,
    ) -> SharedTrustAuditActivationEvidence:
        """Return proof data accepted by the activation saga without activating it."""
        return SharedTrustAuditActivationEvidence(
            namespace_id=namespace_id,
            activation_id=activation_id,
            member_migrations=self.member_migrations,
            source_fingerprint=self.source_fingerprint,
            target_fingerprint=self.target_fingerprint,
        )


def scan_shared_trust_audit_fixture_inventory(
    object_store_root: Path,
    *,
    namespace_id: str,
) -> SharedTrustAuditFixtureInventory:
    source_root = object_store_root.expanduser().resolve(strict=False)
    source = _source_records(source_root, namespace_id)
    memory_records = {
        collection: dict(records)
        for collection, records in source.items()
        if collection in _MEMORY_COLLECTIONS
    }
    for collection in _SHARED_AUDIT_COLLECTIONS:
        memory_records[collection] = {
            object_id: payload
            for object_id, payload in source[collection].items()
            if payload.get("object_type") in _MEMORY_OBJECT_TYPES
        }
    staging = {
        collection: _read_collection(source_root, namespace_id, collection)[0]
        for collection in _MEMORY_STAGING_COLLECTIONS
    }
    issues = [
        f"memory:{issue.code}:{issue.collection}:{issue.object_id}"
        for issue in _memory_issues(memory_records, staging, namespace_id)
    ]

    project = scan_project_skill_publication_fixture_inventory(
        source_root,
        namespace_id=namespace_id,
    )
    issues.extend(
        f"project:{issue.code}:{issue.collection}:{issue.object_id}"
        for issue in project.issues
    )
    issues.extend(_shared_audit_issues(source))

    collections = tuple(
        _read_collection(source_root, namespace_id, collection)[1]
        for collection in sorted(_COPIED_COLLECTIONS)
    )
    inventory = JsonObjectStoreInventory(
        namespace_id=namespace_id,
        collections=collections,
        object_count=sum(item.object_count for item in collections),
        fingerprint=_source_fingerprint(namespace_id, collections),
    )
    return SharedTrustAuditFixtureInventory(
        inventory=inventory,
        issues=tuple(sorted(set(issues))),
    )


def plan_shared_trust_audit_fixture_migration_dry_run(
    *,
    ledger: SQLiteMigrationLedger,
    migration_id: str,
    target_schema_version: int,
    inventory: SharedTrustAuditFixtureInventory,
    rollback_pointer: str,
) -> MigrationRecord:
    _require_migratable(inventory, context="inventory")
    return ledger.plan_dry_run(
        migration_id=migration_id,
        target_schema_version=target_schema_version,
        inventory=inventory.inventory,
        rollback_pointer=rollback_pointer,
    )


def execute_shared_trust_audit_fixture_migration(
    *,
    object_store_root: Path,
    target_database_path: Path,
    ledger: SQLiteMigrationLedger,
    dry_run: MigrationRecord,
) -> SharedTrustAuditFixtureMigrationResult:
    source_root = object_store_root.expanduser().resolve(strict=False)
    target = target_database_path.expanduser().resolve(strict=False)
    _require_target_outside_source(source_root, target)
    artifacts = _sqlite_artifacts(target)
    if any(path.exists() for path in artifacts):
        raise SharedTrustAuditFixtureMigrationError(
            "composite migration target SQLite artifacts already exist"
        )

    current = scan_shared_trust_audit_fixture_inventory(
        source_root,
        namespace_id=dry_run.inventory.namespace_id,
    )
    _validate_dry_run(ledger, dry_run, current)
    source = _source_records(source_root, dry_run.inventory.namespace_id)
    if scan_shared_trust_audit_fixture_inventory(
        source_root,
        namespace_id=dry_run.inventory.namespace_id,
    ) != current:
        raise SharedTrustAuditFixtureMigrationError(
            "composite migration source changed while preparing copy"
        )

    try:
        records = SQLiteStructuredRecordStore(target)
        with records.begin() as transaction:
            for collection in sorted(_COPIED_COLLECTIONS):
                for object_id, payload in sorted(source[collection].items()):
                    transaction.put(
                        collection,
                        object_id,
                        payload,
                        expected_revision=0,
                    )
            transaction.commit()
        _validate_target(target, source)
        return _result(
            migration_id=dry_run.migration_id,
            source_fingerprint=current.inventory.fingerprint,
            target_database_path=target,
            object_count=sum(len(records) for records in source.values()),
        )
    except Exception as error:
        _remove_new_target(artifacts)
        if isinstance(error, SharedTrustAuditFixtureMigrationError):
            raise
        raise SharedTrustAuditFixtureMigrationError(str(error)) from error


def compare_shared_trust_audit_fixture(
    *,
    object_store_root: Path,
    target_database_path: Path,
    namespace_id: str,
    migration_id: str,
) -> SharedTrustAuditFixtureMigrationResult:
    source_root = object_store_root.expanduser().resolve(strict=False)
    target = target_database_path.expanduser().resolve(strict=False)
    inventory = scan_shared_trust_audit_fixture_inventory(
        source_root,
        namespace_id=namespace_id,
    )
    _require_migratable(inventory, context="compatibility source")
    if not target.is_file():
        raise SharedTrustAuditFixtureMigrationError(
            "composite compatibility target is missing"
        )
    source = _source_records(source_root, namespace_id)
    before = _sqlite_file_state(target)
    _validate_target(target, source)
    _validate_read_models(target, source)
    result = _result(
        migration_id=migration_id,
        source_fingerprint=inventory.inventory.fingerprint,
        target_database_path=target,
        object_count=sum(len(records) for records in source.values()),
    )
    if _sqlite_file_state(target) != before:
        raise SharedTrustAuditFixtureMigrationError(
            "composite compatibility modified the target"
        )
    return result


def _shared_audit_issues(
    source: dict[str, dict[str, dict[str, object]]],
) -> list[str]:
    issues: list[str] = []
    owners_by_entity: dict[str, str] = {}
    for collection in _SHARED_AUDIT_COLLECTIONS:
        for object_id, payload in source[collection].items():
            object_type = payload.get("object_type")
            if object_type in _MEMORY_OBJECT_TYPES:
                owner = "memory"
            elif object_type == _PROJECT_SKILL_OBJECT_TYPE:
                owner = "project_skill"
            else:
                issues.append(f"shared:unknown_owner:{collection}:{object_id}")
                continue
            entity_id = (
                payload.get("published_object_id")
                if collection == "memory_publications"
                else payload.get("object_id")
            )
            if not isinstance(entity_id, str) or not entity_id:
                # Owner-specific validators report the more precise identity issue.
                continue
            previous = owners_by_entity.setdefault(entity_id, owner)
            if previous != owner:
                issues.append(f"shared:owner_collision:{collection}:{object_id}")
    return issues


def _validate_dry_run(
    ledger: SQLiteMigrationLedger,
    dry_run: MigrationRecord,
    current: SharedTrustAuditFixtureInventory,
) -> None:
    _require_migratable(current, context="inventory")
    records = {record.migration_id: record for record in ledger.list_records()}
    if records.get(dry_run.migration_id) != dry_run or dry_run.state != "dry_run_ready":
        raise SharedTrustAuditFixtureMigrationError(
            "composite migration dry-run is not ready"
        )
    if dry_run.input_fingerprint != current.inventory.fingerprint:
        raise SharedTrustAuditFixtureMigrationError(
            "composite migration dry-run fingerprint changed"
        )


def _require_migratable(
    inventory: SharedTrustAuditFixtureInventory,
    *,
    context: str,
) -> None:
    if inventory.issues:
        codes = ", ".join(inventory.issues)
        raise SharedTrustAuditFixtureMigrationError(
            f"composite {context} is inconsistent: {codes}"
        )


def _source_records(
    source_root: Path,
    namespace_id: str,
) -> dict[str, dict[str, dict[str, object]]]:
    return {
        collection: _read_collection(source_root, namespace_id, collection)[0]
        for collection in _COPIED_COLLECTIONS
    }


def _validate_target(
    target: Path,
    expected: dict[str, dict[str, dict[str, object]]],
) -> None:
    actual = _read_target_records(target)
    expected_nonempty = {
        collection: records for collection, records in expected.items() if records
    }
    if set(actual) != set(expected_nonempty):
        raise SharedTrustAuditFixtureMigrationError(
            "composite compatibility target collection set mismatch"
        )
    for collection, expected_records in expected_nonempty.items():
        actual_records = actual[collection]
        if set(actual_records) != set(expected_records):
            raise SharedTrustAuditFixtureMigrationError(
                f"composite compatibility object set mismatch: {collection}"
            )
        for object_id, payload in expected_records.items():
            actual_payload, revision = actual_records[object_id]
            if revision != 1 or actual_payload != payload:
                raise SharedTrustAuditFixtureMigrationError(
                    f"composite compatibility payload or revision mismatch: "
                    f"{collection}/{object_id}"
                )


def _validate_read_models(
    target: Path,
    source: dict[str, dict[str, dict[str, object]]],
) -> None:
    with _ReadOnlyStructuredRecords(target) as read_only_records:
        memory = SQLiteMemoryReader(read_only_records)
        for layer, collection in (
            ("atom", "memory_atoms"),
            ("scenario", "memory_scenarios"),
            ("series_memory", "memory_series_memory"),
        ):
            expected = tuple(source[collection][object_id] for object_id in sorted(source[collection]))
            if tuple(memory.list(layer)) != expected:
                raise SharedTrustAuditFixtureMigrationError(
                    f"composite Memory read mismatch: {layer}"
                )
        repository = SQLiteProjectSkillRepository(read_only_records)
        for project_id, index in source["project_skill_index"].items():
            skill_id = index.get("skill_id")
            skill = source["project_skills"].get(str(skill_id))
            if skill is None or repository.load(project_id) != skill:
                raise SharedTrustAuditFixtureMigrationError(
                    f"composite Project Skill current read mismatch: {project_id}"
                )
            revision = skill.get("revision")
            if not isinstance(revision, int) or revision < 1:
                raise SharedTrustAuditFixtureMigrationError(
                    f"composite Project Skill revision is invalid: {project_id}"
                )
            if len(repository.revisions(project_id)) != revision:
                raise SharedTrustAuditFixtureMigrationError(
                    f"composite Project Skill history mismatch: {project_id}"
                )
            for expected_revision in range(1, revision + 1):
                if repository.markdown(project_id, revision=expected_revision) is None:
                    raise SharedTrustAuditFixtureMigrationError(
                        f"composite Project Skill Markdown read missing: {project_id} r{expected_revision}"
                    )
                if repository.structured(project_id, revision=expected_revision) is None:
                    raise SharedTrustAuditFixtureMigrationError(
                        f"composite Project Skill JSON read missing: {project_id} r{expected_revision}"
                    )


class _ReadOnlyStructuredRecords:
    """Small read port for exercising repositories on a SQLite mode=ro handle."""

    def __init__(self, target: Path) -> None:
        self._connection = sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True)
        self._connection.row_factory = sqlite3.Row

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self._connection.close()
        return False

    def read(self, collection: str, object_id: str) -> SQLiteStructuredRecord | None:
        row = self._connection.execute(
            "SELECT collection, object_id, payload_json, revision "
            "FROM crp_structured_records WHERE collection = ? AND object_id = ?",
            (collection, object_id),
        ).fetchone()
        return None if row is None else _structured_record(row)

    def list(self, collection: str) -> tuple[SQLiteStructuredRecord, ...]:
        rows = self._connection.execute(
            "SELECT collection, object_id, payload_json, revision "
            "FROM crp_structured_records WHERE collection = ? ORDER BY object_id",
            (collection,),
        ).fetchall()
        return tuple(_structured_record(row) for row in rows)


def _structured_record(row: sqlite3.Row) -> SQLiteStructuredRecord:
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        raise SharedTrustAuditFixtureMigrationError(
            "composite compatibility target payload is invalid"
        )
    return SQLiteStructuredRecord(
        collection=row["collection"],
        object_id=row["object_id"],
        payload=payload,
        revision=row["revision"],
    )


def _read_target_records(
    target: Path,
) -> dict[str, dict[str, tuple[dict[str, object], int]]]:
    try:
        connection = sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True)
        rows = connection.execute(
            "SELECT collection, object_id, payload_json, revision "
            "FROM crp_structured_records ORDER BY collection, object_id"
        ).fetchall()
    except (OSError, sqlite3.Error) as error:
        raise SharedTrustAuditFixtureMigrationError(
            "composite compatibility target cannot be read"
        ) from error
    finally:
        if "connection" in locals():
            connection.close()
    result: dict[str, dict[str, tuple[dict[str, object], int]]] = {}
    for collection, object_id, payload_json, revision in rows:
        try:
            payload = json.loads(payload_json)
        except (TypeError, json.JSONDecodeError) as error:
            raise SharedTrustAuditFixtureMigrationError(
                "composite compatibility target payload is invalid"
            ) from error
        if not isinstance(payload, dict) or not isinstance(revision, int):
            raise SharedTrustAuditFixtureMigrationError(
                "composite compatibility target row is invalid"
            )
        collection_records = result.setdefault(str(collection), {})
        if object_id in collection_records:
            raise SharedTrustAuditFixtureMigrationError(
                "composite compatibility target identity is duplicated"
            )
        collection_records[str(object_id)] = (payload, revision)
    return result


def _result(
    *,
    migration_id: str,
    source_fingerprint: str,
    target_database_path: Path,
    object_count: int,
) -> SharedTrustAuditFixtureMigrationResult:
    return SharedTrustAuditFixtureMigrationResult(
        migration_id=migration_id,
        member_migrations={member: migration_id for member in _AUTHORITY_MEMBERS},
        source_fingerprint=source_fingerprint,
        target_fingerprint=_target_fingerprint(target_database_path),
        object_count=object_count,
    )


def _source_fingerprint(namespace_id: str, collections) -> str:
    return _digest(
        (
            namespace_id,
            *(
                f"{item.collection}\0{item.object_count}\0{item.fingerprint}"
                for item in collections
            ),
        )
    )


def _target_fingerprint(target: Path) -> str:
    records = _read_target_records(target)
    return _digest(
        f"{collection}\0{object_id}\0{json.dumps(payload, sort_keys=True, separators=(',', ':'))}\0{revision}"
        for collection, collection_records in sorted(records.items())
        if collection in _COPIED_COLLECTIONS
        for object_id, (payload, revision) in sorted(collection_records.items())
    )


def shared_trust_audit_target_fingerprint(target_database_path: Path) -> str:
    """Return the copied-collection fingerprint used by activation proof validation."""

    target = target_database_path.expanduser().resolve(strict=False)
    if not target.is_file():
        raise SharedTrustAuditFixtureMigrationError(
            "composite compatibility target is missing"
        )
    return _target_fingerprint(target)


def _digest(parts) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _require_target_outside_source(source_root: Path, target: Path) -> None:
    if target == source_root / STRUCTURED_DATABASE_NAME:
        return
    try:
        target.relative_to(source_root)
    except ValueError:
        return
    raise SharedTrustAuditFixtureMigrationError(
        "composite migration target cannot be inside source root"
    )


def _sqlite_artifacts(database_path: Path) -> tuple[Path, ...]:
    return database_path, Path(f"{database_path}-wal"), Path(f"{database_path}-shm")


def _remove_new_target(artifacts: tuple[Path, ...]) -> None:
    for path in reversed(artifacts):
        path.unlink(missing_ok=True)


def _sqlite_file_state(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns
