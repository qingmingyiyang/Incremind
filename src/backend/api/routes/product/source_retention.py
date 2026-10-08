"""Source retention ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timezone
import json, os, re, time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep

from core.aggregate_repository_factory import (
    AggregateRepositoryFactoryError,
    STRUCTURED_DATABASE_NAME,
)
from core.product_core.retention import (
    BuildRetentionDryRun,
    RetentionBackupEvidence,
    RetentionCandidate,
    RetentionDryRunItem,
    RetentionDryRunReport,
    RetentionRecord,
    RetentionReference,
    serialize_retention_dry_run,
)
from core.product_core.source_retention_purge import (
    ExecuteSourceRetentionPurge,
    SourceRetentionPurgeError,
)
from core.retention_inventory import (
    RetentionExternalRecord,
    RetentionInventoryError,
    VaultRetentionInventory,
    build_retention_backup_evidence,
)
from core.storage_provider import (
    JsonObjectStore,
    RebuildStorageSettings,
    SQLiteStructuredRecordStore,
    VaultBackupRestoreError,
    create_vault_backup,
    fingerprint_vault_root,
)

from . import http as product_http
from . import recovery_points as product_recovery_points
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


_SOURCE_RETENTION_PLAN_ID = re.compile(r"retention-dry-run-[0-9a-f]{64}")


_SOURCE_RETENTION_PLAN_TTL_SECONDS = 10 * 60


def _source_retention_plan_root(operations_root: Path) -> Path:
    root = (operations_root / "source-retention-plans").resolve(strict=False)
    expected_parent = operations_root.resolve(strict=False)
    if root.parent != expected_parent:
        raise SourceRetentionPurgeError("Source purge plan root is unsafe")
    if root.exists() and (not root.is_dir() or root.is_symlink()):
        raise SourceRetentionPurgeError("Source purge plan root is unsafe")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _source_retention_plan_path(
    operations_root: Path, plan_id: str
) -> Path:
    if _SOURCE_RETENTION_PLAN_ID.fullmatch(plan_id) is None:
        raise SourceRetentionPurgeError("Source purge plan identity is invalid")
    root = _source_retention_plan_root(operations_root)
    path = (root / f"{plan_id}.json").resolve(strict=False)
    if path.parent != root:
        raise SourceRetentionPurgeError("Source purge plan path is unsafe")
    return path


def _write_source_retention_plan(
    *,
    operations_root: Path,
    candidate: RetentionCandidate,
    report: RetentionDryRunReport,
    snapshot_id: str,
) -> str:
    created = datetime.now(timezone.utc)
    expires = created.timestamp() + _SOURCE_RETENTION_PLAN_TTL_SECONDS
    payload = {
        "schema_version": "1.0.0",
        "kind": "source_retention_purge_plan",
        "plan_id": report.plan_id,
        "source_id": candidate.object_id,
        "snapshot_id": snapshot_id,
        "created_at": created.isoformat().replace("+00:00", "Z"),
        "expires_at": datetime.fromtimestamp(expires, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "candidate": asdict(candidate),
        "report": serialize_retention_dry_run(report),
    }
    path = _source_retention_plan_path(operations_root, report.plan_id)
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    try:
        with path.open("x", encoding="utf-8", newline="\n") as output:
            output.write(encoded)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
    except FileExistsError:
        existing = _read_source_retention_plan(
            operations_root=operations_root,
            plan_id=report.plan_id,
        )
        if existing != payload:
            raise SourceRetentionPurgeError("Source purge plan identity collided")
    return str(payload["expires_at"])


def _read_source_retention_plan(
    *, operations_root: Path, plan_id: str
) -> dict[str, object]:
    path = _source_retention_plan_path(operations_root, plan_id)
    if not path.is_file() or path.is_symlink():
        raise SourceRetentionPurgeError("Source purge plan is unavailable")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceRetentionPurgeError("Source purge plan is unreadable") from error
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != "1.0.0"
        or payload.get("kind") != "source_retention_purge_plan"
        or payload.get("plan_id") != plan_id
    ):
        raise SourceRetentionPurgeError("Source purge plan is invalid")
    return payload


def _retention_inventory(
    *,
    container: ApiContainerDep,
    store: JsonObjectStore,
    settings: RebuildStorageSettings,
) -> VaultRetentionInventory:
    resolution = product_repositories._document_repository_resolution(container.root_dir, store, settings)
    document_authority = (
        "sqlite_document"
        if resolution.authority_identity == "sqlite:structured-records-v1"
        else "json_document"
    )
    database = (
        Path(container.root_dir) / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    ).resolve(strict=False)
    additional: tuple[RetentionExternalRecord, ...] = ()
    if database.is_file() and not database.is_symlink():
        additional = tuple(
            RetentionExternalRecord(
                authority="sqlite_structured_record",
                collection=record.collection,
                object_id=record.object_id,
                payload=dict(record.payload),
                revision=record.revision,
            )
            for record in SQLiteStructuredRecordStore(database).list_all()
        )
    return VaultRetentionInventory(
        active_vault_root=Path(container.root_dir) / ".rebuild-data",
        source_store=store,
        document_repository=resolution.repository,
        document_authority=document_authority,
        additional_records=additional,
        reference_catalog_complete=True,
    )


def _source_retention_report(
    *,
    container: ApiContainerDep,
    store: JsonObjectStore,
    settings: RebuildStorageSettings,
    source_id: str,
    snapshot_id: str,
    evaluated_at: datetime,
) -> tuple[RetentionCandidate, RetentionDryRunReport]:
    _active_root, snapshots_root, _operations_root = product_recovery_points._vault_recovery_roots(
        container.root_dir
    )
    if re.fullmatch(r"snap-[a-z0-9][a-z0-9._-]{0,122}", snapshot_id) is None:
        raise SourceRetentionPurgeError("Source purge snapshot identity is invalid")
    snapshot_root = (snapshots_root / snapshot_id).resolve(strict=False)
    if (
        snapshot_root.parent != snapshots_root.resolve(strict=False)
        or product_recovery_points._recovery_point_record(snapshot_root) is None
    ):
        raise SourceRetentionPurgeError("Source purge recovery point is unavailable")
    candidates = tuple(
        candidate
        for candidate in _retention_inventory(
            container=container,
            store=store,
            settings=settings,
        ).scan()
        if candidate.aggregate_type == "source" and candidate.object_id == source_id
    )
    if len(candidates) != 1:
        raise SourceRetentionPurgeError(
            "Source purge requires one deleted Source candidate"
        )
    evidence = build_retention_backup_evidence(
        snapshot_root=snapshot_root,
        active_vault_root=Path(container.root_dir) / ".rebuild-data",
    )
    report = BuildRetentionDryRun().execute(
        candidates=candidates,
        backup_evidence=evidence,
        evaluated_at=evaluated_at,
    )
    return candidates[0], report


def _retention_record(value: object) -> RetentionRecord:
    if not isinstance(value, Mapping):
        raise SourceRetentionPurgeError("Source purge plan record is invalid")
    return RetentionRecord(
        authority=str(value.get("authority", "")),
        collection=str(value.get("collection", "")),
        object_id=str(value.get("object_id", "")),
        revision=value.get("revision") if isinstance(value.get("revision"), int) else None,
    )


def _retention_reference(value: object) -> RetentionReference:
    if not isinstance(value, Mapping):
        raise SourceRetentionPurgeError("Source purge plan reference is invalid")
    return RetentionReference(
        authority=str(value.get("authority", "")),
        collection=str(value.get("collection", "")),
        object_id=str(value.get("object_id", "")),
        field_path=str(value.get("field_path", "")),
    )


def _source_retention_plan_objects(
    payload: Mapping[str, object],
) -> tuple[RetentionCandidate, RetentionDryRunReport]:
    candidate_raw = payload.get("candidate")
    report_raw = payload.get("report")
    if not isinstance(candidate_raw, Mapping) or not isinstance(report_raw, Mapping):
        raise SourceRetentionPurgeError("Source purge plan payload is invalid")
    owned_raw = candidate_raw.get("owned_records")
    inbound_raw = candidate_raw.get("inbound_references")
    items_raw = report_raw.get("items")
    backup_raw = report_raw.get("backup_evidence")
    if (
        not isinstance(owned_raw, list)
        or not isinstance(inbound_raw, list)
        or not isinstance(items_raw, list)
        or not isinstance(backup_raw, Mapping)
    ):
        raise SourceRetentionPurgeError("Source purge plan payload is invalid")
    candidate = RetentionCandidate(
        aggregate_type=str(candidate_raw.get("aggregate_type", "")),
        object_id=str(candidate_raw.get("object_id", "")),
        authority=str(candidate_raw.get("authority", "")),
        revision=int(candidate_raw.get("revision", 0)),
        lifecycle_status=str(candidate_raw.get("lifecycle_status", "")),
        lifecycle_at=candidate_raw.get("lifecycle_at")
        if isinstance(candidate_raw.get("lifecycle_at"), str)
        else None,
        undo_expires_at=candidate_raw.get("undo_expires_at")
        if isinstance(candidate_raw.get("undo_expires_at"), str)
        else None,
        observed_vault_fingerprint=str(
            candidate_raw.get("observed_vault_fingerprint", "")
        ),
        inventory_complete=candidate_raw.get("inventory_complete") is True,
        owned_records=tuple(_retention_record(item) for item in owned_raw),
        inbound_references=tuple(_retention_reference(item) for item in inbound_raw),
    )
    backup = RetentionBackupEvidence(
        status=str(backup_raw.get("status", "")),
        snapshot_id=backup_raw.get("snapshot_id")
        if isinstance(backup_raw.get("snapshot_id"), str)
        else None,
        snapshot_fingerprint=backup_raw.get("snapshot_fingerprint")
        if isinstance(backup_raw.get("snapshot_fingerprint"), str)
        else None,
        active_fingerprint=backup_raw.get("active_fingerprint")
        if isinstance(backup_raw.get("active_fingerprint"), str)
        else None,
        file_count=backup_raw.get("file_count")
        if isinstance(backup_raw.get("file_count"), int)
        else None,
        error_code=backup_raw.get("error_code")
        if isinstance(backup_raw.get("error_code"), str)
        else None,
    )
    items: list[RetentionDryRunItem] = []
    for raw in items_raw:
        if not isinstance(raw, Mapping):
            raise SourceRetentionPurgeError("Source purge plan item is invalid")
        raw_owned = raw.get("owned_records")
        raw_inbound = raw.get("inbound_references")
        blockers = raw.get("blockers")
        if (
            not isinstance(raw_owned, list)
            or not isinstance(raw_inbound, list)
            or not isinstance(blockers, list)
        ):
            raise SourceRetentionPurgeError("Source purge plan item is invalid")
        items.append(
            RetentionDryRunItem(
                aggregate_type=str(raw.get("aggregate_type", "")),
                object_id=str(raw.get("object_id", "")),
                authority=str(raw.get("authority", "")),
                revision=int(raw.get("revision", 0)),
                eligible=raw.get("eligible") is True,
                eligible_after=raw.get("eligible_after")
                if isinstance(raw.get("eligible_after"), str)
                else None,
                blockers=tuple(str(item) for item in blockers),
                owned_records=tuple(_retention_record(item) for item in raw_owned),
                inbound_references=tuple(
                    _retention_reference(item) for item in raw_inbound
                ),
            )
        )
    report = RetentionDryRunReport(
        schema_version=str(report_raw.get("schema_version", "")),
        plan_id=str(report_raw.get("plan_id", "")),
        evaluated_at=str(report_raw.get("evaluated_at", "")),
        execution_supported=report_raw.get("execution_supported") is True,
        approval_token=None,
        backup_evidence=backup,
        items=tuple(items),
    )
    return candidate, report


def _configured_source_media_roots(store: object) -> tuple[Path, ...]:
    read = getattr(store, "read", None)
    if not callable(read):
        return ()
    settings = read("video_audio_extractor_settings", "default")
    if not isinstance(settings, Mapping):
        return ()
    raw = settings.get("output_root")
    if not isinstance(raw, str) or not raw.strip():
        return ()
    root = Path(raw).expanduser()
    return (root,) if root.is_absolute() else ()


def _source_retention_receipt_exists(
    store: JsonObjectStore, *, plan_id: str, source_id: str
) -> bool:
    return any(
        record.get("plan_id") == plan_id and record.get("source_id") == source_id
        for collection in (
            "source_retention_purge_receipts",
            "source_retention_purge_intents",
            "source_retention_purge_operations",
        )
        for record in store.list(collection)
    )


@router.post("/api/rebuild/retention/source-purge/plan")
async def create_source_retention_purge_plan(
    request: Request, container: ApiContainerDep
) -> JSONResponse:
    """Build a short-lived, body-free Source purge plan with a verified backup."""

    store, settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request) or {}
    source_id = str(body.get("source_id", "") or "").strip()
    snapshot_id = str(body.get("snapshot_id", "") or "").strip()
    active_root, snapshots_root, operations_root = product_recovery_points._vault_recovery_roots(
        container.root_dir
    )
    try:
        if not source_id:
            raise SourceRetentionPurgeError("Source purge requires source_id")
        source = store.read_including_deleted("sources", source_id)
        lifecycle = source.get("library_lifecycle") if isinstance(source, Mapping) else None
        if (
            not isinstance(lifecycle, Mapping)
            or lifecycle.get("status") != "deleted"
        ):
            raise SourceRetentionPurgeError(
                "Source purge requires one deleted Source candidate"
            )
        if not snapshot_id:
            snapshot_id = f"snap-retention-{time.time_ns():x}"
            backup = create_vault_backup(
                source_root=active_root,
                backups_root=snapshots_root,
                snapshot_id=snapshot_id,
            )
            now_text = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            product_recovery_points._write_recovery_point_record(
                backup.snapshot_root,
                {
                    "schema_version": "2.0.0",
                    "id": snapshot_id,
                    "created_at": now_text,
                    "label": "Source retention purge safety backup",
                    "namespace_id": settings.namespace_id,
                    "layer_fingerprints": {},
                    "layer_counts": {},
                    "notes": "",
                    "restorable": True,
                    "vault_fingerprint": backup.source_fingerprint,
                    "backup_file_count": backup.file_count,
                },
            )
        candidate, report = _source_retention_report(
            container=container,
            store=store,
            settings=settings,
            source_id=source_id,
            snapshot_id=snapshot_id,
            evaluated_at=datetime.now(timezone.utc),
        )
        if report.items and report.items[0].eligible:
            ExecuteSourceRetentionPurge(
                store,
                active_fingerprint=lambda: fingerprint_vault_root(active_root),
                owned_media_roots=_configured_source_media_roots(store),
            ).preflight_owned_records(candidate)
        expires_at = _write_source_retention_plan(
            operations_root=operations_root,
            candidate=candidate,
            report=report,
            snapshot_id=snapshot_id,
        )
        payload = serialize_retention_dry_run(report)
        payload.update(
            {
                "source_id": source_id,
                "snapshot_id": snapshot_id,
                "expires_at": expires_at,
                "purge_supported": True,
            }
        )
        return product_http._json_response(200, payload, product_http._no_store_headers())
    except (
        AggregateRepositoryFactoryError,
        OSError,
        RetentionInventoryError,
        SourceRetentionPurgeError,
        VaultBackupRestoreError,
    ) as error:
        return product_http._json_response(400, {"detail": str(error)}, product_http._no_store_headers())


@router.get("/api/rebuild/retention/source-purge/candidates")
def list_source_retention_purge_candidates(
    request: Request, container: ApiContainerDep
) -> JSONResponse:
    """List deleted Sources without returning their original body or local paths."""

    store, _settings = product_repositories._object_store(container.root_dir)
    items = _source_retention_candidate_payloads(
        store,
        now=datetime.now(timezone.utc),
    )
    return product_http._json_response(200, {"items": items}, product_http._no_store_headers())


def _source_retention_candidate_payloads(
    store,
    *,
    now: datetime,
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for source in store.list_including_deleted("sources"):
        lifecycle = source.get("library_lifecycle")
        source_id = source.get("id")
        if (
            not isinstance(lifecycle, Mapping)
            or lifecycle.get("status") != "deleted"
            or not isinstance(source_id, str)
            or not source_id
        ):
            continue
        expires_text = lifecycle.get("undo_expires_at")
        try:
            expires = datetime.fromisoformat(
                str(expires_text or "").replace("Z", "+00:00")
            )
        except ValueError:
            expires = None
        items.append(
            {
                "source_id": source_id,
                "title": str(source.get("title", "") or source_id),
                "revision": store.revision("sources", source_id),
                "deleted_at": lifecycle.get("deleted_at")
                if isinstance(lifecycle.get("deleted_at"), str)
                else None,
                "undo_expires_at": expires_text
                if isinstance(expires_text, str)
                else None,
                "retention_elapsed": bool(
                    expires is not None
                    and expires.tzinfo is not None
                    and now >= expires.astimezone(timezone.utc)
                ),
            }
        )
    items.sort(key=lambda item: (str(item["deleted_at"]), str(item["source_id"])))
    return items


@router.post("/api/rebuild/retention/source-purge")
async def execute_source_retention_purge(
    request: Request, container: ApiContainerDep
) -> JSONResponse:
    """Execute or resume one exact Source purge plan after explicit confirmation."""

    store, settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request) or {}
    source_id = str(body.get("source_id", "") or "").strip()
    plan_id = str(body.get("plan_id", "") or "").strip()
    expected_revision = body.get("expected_revision")
    confirm = body.get("confirm")
    _active_root, _snapshots_root, operations_root = product_recovery_points._vault_recovery_roots(
        container.root_dir
    )
    try:
        plan = _read_source_retention_plan(
            operations_root=operations_root,
            plan_id=plan_id,
        )
        if plan.get("source_id") != source_id:
            raise SourceRetentionPurgeError("Source purge plan identity mismatch")
        candidate, report = _source_retention_plan_objects(plan)
        has_receipt = _source_retention_receipt_exists(
            store, plan_id=plan_id, source_id=source_id
        )
        if not has_receipt:
            expires_at = datetime.fromisoformat(
                str(plan.get("expires_at", "")).replace("Z", "+00:00")
            )
            if expires_at.tzinfo is None or datetime.now(timezone.utc) > expires_at:
                raise SourceRetentionPurgeError("Source purge plan expired")
            evaluated_at = datetime.fromisoformat(
                report.evaluated_at.replace("Z", "+00:00")
            )
            current_candidate, current_report = _source_retention_report(
                container=container,
                store=store,
                settings=settings,
                source_id=source_id,
                snapshot_id=str(plan.get("snapshot_id", "")),
                evaluated_at=evaluated_at,
            )
            if (
                asdict(current_candidate) != asdict(candidate)
                or serialize_retention_dry_run(current_report)
                != serialize_retention_dry_run(report)
            ):
                raise SourceRetentionPurgeError(
                    "Source purge inventory changed after planning"
                )
            candidate, report = current_candidate, current_report
        result = ExecuteSourceRetentionPurge(
            store,
            effect_runner=request.app.state.effect_runtime.runner,
            active_fingerprint=lambda: fingerprint_vault_root(
                Path(container.root_dir) / ".rebuild-data"
            ),
            owned_media_roots=_configured_source_media_roots(store),
        ).execute(
            candidate=candidate,
            report=report,
            plan_id=plan_id,
            expected_source_revision=(
                expected_revision
                if isinstance(expected_revision, int)
                and not isinstance(expected_revision, bool)
                else 0
            ),
            confirm=confirm is True,
        )
        return product_http._json_response(200, asdict(result), product_http._no_store_headers())
    except (
        AggregateRepositoryFactoryError,
        OSError,
        RetentionInventoryError,
        SourceRetentionPurgeError,
        TypeError,
        ValueError,
        VaultBackupRestoreError,
    ) as error:
        return product_http._json_response(409, {"detail": str(error)}, product_http._no_store_headers())
