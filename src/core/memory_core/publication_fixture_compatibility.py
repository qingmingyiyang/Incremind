"""Read-only JSON/SQLite comparison for canonical Memory publication fixtures."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .publication_fixture_migration import (
    _COPIED_COLLECTIONS,
    _source_records,
    scan_memory_publication_fixture_inventory,
)


class MemoryPublicationFixtureCompatibilityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MemoryPublicationFixtureCompatibilityReport:
    compared_collections: tuple[str, ...]
    object_count: int


def compare_memory_publication_fixture(
    *,
    object_store_root: Path,
    target_database_path: Path,
    namespace_id: str,
) -> MemoryPublicationFixtureCompatibilityReport:
    source_root = object_store_root.expanduser().resolve(strict=False)
    target = target_database_path.expanduser().resolve(strict=False)
    if not target.is_file():
        raise MemoryPublicationFixtureCompatibilityError("Memory publication compatibility target is missing")
    inventory = scan_memory_publication_fixture_inventory(source_root, namespace_id=namespace_id)
    if inventory.issues:
        codes = ", ".join(sorted({item.code for item in inventory.issues}))
        raise MemoryPublicationFixtureCompatibilityError(
            f"Memory publication compatibility source inventory is inconsistent: {codes}"
        )
    source = _source_records(source_root, namespace_id)
    target_records = _target_records(target)
    if set(target_records) != set(_COPIED_COLLECTIONS):
        raise MemoryPublicationFixtureCompatibilityError("Memory publication compatibility target collection set mismatch")
    for collection in _COPIED_COLLECTIONS:
        expected = source[collection]
        actual = target_records[collection]
        if set(actual) != set(expected):
            raise MemoryPublicationFixtureCompatibilityError(
                f"Memory publication compatibility object set mismatch: {collection}"
            )
        for object_id, payload in expected.items():
            actual_payload, actual_revision = actual[object_id]
            if actual_revision != 1:
                raise MemoryPublicationFixtureCompatibilityError(
                    f"Memory publication compatibility SQLite revision mismatch: {collection}/{object_id}"
                )
            if actual_payload != payload:
                raise MemoryPublicationFixtureCompatibilityError(
                    f"Memory publication compatibility payload mismatch: {collection}/{object_id}"
                )
    return MemoryPublicationFixtureCompatibilityReport(
        compared_collections=_COPIED_COLLECTIONS,
        object_count=sum(len(values) for values in source.values()),
    )


def _target_records(target: Path) -> dict[str, dict[str, tuple[dict[str, object], int]]]:
    try:
        connection = sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True)
    except (OSError, sqlite3.Error) as error:
        raise MemoryPublicationFixtureCompatibilityError("Memory publication compatibility target cannot be opened read-only") from error
    try:
        rows = connection.execute(
            "SELECT collection, object_id, payload_json, revision FROM crp_structured_records ORDER BY collection, object_id"
        ).fetchall()
    except sqlite3.Error as error:
        raise MemoryPublicationFixtureCompatibilityError("Memory publication compatibility target schema is invalid") from error
    finally:
        connection.close()
    result: dict[str, dict[str, tuple[dict[str, object], int]]] = {}
    for collection, object_id, payload_json, revision in rows:
        if not isinstance(collection, str) or not isinstance(object_id, str) or not isinstance(payload_json, str):
            raise MemoryPublicationFixtureCompatibilityError("Memory publication compatibility target row is invalid")
        if not isinstance(revision, int) or revision < 1:
            raise MemoryPublicationFixtureCompatibilityError("Memory publication compatibility target revision is invalid")
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as error:
            raise MemoryPublicationFixtureCompatibilityError("Memory publication compatibility target payload is invalid") from error
        if not isinstance(payload, dict):
            raise MemoryPublicationFixtureCompatibilityError("Memory publication compatibility target payload is invalid")
        collection_records = result.setdefault(collection, {})
        if object_id in collection_records:
            raise MemoryPublicationFixtureCompatibilityError("Memory publication compatibility target identity is duplicated")
        collection_records[object_id] = (dict(payload), revision)
    return result
