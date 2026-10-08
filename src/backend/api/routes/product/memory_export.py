"""Memory export ownership for the product API."""
from __future__ import annotations

import base64, json, time, uuid
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from backend.api.container import ApiContainerDep
from backend.api.memory_asset_package_importer import (
    MemoryAssetPackageImportError,
    import_memory_asset_package,
    parse_memory_asset_package,
)
from backend.api.memory_export_assembler import MemoryExportAssemblyError, assemble_memory_export

from core.product_core.memory_export_framework import (
    AssetPackageImportReport,
    ExportResult,
    ExportScope,
    MemoryExportError,
    export_memory,
    import_asset_package,
    serialize_export_result,
    serialize_export_scope,
)

from . import http as product_http
from . import memory_import_records as product_memory_import_records
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


def _build_memory_export(
    container: Any,
    body: Mapping[str, object],
) -> ExportResult:
    store, settings = product_repositories._object_store(container.root_dir)
    preset_id = str(body.get("preset", "") or "")
    project_id = str(body.get("project_id", "") or "")
    assembly = assemble_memory_export(
        runtime_root=container.root_dir,
        namespace_id=settings.namespace_id,
        store=store,
        project_id=project_id,
    )

    scope_dict = body.get("scope") if isinstance(body.get("scope"), Mapping) else {}
    scope = ExportScope(
        preset=preset_id,
        redact_secrets=True,
        only_confirmed=bool(scope_dict.get("only_confirmed", False)),
        skip_raw_sources=bool(scope_dict.get("skip_raw_sources", False)),
        skip_av=bool(scope_dict.get("skip_av", False)),
        skip_evidence_text=bool(scope_dict.get("skip_evidence_text", False)),
        skip_low_trust=bool(scope_dict.get("skip_low_trust", True)),
        skip_conflicts=bool(scope_dict.get("skip_conflicts", False)),
        skip_provider_audit=bool(scope_dict.get("skip_provider_audit", True)),
        include_paths=bool(scope_dict.get("include_paths", False)),
    )

    import uuid as _uuid
    export_batch_id = f"export-{_uuid.uuid4().hex[:12]}"
    try:
        result: ExportResult = export_memory(
            assembly.payload,
            scope,
            export_batch_id=export_batch_id,
        )
    except MemoryExportError as exc:
        raise ValueError(str(exc)) from exc
    return result


@router.post("/api/rebuild/memory/export")
async def memory_export(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    """Return bounded metadata for a memory export without placing bytes in JSON."""
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping):
        return product_memory_import_records._stage16_error_response("memory export rejected", "request body is required")
    if not str(body.get("preset", "") or ""):
        return product_memory_import_records._stage16_error_response("memory export rejected", "preset is required")
    try:
        result = _build_memory_export(container, body)
    except MemoryExportAssemblyError as exc:
        return product_memory_import_records._stage16_error_response(
            "memory export authority unavailable",
            str(exc),
            status_code=409,
        )
    except ValueError as exc:
        return product_memory_import_records._stage16_error_response("memory export rejected", str(exc))
    if result.error:
        return product_memory_import_records._stage16_error_response("memory export rejected", result.error)

    return product_memory_import_records._stage16_ok_response(serialize_export_result(result))


@router.post("/api/rebuild/memory/export/file")
async def memory_export_file(
    request: Request, container: ApiContainerDep,
) -> Response:
    """Render the selected preset as an attachment for the main desktop process."""
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping):
        return product_memory_import_records._stage16_error_response("memory export rejected", "request body is required")
    if not str(body.get("preset", "") or ""):
        return product_memory_import_records._stage16_error_response("memory export rejected", "preset is required")
    try:
        result = _build_memory_export(container, body)
    except MemoryExportAssemblyError as exc:
        return product_memory_import_records._stage16_error_response(
            "memory export authority unavailable",
            str(exc),
            status_code=409,
        )
    except ValueError as exc:
        return product_memory_import_records._stage16_error_response("memory export rejected", str(exc))
    if result.error or not result.file_name:
        return product_memory_import_records._stage16_error_response(
            "memory export rejected",
            result.error or "export filename is empty",
        )
    media_types = {
        "zip": "application/zip",
        "markdown": "text/markdown; charset=utf-8",
        "prompt_text": "text/markdown; charset=utf-8",
        "ndjson": "application/x-ndjson; charset=utf-8",
    }
    return Response(
        content=result.bytes_payload,
        media_type=media_types.get(result.format, "application/octet-stream"),
        headers={
            "Content-Disposition": f'attachment; filename="{result.file_name}"',
            "X-Chriptmas-Export-Format": result.format,
            "X-Chriptmas-Export-Batch": result.export_batch_id,
            "Cache-Control": "no-store",
        },
    )


@router.post("/api/rebuild/memory/export/preview")
async def memory_export_preview(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    """导出预览（含脱敏采样）。body: { preset, scope, project_id? }"""
    store, settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping):
        return product_memory_import_records._stage16_error_response("export preview rejected", "request body is required")
    preset_id = str(body.get("preset", "") or "")
    if not preset_id:
        return product_memory_import_records._stage16_error_response("export preview rejected", "preset is required")

    project_id = str(body.get("project_id", "") or "")
    try:
        assembly = assemble_memory_export(
            runtime_root=container.root_dir,
            namespace_id=settings.namespace_id,
            store=store,
            project_id=project_id,
        )
    except MemoryExportAssemblyError as exc:
        return product_memory_import_records._stage16_error_response(
            "memory export authority unavailable",
            str(exc),
            status_code=409,
        )

    scope_dict = body.get("scope") if isinstance(body.get("scope"), Mapping) else {}
    scope = ExportScope(
        preset=preset_id,
        redact_secrets=True,
        only_confirmed=bool(scope_dict.get("only_confirmed", False)),
        skip_raw_sources=bool(scope_dict.get("skip_raw_sources", False)),
        skip_av=bool(scope_dict.get("skip_av", False)),
        skip_evidence_text=bool(scope_dict.get("skip_evidence_text", False)),
        skip_low_trust=bool(scope_dict.get("skip_low_trust", True)),
        skip_conflicts=bool(scope_dict.get("skip_conflicts", False)),
        skip_provider_audit=bool(scope_dict.get("skip_provider_audit", True)),
        include_paths=bool(scope_dict.get("include_paths", False)),
    )

    # 生成脱敏采样
    redaction_samples: list[dict[str, str]] = []
    from core.product_core.memory_quality_gate import detect_secrets, redact_content
    for mem in assembly.payload.memories[:5]:
        secrets = detect_secrets(mem.content)
        if secrets:
            redaction_samples.append({
                "before": "[敏感内容已隐藏]",
                "after": redact_content(mem.content)[:200],
                "pattern": ", ".join(secrets[:3]),
            })

    preview_result = export_memory(
        assembly.payload,
        scope,
        export_batch_id="export-preview",
    )
    if preview_result.error:
        return product_memory_import_records._stage16_error_response(
            "export preview rejected",
            preview_result.error,
        )
    manifest_preview: dict[str, object] = {
        "format": "memory_asset_package",
        "version": "1.0",
        "memory_count": preview_result.memory_count,
        "source_count": preview_result.source_count,
        "tag_count": len(assembly.payload.tags),
        "evidence_count": len(assembly.payload.evidence_links),
        "files": [],
    }
    if preset_id == "full_asset_package" and preview_result.bytes_payload:
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(preview_result.bytes_payload), "r") as archive:
            manifest_preview.update(json.loads(archive.read("manifest.json")))

    return product_memory_import_records._stage16_ok_response({
        "preset": preset_id,
        "scope": serialize_export_scope(scope),
        "redaction_samples": redaction_samples,
        "manifest": manifest_preview,
        "total_memories": len(assembly.payload.memories),
        "authority_identity": assembly.authority_identity,
        "published_memory_count": assembly.published_count,
        "candidate_memory_count": assembly.candidate_count,
    })


@router.post("/api/rebuild/memory/export/round-trip")
async def memory_export_round_trip(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    """资产包回导校验：上传 ZIP，校验 manifest + 重建到 ObjectStore。

    body: { zip_base64 }  或 multipart（沿用 base64 模式）
    """
    import base64
    import time
    import uuid

    store, settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping):
        return product_memory_import_records._stage16_error_response("round-trip rejected", "request body is required")
    zip_b64 = str(body.get("zip_base64", "") or "")
    if not zip_b64:
        return product_memory_import_records._stage16_error_response("round-trip rejected", "zip_base64 is required")

    try:
        zip_bytes = base64.b64decode(zip_b64, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        return product_memory_import_records._stage16_error_response("round-trip rejected", f"invalid base64: {exc}")

    try:
        package = parse_memory_asset_package(zip_bytes)
    except MemoryAssetPackageImportError as exc:
        return product_memory_import_records._stage16_error_response(
            "round-trip rejected",
            str(exc),
            extra={"is_valid": False, "rebuilt": False},
        )

    report: AssetPackageImportReport = import_asset_package(zip_bytes)
    if not report.is_valid:
        return product_memory_import_records._stage16_error_response(
            "round-trip rejected",
            report.summary,
            extra={
                "is_valid": False,
                "rebuilt": False,
                "rebuilt_count": 0,
                "errors": list(report.errors),
                "warnings": list(report.warnings),
                "summary": report.summary,
            },
        )
    import_batch_id = f"roundtrip-{uuid.uuid4().hex[:12]}"
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    processing_batch = {
        "batch_id": import_batch_id,
        "source_type": "roundtrip",
        "status": "processing",
        "runtime_session_id": product_memory_import_records._MEMORY_IMPORT_RUNTIME_SESSION_ID,
        "total": len(package.memories) + len(package.project_skills),
        "succeeded": 0,
        "failed": 0,
        "needs_review": 0,
        "candidate_count": 0,
        "skipped": 0,
        "conflicted": 0,
        "occurred_at": created_at,
        "recorded_at": created_at,
        "created_at": created_at,
        "completed_at": "",
        "failures": [],
        "conflict_ids": [],
        "series": [],
        "items": [],
    }
    try:
        store.write(
            product_memory_import_records._MEMORY_IMPORT_BATCHES_COLLECTION,
            import_batch_id,
            processing_batch,
            expected_revision=0,
        )
    except Exception as exc:  # noqa: BLE001
        return product_memory_import_records._stage16_error_response(
            "round-trip rejected",
            f"failed to initialize import batch: {exc}",
            status_code=500,
            extra={
                "is_valid": True,
                "rebuilt": False,
                "rebuilt_count": 0,
            },
        )
    result = import_memory_asset_package(
        store=store,
        package=package,
        import_batch_id=import_batch_id,
        created_at=created_at,
        library_root=container.root_dir / "library",
    )

    # 记录 round-trip 批次
    processed_count = (
        result.imported_count
        + result.skipped_count
        + result.conflict_count
        + result.source_imported_count
        + result.source_skipped_count
        + result.source_conflict_count
        + result.asset_imported_count
        + result.asset_skipped_count
        + result.asset_conflict_count
    )
    total_failed = (
        result.failed_count
        + result.source_failed_count
        + result.asset_failed_count
    )
    status = "completed" if total_failed == 0 else (
        "partial" if processed_count else "failed"
    )
    batch_record = {
        "batch_id": import_batch_id,
        "operation_kind": "memory_asset_package_import",
        "operation_id": import_batch_id,
        "source_type": "roundtrip",
        "status": status,
        "total": len(package.memories) + len(package.project_skills),
        "succeeded": result.imported_count,
        "failed": total_failed,
        "needs_review": result.imported_count + result.conflict_count,
        "candidate_count": result.imported_count,
        "skipped": result.skipped_count,
        "conflicted": result.conflict_count,
        "source_imported": result.source_imported_count,
        "source_skipped": result.source_skipped_count,
        "source_conflicted": result.source_conflict_count,
        "source_failed": result.source_failed_count,
        "asset_imported": result.asset_imported_count,
        "asset_skipped": result.asset_skipped_count,
        "asset_conflicted": result.asset_conflict_count,
        "asset_failed": result.asset_failed_count,
        "project_skill_imported": result.project_skill_imported_count,
        "project_skill_skipped": result.project_skill_skipped_count,
        "project_skill_conflicted": result.project_skill_conflict_count,
        "project_skill_failed": result.project_skill_failed_count,
        "occurred_at": created_at,
        "recorded_at": created_at,
        "delta_summary": (
            f"round-trip 导入 {result.imported_count} 条候选，"
            f"跳过 {result.skipped_count} 条相同记录，"
            f"生成 {result.conflict_count} 条冲突，"
            f"失败 {result.failed_count} 条；"
            f"来源导入 {result.source_imported_count} 条，"
            f"跳过 {result.source_skipped_count} 条，"
            f"冲突 {result.source_conflict_count} 条，"
            f"失败 {result.source_failed_count} 条；"
            f"原档恢复 {result.asset_imported_count} 个，"
            f"跳过 {result.asset_skipped_count} 个，"
            f"冲突 {result.asset_conflict_count} 个，"
            f"失败 {result.asset_failed_count} 个。"
        ),
        "created_at": created_at,
        "completed_at": created_at,
        "failures": list(result.failures),
        "conflict_ids": list(result.conflict_ids),
        "series": [],
        "items": [],
    }
    try:
        store.write(
            product_memory_import_records._MEMORY_IMPORT_BATCHES_COLLECTION,
            import_batch_id,
            batch_record,
            expected_revision=1,
        )
    except Exception as exc:  # noqa: BLE001
        return product_memory_import_records._stage16_error_response(
            "round-trip rejected",
            f"failed to persist import batch: {exc}",
            status_code=500,
            extra={
                "is_valid": True,
                "rebuilt": (
                    result.imported_count > 0
                    or result.source_imported_count > 0
                    or result.asset_imported_count > 0
                ),
                "rebuilt_count": result.imported_count,
                "import_batch_id": import_batch_id,
                "status": "processing",
            },
        )

    return product_memory_import_records._stage16_ok_response({
        "is_valid": True,
        "import_batch_id": import_batch_id,
        "status": status,
        "rebuilt": (
            result.imported_count > 0
            or result.source_imported_count > 0
            or result.asset_imported_count > 0
        ),
        "rebuilt_count": result.imported_count,
        "skipped_count": result.skipped_count,
        "conflict_count": result.conflict_count,
        "failed_count": total_failed,
        "conflict_ids": list(result.conflict_ids),
        "source_imported_count": result.source_imported_count,
        "source_skipped_count": result.source_skipped_count,
        "source_conflict_count": result.source_conflict_count,
        "source_failed_count": result.source_failed_count,
        "source_conflict_ids": list(result.source_conflict_ids),
        "asset_imported_count": result.asset_imported_count,
        "asset_skipped_count": result.asset_skipped_count,
        "asset_conflict_count": result.asset_conflict_count,
        "asset_failed_count": result.asset_failed_count,
        "asset_conflict_ids": list(result.asset_conflict_ids),
        "project_skill_imported_count": result.project_skill_imported_count,
        "project_skill_skipped_count": result.project_skill_skipped_count,
        "project_skill_conflict_count": result.project_skill_conflict_count,
        "project_skill_failed_count": result.project_skill_failed_count,
        "manifest": dict(package.manifest),
        "errors": list(report.errors),
        "warnings": list(report.warnings),
        "summary": batch_record["delta_summary"],
    }, status_code=200 if total_failed == 0 else 207)
