from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .ports import ObjectStorePort


class WorkbenchOriginalAssetError(ValueError):
    """Raised when uploaded original asset persistence is rejected."""


@dataclass(frozen=True, slots=True)
class WorkbenchOriginalAssetAvailability:
    asset_id: str
    source_id: str
    status: str
    reason: str
    display_name: str
    media_type: str
    byte_count: int
    path: Path | None = None


class ResolveWorkbenchOriginalAsset:
    """Resolve a linked original asset inside the current Vault authority."""

    def __init__(self, *, object_store: ObjectStorePort, library_root: Path) -> None:
        self._object_store = object_store
        self._library_root = library_root.expanduser().resolve(strict=False)
        self._originals_root = (self._library_root / "assets" / "originals").resolve(strict=False)

    def for_source(self, source_id: str) -> WorkbenchOriginalAssetAvailability | None:
        clean_source_id = _opaque_id(source_id, "source_id")
        link = next(
            (
                item
                for item in self._object_store.list("source_asset_links")
                if item.get("source_id") == clean_source_id and item.get("role") == "original"
            ),
            None,
        )
        if link is None:
            return None
        return self.for_asset(str(link.get("asset_id") or ""), source_id=clean_source_id)

    def for_asset(self, asset_id: str, *, source_id: str = "") -> WorkbenchOriginalAssetAvailability:
        clean_asset_id = _opaque_id(asset_id, "asset_id")
        asset = self._object_store.read("workbench_original_assets", clean_asset_id)
        if asset is None:
            return self._result(clean_asset_id, source_id, "missing", "asset_record_missing", {})
        links = tuple(self._object_store.list("source_asset_links"))
        if not any(
            link.get("asset_id") == clean_asset_id
            and (not source_id or link.get("source_id") == source_id)
            for link in links
        ):
            return self._result(clean_asset_id, source_id, "unavailable", "source_asset_link_missing", asset)
        vault_ref = asset.get("vault_ref")
        if not isinstance(vault_ref, str) or not vault_ref.startswith("assets/originals/"):
            return self._result(clean_asset_id, source_id, "unavailable", "vault_ref_invalid", asset)
        relative = Path(*vault_ref.split("/"))
        candidate = (self._library_root / relative).resolve(strict=False)
        try:
            candidate.relative_to(self._originals_root)
        except ValueError:
            return self._result(clean_asset_id, source_id, "unavailable", "vault_ref_escape", asset)
        if not candidate.is_file():
            return self._result(clean_asset_id, source_id, "missing", "original_file_missing", asset)
        expected_size = asset.get("byte_count")
        if not isinstance(expected_size, int) or candidate.stat().st_size != expected_size:
            return self._result(clean_asset_id, source_id, "drifted", "original_file_size_drift", asset)
        expected_sha256 = asset.get("sha256")
        if not isinstance(expected_sha256, str) or _sha256_file(candidate) != expected_sha256:
            return self._result(clean_asset_id, source_id, "drifted", "original_file_hash_drift", asset)
        return self._result(clean_asset_id, source_id, "available", "verified_original_file", asset, candidate)

    @staticmethod
    def _result(
        asset_id: str,
        source_id: str,
        status: str,
        reason: str,
        asset: Mapping[str, object],
        path: Path | None = None,
    ) -> WorkbenchOriginalAssetAvailability:
        return WorkbenchOriginalAssetAvailability(
            asset_id=asset_id,
            source_id=source_id,
            status=status,
            reason=reason,
            display_name=str(asset.get("display_name") or ""),
            media_type=str(asset.get("media_type") or "application/octet-stream"),
            byte_count=int(asset.get("byte_count") or 0),
            path=path,
        )


def serialize_workbench_original_asset_availability(
    result: WorkbenchOriginalAssetAvailability,
) -> dict[str, object]:
    return {
        "asset_id": result.asset_id,
        "source_id": result.source_id,
        "status": result.status,
        "reason": result.reason,
        "display_name": result.display_name,
        "media_type": result.media_type,
        "byte_count": result.byte_count,
    }


@dataclass(frozen=True, slots=True)
class WorkbenchOriginalAssetResult:
    status: str
    asset_id: str
    asset_ref: str
    vault_ref: str
    display_name: str
    media_type: str
    byte_count: int
    sha256: str
    storage_mode: str
    availability: str
    availability_reason: str
    metadata: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class WorkbenchOriginalAssetBatchItemResult:
    index: int
    status: str
    asset: WorkbenchOriginalAssetResult | None
    error: str | None


@dataclass(frozen=True, slots=True)
class WorkbenchOriginalAssetBatchResult:
    status: str
    total: int
    succeeded: int
    failed: int
    uploaded: tuple[WorkbenchOriginalAssetBatchItemResult, ...]
    failed_items: tuple[WorkbenchOriginalAssetBatchItemResult, ...]


class StoreWorkbenchOriginalAsset:
    """Persist browser-uploaded original bytes before Source intake."""

    max_bytes = 64 * 1024 * 1024

    def __init__(
        self,
        *,
        object_store: ObjectStorePort,
        assets_root: Path,
        namespace_id: str = "default",
        now: str = "2026-07-02T00:00:00Z",
    ) -> None:
        self._object_store = object_store
        self._assets_root = assets_root.expanduser().resolve(strict=False)
        self._namespace_id = namespace_id
        self._now = now

    def execute(
        self,
        *,
        display_name: str,
        media_type: str,
        size_bytes: int,
        content_base64: str,
        source_kind: str = "file",
    ) -> WorkbenchOriginalAssetResult:
        clean_display_name = _clean_display_name(display_name)
        clean_media_type = _clean_media_type(media_type)
        clean_source_kind = _clean_source_kind(source_kind)
        content = _decode_base64(content_base64)
        if len(content) > self.max_bytes:
            raise WorkbenchOriginalAssetError("uploaded original asset exceeds local intake size limit")
        if size_bytes >= 0 and size_bytes != len(content):
            raise WorkbenchOriginalAssetError("uploaded original asset size does not match declared size_bytes")
        sha256 = hashlib.sha256(content).hexdigest()
        asset_id = f"original-{clean_source_kind}-{sha256[:16]}"
        suffix = _safe_suffix(clean_display_name)
        default_name = f"{asset_id}{suffix}"
        vault_ref, asset_path = _active_asset_location(
            self._object_store,
            assets_root=self._assets_root,
            sha256=sha256,
            legacy_filename=default_name,
        )
        existing = self._object_store.read("workbench_original_assets", asset_id)
        if existing is not None:
            if (
                existing.get("sha256") != sha256
                or existing.get("byte_count") != len(content)
                or existing.get("asset_ref")
                != f"crp-ref-{self._namespace_id}-assets-originals-{asset_id}"
            ):
                raise WorkbenchOriginalAssetError(
                    "uploaded original asset identity conflicts with existing record"
                )
            existing_vault_ref = str(existing.get("vault_ref") or "")
            expected_vault_ref, existing_path = _active_asset_location(
                self._object_store,
                assets_root=self._assets_root,
                sha256=sha256,
                legacy_filename=Path(existing_vault_ref).name,
            )
            if existing_vault_ref != expected_vault_ref:
                raise WorkbenchOriginalAssetError(
                    "existing original asset vault reference is invalid"
                )
            asset_path = existing_path
            asset_path.parent.mkdir(parents=True, exist_ok=True)
            if asset_path.exists():
                if asset_path.stat().st_size != len(content) or _sha256_file(asset_path) != sha256:
                    raise WorkbenchOriginalAssetError(
                        "existing original asset bytes do not match uploaded identity"
                    )
            else:
                asset_path.write_bytes(content)
            return WorkbenchOriginalAssetResult(
                status="stored",
                asset_id=asset_id,
                asset_ref=str(existing["asset_ref"]),
                vault_ref=existing_vault_ref,
                display_name=str(existing.get("display_name") or clean_display_name),
                media_type=str(existing.get("media_type") or clean_media_type),
                byte_count=len(content),
                sha256=sha256,
                storage_mode=str(existing.get("storage_mode") or "stored_original"),
                availability=str(existing.get("availability") or "available"),
                availability_reason=str(
                    existing.get("availability_reason")
                    or "browser_upload_saved_to_local_assets"
                ),
                metadata=dict(existing.get("metadata") or {}),
            )
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        if asset_path.exists():
            if asset_path.stat().st_size != len(content) or _sha256_file(asset_path) != sha256:
                raise WorkbenchOriginalAssetError(
                    "existing original asset bytes do not match uploaded identity"
                )
        else:
            asset_path.write_bytes(content)
        asset_ref = f"crp-ref-{self._namespace_id}-assets-originals-{asset_id}"
        record = {
            "schema_version": "1.0.0",
            "id": asset_id,
            "kind": "workbench_original_asset",
            "status": "stored",
            "asset_ref": asset_ref,
            "vault_ref": vault_ref,
            "display_name": clean_display_name,
            "media_type": clean_media_type,
            "byte_count": len(content),
            "sha256": sha256,
            "storage_mode": "stored_original",
            "availability": "available",
            "availability_reason": "browser_upload_saved_to_local_assets",
            "link_status": "pending",
            "orphan_reason": "awaiting_source_capture",
            "created_at": self._now,
            "metadata": {
                "source_kind": clean_source_kind,
                "declared_size_bytes": size_bytes,
                "content_hash_basis": "uploaded_original_bytes_sha256",
                "original_filename": clean_display_name,
            },
        }
        self._object_store.write("workbench_original_assets", asset_id, record, expected_revision=0)
        return WorkbenchOriginalAssetResult(
            status="stored",
            asset_id=asset_id,
            asset_ref=asset_ref,
            vault_ref=vault_ref,
            display_name=clean_display_name,
            media_type=clean_media_type,
            byte_count=len(content),
            sha256=sha256,
            storage_mode="stored_original",
            availability="available",
            availability_reason="browser_upload_saved_to_local_assets",
            metadata=record["metadata"],
        )

    def execute_staged_file(
        self,
        *,
        display_name: str,
        media_type: str,
        size_bytes: int,
        sha256: str,
        staged_path: Path,
        source_kind: str = "file",
    ) -> WorkbenchOriginalAssetResult:
        clean_display_name = _clean_display_name(display_name)
        clean_media_type = _clean_media_type(media_type)
        clean_source_kind = _clean_source_kind(source_kind)
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise WorkbenchOriginalAssetError("streamed original asset sha256 is invalid")
        staged = staged_path.resolve(strict=True)
        actual_size = staged.stat().st_size
        if size_bytes < 0 or size_bytes != actual_size:
            raise WorkbenchOriginalAssetError("streamed original asset size does not match grant")
        asset_id = f"original-{clean_source_kind}-{sha256[:16]}"
        suffix = _safe_suffix(clean_display_name)
        vault_ref, asset_path = _active_asset_location(
            self._object_store,
            assets_root=self._assets_root,
            sha256=sha256,
            legacy_filename=f"{asset_id}{suffix}",
        )
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        if asset_path.exists():
            if asset_path.stat().st_size != actual_size or _sha256_file(asset_path) != sha256:
                raise WorkbenchOriginalAssetError("existing original asset bytes do not match streamed identity")
            staged.unlink(missing_ok=True)
        else:
            os.replace(staged, asset_path)
        asset_ref = f"crp-ref-{self._namespace_id}-assets-originals-{asset_id}"
        record = {
            "schema_version": "1.0.0",
            "id": asset_id,
            "kind": "workbench_original_asset",
            "status": "stored",
            "asset_ref": asset_ref,
            "vault_ref": vault_ref,
            "display_name": clean_display_name,
            "media_type": clean_media_type,
            "byte_count": actual_size,
            "sha256": sha256,
            "storage_mode": "stored_original",
            "availability": "available",
            "availability_reason": "desktop_grant_stream_saved_to_local_assets",
            "link_status": "pending",
            "orphan_reason": "awaiting_source_capture",
            "created_at": self._now,
            "metadata": {
                "source_kind": clean_source_kind,
                "declared_size_bytes": size_bytes,
                "content_hash_basis": "desktop_grant_stream_sha256",
                "original_filename": clean_display_name,
            },
        }
        existing = self._object_store.read("workbench_original_assets", asset_id)
        if existing is not None:
            if (
                existing.get("sha256") != sha256
                or existing.get("byte_count") != actual_size
                or existing.get("asset_ref") != asset_ref
            ):
                raise WorkbenchOriginalAssetError("streamed original asset identity conflicts with existing record")
            return WorkbenchOriginalAssetResult(
                status="stored",
                asset_id=asset_id,
                asset_ref=asset_ref,
                vault_ref=str(existing.get("vault_ref") or vault_ref),
                display_name=str(existing.get("display_name") or clean_display_name),
                media_type=str(existing.get("media_type") or clean_media_type),
                byte_count=actual_size,
                sha256=sha256,
                storage_mode=str(existing.get("storage_mode") or "stored_original"),
                availability=str(existing.get("availability") or "available"),
                availability_reason=str(existing.get("availability_reason") or "desktop_grant_stream_saved_to_local_assets"),
                metadata=dict(existing.get("metadata") or record["metadata"]),
            )
        self._object_store.write("workbench_original_assets", asset_id, record, expected_revision=0)
        return WorkbenchOriginalAssetResult(
            status="stored",
            asset_id=asset_id,
            asset_ref=asset_ref,
            vault_ref=vault_ref,
            display_name=clean_display_name,
            media_type=clean_media_type,
            byte_count=actual_size,
            sha256=sha256,
            storage_mode="stored_original",
            availability="available",
            availability_reason="desktop_grant_stream_saved_to_local_assets",
            metadata=record["metadata"],
        )


def serialize_workbench_original_asset(result: WorkbenchOriginalAssetResult) -> dict[str, object]:
    return {
        "status": result.status,
        "asset_id": result.asset_id,
        "asset_ref": result.asset_ref,
        "vault_ref": result.vault_ref,
        "display_name": result.display_name,
        "media_type": result.media_type,
        "byte_count": result.byte_count,
        "sha256": result.sha256,
        "storage_mode": result.storage_mode,
        "availability": result.availability,
        "availability_reason": result.availability_reason,
        "metadata": dict(result.metadata),
    }


@dataclass(frozen=True, slots=True)
class WorkbenchOriginalAssetLinkResult:
    link_id: str
    link_ref: str
    asset_id: str
    source_id: str


def link_workbench_original_asset_to_source(
    *,
    object_store: ObjectStorePort,
    namespace_id: str,
    asset_ref: str,
    source_id: str,
    source_uri: str,
    now: str,
) -> WorkbenchOriginalAssetLinkResult:
    asset = next((item for item in object_store.list("workbench_original_assets") if item.get("asset_ref") == asset_ref), None)
    if asset is None:
        raise WorkbenchOriginalAssetError("referenced original asset was not found")
    asset_id = str(asset.get("id") or "")
    if not asset_id or not source_id or not source_uri:
        raise WorkbenchOriginalAssetError("original asset link requires opaque asset and source identifiers")
    link_id = f"source-asset-{source_id}-{asset_id}"
    link_ref = f"crp://{namespace_id}/source-assets/{link_id}"
    link = {
        "schema_version": "1.0.0", "id": link_id, "source_id": source_id, "source_uri": source_uri,
        "asset_id": asset_id, "asset_ref": asset_ref, "role": "original", "content_hash": asset.get("sha256"),
        "size_bytes": asset.get("byte_count"), "created_at": now, "provenance": "workbench_auto_intake",
    }
    atomic_link = getattr(object_store, "link_source_asset", None)
    if callable(atomic_link) and getattr(object_store, "sqlite_active", False):
        try:
            atomic_link(
                asset_ref=asset_ref,
                link_id=link_id,
                link_payload=link,
                source_id=source_id,
            )
        except Exception as error:  # noqa: BLE001
            raise WorkbenchOriginalAssetError(
                "original asset could not be linked to the captured source"
            ) from error
        return WorkbenchOriginalAssetLinkResult(
            link_id=link_id,
            link_ref=link_ref,
            asset_id=asset_id,
            source_id=source_id,
        )
    try:
        object_store.write("source_asset_links", link_id, link, expected_revision=None)
        updated = dict(asset)
        source_ids = list(updated.get("linked_source_ids") or [])
        if source_id not in source_ids:
            source_ids.append(source_id)
        updated.update({"linked_source_ids": source_ids, "link_status": "linked", "orphan_reason": None})
        object_store.write("workbench_original_assets", asset_id, updated, expected_revision=None)
    except Exception as error:  # noqa: BLE001
        orphan = dict(asset)
        orphan.update({"link_status": "orphaned", "orphan_reason": "source_asset_link_failed"})
        object_store.write("workbench_original_assets", asset_id, orphan, expected_revision=None)
        raise WorkbenchOriginalAssetError("original asset could not be linked to the captured source") from error
    return WorkbenchOriginalAssetLinkResult(link_id=link_id, link_ref=link_ref, asset_id=asset_id, source_id=source_id)


def _active_asset_location(
    store: ObjectStorePort,
    *,
    assets_root: Path,
    sha256: str,
    legacy_filename: str,
) -> tuple[str, Path]:
    resolver = getattr(store, "asset_location", None)
    if callable(resolver):
        vault_ref, path = resolver(
            sha256=sha256,
            legacy_filename=legacy_filename,
        )
        return str(vault_ref), Path(path)
    vault_ref = f"assets/originals/{sha256[:2]}/{legacy_filename}"
    return vault_ref, assets_root / sha256[:2] / legacy_filename


class StoreWorkbenchOriginalAssetBatch:
    """Persist multiple browser-uploaded original bytes in one request.

    Wraps ``StoreWorkbenchOriginalAsset`` so each asset goes through the same
    validation and storage path as the single-asset endpoint. A single asset
    failure does not block the rest; the caller receives per-item results and
    can surface partial failures to the UI.
    """

    def __init__(
        self,
        *,
        object_store: ObjectStorePort,
        assets_root: Path,
        namespace_id: str = "default",
        now: str = "2026-07-02T00:00:00Z",
    ) -> None:
        self._single = StoreWorkbenchOriginalAsset(
            object_store=object_store,
            assets_root=assets_root,
            namespace_id=namespace_id,
            now=now,
        )

    def execute(
        self,
        *,
        assets: Sequence[Mapping[str, object]],
    ) -> WorkbenchOriginalAssetBatchResult:
        if not isinstance(assets, Sequence) or isinstance(assets, (str, bytes)):
            raise WorkbenchOriginalAssetError("assets must be a list of objects")
        results: list[WorkbenchOriginalAssetBatchItemResult] = []
        succeeded = 0
        failed = 0
        for index, raw in enumerate(assets):
            if not isinstance(raw, Mapping):
                item = WorkbenchOriginalAssetBatchItemResult(
                    index=index,
                    status="failed",
                    asset=None,
                    error="asset entry must be an object",
                )
                results.append(item)
                failed += 1
                continue
            try:
                asset_result = self._single.execute(
                    display_name=str(raw.get("display_name") or ""),
                    media_type=str(raw.get("media_type") or "application/octet-stream"),
                    size_bytes=_as_int(raw.get("size_bytes")) or 0,
                    content_base64=str(raw.get("content_base64") or ""),
                    source_kind=str(raw.get("source_kind") or "file"),
                )
                results.append(
                    WorkbenchOriginalAssetBatchItemResult(
                        index=index,
                        status="stored",
                        asset=asset_result,
                        error=None,
                    )
                )
                succeeded += 1
            except WorkbenchOriginalAssetError as error:
                results.append(
                    WorkbenchOriginalAssetBatchItemResult(
                        index=index,
                        status="failed",
                        asset=None,
                        error=str(error),
                    )
                )
                failed += 1
        if succeeded == 0:
            status = "failed"
        elif failed > 0:
            status = "partial"
        else:
            status = "completed"
        return WorkbenchOriginalAssetBatchResult(
            status=status,
            total=len(results),
            succeeded=succeeded,
            failed=failed,
            uploaded=tuple(results),
            failed_items=tuple(item for item in results if item.status != "stored"),
        )


def serialize_workbench_original_asset_batch_item(
    item: WorkbenchOriginalAssetBatchItemResult,
) -> dict[str, object]:
    return {
        "index": item.index,
        "status": item.status,
        "asset": serialize_workbench_original_asset(item.asset) if item.asset else None,
        "error": item.error,
    }


def serialize_workbench_original_asset_batch(
    result: WorkbenchOriginalAssetBatchResult,
) -> dict[str, object]:
    return {
        "status": result.status,
        "total": result.total,
        "succeeded": result.succeeded,
        "failed": result.failed,
        "uploaded": [
            serialize_workbench_original_asset_batch_item(item) for item in result.uploaded
        ],
        "failed_items": [
            serialize_workbench_original_asset_batch_item(item) for item in result.failed_items
        ],
    }


def _as_int(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value).strip())
    except ValueError:
        return 0


def _opaque_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,239}", value):
        raise WorkbenchOriginalAssetError(f"{label} must be an opaque identifier")
    return value


def _decode_base64(value: str) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise WorkbenchOriginalAssetError("content_base64 is required")
    encoded = value.split(",", 1)[1] if value.startswith("data:") and "," in value else value
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise WorkbenchOriginalAssetError("content_base64 must be valid base64") from error


def _clean_display_name(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkbenchOriginalAssetError("display_name is required")
    name = Path(value.strip()).name
    if not name or name in {".", ".."}:
        raise WorkbenchOriginalAssetError("display_name must include a filename")
    return name[:180]


def _clean_media_type(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return "application/octet-stream"
    media_type = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", media_type):
        raise WorkbenchOriginalAssetError("media_type must be a valid type/subtype")
    return media_type


def _clean_source_kind(value: str) -> str:
    if value not in {"file", "image", "audio", "video"}:
        return "file"
    return value


def _safe_suffix(display_name: str) -> str:
    suffix = Path(display_name).suffix.lower()
    if not suffix or len(suffix) > 16 or not re.fullmatch(r"\.[a-z0-9][a-z0-9._-]*", suffix):
        return ".bin"
    return suffix


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(256 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
