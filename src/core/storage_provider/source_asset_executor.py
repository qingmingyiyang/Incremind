from __future__ import annotations

import hashlib
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .migration_ledger import MigrationRecord, SQLiteMigrationLedger
from .source_asset_inventory import scan_source_asset_inventory
from .source_asset_sqlite import SQLiteSourceAssetMappingAdapter, SQLiteSourceAssetMappingResult


class SourceAssetMigrationExecutionError(ValueError):
    """Raised when a temporary fixture migration cannot complete safely."""


@dataclass(frozen=True, slots=True)
class SourceAssetMigrationExecutionResult:
    copied_blob_refs: tuple[str, ...]
    mapping: SQLiteSourceAssetMappingResult


def execute_source_asset_fixture_migration(
    *,
    rebuild_root: Path,
    legacy_library_root: Path,
    target_library_root: Path,
    ledger: SQLiteMigrationLedger,
    dry_run: MigrationRecord,
    assets: Sequence[Mapping[str, object]],
    links: Sequence[Mapping[str, object]],
    adapter: SQLiteSourceAssetMappingAdapter,
) -> SourceAssetMigrationExecutionResult:
    """Copy a verified temporary fixture, never a pre-existing target Vault."""

    target = target_library_root.expanduser().resolve(strict=False)
    legacy_library = legacy_library_root.expanduser().resolve(strict=False)
    _require_distinct_roots(legacy_library, target)
    if target.exists() and any(target.iterdir()):
        raise SourceAssetMigrationExecutionError("source asset migration target is not empty")
    current = scan_source_asset_inventory(rebuild_root, library_root=legacy_library_root, namespace_id=dry_run.inventory.namespace_id)
    _validate_dry_run(ledger, dry_run, current.combined_inventory.fingerprint)
    created_root = not target.exists()
    copied: list[Path] = []
    try:
        blob_sources = _blob_sources(legacy_library, assets)
        for sha256, source in sorted(blob_sources.items()):
            destination = target / "assets" / "blobs" / sha256[:2] / sha256
            destination.parent.mkdir(parents=True, exist_ok=True)
            _copy_verified(source, destination, sha256)
            copied.append(destination)
        mapping = adapter.stage_legacy_mappings(assets=assets, links=links)
        return SourceAssetMigrationExecutionResult(
            copied_blob_refs=tuple(f"assets/blobs/{sha256[:2]}/{sha256}" for sha256 in sorted(blob_sources)),
            mapping=mapping,
        )
    except Exception as exc:
        for path in reversed(copied):
            path.unlink(missing_ok=True)
        if created_root:
            shutil.rmtree(target, ignore_errors=True)
        if isinstance(exc, SourceAssetMigrationExecutionError):
            raise
        raise SourceAssetMigrationExecutionError(str(exc)) from exc


def _validate_dry_run(ledger: SQLiteMigrationLedger, dry_run: MigrationRecord, fingerprint: str) -> None:
    records = {record.migration_id: record for record in ledger.list_records()}
    current = records.get(dry_run.migration_id)
    if current != dry_run or dry_run.state != "dry_run_ready":
        raise SourceAssetMigrationExecutionError("source asset migration dry-run is not ready")
    if dry_run.input_fingerprint != fingerprint:
        raise SourceAssetMigrationExecutionError("source asset migration input fingerprint changed")


def _require_distinct_roots(source: Path, target: Path) -> None:
    if source == target:
        raise SourceAssetMigrationExecutionError("source asset migration source and target must differ")
    try:
        target.relative_to(source)
    except ValueError:
        pass
    else:
        raise SourceAssetMigrationExecutionError("source asset migration target cannot be inside source")
    try:
        source.relative_to(target)
    except ValueError:
        return
    raise SourceAssetMigrationExecutionError("source asset migration source cannot be inside target")


def _blob_sources(library_root: Path, assets: Sequence[Mapping[str, object]]) -> dict[str, Path]:
    sources: dict[str, Path] = {}
    for asset in assets:
        sha256 = asset.get("sha256")
        vault_ref = asset.get("vault_ref")
        if not isinstance(sha256, str) or len(sha256) != 64 or not isinstance(vault_ref, str):
            raise SourceAssetMigrationExecutionError("source asset migration asset is invalid")
        path_ref = PurePosixPath(vault_ref)
        if vault_ref.startswith("/") or "\\" in vault_ref or any(part in {".", ".."} for part in path_ref.parts):
            raise SourceAssetMigrationExecutionError("source asset migration vault_ref is invalid")
        source = library_root.expanduser().resolve(strict=False) / path_ref
        if not source.exists() or not source.is_file() or source.is_symlink():
            raise SourceAssetMigrationExecutionError("source asset migration source blob is unavailable")
        if _sha256_file(source) != sha256:
            raise SourceAssetMigrationExecutionError("source asset migration source blob hash mismatch")
        sources.setdefault(sha256, source)
    return sources


def _copy_verified(source: Path, destination: Path, sha256: str) -> None:
    temporary = destination.with_suffix(".tmp")
    shutil.copyfile(source, temporary)
    if _sha256_file(temporary) != sha256:
        temporary.unlink(missing_ok=True)
        raise SourceAssetMigrationExecutionError("source asset migration target blob hash mismatch")
    temporary.replace(destination)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
