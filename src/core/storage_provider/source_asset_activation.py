from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .aggregate_authority import (
    AggregateAuthorityEvidence,
    AggregateAuthorityTransition,
    SQLiteAggregateAuthorityStore,
)
from .runtime import JsonObjectStore
from .source_asset_authority import read_source_asset_authority
from .source_asset_inventory import scan_source_asset_inventory
from .sqlite_uow import SQLiteStructuredRecordStore
from .vault_backup_restore import compare_vault_to_backup


AUTHORITY_DATABASE_NAME = "aggregate-authority.sqlite3"
STRUCTURED_DATABASE_NAME = "structured-records.sqlite3"
TARGET_IDENTITY = "sqlite:structured-records-v1"
SOURCE_ASSET_AUTHORITY_MEMBERS = (
    "asset_blobs",
    "original_assets",
    "source_asset_links",
)


class SourceAssetActivationError(ValueError):
    """Raised when a staged Source Asset Vault cannot be activated safely."""


@dataclass(frozen=True, slots=True)
class SourceAssetActivationReceipt:
    migration_id: str
    namespace_id: str
    source_fingerprint: str
    target_fingerprint: str
    backup_fingerprint: str
    blob_count: int
    asset_count: int
    link_count: int
    state: str
    idempotent: bool


def activate_staged_source_asset_vault(
    *,
    source_vault_root: Path,
    target_vault_root: Path,
    backup_snapshot_root: Path,
    namespace_id: str,
    migration_id: str,
    fault_hook: Callable[[str], None] | None = None,
) -> SourceAssetActivationReceipt:
    """Activate a verified staged clone; never migrate the source in place."""

    source = source_vault_root.expanduser().resolve(strict=True)
    target = target_vault_root.expanduser().resolve(strict=True)
    _require_disjoint(source, target)
    backup = compare_vault_to_backup(
        snapshot_root=backup_snapshot_root,
        vault_root=source,
    )
    source_inventory = scan_source_asset_inventory(
        source / ".rebuild-data",
        library_root=source / "library",
        namespace_id=namespace_id,
    )
    target_inventory = scan_source_asset_inventory(
        target / ".rebuild-data",
        library_root=target / "library",
        namespace_id=namespace_id,
    )
    if (
        source_inventory.combined_inventory.fingerprint
        != target_inventory.combined_inventory.fingerprint
    ):
        raise SourceAssetActivationError(
            "staged Source Asset clone does not match verified source inventory"
        )

    json_store = JsonObjectStore(
        target / ".rebuild-data",
        legacy_root=target / "library",
    )
    records_path = target / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    if not records_path.exists():
        raise SourceAssetActivationError(
            "staged Source Asset structured target is missing"
        )
    records = SQLiteStructuredRecordStore(records_path)
    json_snapshot = read_source_asset_authority(
        json_store=json_store,
        sqlite_records=None,
        authority_identity="json:object-store-v1",
    )
    sqlite_snapshot = read_source_asset_authority(
        json_store=json_store,
        sqlite_records=records,
        authority_identity=TARGET_IDENTITY,
    )
    if (
        json_snapshot.assets != sqlite_snapshot.assets
        or json_snapshot.links != sqlite_snapshot.links
        or tuple(
            (blob.sha256, blob.legacy_vault_refs)
            for blob in json_snapshot.blobs
        )
        != tuple(
            (blob.sha256, blob.legacy_vault_refs)
            for blob in sqlite_snapshot.blobs
        )
    ):
        raise SourceAssetActivationError(
            "staged Source Asset mappings do not match JSON authority"
        )
    _verify_canonical_blobs(target / "library", sqlite_snapshot)
    target_fingerprint = _target_fingerprint(sqlite_snapshot)
    evidence = AggregateAuthorityEvidence(
        migration_id=migration_id,
        source_fingerprint=source_inventory.combined_inventory.fingerprint,
        target_fingerprint=target_fingerprint,
        target_identity=TARGET_IDENTITY,
    )
    authority = SQLiteAggregateAuthorityStore(
        target / ".rebuild-data" / AUTHORITY_DATABASE_NAME
    )
    current = tuple(
        authority.get(namespace_id, member)
        for member in SOURCE_ASSET_AUTHORITY_MEMBERS
    )
    if all(item is not None and item.state == "sqlite_active" for item in current):
        if any(item.evidence != evidence for item in current if item is not None):
            raise SourceAssetActivationError(
                "active Source Asset authority evidence does not match request"
            )
        _require_markers(
            records,
            namespace_id=namespace_id,
            evidence=evidence,
            backup_snapshot_id=backup.snapshot_id,
            backup_fingerprint=backup.source_fingerprint,
        )
        from ..aggregate_repository_factory import AggregateRepositoryFactory

        AggregateRepositoryFactory(
            runtime_root=target,
            namespace_id=namespace_id,
            json_store=json_store,
        ).source_asset_authority_resolution()
        return _receipt(
            evidence,
            namespace_id,
            backup.source_fingerprint,
            sqlite_snapshot,
            idempotent=True,
        )
    if all(item is not None and item.state == "json_active" for item in current):
        created = tuple(item for item in current if item is not None)
    elif any(item is not None for item in current):
        raise SourceAssetActivationError(
            "Source Asset authority is not in a clean activation state"
        )
    else:
        created = tuple(
            authority.create_json_active(
                namespace_id=namespace_id,
                aggregate=member,
                reason="Source Asset activation baseline",
            )
            for member in SOURCE_ASSET_AUTHORITY_MEMBERS
        )
    _prepare_markers(
        records,
        namespace_id=namespace_id,
        evidence=evidence,
        backup_snapshot_id=backup.snapshot_id,
        backup_fingerprint=backup.source_fingerprint,
    )
    staged = authority.transition_many(
        tuple(
            AggregateAuthorityTransition(
                namespace_id=namespace_id,
                aggregate=item.aggregate,
                expected_revision=item.revision,
                to_state="sqlite_staged",
                reason="Source Asset mappings and blobs verified",
                evidence=evidence,
            )
            for item in created
        )
    )
    try:
        if fault_hook is not None:
            fault_hook("sqlite_staged")
        authority.transition_many(
            tuple(
                AggregateAuthorityTransition(
                    namespace_id=namespace_id,
                    aggregate=item.aggregate,
                    expected_revision=item.revision,
                    to_state="sqlite_active",
                    reason="Source Asset compound activated",
                    evidence=evidence,
                )
                for item in staged
            )
        )
    except Exception as error:
        try:
            authority.transition_many(
                tuple(
                    AggregateAuthorityTransition(
                        namespace_id=namespace_id,
                        aggregate=item.aggregate,
                        expected_revision=item.revision,
                        to_state="json_active",
                        reason="Source Asset activation aborted before cutover",
                    )
                    for item in staged
                )
            )
        except Exception as rollback_error:
            raise SourceAssetActivationError(
                "Source Asset activation failed and staged rollback did not complete"
            ) from rollback_error
        raise SourceAssetActivationError(
            "Source Asset activation aborted before cutover"
        ) from error

    from ..aggregate_repository_factory import AggregateRepositoryFactory

    AggregateRepositoryFactory(
        runtime_root=target,
        namespace_id=namespace_id,
        json_store=json_store,
    ).source_asset_authority_resolution()
    return _receipt(
        evidence,
        namespace_id,
        backup.source_fingerprint,
        sqlite_snapshot,
        idempotent=False,
    )


def _write_markers(
    records: SQLiteStructuredRecordStore,
    *,
    namespace_id: str,
    evidence: AggregateAuthorityEvidence,
    backup_snapshot_id: str,
    backup_fingerprint: str,
) -> None:
    with records.begin() as uow:
        for member in SOURCE_ASSET_AUTHORITY_MEMBERS:
            uow.put(
                "aggregate_authority_targets",
                f"{namespace_id}~{member}",
                _marker_payload(
                    namespace_id,
                    member,
                    evidence,
                    backup_snapshot_id,
                    backup_fingerprint,
                ),
                expected_revision=0,
            )
        uow.commit()


def _prepare_markers(
    records: SQLiteStructuredRecordStore,
    *,
    namespace_id: str,
    evidence: AggregateAuthorityEvidence,
    backup_snapshot_id: str,
    backup_fingerprint: str,
) -> None:
    existing = tuple(
        records.read(
            "aggregate_authority_targets",
            f"{namespace_id}~{member}",
        )
        for member in SOURCE_ASSET_AUTHORITY_MEMBERS
    )
    if not any(item is not None for item in existing):
        _write_markers(
            records,
            namespace_id=namespace_id,
            evidence=evidence,
            backup_snapshot_id=backup_snapshot_id,
            backup_fingerprint=backup_fingerprint,
        )
        return
    if not all(item is not None for item in existing):
        raise SourceAssetActivationError(
            "Source Asset activation target markers are incomplete"
        )
    _require_markers(
        records,
        namespace_id=namespace_id,
        evidence=evidence,
        backup_snapshot_id=backup_snapshot_id,
        backup_fingerprint=backup_fingerprint,
    )


def _require_markers(
    records: SQLiteStructuredRecordStore,
    *,
    namespace_id: str,
    evidence: AggregateAuthorityEvidence,
    backup_snapshot_id: str,
    backup_fingerprint: str,
) -> None:
    for member in SOURCE_ASSET_AUTHORITY_MEMBERS:
        marker = records.read(
            "aggregate_authority_targets",
            f"{namespace_id}~{member}",
        )
        expected = _marker_payload(
            namespace_id,
            member,
            evidence,
            backup_snapshot_id,
            backup_fingerprint,
        )
        if marker is None or marker.payload != expected:
            raise SourceAssetActivationError(
                "active Source Asset authority marker does not match request"
            )


def _marker_payload(
    namespace_id: str,
    member: str,
    evidence: AggregateAuthorityEvidence,
    backup_snapshot_id: str,
    backup_fingerprint: str,
) -> dict[str, object]:
    return {
        "namespace_id": namespace_id,
        "aggregate": member,
        "target_identity": evidence.target_identity,
        "source_fingerprint": evidence.source_fingerprint,
        "target_fingerprint": evidence.target_fingerprint,
        "migration_id": evidence.migration_id,
        "backup_snapshot_id": backup_snapshot_id,
        "backup_fingerprint": backup_fingerprint,
    }


def _verify_canonical_blobs(library: Path, snapshot) -> None:
    for blob in snapshot.blobs:
        relative = PurePosixPath(blob.active_vault_ref)
        if (
            blob.active_vault_ref.startswith("/")
            or "\\" in blob.active_vault_ref
            or any(part in {".", ".."} for part in relative.parts)
        ):
            raise SourceAssetActivationError(
                "Source Asset canonical Blob ref is invalid"
            )
        path = library / relative
        if path.is_symlink() or not path.is_file():
            raise SourceAssetActivationError(
                "Source Asset canonical Blob is unavailable"
            )
        if _sha256_file(path) != blob.sha256:
            raise SourceAssetActivationError(
                "Source Asset canonical Blob hash does not match mapping"
            )


def _target_fingerprint(snapshot) -> str:
    payload = {
        "authority_identity": snapshot.authority_identity,
        "blobs": [
            {
                "sha256": item.sha256,
                "active_vault_ref": item.active_vault_ref,
                "legacy_vault_refs": list(item.legacy_vault_refs),
            }
            for item in snapshot.blobs
        ],
        "assets": [
            {
                "asset_id": item.asset_id,
                "asset_ref": item.asset_ref,
                "blob_sha256": item.blob_sha256,
                "byte_count": item.byte_count,
                "legacy_vault_ref": item.legacy_vault_ref,
                "metadata": dict(item.metadata),
            }
            for item in snapshot.assets
        ],
        "links": [
            {
                "link_id": item.link_id,
                "source_id": item.source_id,
                "asset_id": item.asset_id,
                "asset_ref": item.asset_ref,
                "content_hash": item.content_hash,
                "role": item.role,
                "provenance": item.provenance,
            }
            for item in snapshot.links
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _receipt(
    evidence: AggregateAuthorityEvidence,
    namespace_id: str,
    backup_fingerprint: str,
    snapshot,
    *,
    idempotent: bool,
) -> SourceAssetActivationReceipt:
    return SourceAssetActivationReceipt(
        migration_id=evidence.migration_id,
        namespace_id=namespace_id,
        source_fingerprint=evidence.source_fingerprint,
        target_fingerprint=evidence.target_fingerprint,
        backup_fingerprint=backup_fingerprint,
        blob_count=len(snapshot.blobs),
        asset_count=len(snapshot.assets),
        link_count=len(snapshot.links),
        state="sqlite_active",
        idempotent=idempotent,
    )


def _require_disjoint(first: Path, second: Path) -> None:
    if first == second:
        raise SourceAssetActivationError(
            "Source Asset activation requires a distinct staged clone"
        )
    try:
        second.relative_to(first)
    except ValueError:
        pass
    else:
        raise SourceAssetActivationError(
            "Source Asset activation target cannot be inside source"
        )
    try:
        first.relative_to(second)
    except ValueError:
        return
    raise SourceAssetActivationError(
        "Source Asset activation source cannot be inside target"
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
