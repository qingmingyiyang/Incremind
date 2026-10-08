from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from .sqlite_uow import SQLiteStructuredRecord, SQLiteStructuredRecordStore


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SQLiteSourceAssetMappingError(ValueError):
    """Raised when legacy Source/Asset mapping cannot be staged safely."""


@dataclass(frozen=True, slots=True)
class SQLiteSourceAssetMappingResult:
    blobs: tuple[SQLiteStructuredRecord, ...]
    assets: tuple[SQLiteStructuredRecord, ...]
    links: tuple[SQLiteStructuredRecord, ...]


class SQLiteSourceAssetMappingAdapter:
    """Stages D-018 mappings in an explicit temporary SQLite record store.

    It does not read legacy JSON, copy Blob bytes, choose a legacy path, or
    participate in application composition. A later migration executor must
    create its own inventory, ledger, backup and rollback boundary.
    """

    def __init__(self, records: SQLiteStructuredRecordStore) -> None:
        self.records = records

    def stage_legacy_mappings(
        self,
        *,
        assets: Sequence[Mapping[str, object]],
        links: Sequence[Mapping[str, object]],
    ) -> SQLiteSourceAssetMappingResult:
        normalized_assets = tuple(_normalize_asset(asset) for asset in assets)
        _require_unique((asset["id"] for asset in normalized_assets), "legacy asset ids")
        asset_by_id = {asset["id"]: asset for asset in normalized_assets}
        normalized_links = tuple(_normalize_link(link, asset_by_id) for link in links)
        _require_unique((link["id"] for link in normalized_links), "legacy link ids")

        blob_refs: dict[str, set[str]] = {}
        for asset in normalized_assets:
            blob_refs.setdefault(asset["sha256"], set()).add(asset["vault_ref"])

        with self.records.begin() as uow:
            blobs = tuple(
                uow.put(
                    "asset_blobs",
                    sha256,
                    {
                        "schema_version": "1.0.0",
                        "sha256": sha256,
                        "canonical_vault_ref": f"assets/blobs/{sha256[:2]}/{sha256}",
                        "legacy_vault_refs": sorted(blob_refs[sha256]),
                    },
                    expected_revision=0,
                )
                for sha256 in sorted(blob_refs)
            )
            staged_assets = tuple(
                uow.put(
                    "original_assets",
                    asset["id"],
                    {
                        "schema_version": "1.0.0",
                        "legacy_asset_id": asset["id"],
                        "legacy_asset_ref": asset["asset_ref"],
                        "legacy_vault_ref": asset["vault_ref"],
                        "blob_sha256": asset["sha256"],
                        "byte_count": asset["byte_count"],
                        "metadata": asset["metadata"],
                        "runtime_payload": asset["runtime_payload"],
                    },
                    expected_revision=0,
                )
                for asset in normalized_assets
            )
            staged_links = tuple(
                uow.put(
                    "source_asset_links",
                    link["id"],
                    {
                        "schema_version": "1.0.0",
                        "legacy_link_id": link["id"],
                        "source_id": link["source_id"],
                        "asset_id": link["asset_id"],
                        "asset_ref": link["asset_ref"],
                        "content_hash": link["content_hash"],
                        "role": link["role"],
                        "provenance": link["provenance"],
                    },
                    expected_revision=0,
                )
                for link in normalized_links
            )
            uow.commit()
        return SQLiteSourceAssetMappingResult(blobs, staged_assets, staged_links)


def _normalize_asset(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise SQLiteSourceAssetMappingError("legacy asset must be an object")
    asset_id = _required_string(value, "id")
    asset_ref = _required_string(value, "asset_ref")
    sha256 = _required_sha256(value, "sha256")
    vault_ref = _required_vault_ref(value, "vault_ref")
    byte_count = value.get("byte_count")
    if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count < 0:
        raise SQLiteSourceAssetMappingError("legacy asset byte_count is invalid")
    metadata = value.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise SQLiteSourceAssetMappingError("legacy asset metadata is invalid")
    return {
        "id": asset_id,
        "asset_ref": asset_ref,
        "sha256": sha256,
        "vault_ref": vault_ref,
        "byte_count": byte_count,
        "metadata": dict(metadata),
        "runtime_payload": dict(value),
    }


def _normalize_link(value: Mapping[str, object], assets: Mapping[str, Mapping[str, object]]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise SQLiteSourceAssetMappingError("legacy link must be an object")
    asset_id = _required_string(value, "asset_id")
    asset = assets.get(asset_id)
    if asset is None:
        raise SQLiteSourceAssetMappingError("legacy link references an unknown legacy asset")
    asset_ref = _required_string(value, "asset_ref")
    content_hash = _required_sha256(value, "content_hash")
    if asset_ref != asset["asset_ref"] or content_hash != asset["sha256"]:
        raise SQLiteSourceAssetMappingError("legacy link asset reference does not match legacy asset")
    return {
        "id": _required_string(value, "id"),
        "source_id": _required_string(value, "source_id"),
        "asset_id": asset_id,
        "asset_ref": asset_ref,
        "content_hash": content_hash,
        "role": _required_string(value, "role"),
        "provenance": _required_string(value, "provenance"),
    }


def _require_unique(values, label: str) -> None:
    items = tuple(values)
    if len(set(items)) != len(items):
        raise SQLiteSourceAssetMappingError(f"{label} must be unique")


def _required_string(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value or len(value) > 256:
        raise SQLiteSourceAssetMappingError(f"legacy mapping {key} is invalid")
    return value


def _required_sha256(mapping: Mapping[str, object], key: str) -> str:
    value = _required_string(mapping, key)
    if not _SHA256.fullmatch(value):
        raise SQLiteSourceAssetMappingError(f"legacy mapping {key} must be a SHA-256 hex string")
    return value


def _required_vault_ref(mapping: Mapping[str, object], key: str) -> str:
    value = _required_string(mapping, key)
    path = PurePosixPath(value)
    if value.startswith("/") or "\\" in value or any(part in {".", ".."} for part in path.parts):
        raise SQLiteSourceAssetMappingError("legacy mapping vault_ref must be Vault-relative")
    return value
