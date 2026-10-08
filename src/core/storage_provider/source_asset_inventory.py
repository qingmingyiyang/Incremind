from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .migration_ledger import (
    InventoryCollection,
    JsonObjectStoreInventory,
    MigrationRecord,
    SQLiteMigrationLedger,
    scan_json_object_inventory,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class SourceAssetInventory:
    combined_inventory: JsonObjectStoreInventory
    blob_count: int
    blob_fingerprint: str


def scan_source_asset_inventory(
    rebuild_root: Path,
    *,
    library_root: Path,
    namespace_id: str,
    validate_assets: bool = True,
) -> SourceAssetInventory:
    """Read a legacy fixture into a path-free JSON plus Blob inventory."""

    json_inventory = scan_json_object_inventory(rebuild_root, namespace_id=namespace_id)
    root = rebuild_root.expanduser().resolve(strict=False)
    library = library_root.expanduser().resolve(strict=False)
    sources = _records(root, namespace_id, "sources")
    assets = _records(root, namespace_id, "workbench_original_assets")
    links = _records(root, namespace_id, "source_asset_links")
    blob_pairs = _blob_pairs(library)
    if validate_assets:
        _validate_asset_records(assets, links, sources, library, dict(blob_pairs))
    blob_collection = InventoryCollection(
        collection="asset_blob_files",
        object_count=len(blob_pairs),
        fingerprint=_fingerprint_pairs(blob_pairs),
    )
    collections = tuple(sorted((*json_inventory.collections, blob_collection), key=lambda item: item.collection))
    if len({item.collection for item in collections}) != len(collections):
        raise ValueError("source asset inventory collection name conflicts with legacy JSON")
    combined = JsonObjectStoreInventory(
        namespace_id=namespace_id,
        collections=collections,
        object_count=json_inventory.object_count + blob_collection.object_count,
        fingerprint=_combined_fingerprint(namespace_id, collections),
    )
    return SourceAssetInventory(combined, blob_collection.object_count, blob_collection.fingerprint)


def plan_source_asset_migration_dry_run(
    *,
    ledger: SQLiteMigrationLedger,
    migration_id: str,
    target_schema_version: int,
    inventory: SourceAssetInventory,
    rollback_pointer: str,
) -> MigrationRecord:
    return ledger.plan_dry_run(
        migration_id=migration_id,
        target_schema_version=target_schema_version,
        inventory=inventory.combined_inventory,
        rollback_pointer=rollback_pointer,
    )


def _records(root: Path, namespace_id: str, collection: str) -> tuple[dict[str, object], ...]:
    directory = root / "objects" / namespace_id / collection
    if not directory.exists():
        return ()
    records: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
        if path.name.endswith(".meta.json") or path.is_symlink():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("source asset inventory requires readable JSON records") from exc
        if not isinstance(payload, dict):
            raise ValueError("source asset inventory requires JSON object records")
        records.append(dict(payload))
    return tuple(records)


def _blob_pairs(library_root: Path) -> tuple[tuple[str, str], ...]:
    directory = library_root / "assets" / "originals"
    if not directory.exists():
        return ()
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("source asset blob root is invalid")
    pairs: list[tuple[str, str]] = []
    for path in sorted(directory.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise ValueError("source asset inventory rejects symlinked blobs")
        if path.is_file():
            relative = (PurePosixPath("assets") / "originals" / path.relative_to(directory).as_posix()).as_posix()
            pairs.append((relative, _sha256_file(path)))
    return tuple(pairs)


def _validate_asset_records(
    assets: tuple[dict[str, object], ...],
    links: tuple[dict[str, object], ...],
    sources: tuple[dict[str, object], ...],
    library_root: Path,
    blobs: Mapping[str, str],
) -> None:
    asset_by_id: dict[str, dict[str, object]] = {}
    for asset in assets:
        asset_id = _required_str(asset, "id")
        sha256 = _required_hash(asset, "sha256")
        byte_count = asset.get("byte_count")
        if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count < 0:
            raise ValueError("source asset inventory asset byte_count is invalid")
        vault_ref = _vault_ref(asset)
        digest = blobs.get(vault_ref)
        if digest is None:
            raise ValueError("source asset inventory asset blob is missing")
        if digest != sha256:
            raise ValueError("source asset inventory asset blob hash mismatch")
        blob_path = library_root / PurePosixPath(vault_ref)
        if blob_path.stat().st_size != byte_count:
            raise ValueError("source asset inventory asset blob size mismatch")
        asset_by_id[asset_id] = asset
    source_ids = {_required_str(source, "id") for source in sources}
    for link in links:
        source_id = _required_str(link, "source_id")
        if source_id not in source_ids:
            raise ValueError("source asset inventory link references an unknown source")
        asset_id = _required_str(link, "asset_id")
        asset = asset_by_id.get(asset_id)
        if asset is None:
            raise ValueError("source asset inventory link references an unknown asset")
        if _required_hash(link, "content_hash") != _required_hash(asset, "sha256"):
            raise ValueError("source asset inventory link content hash mismatch")
        if _required_str(link, "asset_ref") != _required_str(asset, "asset_ref"):
            raise ValueError("source asset inventory link asset ref mismatch")


def _vault_ref(asset: Mapping[str, object]) -> str:
    value = _required_str(asset, "vault_ref")
    path = PurePosixPath(value)
    if value.startswith("/") or "\\" in value or any(part in {".", ".."} for part in path.parts):
        raise ValueError("source asset inventory vault_ref is invalid")
    return value


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"source asset inventory {key} is invalid")
    return value


def _required_hash(mapping: Mapping[str, object], key: str) -> str:
    value = _required_str(mapping, key)
    if not _SHA256.fullmatch(value):
        raise ValueError(f"source asset inventory {key} is invalid")
    return value


def _combined_fingerprint(namespace_id: str, collections: tuple[InventoryCollection, ...]) -> str:
    return _fingerprint_pairs(
        tuple((collection.collection, f"{collection.object_count}:{collection.fingerprint}") for collection in collections)
        + (("namespace", namespace_id),)
    )


def _fingerprint_pairs(pairs: tuple[tuple[str, str], ...]) -> str:
    digest = hashlib.sha256()
    for name, value in pairs:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
