from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
import re

from .runtime import JsonObjectStore
from .sqlite_uow import SQLiteStructuredRecordStore


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SourceAssetAuthorityReaderError(ValueError):
    """Raised when the selected Source Asset authority is internally invalid."""


@dataclass(frozen=True, slots=True)
class SourceAssetBlob:
    sha256: str
    active_vault_ref: str
    legacy_vault_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceAssetRecord:
    asset_id: str
    asset_ref: str
    blob_sha256: str
    byte_count: int
    legacy_vault_ref: str
    metadata: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class SourceAssetLink:
    link_id: str
    source_id: str
    asset_id: str
    asset_ref: str
    content_hash: str
    role: str
    provenance: str


@dataclass(frozen=True, slots=True)
class SourceAssetAuthoritySnapshot:
    authority_identity: str
    blobs: tuple[SourceAssetBlob, ...]
    assets: tuple[SourceAssetRecord, ...]
    links: tuple[SourceAssetLink, ...]


def read_source_asset_authority(
    *,
    json_store: JsonObjectStore,
    sqlite_records: SQLiteStructuredRecordStore | None,
    authority_identity: str,
) -> SourceAssetAuthoritySnapshot:
    """Read exactly one selected authority into a body-free normalized snapshot."""

    if sqlite_records is None:
        return _read_json(json_store, authority_identity)
    return _read_sqlite(sqlite_records, authority_identity)


def _read_json(
    store: JsonObjectStore,
    authority_identity: str,
) -> SourceAssetAuthoritySnapshot:
    assets = tuple(
        _json_asset(item)
        for item in store.list("workbench_original_assets")
    )
    asset_by_id = _index_assets(assets)
    _validate_shared_blob_sizes(assets)
    blob_refs: dict[str, set[str]] = {}
    for asset in assets:
        blob_refs.setdefault(asset.blob_sha256, set()).add(asset.legacy_vault_ref)
    blobs = tuple(
        SourceAssetBlob(
            sha256=sha256,
            active_vault_ref=sorted(refs)[0],
            legacy_vault_refs=tuple(sorted(refs)),
        )
        for sha256, refs in sorted(blob_refs.items())
    )
    links = tuple(
        _link(item, asset_by_id, sqlite_payload=False)
        for item in store.list("source_asset_links")
    )
    return SourceAssetAuthoritySnapshot(
        authority_identity=authority_identity,
        blobs=blobs,
        assets=tuple(sorted(assets, key=lambda item: item.asset_id)),
        links=tuple(sorted(links, key=lambda item: item.link_id)),
    )


def _read_sqlite(
    records: SQLiteStructuredRecordStore,
    authority_identity: str,
) -> SourceAssetAuthoritySnapshot:
    blobs = tuple(
        _sqlite_blob(item.payload, item.object_id)
        for item in records.list("asset_blobs")
    )
    blob_by_sha = {blob.sha256: blob for blob in blobs}
    if len(blob_by_sha) != len(blobs):
        raise SourceAssetAuthorityReaderError(
            "Source Asset SQLite blob identity is duplicated"
        )
    assets = tuple(
        _sqlite_asset(item.payload, item.object_id, blob_by_sha)
        for item in records.list("original_assets")
    )
    asset_by_id = _index_assets(assets)
    _validate_shared_blob_sizes(assets)
    for asset in assets:
        blob = blob_by_sha[asset.blob_sha256]
        if asset.legacy_vault_ref not in blob.legacy_vault_refs:
            raise SourceAssetAuthorityReaderError(
                "Source Asset SQLite asset legacy ref is absent from its blob"
            )
    links = tuple(
        _link(item.payload, asset_by_id, sqlite_payload=True, object_id=item.object_id)
        for item in records.list("source_asset_links")
    )
    return SourceAssetAuthoritySnapshot(
        authority_identity=authority_identity,
        blobs=tuple(sorted(blobs, key=lambda item: item.sha256)),
        assets=tuple(sorted(assets, key=lambda item: item.asset_id)),
        links=tuple(sorted(links, key=lambda item: item.link_id)),
    )


def _json_asset(value: Mapping[str, object]) -> SourceAssetRecord:
    return SourceAssetRecord(
        asset_id=_required(value, "id"),
        asset_ref=_required(value, "asset_ref"),
        blob_sha256=_hash(value, "sha256"),
        byte_count=_byte_count(value),
        legacy_vault_ref=_vault_ref(value, "vault_ref"),
        metadata=_metadata(value),
    )


def _sqlite_blob(value: Mapping[str, object], object_id: str) -> SourceAssetBlob:
    sha256 = _hash(value, "sha256")
    if object_id != sha256:
        raise SourceAssetAuthorityReaderError(
            "Source Asset SQLite blob object id does not match SHA-256"
        )
    legacy = value.get("legacy_vault_refs")
    if not isinstance(legacy, list) or not legacy:
        raise SourceAssetAuthorityReaderError(
            "Source Asset SQLite blob legacy refs are invalid"
        )
    refs = tuple(sorted({_vault_ref_value(item) for item in legacy}))
    return SourceAssetBlob(
        sha256=sha256,
        active_vault_ref=_vault_ref(value, "canonical_vault_ref"),
        legacy_vault_refs=refs,
    )


def _sqlite_asset(
    value: Mapping[str, object],
    object_id: str,
    blobs: Mapping[str, SourceAssetBlob],
) -> SourceAssetRecord:
    asset_id = _required(value, "legacy_asset_id")
    if object_id != asset_id:
        raise SourceAssetAuthorityReaderError(
            "Source Asset SQLite asset object id does not match legacy identity"
        )
    sha256 = _hash(value, "blob_sha256")
    if sha256 not in blobs:
        raise SourceAssetAuthorityReaderError(
            "Source Asset SQLite asset references a missing blob"
        )
    return SourceAssetRecord(
        asset_id=asset_id,
        asset_ref=_required(value, "legacy_asset_ref"),
        blob_sha256=sha256,
        byte_count=_byte_count(value),
        legacy_vault_ref=_vault_ref(value, "legacy_vault_ref"),
        metadata=_metadata(value),
    )


def _link(
    value: Mapping[str, object],
    assets: Mapping[str, SourceAssetRecord],
    *,
    sqlite_payload: bool,
    object_id: str | None = None,
) -> SourceAssetLink:
    link_id = _required(value, "legacy_link_id" if sqlite_payload else "id")
    if object_id is not None and object_id != link_id:
        raise SourceAssetAuthorityReaderError(
            "Source Asset SQLite link object id does not match legacy identity"
        )
    asset_id = _required(value, "asset_id")
    asset = assets.get(asset_id)
    if asset is None:
        raise SourceAssetAuthorityReaderError(
            "Source Asset link references a missing asset"
        )
    asset_ref = _required(value, "asset_ref")
    content_hash = _hash(value, "content_hash")
    if asset_ref != asset.asset_ref or content_hash != asset.blob_sha256:
        raise SourceAssetAuthorityReaderError(
            "Source Asset link identity does not match its asset"
        )
    return SourceAssetLink(
        link_id=link_id,
        source_id=_required(value, "source_id"),
        asset_id=asset_id,
        asset_ref=asset_ref,
        content_hash=content_hash,
        role=_required(value, "role"),
        provenance=_required(value, "provenance"),
    )


def _index_assets(
    assets: tuple[SourceAssetRecord, ...],
) -> dict[str, SourceAssetRecord]:
    indexed = {asset.asset_id: asset for asset in assets}
    if len(indexed) != len(assets):
        raise SourceAssetAuthorityReaderError(
            "Source Asset authority contains duplicate asset identities"
        )
    return indexed


def _validate_shared_blob_sizes(assets: tuple[SourceAssetRecord, ...]) -> None:
    sizes: dict[str, int] = {}
    for asset in assets:
        previous = sizes.setdefault(asset.blob_sha256, asset.byte_count)
        if previous != asset.byte_count:
            raise SourceAssetAuthorityReaderError(
                "Source Asset shared blob byte counts do not match"
            )


def _required(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item or len(item) > 512:
        raise SourceAssetAuthorityReaderError(f"Source Asset {key} is invalid")
    return item


def _hash(value: Mapping[str, object], key: str) -> str:
    item = _required(value, key)
    if not _SHA256.fullmatch(item):
        raise SourceAssetAuthorityReaderError(f"Source Asset {key} is invalid")
    return item


def _byte_count(value: Mapping[str, object]) -> int:
    item = value.get("byte_count")
    if not isinstance(item, int) or isinstance(item, bool) or item < 0:
        raise SourceAssetAuthorityReaderError("Source Asset byte_count is invalid")
    return item


def _metadata(value: Mapping[str, object]) -> Mapping[str, object]:
    item = value.get("metadata", {})
    if not isinstance(item, Mapping):
        raise SourceAssetAuthorityReaderError("Source Asset metadata is invalid")
    return dict(item)


def _vault_ref(value: Mapping[str, object], key: str) -> str:
    return _vault_ref_value(_required(value, key))


def _vault_ref_value(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise SourceAssetAuthorityReaderError("Source Asset Vault ref is invalid")
    path = PurePosixPath(value)
    if value.startswith("/") or "\\" in value or any(
        part in {".", ".."} for part in path.parts
    ):
        raise SourceAssetAuthorityReaderError("Source Asset Vault ref is invalid")
    return value
