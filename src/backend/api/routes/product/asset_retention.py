"""Asset retention ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json, os, shutil, time
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep

from core.aggregate_repository_factory import (
    AggregateRepositoryFactory,
    AggregateRepositoryFactoryError,
)
from core.document_engine import DocumentRepositoryError
from core.product_core.asset_ownership_graph import (
    AssetOwnershipGraphError,
    BuildAssetOwnershipGraph,
    serialize_asset_ownership_graph,
)
from core.product_core.original_asset_retention import (
    BuildOriginalAssetRetentionPlan,
    ExecuteOriginalAssetRetentionPurge,
    OriginalAssetRetentionError,
    ReconcileOriginalAssetOrphans,
)
from core.product_core.storage_governance import (
    StorageGovernanceError,
    build_storage_governance_report,
)
from core.retention_inventory import build_retention_backup_evidence
from core.storage_provider import (
    SourceAssetAuthorityReaderError,
    VaultBackupRestoreError,
    create_vault_backup,
    fingerprint_vault_root,
    read_source_asset_authority,
)

from . import asset_retention_storage as product_asset_retention_storage
from . import http as product_http
from . import recovery_points as product_recovery_points
from . import repositories as product_repositories
from . import source_retention as product_source_retention

router = APIRouter(tags=["rebuild-product-core"])


@router.post("/api/rebuild/retention/original-assets/reconcile")
def reconcile_original_asset_retention(
    request: Request, container: ApiContainerDep
) -> JSONResponse:
    """Project current Source links into explicit orphan retention clocks."""

    store, _settings = product_repositories._object_store(container.root_dir)
    try:
        results = ReconcileOriginalAssetOrphans(store).execute()
        return product_http._json_response(
            200,
            {
                "items": [
                    {
                        "asset_id": item.asset_id,
                        "status": item.status,
                        "revision": item.revision,
                        "orphaned_at": item.orphaned_at,
                    }
                    for item in results
                ]
            },
            product_http._no_store_headers(),
        )
    except (OSError, OriginalAssetRetentionError, ValueError) as error:
        return product_http._json_response(409, {"detail": str(error)}, product_http._no_store_headers())


@router.get("/api/rebuild/retention/asset-ownership")
def get_asset_ownership_graph(
    request: Request, container: ApiContainerDep
) -> JSONResponse:
    """Return a body-free, read-only ownership graph for retained assets."""

    store, _settings = product_repositories._object_store(container.root_dir)
    media_roots: list[Path] = []
    extractor = store.read("video_audio_extractor_settings", "default")
    if isinstance(extractor, Mapping):
        output_root = extractor.get("output_root")
        if isinstance(output_root, str) and output_root.strip():
            candidate = Path(output_root).expanduser()
            if candidate.is_absolute():
                media_roots.append(candidate)
    try:
        resolution = AggregateRepositoryFactory(
            runtime_root=container.root_dir,
            namespace_id=_settings.namespace_id,
            json_store=store,
        ).source_asset_authority_resolution()
        sqlite_active = resolution.records is not None
        asset_records = None
        link_records = None
        if sqlite_active:
            snapshot = read_source_asset_authority(
                json_store=store,
                sqlite_records=resolution.records,
                authority_identity=resolution.authority_identity,
            )
            blob_refs = {
                blob.sha256: blob.active_vault_ref for blob in snapshot.blobs
            }
            asset_records = tuple(
                {
                    "id": asset.asset_id,
                    "asset_ref": asset.asset_ref,
                    "sha256": asset.blob_sha256,
                    "byte_count": asset.byte_count,
                    "vault_ref": blob_refs[asset.blob_sha256],
                    "metadata": dict(asset.metadata),
                    "link_status": "linked",
                }
                for asset in snapshot.assets
            )
            link_records = tuple(
                {
                    "id": link.link_id,
                    "source_id": link.source_id,
                    "asset_id": link.asset_id,
                    "asset_ref": link.asset_ref,
                    "content_hash": link.content_hash,
                    "role": link.role,
                    "provenance": link.provenance,
                }
                for link in snapshot.links
            )
        graph = BuildAssetOwnershipGraph(
            store,
            allowed_media_roots=media_roots,
            source_asset_records=asset_records,
            source_asset_link_records=link_records,
            source_asset_authority=(
                "sqlite.original_assets"
                if sqlite_active
                else "workbench_original_assets"
            ),
            source_blob_authority=(
                "sqlite.asset_blobs"
                if sqlite_active
                else "original_blob_store"
            ),
        ).execute()
        return product_http._json_response(
            200,
            serialize_asset_ownership_graph(graph),
            product_http._no_store_headers(),
        )
    except (
        AggregateRepositoryFactoryError,
        AssetOwnershipGraphError,
        SourceAssetAuthorityReaderError,
        OSError,
        ValueError,
    ) as error:
        return product_http._json_response(409, {"detail": str(error)}, product_http._no_store_headers())


@router.get("/api/rebuild/retention/original-assets/candidates")
def list_original_asset_retention_candidates(
    request: Request, container: ApiContainerDep
) -> JSONResponse:
    """List body-free original-asset retention state."""

    store, _settings = product_repositories._object_store(container.root_dir)
    items = _original_asset_retention_candidate_payloads(
        store,
        now=datetime.now(timezone.utc),
    )
    return product_http._json_response(200, {"items": items}, product_http._no_store_headers())


def _original_asset_retention_candidate_payloads(
    store,
    *,
    now: datetime,
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for asset in store.list("workbench_original_assets"):
        asset_id = asset.get("id")
        if not isinstance(asset_id, str) or not asset_id:
            continue
        orphaned_text = asset.get("orphaned_at")
        try:
            orphaned = datetime.fromisoformat(
                str(orphaned_text or "").replace("Z", "+00:00")
            )
        except ValueError:
            orphaned = None
        eligible_after = (
            orphaned.astimezone(timezone.utc) + timedelta(days=7)
            if orphaned is not None and orphaned.tzinfo is not None
            else None
        )
        items.append(
            {
                "asset_id": asset_id,
                "display_name": str(asset.get("display_name", "") or asset_id),
                "media_type": str(
                    asset.get("media_type", "") or "application/octet-stream"
                ),
                "byte_count": (
                    asset.get("byte_count")
                    if isinstance(asset.get("byte_count"), int)
                    and not isinstance(asset.get("byte_count"), bool)
                    else 0
                ),
                "revision": store.revision(
                    "workbench_original_assets", asset_id
                ),
                "link_status": str(asset.get("link_status", "") or "unknown"),
                "orphaned_at": orphaned_text
                if isinstance(orphaned_text, str)
                else None,
                "eligible_after": (
                    eligible_after.isoformat().replace("+00:00", "Z")
                    if eligible_after is not None
                    else None
                ),
                "retention_elapsed": bool(
                    eligible_after is not None and now >= eligible_after
                ),
            }
        )
    items.sort(key=lambda item: (str(item["orphaned_at"]), str(item["asset_id"])))
    return items


@router.get("/api/rebuild/retention/storage-governance")
def get_storage_governance(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    """Return actual disk usage and lifecycle capabilities without paths or bodies."""

    del request
    store, settings = product_repositories._object_store(container.root_dir)
    now = datetime.now(timezone.utc)
    source_candidates = product_source_retention._source_retention_candidate_payloads(
        store,
        now=now,
    )
    asset_candidates = _original_asset_retention_candidate_payloads(
        store,
        now=now,
    )
    archived_document_count: int | None = None
    try:
        documents = product_repositories._document_repository(
            container.root_dir,
            store,
            settings,
        )
        archived_document_count = sum(
            document.get("status") == "archived"
            for document in documents.list(include_archived=True)
            if isinstance(document, Mapping)
        )
    except (
        AggregateRepositoryFactoryError,
        DocumentRepositoryError,
        OSError,
        ValueError,
    ):
        archived_document_count = None
    active_root, snapshots_root, _operations_root = product_recovery_points._vault_recovery_roots(
        container.root_dir
    )
    try:
        report = build_storage_governance_report(
            active_vault_root=active_root,
            recovery_root=snapshots_root.parent,
            library_asset_root=Path(container.root_dir) / "library" / "assets",
            source_candidates=source_candidates,
            original_asset_candidates=asset_candidates,
            archived_document_count=archived_document_count,
            recovery_point_count=len(
                product_recovery_points._external_recovery_point_records(snapshots_root)
            ),
            backup_retention_count=settings.backup_retention_count,
        )
        return product_http._json_response(
            200,
            report.to_payload(),
            product_http._no_store_headers(),
        )
    except (OSError, StorageGovernanceError, ValueError) as error:
        return product_http._json_response(
            409,
            {"detail": str(error), "actionable": True},
            product_http._no_store_headers(),
        )


@router.post("/api/rebuild/retention/original-assets/plan")
async def create_original_asset_retention_plan(
    request: Request, container: ApiContainerDep
) -> JSONResponse:
    """Create structured and byte recovery evidence before planning deletion."""

    store, settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request) or {}
    asset_id = str(body.get("asset_id", "") or "").strip()
    active_root, snapshots_root, operations_root = product_recovery_points._vault_recovery_roots(
        container.root_dir
    )
    try:
        if not asset_id:
            raise OriginalAssetRetentionError(
                "original asset retention requires asset_id"
            )
        asset = store.read("workbench_original_assets", asset_id)
        if not isinstance(asset, Mapping):
            raise OriginalAssetRetentionError(
                "original asset authority is unavailable"
            )
        snapshot_id = f"snap-asset-retention-{time.time_ns():x}"
        backup = create_vault_backup(
            source_root=active_root,
            backups_root=snapshots_root,
            snapshot_id=snapshot_id,
        )
        asset_backup = product_asset_retention_storage._create_original_asset_backup(
            runtime_root=Path(container.root_dir),
            snapshots_root=snapshots_root,
            snapshot_id=snapshot_id,
            store=store,
            asset=asset,
        )
        now_text = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        product_recovery_points._write_recovery_point_record(
            backup.snapshot_root,
            {
                "schema_version": "2.0.0",
                "id": snapshot_id,
                "created_at": now_text,
                "label": "Original asset retention safety backup",
                "namespace_id": settings.namespace_id,
                "layer_fingerprints": {},
                "layer_counts": {},
                "notes": "",
                "restorable": True,
                "vault_fingerprint": backup.source_fingerprint,
                "backup_file_count": backup.file_count,
                "original_asset_backup_ids": [asset_backup.backup_id],
            },
        )
        evidence = build_retention_backup_evidence(
            snapshot_root=backup.snapshot_root,
            active_vault_root=active_root,
        )
        plan = BuildOriginalAssetRetentionPlan(
            store,
            library_root=Path(container.root_dir) / "library",
        ).execute(
            asset_id=asset_id,
            backup_evidence=evidence,
            asset_backup_evidence=asset_backup,
            observed_vault_fingerprint=fingerprint_vault_root(active_root),
            evaluated_at=datetime.now(timezone.utc),
        )
        expires_at = product_asset_retention_storage._write_original_asset_plan(
            operations_root=operations_root,
            plan=plan,
        )
        return product_http._json_response(
            200,
            {
                "plan_id": plan.plan_id,
                "asset_id": asset_id,
                "revision": plan.candidate.revision,
                "eligible": plan.candidate.eligible,
                "eligible_after": plan.candidate.eligible_after,
                "blockers": list(plan.candidate.blockers),
                "byte_action": plan.candidate.byte_action,
                "snapshot_id": snapshot_id,
                "asset_backup_id": asset_backup.backup_id,
                "expires_at": expires_at,
                "purge_supported": True,
            },
            product_http._no_store_headers(),
        )
    except (
        OSError,
        OriginalAssetRetentionError,
        VaultBackupRestoreError,
        ValueError,
    ) as error:
        return product_http._json_response(400, {"detail": str(error)}, product_http._no_store_headers())


@router.post("/api/rebuild/retention/original-assets")
async def execute_original_asset_retention(
    request: Request, container: ApiContainerDep
) -> JSONResponse:
    """Execute or resume one exact original-asset retention plan."""

    store, _settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request) or {}
    asset_id = str(body.get("asset_id", "") or "").strip()
    plan_id = str(body.get("plan_id", "") or "").strip()
    expected_revision = body.get("expected_revision")
    confirm = body.get("confirm")
    active_root, snapshots_root, operations_root = product_recovery_points._vault_recovery_roots(
        container.root_dir
    )
    try:
        payload = product_asset_retention_storage._read_original_asset_plan(
            operations_root=operations_root,
            plan_id=plan_id,
        )
        if payload.get("asset_id") != asset_id:
            raise OriginalAssetRetentionError(
                "original asset retention plan identity mismatch"
            )
        plan = product_asset_retention_storage._original_asset_plan_objects(payload)
        has_receipt = product_asset_retention_storage._original_asset_receipt_exists(
            store, plan_id=plan_id, asset_id=asset_id
        )
        if not has_receipt:
            expires_at = datetime.fromisoformat(
                str(payload.get("expires_at", "")).replace("Z", "+00:00")
            )
            if expires_at.tzinfo is None or datetime.now(timezone.utc) > expires_at:
                raise OriginalAssetRetentionError(
                    "original asset retention plan expired"
                )
            snapshot_id = str(plan.backup_evidence.snapshot_id or "")
            snapshot_root = snapshots_root / snapshot_id
            evidence = build_retention_backup_evidence(
                snapshot_root=snapshot_root,
                active_vault_root=active_root,
            )
            asset_backup = product_asset_retention_storage._verify_original_asset_backup(
                snapshots_root=snapshots_root,
                backup_id=str(plan.asset_backup_evidence.backup_id or ""),
                asset_id=asset_id,
                snapshot_id=snapshot_id,
            )
            evaluated_at = datetime.fromisoformat(
                plan.evaluated_at.replace("Z", "+00:00")
            )
            current = BuildOriginalAssetRetentionPlan(
                store,
                library_root=Path(container.root_dir) / "library",
            ).execute(
                asset_id=asset_id,
                backup_evidence=evidence,
                asset_backup_evidence=asset_backup,
                observed_vault_fingerprint=fingerprint_vault_root(active_root),
                evaluated_at=evaluated_at,
            )
            if asdict(current) != asdict(plan):
                raise OriginalAssetRetentionError(
                    "original asset retention inventory changed after planning"
                )
            plan = current
        result = ExecuteOriginalAssetRetentionPurge(
            store,
            library_root=Path(container.root_dir) / "library",
            effect_runner=request.app.state.effect_runtime.runner,
            active_fingerprint=lambda: fingerprint_vault_root(active_root),
        ).execute(
            plan=plan,
            expected_asset_revision=(
                expected_revision
                if isinstance(expected_revision, int)
                and not isinstance(expected_revision, bool)
                else 0
            ),
            confirm=confirm is True,
        )
        return product_http._json_response(200, asdict(result), product_http._no_store_headers())
    except (
        OSError,
        OriginalAssetRetentionError,
        TypeError,
        ValueError,
        VaultBackupRestoreError,
    ) as error:
        return product_http._json_response(409, {"detail": str(error)}, product_http._no_store_headers())


@router.post(
    "/api/rebuild/retention/original-assets/backups/{backup_id}/restore"
)
async def restore_original_asset_retention_backup(
    backup_id: str,
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    """Restore verified original bytes after the structured snapshot is restored."""

    store, _settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request) or {}
    asset_id = str(body.get("asset_id", "") or "").strip()
    confirm = body.get("confirm")
    _active_root, snapshots_root, _operations_root = product_recovery_points._vault_recovery_roots(
        container.root_dir
    )
    try:
        if confirm is not True:
            raise OriginalAssetRetentionError(
                "original asset restore requires explicit confirmation"
            )
        if product_asset_retention_storage._ORIGINAL_ASSET_BACKUP_ID.fullmatch(backup_id) is None:
            raise OriginalAssetRetentionError(
                "original asset backup identity is invalid"
            )
        backup_root = product_asset_retention_storage._original_asset_backup_root(snapshots_root) / backup_id
        manifest_path = backup_root / "manifest.json"
        if (
            backup_root.is_symlink()
            or not backup_root.is_dir()
            or manifest_path.is_symlink()
            or not manifest_path.is_file()
        ):
            raise OriginalAssetRetentionError(
                "original asset backup is unavailable"
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, Mapping):
            raise OriginalAssetRetentionError(
                "original asset backup manifest is invalid"
            )
        snapshot_id = str(manifest.get("snapshot_id", ""))
        if manifest.get("asset_id") != asset_id:
            raise OriginalAssetRetentionError(
                "original asset restore identity mismatch"
            )
        evidence = product_asset_retention_storage._verify_original_asset_backup(
            snapshots_root=snapshots_root,
            backup_id=backup_id,
            asset_id=asset_id,
            snapshot_id=snapshot_id,
        )
        asset = store.read("workbench_original_assets", asset_id)
        if not isinstance(asset, Mapping):
            raise OriginalAssetRetentionError(
                "restore the structured recovery snapshot before original bytes"
            )
        if (
            asset.get("sha256") != evidence.sha256
            or asset.get("byte_count") != evidence.byte_count
            or asset.get("vault_ref") != manifest.get("vault_ref")
        ):
            raise OriginalAssetRetentionError(
                "restored original asset authority does not match byte backup"
            )
        vault_ref = str(manifest.get("vault_ref", ""))
        library_root = (Path(container.root_dir) / "library").resolve(
            strict=False
        )
        location = getattr(store, "asset_location", None)
        if callable(location):
            expected_ref, expected_target = location(
                sha256=evidence.sha256,
                legacy_filename=PurePosixPath(vault_ref).name,
            )
            if vault_ref != expected_ref:
                raise OriginalAssetRetentionError(
                    "original asset restore authority path drifted"
                )
            target = Path(expected_target).resolve(strict=False)
            authority_root = (
                library_root
                / "assets"
                / ("blobs" if getattr(store, "sqlite_active", False) else "originals")
            ).resolve(strict=False)
        else:
            authority_root = (library_root / "assets" / "originals").resolve(
                strict=False
            )
            target = (
                library_root / Path(*PurePosixPath(vault_ref).parts)
            ).resolve(strict=False)
        try:
            target.relative_to(authority_root)
        except ValueError as error:
            raise OriginalAssetRetentionError(
                "original asset restore path escapes active authority root"
            ) from error
        target.parent.mkdir(parents=True, exist_ok=True)
        current = target.parent
        while current != library_root:
            stat = current.lstat()
            if current.is_symlink() or bool(
                getattr(stat, "st_file_attributes", 0) & product_asset_retention_storage._REPARSE_POINT
            ):
                raise OriginalAssetRetentionError(
                    "original asset restore path contains a reparse directory"
                )
            current = current.parent
        payload = backup_root / "payload.bin"
        idempotent = target.exists()
        if idempotent:
            if (
                target.is_symlink()
                or not target.is_file()
                or target.stat().st_size != evidence.byte_count
                or product_asset_retention_storage._sha256_path(target) != evidence.sha256
            ):
                raise OriginalAssetRetentionError(
                    "original asset restore target conflicts"
                )
        else:
            partial = target.with_name(f".{target.name}.{backup_id}.partial")
            partial.unlink(missing_ok=True)
            try:
                with payload.open("rb") as source, partial.open("xb") as output:
                    shutil.copyfileobj(source, output, length=256 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                if (
                    partial.stat().st_size != evidence.byte_count
                    or product_asset_retention_storage._sha256_path(partial) != evidence.sha256
                ):
                    raise OriginalAssetRetentionError(
                        "original asset restore copy verification failed"
                    )
                os.replace(partial, target)
            finally:
                partial.unlink(missing_ok=True)
        return product_http._json_response(
            200,
            {
                "status": "restored",
                "asset_id": asset_id,
                "backup_id": backup_id,
                "byte_count": evidence.byte_count,
                "idempotent": idempotent,
            },
            product_http._no_store_headers(),
        )
    except (
        OSError,
        OriginalAssetRetentionError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        return product_http._json_response(409, {"detail": str(error)}, product_http._no_store_headers())
