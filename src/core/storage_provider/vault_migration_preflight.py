from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .migration_ledger import (
    InventoryCollection,
    JsonObjectStoreInventory,
    MigrationRecord,
    SQLiteMigrationLedger,
)


class VaultMigrationPreflightError(ValueError):
    """Raised when a Vault migration cannot be planned safely."""


class VaultMigrationPreflightConflict(VaultMigrationPreflightError):
    """Raised when source/target roots are unsafe for a dry-run."""


_MARKERS: tuple[tuple[str, str], ...] = (
    ("rebuild_data", ".rebuild-data"),
    ("library", "library"),
    ("data", "data"),
    ("config", "config"),
)


@dataclass(frozen=True, slots=True)
class VaultMigrationPreflight:
    migration: MigrationRecord
    source_fingerprint: str
    source_file_count: int
    rollback_plan: str


def build_vault_migration_preflight(
    *,
    legacy_root: Path,
    target_vault_root: Path,
    ledger: SQLiteMigrationLedger,
    migration_id: str,
    target_schema_version: int,
    backup_pointer: str,
    now: str | None = None,
) -> VaultMigrationPreflight:
    """Plan a data migration without reading or writing the target Vault.

    The returned plan is not an authorization to copy files. A future executor
    must revalidate the fingerprint under a dedicated lease and implement its
    own backup verification and rollback operation.
    """

    source = legacy_root.expanduser().resolve(strict=False)
    target = target_vault_root.expanduser().resolve(strict=False)
    _require_distinct_roots(source, target)
    if _has_entries(target):
        raise VaultMigrationPreflightConflict("vault migration target is not empty")
    inventory = scan_legacy_vault_inventory(source)
    if inventory.object_count == 0:
        raise VaultMigrationPreflightError("legacy vault has no migration markers")
    if not isinstance(backup_pointer, str) or not backup_pointer.strip() or len(backup_pointer) > 256:
        raise VaultMigrationPreflightError("vault migration requires a bounded backup pointer")
    rollback_plan = f"restore:{backup_pointer.strip()}"
    migration = ledger.plan_dry_run(
        migration_id=migration_id,
        target_schema_version=target_schema_version,
        inventory=inventory,
        rollback_pointer=rollback_plan,
        now=now,
    )
    return VaultMigrationPreflight(
        migration=migration,
        source_fingerprint=inventory.fingerprint,
        source_file_count=inventory.object_count,
        rollback_plan=rollback_plan,
    )


def scan_legacy_vault_inventory(legacy_root: Path) -> JsonObjectStoreInventory:
    """Read marker file names and digests without retaining content or paths."""

    root = legacy_root.expanduser().resolve(strict=False)
    collections: list[InventoryCollection] = []
    for collection, marker in _MARKERS:
        marker_root = root / marker
        if not marker_root.exists():
            continue
        if marker_root.is_symlink():
            raise VaultMigrationPreflightError("vault migration inventory rejects symlinked markers")
        entries = _marker_files(marker_root)
        collections.append(
            InventoryCollection(
                collection=collection,
                object_count=len(entries),
                fingerprint=_fingerprint_entries(entries),
            )
        )
    collections.sort(key=lambda collection: collection.collection)
    object_count = sum(collection.object_count for collection in collections)
    fingerprint = _fingerprint_entries(
        (collection.collection, f"{collection.object_count}:{collection.fingerprint}")
        for collection in collections
    )
    return JsonObjectStoreInventory(
        namespace_id="vault",
        collections=tuple(collections),
        object_count=object_count,
        fingerprint=fingerprint,
    )


def _marker_files(marker_root: Path) -> tuple[tuple[str, str], ...]:
    if marker_root.is_file():
        return ((marker_root.name, _sha256_file(marker_root)),)
    if not marker_root.is_dir():
        raise VaultMigrationPreflightError("vault migration marker must be a file or directory")
    entries: list[tuple[str, str]] = []
    for path in sorted(marker_root.rglob("*"), key=lambda value: value.as_posix()):
        if path.is_symlink():
            raise VaultMigrationPreflightError("vault migration inventory rejects symlinked entries")
        if path.is_file():
            entries.append((path.relative_to(marker_root).as_posix(), _sha256_file(path)))
    return tuple(entries)


def _require_distinct_roots(source: Path, target: Path) -> None:
    if source == target:
        raise VaultMigrationPreflightConflict("vault migration source and target must differ")
    try:
        target.relative_to(source)
    except ValueError:
        pass
    else:
        raise VaultMigrationPreflightConflict("vault migration target cannot be inside source")
    try:
        source.relative_to(target)
    except ValueError:
        return
    raise VaultMigrationPreflightConflict("vault migration source cannot be inside target")


def _has_entries(path: Path) -> bool:
    if not path.exists():
        return False
    if not path.is_dir() or path.is_symlink():
        raise VaultMigrationPreflightConflict("vault migration target must be an empty directory")
    return any(path.iterdir())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint_entries(entries) -> str:
    digest = hashlib.sha256()
    for name, fingerprint in entries:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(fingerprint.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()
