"""Fail-closed fixture migration for generic Memory publication + Trust Audit."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from core.storage_provider import (
    InventoryCollection,
    JsonObjectStoreInventory,
    MigrationRecord,
    ObjectStorePathError,
    SQLiteMigrationLedger,
    SQLiteStructuredRecordStore,
    read_json_object_store_collection,
)


_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_LAYERS = {
    "atom": ("memory_atoms", "memory_atom_revisions"),
    "scenario": ("memory_scenarios", "memory_scenario_revisions"),
    "series_memory": ("memory_series_memory", "memory_series_memory_revisions"),
}
_COPIED_COLLECTIONS = (
    "memory_atoms",
    "memory_atom_revisions",
    "memory_scenarios",
    "memory_scenario_revisions",
    "memory_series_memory",
    "memory_series_memory_revisions",
    "memory_publications",
    "memory_transitions",
)
_STAGING_COLLECTIONS = (
    "staging_atoms",
    "staging_scenarios",
    "staging_series_memory",
    "staging_memory_publication_contexts",
)


class MemoryPublicationFixtureMigrationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MemoryPublicationFixtureMigrationIssue:
    code: str
    collection: str
    object_id: str


@dataclass(frozen=True, slots=True)
class MemoryPublicationFixtureInventory:
    inventory: JsonObjectStoreInventory
    issues: tuple[MemoryPublicationFixtureMigrationIssue, ...]

    @property
    def is_migratable(self) -> bool:
        return not self.issues


@dataclass(frozen=True, slots=True)
class MemoryPublicationFixtureMigrationResult:
    object_count: int
    input_fingerprint: str


def scan_memory_publication_fixture_inventory(
    object_store_root: Path,
    *,
    namespace_id: str,
) -> MemoryPublicationFixtureInventory:
    _segment("namespace_id", namespace_id)
    source_root = object_store_root.expanduser().resolve(strict=False)
    namespace_root = source_root / "objects" / namespace_id
    copied = {collection: _read_collection(source_root, namespace_id, collection) for collection in _COPIED_COLLECTIONS}
    records = {collection: value[0] for collection, value in copied.items()}
    staging = {collection: _read_collection(source_root, namespace_id, collection)[0] for collection in _STAGING_COLLECTIONS}
    issues = _issues(records, staging, namespace_id)
    collections = tuple(sorted((value[1] for value in copied.values()), key=lambda item: item.collection))
    inventory = JsonObjectStoreInventory(
        namespace_id=namespace_id,
        collections=collections,
        object_count=sum(item.object_count for item in collections),
        fingerprint=_fingerprint(namespace_id, collections),
    )
    if namespace_root.exists() and (namespace_root.is_symlink() or not namespace_root.is_dir()):
        raise MemoryPublicationFixtureMigrationError("memory publication fixture namespace root is invalid")
    return MemoryPublicationFixtureInventory(inventory, tuple(sorted(issues, key=_issue_key)))


def plan_memory_publication_fixture_migration_dry_run(
    *,
    ledger: SQLiteMigrationLedger,
    migration_id: str,
    target_schema_version: int,
    inventory: MemoryPublicationFixtureInventory,
    rollback_pointer: str,
) -> MigrationRecord:
    if inventory.issues:
        codes = ", ".join(sorted({item.code for item in inventory.issues}))
        raise MemoryPublicationFixtureMigrationError(
            f"Memory publication fixture inventory is inconsistent: {codes}"
        )
    return ledger.plan_dry_run(
        migration_id=migration_id,
        target_schema_version=target_schema_version,
        inventory=inventory.inventory,
        rollback_pointer=rollback_pointer,
    )


def execute_memory_publication_fixture_migration(
    *,
    object_store_root: Path,
    target_database_path: Path,
    ledger: SQLiteMigrationLedger,
    dry_run: MigrationRecord,
) -> MemoryPublicationFixtureMigrationResult:
    source_root = object_store_root.expanduser().resolve(strict=False)
    target = target_database_path.expanduser().resolve(strict=False)
    _target_outside_source(source_root, target)
    artifacts = _artifacts(target)
    if any(path.exists() for path in artifacts):
        raise MemoryPublicationFixtureMigrationError("Memory publication migration target SQLite artifacts already exist")
    current = scan_memory_publication_fixture_inventory(source_root, namespace_id=dry_run.inventory.namespace_id)
    _validate_dry_run(ledger, dry_run, current)
    source = _source_records(source_root, dry_run.inventory.namespace_id)
    if scan_memory_publication_fixture_inventory(source_root, namespace_id=dry_run.inventory.namespace_id) != current:
        raise MemoryPublicationFixtureMigrationError("Memory publication migration source changed while preparing copy")
    records = SQLiteStructuredRecordStore(target)
    try:
        with records.begin() as transaction:
            for collection in _COPIED_COLLECTIONS:
                for object_id, payload in sorted(source[collection].items()):
                    transaction.put(collection, object_id, payload, expected_revision=0)
            transaction.commit()
        _validate_target(records, source)
        return MemoryPublicationFixtureMigrationResult(
            object_count=sum(len(items) for items in source.values()),
            input_fingerprint=current.inventory.fingerprint,
        )
    except Exception as error:
        _remove_new_target(artifacts)
        if isinstance(error, MemoryPublicationFixtureMigrationError):
            raise
        raise MemoryPublicationFixtureMigrationError(str(error)) from error


def _issues(
    records: dict[str, dict[str, dict[str, object]]],
    staging: dict[str, dict[str, dict[str, object]]],
    namespace_id: str,
) -> list[MemoryPublicationFixtureMigrationIssue]:
    issues: list[MemoryPublicationFixtureMigrationIssue] = []
    for collection, values in staging.items():
        for object_id in values:
            issues.append(_issue("staging_record_present", collection, object_id))

    publications = records["memory_publications"]
    transitions = records["memory_transitions"]
    current_by_object: dict[tuple[str, str], dict[str, object]] = {}
    revisions_by_object: dict[tuple[str, str, int], dict[str, object]] = {}
    for layer, (current_collection, revisions_collection) in _LAYERS.items():
        for object_id, payload in records[current_collection].items():
            if payload.get("id") != object_id or payload.get("trust_status") != "user_confirmed":
                issues.append(_issue("current_projection_identity_or_trust_invalid", current_collection, object_id))
                continue
            revision = _positive(payload.get("revision"))
            if revision is None:
                issues.append(_issue("current_projection_revision_invalid", current_collection, object_id))
                continue
            current_by_object[(layer, object_id)] = payload
        for revision_id, payload in records[revisions_collection].items():
            object_id = payload.get("object_id")
            revision = _positive(payload.get("revision"))
            if (
                payload.get("id") != revision_id
                or not isinstance(object_id, str)
                or not object_id
                or revision is None
                or revision_id != f"{object_id}~r{revision}"
                or payload.get("layer") != layer
                or payload.get("state") not in {"published", "rolled_back"}
                or not isinstance(payload.get("payload"), Mapping)
                or not _canonical_facts(payload)
            ):
                issues.append(_issue("immutable_revision_invalid", revisions_collection, revision_id))
                continue
            revisions_by_object[(layer, object_id, revision)] = payload

    publication_by_revision: dict[tuple[str, str, int], dict[str, object]] = {}
    referenced_transitions: set[str] = set()
    for publication_id, publication in publications.items():
        layer = publication.get("layer")
        object_id = publication.get("published_object_id")
        revision = _positive(publication.get("published_revision"))
        if layer not in _LAYERS:
            issues.append(_issue("cross_aggregate_or_legacy_publication", "memory_publications", publication_id))
            continue
        if (
            publication.get("id") != publication_id
            or publication.get("publication_id") != publication_id
            or not isinstance(object_id, str)
            or not object_id
            or revision is None
            or publication.get("object_type") != layer
            or not _canonical_facts(publication)
        ):
            issues.append(_issue("publication_canonical_facts_invalid", "memory_publications", publication_id))
            continue
        key = (layer, object_id, revision)
        if key in publication_by_revision:
            issues.append(_issue("publication_revision_duplicate", "memory_publications", publication_id))
            continue
        publication_by_revision[key] = publication
        published_revision = revisions_by_object.get((layer, object_id, revision))
        if (
            published_revision is None
            or published_revision.get("state") != "published"
            or published_revision.get("publication_id") != publication_id
        ):
            issues.append(_issue("publication_revision_missing_or_mismatch", "memory_publications", publication_id))
        transition_id = _reference_id(publication.get("transition_ref"), namespace_id)
        transition = transitions.get(transition_id) if transition_id is not None else None
        if not _valid_transition(transition, transition_id, layer, object_id, revision, "confirm"):
            issues.append(_issue("publication_transition_missing_or_mismatch", "memory_publications", publication_id))
        elif transition_id is not None:
            referenced_transitions.add(transition_id)
        if publication.get("status") == "published":
            current = current_by_object.get((layer, object_id))
            if current is None or current.get("revision") != revision:
                issues.append(_issue("published_current_projection_missing_or_mismatch", "memory_publications", publication_id))
        elif publication.get("status") == "rolled_back":
            rollback_revision = _positive(publication.get("rollback_revision"))
            rollback_id = _reference_id(publication.get("rollback_transition_ref"), namespace_id)
            rollback = transitions.get(rollback_id) if rollback_id is not None else None
            if (
                rollback_revision is None
                or current_by_object.get((layer, object_id)) is not None
                or (layer, object_id, rollback_revision) not in revisions_by_object
                or revisions_by_object[(layer, object_id, rollback_revision)].get("state") != "rolled_back"
                or not _valid_transition(rollback, rollback_id, layer, object_id, rollback_revision, "demote")
            ):
                issues.append(_issue("rollback_history_or_transition_missing", "memory_publications", publication_id))
            elif rollback_id is not None:
                referenced_transitions.add(rollback_id)
        elif publication.get("status") == "superseded":
            successor_id = publication.get("superseded_by_publication_id")
            successor_revision = _positive(publication.get("superseded_revision"))
            if not isinstance(successor_id, str) or successor_revision != revision + 1:
                issues.append(_issue("superseded_publication_facts_invalid", "memory_publications", publication_id))
        else:
            issues.append(_issue("publication_status_invalid", "memory_publications", publication_id))

    for key, current in current_by_object.items():
        layer, object_id = key
        revision = _positive(current.get("revision"))
        if revision is None or (layer, object_id, revision) not in publication_by_revision:
            issues.append(_issue("current_publication_missing", _LAYERS[layer][0], object_id))
        elif (layer, object_id, revision) not in revisions_by_object:
            issues.append(_issue("current_revision_missing", _LAYERS[layer][0], object_id))
        elif revisions_by_object[(layer, object_id, revision)].get("payload") != current:
            issues.append(_issue("current_revision_payload_mismatch", _LAYERS[layer][0], object_id))
    for (layer, object_id, _revision), revision_payload in revisions_by_object.items():
        publication_id = revision_payload.get("publication_id")
        publication = publications.get(publication_id) if isinstance(publication_id, str) else None
        if (
            publication is None
            or publication.get("layer") != layer
            or publication.get("published_object_id") != object_id
            or publication.get("published_revision") != _revision
        ):
            issues.append(_issue("orphan_immutable_revision", _LAYERS[layer][1], str(revision_payload["id"])))
    for layer, object_id in {(item[0], item[1]) for item in revisions_by_object}:
        revisions = sorted(
            revision
            for candidate_layer, candidate_object_id, revision in revisions_by_object
            if candidate_layer == layer and candidate_object_id == object_id
        )
        if revisions != list(range(1, len(revisions) + 1)):
            issues.append(_issue("immutable_revision_chain_invalid", _LAYERS[layer][1], object_id))
            continue
        first = revisions_by_object[(layer, object_id, 1)]
        latest = revisions_by_object[(layer, object_id, revisions[-1])]
        first_publication = publication_by_revision.get((layer, object_id, 1))
        latest_publication = publication_by_revision.get((layer, object_id, revisions[-1]))
        if first_publication is None or first.get("state") != "published" or first.get("publication_id") != first_publication.get("id"):
            issues.append(_issue("immutable_revision_initial_publication_mismatch", _LAYERS[layer][1], object_id))
        if latest_publication is None:
            issues.append(_issue("immutable_revision_publication_missing", _LAYERS[layer][1], object_id))
            continue
        for revision in revisions[:-1]:
            successor_revision = revisions_by_object.get((layer, object_id, revision + 1))
            publication = publication_by_revision.get((layer, object_id, revision))
            if (
                publication is None
                or successor_revision is None
                or successor_revision.get("previous_revision_id") != f"{object_id}~r{revision}"
                or publication.get("status") != "superseded"
                or publication.get("superseded_revision") != revision + 1
                or publication.get("superseded_by_publication_id")
                != publication_by_revision.get((layer, object_id, revision + 1), {}).get("id")
            ):
                issues.append(_issue("supersession_chain_invalid", _LAYERS[layer][1], object_id))
                break
        if latest_publication.get("status") == "published" and revisions[-1] != latest_publication.get("published_revision"):
            issues.append(_issue("published_revision_chain_mismatch", _LAYERS[layer][1], object_id))
        if latest_publication.get("status") == "rolled_back" and (
            latest.get("state") != "rolled_back" or latest.get("revision") != latest_publication.get("rollback_revision")
        ):
            issues.append(_issue("rollback_revision_chain_mismatch", _LAYERS[layer][1], object_id))
    for transition_id, transition in transitions.items():
        if transition_id not in referenced_transitions:
            issues.append(_issue("unreferenced_or_legacy_transition", "memory_transitions", transition_id))
        elif transition.get("id") != transition_id or not _canonical_transition(transition):
            issues.append(_issue("transition_canonical_facts_invalid", "memory_transitions", transition_id))
    return issues


def _canonical_facts(payload: Mapping[str, object]) -> bool:
    required = {
        "source_candidate_id",
        "review_ref",
        "reviewer",
        "reviewed_at",
        "policy_id",
        "source_refs",
        "evidence_refs",
        "published_at",
    }
    return (
        required <= set(payload)
        and payload.get("reviewer") == "user"
        and payload.get("policy_id") == "local-manual-v1"
        and _refs(payload.get("source_refs"))
        and _refs(payload.get("evidence_refs"))
    )


def _canonical_transition(payload: Mapping[str, object]) -> bool:
    required = {
        "schema_version",
        "id",
        "object_type",
        "object_id",
        "transition_type",
        "from_trust_status",
        "to_trust_status",
        "from_revision",
        "to_revision",
        "actor",
        "reason",
        "evidence_refs",
        "created_at",
    }
    return required <= set(payload) and payload.get("actor") == "user" and isinstance(payload.get("evidence_refs"), list)


def _valid_transition(
    transition: Mapping[str, object] | None,
    transition_id: str | None,
    layer: str,
    object_id: str,
    revision: int,
    transition_type: str,
) -> bool:
    if transition is None or transition_id is None or not _canonical_transition(transition):
        return False
    if (
        transition.get("id") != transition_id
        or transition.get("object_type") != layer
        or transition.get("object_id") != object_id
        or transition.get("transition_type") != transition_type
    ):
        return False
    if transition_type == "confirm":
        return transition.get("to_revision") == revision and transition.get("to_trust_status") == "user_confirmed"
    return transition.get("to_revision") == revision and transition.get("to_trust_status") == "system_generated"


def _reference_id(value: object, namespace_id: str) -> str | None:
    prefix = f"crp://{namespace_id}/memory-transitions/"
    if not isinstance(value, str) or not value.startswith(prefix) or not value.endswith(".json"):
        return None
    value = value[len(prefix) : -5]
    return value if _SAFE.fullmatch(value) else None


def _refs(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, Mapping)
        and isinstance(item.get("source_id"), str)
        and bool(item["source_id"])
        and isinstance(item.get("locator"), str)
        and bool(item["locator"])
        for item in value
    )


def _source_records(source_root: Path, namespace_id: str) -> dict[str, dict[str, dict[str, object]]]:
    return {collection: _read_collection(source_root, namespace_id, collection)[0] for collection in _COPIED_COLLECTIONS}


def _read_collection(
    source_root: Path,
    namespace_id: str,
    collection: str,
) -> tuple[dict[str, dict[str, object]], InventoryCollection]:
    _segment("collection", collection)
    try:
        stored = read_json_object_store_collection(source_root, namespace_id=namespace_id, collection=collection)
    except ObjectStorePathError as error:
        raise MemoryPublicationFixtureMigrationError(str(error)) from error
    records: dict[str, dict[str, object]] = {}
    fingerprints: list[str] = []
    for record in stored:
        records[record.object_id] = dict(record.payload)
        fingerprints.append(f"{record.object_id}.json\0{hashlib.sha256(record.payload_bytes).hexdigest()}")
    return records, InventoryCollection(collection, len(records), _fingerprint_parts(fingerprints))


def _validate_dry_run(
    ledger: SQLiteMigrationLedger,
    dry_run: MigrationRecord,
    current: MemoryPublicationFixtureInventory,
) -> None:
    records = {record.migration_id: record for record in ledger.list_records()}
    if records.get(dry_run.migration_id) != dry_run or dry_run.state != "dry_run_ready":
        raise MemoryPublicationFixtureMigrationError("Memory publication migration dry-run is not ready")
    if current.issues:
        codes = ", ".join(sorted({item.code for item in current.issues}))
        raise MemoryPublicationFixtureMigrationError(f"Memory publication fixture inventory is inconsistent: {codes}")
    if current.inventory.fingerprint != dry_run.input_fingerprint:
        raise MemoryPublicationFixtureMigrationError("Memory publication migration dry-run fingerprint changed")


def _validate_target(records: SQLiteStructuredRecordStore, source: dict[str, dict[str, dict[str, object]]]) -> None:
    for collection, expected in source.items():
        actual = {record.object_id: dict(record.payload) for record in records.list(collection)}
        if actual != expected:
            raise MemoryPublicationFixtureMigrationError(f"Memory publication target {collection} mismatch")


def _target_outside_source(source_root: Path, target: Path) -> None:
    try:
        target.relative_to(source_root)
    except ValueError:
        return
    raise MemoryPublicationFixtureMigrationError("Memory publication migration target cannot be inside source root")


def _artifacts(target: Path) -> tuple[Path, ...]:
    return target, Path(f"{target}-wal"), Path(f"{target}-shm")


def _remove_new_target(artifacts: tuple[Path, ...]) -> None:
    for artifact in reversed(artifacts):
        artifact.unlink(missing_ok=True)


def _positive(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _fingerprint(namespace_id: str, collections: tuple[InventoryCollection, ...]) -> str:
    return _fingerprint_parts(
        [namespace_id, *(f"{item.collection}\0{item.object_count}\0{item.fingerprint}" for item in collections)]
    )


def _fingerprint_parts(parts: list[str]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _issue(code: str, collection: str, object_id: str) -> MemoryPublicationFixtureMigrationIssue:
    return MemoryPublicationFixtureMigrationIssue(code, collection, object_id)


def _issue_key(item: MemoryPublicationFixtureMigrationIssue) -> tuple[str, str, str]:
    return item.code, item.collection, item.object_id


def _segment(label: str, value: object) -> None:
    if not isinstance(value, str) or not _SAFE.fullmatch(value):
        raise MemoryPublicationFixtureMigrationError(f"{label} must be a safe repository segment")
