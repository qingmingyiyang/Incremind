from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from core.storage_provider import (
    InventoryCollection,
    JsonObjectStore,
    JsonObjectStoreInventory,
    MigrationRecord,
    ObjectStorePathError,
    SQLiteMigrationLedger,
    SQLiteStructuredRecordStore,
    read_json_object_store_collection,
)

from .source_job_memory_uow import SQLiteSourceJobMemoryTransaction, SQLiteSourceJobMemoryUnitOfWork
from .sqlite_store import SQLiteJobRecord, SQLiteJobStore


_MEMORY_COLLECTIONS = (
    "staging_atoms",
    "staging_scenarios",
    "staging_series_memory",
    "memory_atoms",
    "memory_scenarios",
    "memory_series_memory",
)
_JSON_COLLECTIONS = ("sources", *_MEMORY_COLLECTIONS)
_TARGET_DATABASE_NAME = "structured-records.sqlite3"
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")


class SourceJobMemoryMigrationInventoryError(ValueError):
    """Raised when a temporary Source/Job/Memory fixture cannot be planned safely."""


class SourceJobMemoryMigrationExecutionError(ValueError):
    """Raised when a temporary Source/Job/Memory fixture copy cannot finish safely."""


@dataclass(frozen=True, slots=True)
class SourceJobMemoryMigrationIssue:
    code: str
    collection: str
    object_id: str


@dataclass(frozen=True, slots=True)
class SourceJobMemoryMigrationInventory:
    """Digest-only plan input for the Source/Job/Memory aggregate fixture."""

    inventory: JsonObjectStoreInventory
    issues: tuple[SourceJobMemoryMigrationIssue, ...]


@dataclass(frozen=True, slots=True)
class SourceJobMemoryMigrationExecutionResult:
    source_count: int
    job_count: int
    memory_count: int
    object_count: int
    input_fingerprint: str


@dataclass(frozen=True, slots=True)
class _StructuredFixtureRecord:
    collection: str
    object_id: str
    payload: Mapping[str, object]
    revision: int
    payload_bytes: bytes


@dataclass(frozen=True, slots=True)
class _FixtureRecords:
    structured: tuple[_StructuredFixtureRecord, ...]
    jobs: tuple[SQLiteJobRecord, ...]
    json_extract_memory_jobs: tuple[_StructuredFixtureRecord, ...]
    unsupported_memory_collections: tuple[str, ...]
    transition_count: int


def scan_source_job_memory_migration_inventory(
    object_store_root: Path,
    *,
    jobs_database_path: Path,
    namespace_id: str,
) -> SourceJobMemoryMigrationInventory:
    """Return a deterministic two-authority inventory for a temporary fixture.

    Source and Memory entries are read from JSON ObjectStore. ``extract_memory``
    Jobs are read from the canonical SQLite Job store. The resulting ledger
    digest deliberately has a ``jobs`` collection, but that label is not a
    claim that JSON owns Jobs.
    """

    records = _read_fixture_records(
        object_store_root,
        jobs_database_path=jobs_database_path,
        namespace_id=namespace_id,
    )
    inventory = _inventory_from_records(namespace_id, records)
    return SourceJobMemoryMigrationInventory(
        inventory=inventory,
        issues=_issues(records),
    )


def plan_source_job_memory_migration_dry_run(
    *,
    ledger: SQLiteMigrationLedger,
    migration_id: str,
    target_schema_version: int,
    inventory: SourceJobMemoryMigrationInventory,
    rollback_pointer: str,
) -> MigrationRecord:
    """Persist a ledger plan only when every scoped aggregate is complete."""

    if inventory.issues:
        codes = ", ".join(sorted({issue.code for issue in inventory.issues}))
        raise SourceJobMemoryMigrationInventoryError(
            f"source/job/memory inventory is inconsistent: {codes}"
        )
    return ledger.plan_dry_run(
        migration_id=migration_id,
        target_schema_version=target_schema_version,
        inventory=inventory.inventory,
        rollback_pointer=rollback_pointer,
    )


def execute_source_job_memory_fixture_migration(
    *,
    object_store_root: Path,
    jobs_database_path: Path,
    target_database_path: Path,
    ledger: SQLiteMigrationLedger,
    dry_run: MigrationRecord,
) -> SourceJobMemoryMigrationExecutionResult:
    """Copy one verified fixture into its future aggregate target.

    This is intentionally not a runtime cutover. It accepts only the sibling
    ``structured-records.sqlite3`` target under a temporary rebuild root, and
    removes every newly-created SQLite artifact if copy or validation fails.
    """

    source_root = object_store_root.expanduser().resolve(strict=False)
    jobs_path = jobs_database_path.expanduser().resolve(strict=False)
    target = target_database_path.expanduser().resolve(strict=False)
    _require_fixture_database_paths(source_root, jobs_path, target)
    artifacts = _sqlite_artifacts(target)
    if any(path.exists() for path in artifacts):
        raise SourceJobMemoryMigrationExecutionError(
            "source/job/memory migration target SQLite artifacts already exist"
        )

    namespace_id = dry_run.inventory.namespace_id
    current = scan_source_job_memory_migration_inventory(
        source_root,
        jobs_database_path=jobs_path,
        namespace_id=namespace_id,
    )
    _validate_dry_run(ledger, dry_run, current)
    try:
        records = _read_fixture_records(
            source_root,
            jobs_database_path=jobs_path,
            namespace_id=namespace_id,
        )
        repeated = scan_source_job_memory_migration_inventory(
            source_root,
            jobs_database_path=jobs_path,
            namespace_id=namespace_id,
        )
        if repeated != current:
            raise SourceJobMemoryMigrationExecutionError(
                "source/job/memory migration input changed while preparing copy"
            )

        aggregate = SQLiteSourceJobMemoryUnitOfWork(target)
        with aggregate.begin() as transaction:
            for record in records.structured:
                if record.collection == "sources":
                    _seed_source(transaction, record)
            for job in records.jobs:
                _seed_job(transaction, job)
            for record in records.structured:
                if record.collection != "sources":
                    _seed_memory(transaction, record)
            transaction.commit()
        _validate_target(target, records)
        source_count = sum(record.collection == "sources" for record in records.structured)
        memory_count = len(records.structured) - source_count
        return SourceJobMemoryMigrationExecutionResult(
            source_count=source_count,
            job_count=len(records.jobs),
            memory_count=memory_count,
            object_count=source_count + memory_count + len(records.jobs),
            input_fingerprint=current.inventory.fingerprint,
        )
    except SourceJobMemoryMigrationExecutionError:
        _remove_new_target(artifacts)
        raise
    except Exception as exc:
        _remove_new_target(artifacts)
        raise SourceJobMemoryMigrationExecutionError(
            "source/job/memory fixture migration copy failed"
        ) from exc


def _read_fixture_records(
    object_store_root: Path,
    *,
    jobs_database_path: Path,
    namespace_id: str,
) -> _FixtureRecords:
    root = object_store_root.expanduser().resolve(strict=False)
    jobs_path = jobs_database_path.expanduser().resolve(strict=False)
    _require_namespace_id(namespace_id)
    _require_job_database_path(root, jobs_path)
    store = JsonObjectStore(root, namespace_id=namespace_id)
    structured: list[_StructuredFixtureRecord] = []
    for collection in _JSON_COLLECTIONS:
        try:
            stored = read_json_object_store_collection(
                root,
                namespace_id=namespace_id,
                collection=collection,
            )
        except ObjectStorePathError as exc:
            raise SourceJobMemoryMigrationInventoryError(
                "source/job/memory JSON collection layout is invalid"
            ) from exc
        for item in stored:
            try:
                revision = store.revision(collection, item.object_id)
            except ObjectStorePathError as exc:
                raise SourceJobMemoryMigrationInventoryError(
                    "source/job/memory JSON object layout is invalid"
                ) from exc
            structured.append(
                _StructuredFixtureRecord(
                    collection=collection,
                    object_id=item.object_id,
                    payload=dict(item.payload),
                    revision=revision,
                    payload_bytes=item.payload_bytes,
                )
            )

    json_extract_memory_jobs = tuple(
        item
        for item in _read_json_jobs(root, namespace_id=namespace_id, store=store)
        if item.payload.get("job_type") == "extract_memory"
    )
    jobs = tuple(
        record
        for record in SQLiteJobStore(jobs_path).all()
        if record.payload.get("job_type") == "extract_memory"
    )
    return _FixtureRecords(
        structured=tuple(sorted(structured, key=lambda item: (item.collection, item.object_id))),
        jobs=tuple(sorted(jobs, key=lambda item: _sort_job_id(item.payload))),
        json_extract_memory_jobs=json_extract_memory_jobs,
        unsupported_memory_collections=_unsupported_memory_collections(root, namespace_id=namespace_id),
        transition_count=_transition_count(root, namespace_id=namespace_id),
    )


def _read_json_jobs(
    root: Path,
    *,
    namespace_id: str,
    store: JsonObjectStore,
) -> tuple[_StructuredFixtureRecord, ...]:
    try:
        stored = read_json_object_store_collection(root, namespace_id=namespace_id, collection="jobs")
    except ObjectStorePathError as exc:
        raise SourceJobMemoryMigrationInventoryError(
            "source/job/memory JSON Job layout is invalid"
        ) from exc
    records: list[_StructuredFixtureRecord] = []
    for item in stored:
        try:
            revision = store.revision("jobs", item.object_id)
        except ObjectStorePathError as exc:
            raise SourceJobMemoryMigrationInventoryError(
                "source/job/memory JSON Job layout is invalid"
            ) from exc
        records.append(
            _StructuredFixtureRecord(
                collection="jobs",
                object_id=item.object_id,
                payload=dict(item.payload),
                revision=revision,
                payload_bytes=item.payload_bytes,
            )
        )
    return tuple(sorted(records, key=lambda item: item.object_id))


def _inventory_from_records(namespace_id: str, records: _FixtureRecords) -> JsonObjectStoreInventory:
    collections: list[InventoryCollection] = []
    structured_by_collection = {
        collection: tuple(record for record in records.structured if record.collection == collection)
        for collection in _JSON_COLLECTIONS
    }
    for collection in (*_JSON_COLLECTIONS, "jobs"):
        if collection == "jobs":
            entries = tuple(
                (
                    _required_job_id(job.payload),
                    job.revision,
                    _canonical_payload_bytes(job.payload),
                )
                for job in records.jobs
            )
        else:
            entries = tuple(
                (record.object_id, record.revision, record.payload_bytes)
                for record in structured_by_collection[collection]
            )
        collections.append(_inventory_collection(collection, entries))
    ordered = tuple(sorted(collections, key=lambda item: item.collection))
    return JsonObjectStoreInventory(
        namespace_id=namespace_id,
        collections=ordered,
        object_count=sum(item.object_count for item in ordered),
        fingerprint=_fingerprint_parts(
            (
                namespace_id,
                *(
                    f"{item.collection}\0{item.object_count}\0{item.fingerprint}"
                    for item in ordered
                ),
            )
        ),
    )


def _inventory_collection(
    collection: str,
    entries: Sequence[tuple[str, int, bytes]],
) -> InventoryCollection:
    pairs = []
    for object_id, revision, payload_bytes in sorted(entries, key=lambda item: item[0]):
        payload_fingerprint = hashlib.sha256(payload_bytes).hexdigest()
        pairs.append(f"{object_id}\0{revision}\0{payload_fingerprint}")
    return InventoryCollection(
        collection=collection,
        object_count=len(entries),
        fingerprint=_fingerprint_parts(pairs),
    )


def _issues(records: _FixtureRecords) -> tuple[SourceJobMemoryMigrationIssue, ...]:
    issues: list[SourceJobMemoryMigrationIssue] = []
    sources: dict[str, _StructuredFixtureRecord] = {}
    for record in records.structured:
        if record.revision < 1:
            issues.append(_issue("object_revision_invalid", record.collection, record.object_id))
        payload_id = record.payload.get("id")
        if payload_id != record.object_id:
            issues.append(_issue("object_identity_mismatch", record.collection, record.object_id))
            continue
        if record.collection == "sources":
            sources[record.object_id] = record
    for record in records.structured:
        if record.collection != "sources" and record.payload.get("id") == record.object_id:
            _memory_provenance_issues(record, sources, issues)

    for collection in records.unsupported_memory_collections:
        issues.append(_issue("unsupported_memory_collection", collection, "*"))
    if records.transition_count:
        issues.append(_issue("memory_transitions_not_owned", "memory_transitions", "*"))
    for record in records.json_extract_memory_jobs:
        issues.append(_issue("legacy_json_extract_memory_job", "jobs", record.object_id))

    jobs_by_source: dict[str, int] = {}
    for job in records.jobs:
        job_id = job.payload.get("id")
        source_id = job.payload.get("source_id")
        if not isinstance(job_id, str) or not _SAFE_SEGMENT.fullmatch(job_id):
            issues.append(_issue("job_identity_invalid", "jobs", _issue_object_id(job_id)))
            continue
        if job.revision < 1:
            issues.append(_issue("job_revision_invalid", "jobs", job_id))
        if not isinstance(source_id, str) or source_id not in sources:
            issues.append(_issue("job_source_missing", "jobs", job_id))
            continue
        jobs_by_source[source_id] = jobs_by_source.get(source_id, 0) + 1
    if not sources and not records.jobs:
        issues.append(_issue("aggregate_fixture_empty", "sources", "*"))
    for source_id in sorted(sources):
        job_count = jobs_by_source.get(source_id, 0)
        if job_count == 0:
            issues.append(_issue("source_extract_job_missing", "sources", source_id))
        elif job_count > 1:
            issues.append(_issue("source_extract_job_ambiguous", "sources", source_id))
    return tuple(sorted(issues, key=lambda item: (item.code, item.collection, item.object_id)))


def _memory_provenance_issues(
    record: _StructuredFixtureRecord,
    sources: Mapping[str, _StructuredFixtureRecord],
    issues: list[SourceJobMemoryMigrationIssue],
) -> None:
    source_ids: set[str] = set()
    source_id = record.payload.get("source_id")
    if isinstance(source_id, str) and source_id:
        source_ids.add(source_id)
    source_refs = record.payload.get("source_refs")
    if isinstance(source_refs, list):
        for source_ref in source_refs:
            if isinstance(source_ref, Mapping):
                referenced = source_ref.get("source_id")
                if isinstance(referenced, str) and referenced:
                    source_ids.add(referenced)
    if not source_ids:
        issues.append(_issue("memory_provenance_missing", record.collection, record.object_id))
        return
    for referenced in sorted(source_ids):
        if referenced not in sources:
            issues.append(_issue("memory_source_missing", record.collection, record.object_id))
            return


def _validate_dry_run(
    ledger: SQLiteMigrationLedger,
    dry_run: MigrationRecord,
    current: SourceJobMemoryMigrationInventory,
) -> None:
    records = {record.migration_id: record for record in ledger.list_records()}
    if records.get(dry_run.migration_id) != dry_run or dry_run.state != "dry_run_ready":
        raise SourceJobMemoryMigrationExecutionError("source/job/memory migration dry-run is not ready")
    if current.issues:
        codes = ", ".join(sorted({issue.code for issue in current.issues}))
        raise SourceJobMemoryMigrationExecutionError(
            f"source/job/memory migration inventory is inconsistent: {codes}"
        )
    if current.inventory.fingerprint != dry_run.input_fingerprint:
        raise SourceJobMemoryMigrationExecutionError("source/job/memory migration input fingerprint changed")


def _seed_source(transaction: SQLiteSourceJobMemoryTransaction, record: _StructuredFixtureRecord) -> None:
    for expected_revision in range(record.revision):
        transaction.put_source(record.payload, expected_revision=expected_revision)


def _seed_memory(transaction: SQLiteSourceJobMemoryTransaction, record: _StructuredFixtureRecord) -> None:
    for expected_revision in range(record.revision):
        transaction.put_memory(record.collection, record.payload, expected_revision=expected_revision)


def _seed_job(transaction: SQLiteSourceJobMemoryTransaction, record: SQLiteJobRecord) -> None:
    for expected_revision in range(record.revision):
        transaction.save_job(record.payload, expected_revision=expected_revision)


def _validate_target(target: Path, records: _FixtureRecords) -> None:
    structured = SQLiteStructuredRecordStore(target)
    for source_or_memory in records.structured:
        copied = structured.read(source_or_memory.collection, source_or_memory.object_id)
        if copied is None:
            raise SourceJobMemoryMigrationExecutionError("source/job/memory migration target record is missing")
        if copied.revision != source_or_memory.revision or dict(copied.payload) != dict(source_or_memory.payload):
            raise SourceJobMemoryMigrationExecutionError("source/job/memory migration target record mismatch")
    source_jobs = {job.payload.get("id"): job for job in records.jobs}
    target_jobs = {job.payload.get("id"): job for job in SQLiteJobStore(target).all()}
    if set(target_jobs) != set(source_jobs):
        raise SourceJobMemoryMigrationExecutionError("source/job/memory migration target Job set mismatch")
    for job_id, source_job in source_jobs.items():
        target_job = target_jobs[job_id]
        if target_job.revision != source_job.revision or dict(target_job.payload) != dict(source_job.payload):
            raise SourceJobMemoryMigrationExecutionError("source/job/memory migration target Job mismatch")


def _unsupported_memory_collections(root: Path, *, namespace_id: str) -> tuple[str, ...]:
    namespace_root = root / "objects" / namespace_id
    if not namespace_root.exists():
        return ()
    if not namespace_root.is_dir() or namespace_root.is_symlink():
        raise SourceJobMemoryMigrationInventoryError("source/job/memory namespace root is invalid")
    collections: list[str] = []
    for entry in namespace_root.iterdir():
        if entry.is_symlink() or not entry.is_dir():
            raise SourceJobMemoryMigrationInventoryError("source/job/memory namespace entry is invalid")
        if (
            entry.name.startswith(("memory_", "staging_"))
            and entry.name not in (*_MEMORY_COLLECTIONS, "memory_transitions")
        ):
            collections.append(entry.name)
    return tuple(sorted(collections))


def _transition_count(root: Path, *, namespace_id: str) -> int:
    try:
        return len(read_json_object_store_collection(root, namespace_id=namespace_id, collection="memory_transitions"))
    except ObjectStorePathError as exc:
        raise SourceJobMemoryMigrationInventoryError(
            "source/job/memory transition layout is invalid"
        ) from exc


def _require_fixture_database_paths(source_root: Path, jobs_path: Path, target: Path) -> None:
    _require_job_database_path(source_root, jobs_path)
    expected_target = source_root / _TARGET_DATABASE_NAME
    if target != expected_target:
        raise SourceJobMemoryMigrationExecutionError(
            "source/job/memory migration target must be runtime structured-records.sqlite3"
        )


def _require_job_database_path(source_root: Path, jobs_path: Path) -> None:
    if jobs_path != source_root / "jobs.sqlite3":
        raise SourceJobMemoryMigrationInventoryError(
            "source/job/memory Job database must be runtime jobs.sqlite3"
        )


def _require_namespace_id(namespace_id: str) -> None:
    if not isinstance(namespace_id, str) or not _SAFE_SEGMENT.fullmatch(namespace_id):
        raise SourceJobMemoryMigrationInventoryError("namespace_id must be a safe repository segment")


def _required_job_id(payload: Mapping[str, object]) -> str:
    value = payload.get("id")
    if not isinstance(value, str) or not _SAFE_SEGMENT.fullmatch(value):
        return "invalid-job-id"
    return value


def _sort_job_id(payload: Mapping[str, object]) -> str:
    value = payload.get("id")
    return value if isinstance(value, str) else ""


def _issue_object_id(value: object) -> str:
    return value if isinstance(value, str) and value else "*"


def _issue(code: str, collection: str, object_id: str) -> SourceJobMemoryMigrationIssue:
    return SourceJobMemoryMigrationIssue(code, collection, object_id)


def _canonical_payload_bytes(payload: Mapping[str, object]) -> bytes:
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _fingerprint_parts(parts: Sequence[str]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _sqlite_artifacts(database_path: Path) -> tuple[Path, ...]:
    return database_path, Path(f"{database_path}-wal"), Path(f"{database_path}-shm")


def _remove_new_target(artifacts: tuple[Path, ...]) -> None:
    for path in reversed(artifacts):
        path.unlink(missing_ok=True)
