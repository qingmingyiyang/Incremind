"""Memory import ownership for the product API."""
from __future__ import annotations

import base64, binascii, hashlib, re, time, uuid
from collections.abc import Mapping
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.api.external_import_persistence import (
    ExternalImportPersistenceError,
    persist_external_import,
)

from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.product_core.external_import_framework import (
    ExternalImportError,
    ExternalImportResult,
    extract_docx_text,
    extract_pdf_text,
    get_default_adapters,
    import_external_bundle,
)
from core.product_core.workbench_original_asset import (
    StoreWorkbenchOriginalAsset,
    WorkbenchOriginalAssetError,
    link_workbench_original_asset_to_source,
)

from . import http as product_http
from . import memory_import_records as product_memory_import_records
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


@router.post("/api/rebuild/memory/import-preflight")
async def memory_import_preflight(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    """文件预检：类型 / 数量 / 大小 / 是否需 OCR/ASR / 是否可本地处理 / 是否疑似重复。"""
    store, _settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping):
        return product_memory_import_records._stage16_error_response("import preflight rejected", "request body is required")
    raw_items = body.get("files")
    if not isinstance(raw_items, list) or not raw_items:
        return product_memory_import_records._stage16_error_response(
            "import preflight rejected", "files must be a non-empty array"
        )

    from core.product_core.external_import_framework import (
        detect_file_category as _detect_file_category,  # 复用前端 detectFileCategory 同款逻辑
    )

    items: list[dict[str, object]] = []
    for idx, raw in enumerate(raw_items):
        if not isinstance(raw, Mapping):
            continue
        name = str(raw.get("name", f"file-{idx}"))
        size = int(raw.get("size", 0) or 0)
        media_type = str(raw.get("type", "") or "application/octet-stream")
        sha256 = str(raw.get("sha256", "") or "").lower()
        if sha256 and not re.fullmatch(r"[0-9a-f]{64}", sha256):
            return product_memory_import_records._stage16_error_response(
                "import preflight rejected",
                f"files[{idx}].sha256 must be a lowercase SHA-256 hex digest",
            )
        category = _detect_file_category(name)
        needs_ocr = category == "image"
        needs_asr = category == "audio"
        needs_video_transcribe = category == "video"
        needs_external_provider = (
            needs_ocr or needs_asr or needs_video_transcribe
            or name.lower().endswith(".pdf") or name.lower().endswith(".docx")
        )
        size_mb = size / (1024 * 1024) if size else 0.0
        too_large = size_mb > 100.0
        items.append({
            "name": name,
            "size": size,
            "size_mb": round(size_mb, 2),
            "media_type": media_type,
            "category": category,
            "parseable": category != "other",
            "needs_ocr": needs_ocr,
            "needs_asr": needs_asr,
            "needs_video_transcribe": needs_video_transcribe,
            "needs_external_provider": needs_external_provider,
            "can_process_locally": not needs_external_provider,
            "too_large": too_large,
            "warning": "文件较大，处理可能较慢" if too_large else "",
            "sha256": sha256,
        })

    # 原档身份由 StoreWorkbenchOriginalAsset 的完整 SHA-256 决定。
    duplicates: list[dict[str, object]] = []
    for item in items:
        sha256 = str(item.get("sha256", "") or "")
        if not sha256:
            continue
        asset_id = f"original-file-{sha256[:16]}"
        existing_asset = store.read("workbench_original_assets", asset_id)
        if existing_asset is not None and existing_asset.get("sha256") == sha256:
            duplicates.append({
                "name": item.get("name"),
                "sha256": sha256,
                "asset_id": asset_id,
                "existing_title": str(
                    existing_asset.get("display_name")
                    or item.get("name", "")
                ).rsplit(".", 1)[0],
                "resolution": "needs_review",
            })

    summary = {
        "total": len(items),
        "parseable": sum(1 for i in items if i.get("parseable")),
        "needs_external_provider": sum(1 for i in items if i.get("needs_external_provider")),
        "can_process_locally": sum(1 for i in items if i.get("can_process_locally")),
        "too_large": sum(1 for i in items if i.get("too_large")),
        "duplicates": len(duplicates),
        "by_category": items and {
            cat: sum(1 for i in items if i.get("category") == cat)
            for cat in {i.get("category", "other") for i in items}
        } or {},
    }
    return product_memory_import_records._stage16_ok_response({
        "items": items,
        "summary": summary,
        "duplicates": duplicates,
    })


@router.post("/api/rebuild/memory/import-batch")
async def memory_import_batch(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    """批量导入：解码 base64 文件，写 ObjectStore，生成 memory 候选，记录批次。"""
    import base64
    import io
    import time
    import uuid

    store, settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping):
        return product_memory_import_records._stage16_error_response("import batch rejected", "request body is required")
    raw_files = body.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        return product_memory_import_records._stage16_error_response(
            "import batch rejected", "files must be a non-empty array"
        )

    batch_id = f"batch-{uuid.uuid4().hex[:12]}"
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    project_id = str(body.get("project_id", "") or "")
    duplicate_resolutions = body.get("duplicate_resolutions", {})
    if not isinstance(duplicate_resolutions, Mapping):
        return product_memory_import_records._stage16_error_response(
            "import batch rejected", "duplicate_resolutions must be an object"
        )
    allowed_duplicate_resolutions = {"skip", "import_anyway", "l0_only"}
    normalized_duplicate_resolutions: dict[str, str] = {}
    for digest, resolution in duplicate_resolutions.items():
        clean_digest = str(digest).lower()
        clean_resolution = str(resolution)
        if (
            not re.fullmatch(r"[0-9a-f]{64}", clean_digest)
            or clean_resolution not in allowed_duplicate_resolutions
        ):
            return product_memory_import_records._stage16_error_response(
                "import batch rejected", "duplicate resolution is invalid"
            )
        existing_asset = store.read(
            "workbench_original_assets", f"original-file-{clean_digest[:16]}"
        )
        if existing_asset is None or existing_asset.get("sha256") != clean_digest:
            return product_memory_import_records._stage16_error_response(
                "import batch rejected",
                "duplicate resolution does not match an existing original asset",
            )
        normalized_duplicate_resolutions[clean_digest] = clean_resolution

    processing_batch = {
        "batch_id": batch_id,
        "source_type": "file",
        "status": "processing",
        "runtime_session_id": product_memory_import_records._MEMORY_IMPORT_RUNTIME_SESSION_ID,
        "total": len(raw_files),
        "succeeded": 0,
        "failed": 0,
        "needs_review": 0,
        "candidate_count": 0,
        "skipped": 0,
        "project_id": project_id,
        "occurred_at": created_at,
        "recorded_at": created_at,
        "created_at": created_at,
        "completed_at": "",
        "failures": [],
        "series": [],
        "items": [],
    }
    try:
        store.write(
            product_memory_import_records._MEMORY_IMPORT_BATCHES_COLLECTION,
            batch_id,
            processing_batch,
            expected_revision=0,
        )
    except Exception as exc:  # noqa: BLE001
        return product_memory_import_records._stage16_error_response(
            "import batch rejected",
            f"failed to initialize batch record: {exc}",
            status_code=500,
            extra={
                "batch_id": batch_id,
                "status": "not_started",
                "candidate_count": 0,
            },
        )

    succeeded = 0
    failed = 0
    failures: list[dict[str, object]] = []
    candidate_count = 0
    skipped_count = 0
    items_record: list[dict[str, object]] = []
    asset_store = StoreWorkbenchOriginalAsset(
        object_store=store,
        assets_root=container.root_dir / "library" / "assets" / "originals",
        namespace_id=settings.namespace_id,
        now=created_at,
    )
    source_registrar = ObjectStoreSourceRegistrar(
        store,
        namespace_id=settings.namespace_id,
        created_at=created_at,
    )

    for idx, raw in enumerate(raw_files):
        if not isinstance(raw, Mapping):
            continue
        name = str(raw.get("name", f"file-{idx}"))
        size = int(raw.get("size", 0) or 0)
        media_type = str(raw.get("type", "") or "application/octet-stream")
        content_b64 = str(raw.get("content_base64", "") or "")
        item_id = f"item-{idx}-{uuid.uuid4().hex[:8]}"

        try:
            raw_bytes = base64.b64decode(content_b64, validate=True) if content_b64 else b""
        except (ValueError, base64.binascii.Error) as exc:
            failed += 1
            failures.append({
                "id": item_id, "name": name, "error": "invalid_base64",
                "user_message": f"无法解析文件内容：{exc}",
            })
            items_record.append({"name": name, "size": size, "status": "failed"})
            continue
        if size != len(raw_bytes):
            failed += 1
            failures.append({
                "id": item_id,
                "name": name,
                "error": "size_mismatch",
                "user_message": (
                    f"文件大小不一致：声明 {size} 字节，收到 {len(raw_bytes)} 字节。"
                ),
            })
            items_record.append({"name": name, "size": size, "status": "failed"})
            continue
        content_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        duplicate_resolution = normalized_duplicate_resolutions.get(content_sha256, "")
        if duplicate_resolution == "skip":
            skipped_count += 1
            succeeded += 1
            items_record.append({
                "name": name,
                "size": size,
                "status": "skipped_duplicate",
                "duplicate_resolution": "skip",
            })
            continue

        # 抽取文本（本地优先）
        text_content = ""
        lower_name = name.lower()
        try:
            if lower_name.endswith(".pdf"):
                text_content = extract_pdf_text(raw_bytes)
            elif lower_name.endswith(".docx"):
                text_content = extract_docx_text(raw_bytes)
            elif lower_name.endswith((".md", ".txt", ".json", ".jsonl", ".csv", ".html", ".htm")):
                text_content = raw_bytes.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            text_content = ""

        if not text_content and media_type.startswith("text/"):
            try:
                text_content = raw_bytes.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                text_content = ""

        try:
            asset = asset_store.execute(
                display_name=name,
                media_type=media_type,
                size_bytes=size,
                content_base64=content_b64,
                source_kind="file",
            )
            source = source_registrar.register(SourceSubmission(
                kind="file",
                title=name.rsplit(".", 1)[0] or name,
                display_name=name,
                media_type=media_type,
                size_bytes=size,
                file_reference=asset.asset_ref,
            ))
            source_id = str(source["id"])
            source_uri = str(source["storage_uri"])
            asset_link = link_workbench_original_asset_to_source(
                object_store=store,
                namespace_id=settings.namespace_id,
                asset_ref=asset.asset_ref,
                source_id=source_id,
                source_uri=source_uri,
                now=created_at,
            )
        except (ValueError, WorkbenchOriginalAssetError) as exc:
            failed += 1
            failures.append({
                "id": item_id,
                "name": name,
                "error": "source_persistence_failed",
                "user_message": f"原档或来源保存失败：{exc}",
            })
            items_record.append({"name": name, "size": size, "status": "failed"})
            continue

        # 候选身份派生自稳定 Source；重复导入不会产生随机分叉。
        candidate_identity = hashlib.sha256(
            (
                f"{source_id}\0{project_id}\0{batch_id}"
                if duplicate_resolution == "import_anyway"
                else f"{source_id}\0{project_id}"
            ).encode("utf-8")
        ).hexdigest()[:16]
        candidate_id = f"mem-import-{candidate_identity}"
        candidate_payload = {
            "schema_version": "1.0.0",
            "id": candidate_id,
            "memory_id": candidate_id,
            "layer": "L1",
            "type": "fact",
            "content": text_content[:2000] if text_content else f"已导入文件：{name}",
            "summary": f"从文件 {name} 抽取的候选事实",
            "confidence": 0.7 if text_content else 0.4,
            "trust_level": "high",
            "source_platform": "local",
            "source_type": "document",
            "source_role": "user",
            "source_ref": source_uri,
            "evidence_refs": [source_uri, asset_link.link_ref],
            "occurred_at": created_at,
            "recorded_at": created_at,
            "created_at": created_at,
            "observed_at": created_at,
            "status": "candidate",
            "import_batch_id": batch_id,
            "privacy_level": "private",
            "provider_boundary": "local",
            "group": "needs_review",
            "project_id": project_id,
            "media_type": media_type,
            "size": size,
            "original_asset_ref": asset.asset_ref,
            "duplicate_resolution": duplicate_resolution or None,
        }
        if duplicate_resolution == "l0_only":
            skipped_count += 1
            succeeded += 1
            items_record.append({
                "name": name,
                "size": size,
                "status": "l0_only",
                "source_id": source_id,
                "asset_id": asset.asset_id,
                "duplicate_resolution": "l0_only",
            })
            continue
        existing_candidate = store.read(product_memory_import_records._MEMORY_CANDIDATES_COLLECTION, candidate_id)
        if existing_candidate is not None:
            skipped_count += 1
            succeeded += 1
            items_record.append({
                "name": name,
                "size": size,
                "status": "already_present",
                "source_id": source_id,
                "asset_id": asset.asset_id,
                "candidate_id": candidate_id,
            })
        elif product_memory_import_records._save_stage16_candidate(store, candidate_payload):
            candidate_count += 1
            succeeded += 1
            items_record.append({
                "name": name,
                "size": size,
                "status": "succeeded",
                "source_id": source_id,
                "asset_id": asset.asset_id,
                "candidate_id": candidate_id,
            })
        else:
            failed += 1
            failures.append({
                "id": item_id, "name": name, "error": "candidate_save_failed",
                "user_message": "候选保存失败，请重试。",
            })
            items_record.append({"name": name, "size": size, "status": "failed"})

    delta_summary = (
        f"本次处理 {succeeded} 条原始资料，生成 {candidate_count} 条记忆候选，"
        f"跳过 {skipped_count} 条重复候选。"
    )
    batch_record = {
        "batch_id": batch_id,
        "source_type": "file",
        "status": "completed" if failed == 0 else ("partial" if succeeded > 0 else "failed"),
        "total": len(raw_files),
        "succeeded": succeeded,
        "failed": failed,
        "needs_review": candidate_count,
        "candidate_count": candidate_count,
        "skipped": skipped_count,
        "project_id": project_id,
        "occurred_at": created_at,
        "recorded_at": created_at,
        "delta_summary": delta_summary,
        "created_at": created_at,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "failures": failures,
        "series": [],
        "items": items_record,
    }
    try:
        store.write(
            product_memory_import_records._MEMORY_IMPORT_BATCHES_COLLECTION,
            batch_id,
            batch_record,
            expected_revision=1,
        )
    except Exception as exc:  # noqa: BLE001
        return product_memory_import_records._stage16_error_response(
            "import batch rejected",
            f"failed to finalize batch record: {exc}",
            status_code=500,
            extra={
                "batch_id": batch_id,
                "status": "processing",
                "candidate_count": candidate_count,
            },
        )

    status_code = 201 if failed == 0 else (207 if succeeded > 0 else 400)
    return product_memory_import_records._stage16_ok_response({
        "batch_id": batch_id,
        "status": batch_record["status"],
        "occurred_at": batch_record["occurred_at"],
        "recorded_at": batch_record["recorded_at"],
        "succeeded": succeeded,
        "failed": failed,
        "needs_review": candidate_count,
        "candidate_count": candidate_count,
        "skipped": skipped_count,
        "delta_summary": delta_summary,
        "failures": failures,
        "series": [],
        "items": items_record,
    }, status_code=status_code)


@router.post("/api/rebuild/memory/import-external")
async def memory_import_external(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    """外部 LLM 平台导出包导入：detect → parse → normalize → candidates。"""
    import time
    import uuid

    store, settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping):
        return product_memory_import_records._stage16_error_response("external import rejected", "request body is required")

    bundle = body.get("bundle")
    bundle_base64 = body.get("bundle_base64")
    bundle_name = str(body.get("bundle_name", "bundle.json"))
    if bundle is not None and bundle_base64 is not None:
        return product_memory_import_records._stage16_error_response(
            "external import rejected", "provide exactly one of bundle or bundle_base64"
        )
    if bundle_base64 is not None:
        if body.get("bundle_encoding") != "base64" or not isinstance(bundle_base64, str):
            return product_memory_import_records._stage16_error_response(
                "external import rejected",
                "bundle_base64 requires bundle_encoding=base64",
            )
        try:
            source_bundle = base64.b64decode(bundle_base64, validate=True)
        except (binascii.Error, ValueError):
            return product_memory_import_records._stage16_error_response(
                "external import rejected", "bundle_base64 is invalid"
            )
        if len(source_bundle) > 64 * 1024 * 1024:
            return product_memory_import_records._stage16_error_response(
                "external import rejected", "decoded bundle exceeds 64 MiB"
            )
    elif not isinstance(bundle, (str, Mapping)):
        return product_memory_import_records._stage16_error_response(
            "external import rejected",
            "bundle must be a string (path/content) or object, or bundle_base64",
        )
    else:
        # bundle 可以是文件路径、JSON 字符串、或已解析对象
        if isinstance(bundle, Mapping):
            import json as _json
            source_bundle = _json.dumps(bundle, ensure_ascii=False)
        else:
            source_bundle = str(bundle)

    batch_id = f"ext-{uuid.uuid4().hex[:12]}"
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    try:
        result: ExternalImportResult = import_external_bundle(
            source_bundle,
            import_batch_id=batch_id,
            adapters=get_default_adapters(),
        )
    except ExternalImportError as exc:
        return product_memory_import_records._stage16_error_response("external import rejected", str(exc))
    if result.error:
        return product_memory_import_records._stage16_error_response("external import rejected", result.error)

    project_id = str(body.get("project_id", "") or "")
    processing_batch = {
        "batch_id": batch_id,
        "source_type": "external",
        "status": "processing",
        "runtime_session_id": product_memory_import_records._MEMORY_IMPORT_RUNTIME_SESSION_ID,
        "total": len(result.candidates),
        "succeeded": 0,
        "failed": 0,
        "needs_review": 0,
        "candidate_count": 0,
        "skipped": 0,
        "project_id": project_id,
        "occurred_at": created_at,
        "recorded_at": created_at,
        "created_at": created_at,
        "completed_at": "",
        "failures": [],
        "series": [],
        "platform": result.platform,
        "items": [],
    }
    try:
        store.write(
            product_memory_import_records._MEMORY_IMPORT_BATCHES_COLLECTION,
            batch_id,
            processing_batch,
            expected_revision=0,
        )
    except Exception as exc:  # noqa: BLE001
        return product_memory_import_records._stage16_error_response(
            "external import rejected",
            f"failed to initialize import batch: {exc}",
            status_code=500,
            extra={
                "batch_id": batch_id,
                "status": "not_started",
                "candidate_count": 0,
            },
        )
    try:
        persisted = persist_external_import(
            store=store,
            result=result,
            namespace_id=settings.namespace_id,
            project_id=project_id,
            batch_id=batch_id,
            created_at=created_at,
        )
    except ExternalImportPersistenceError as exc:
        failed_batch = {
            **processing_batch,
            "status": "failed",
            "failed": len(result.candidates),
            "completed_at": created_at,
            "failures": [{"id": batch_id, "error": str(exc)}],
        }
        failure_batch_status = "failed"
        try:
            store.write(
                product_memory_import_records._MEMORY_IMPORT_BATCHES_COLLECTION,
                batch_id,
                failed_batch,
                expected_revision=1,
            )
        except Exception:  # noqa: BLE001
            failure_batch_status = "processing"
        return product_memory_import_records._stage16_error_response(
            "external import rejected",
            str(exc),
            status_code=409,
            extra={
                "batch_id": batch_id,
                "status": failure_batch_status,
                "candidate_count": 0,
            },
        )
    saved_candidates = [
        product_memory_import_records._serialize_memory_candidate(candidate)
        for candidate in persisted.candidates
    ]
    failed_candidates = list(persisted.failures)

    # 持久化批次记录
    import_status = "completed" if not failed_candidates else (
        "partial" if saved_candidates else "failed"
    )
    batch_record = {
        "batch_id": batch_id,
        "source_type": "external",
        "status": import_status,
        "total": len(result.candidates),
        "succeeded": len(saved_candidates) + persisted.skipped_candidates,
        "failed": len(failed_candidates),
        "needs_review": len(saved_candidates),
        "candidate_count": len(saved_candidates),
        "skipped": persisted.skipped_candidates,
        "project_id": project_id,
        "occurred_at": created_at,
        "recorded_at": created_at,
        "delta_summary": (
            f"外部导入 {len(persisted.sources)} 条原始资料，"
            f"生成 {len(saved_candidates)} 条候选，"
            f"跳过 {persisted.skipped_candidates} 条重复候选，"
            f"失败 {len(failed_candidates)} 条。"
        ),
        "created_at": created_at,
        "completed_at": created_at,
        "failures": failed_candidates,
        "series": [],
        "platform": result.platform,
        "items": [
            {
                "name": source["title"],
                "source_id": source["id"],
                "source_ref": source["storage_uri"],
                "status": "succeeded",
            }
            for source in persisted.sources
        ],
    }
    try:
        store.write(
            product_memory_import_records._MEMORY_IMPORT_BATCHES_COLLECTION,
            batch_id,
            batch_record,
            expected_revision=1,
        )
    except Exception as exc:  # noqa: BLE001
        return product_memory_import_records._stage16_error_response(
            "external import rejected",
            f"failed to persist import batch: {exc}",
            status_code=500,
            extra={
                "batch_id": batch_id,
                "status": "processing",
                "candidate_count": len(saved_candidates),
            },
        )

    return product_memory_import_records._stage16_ok_response({
        "batch_id": batch_id,
        "status": import_status,
        "occurred_at": batch_record["occurred_at"],
        "recorded_at": batch_record["recorded_at"],
        "platform": result.platform,
        "sources": [{
            "source_id": source["id"],
            "source_type": source["metadata"]["external_source_type"],
            "source_role": source["metadata"]["source_role"],
            "title": source["title"],
            "content": source["metadata"]["content"][:500],
            "source_ref": source["storage_uri"],
            "raw_format": source["metadata"]["raw_format"],
            "occurred_at": product_memory_import_records._import_dual_time(source)[0],
            "recorded_at": product_memory_import_records._import_dual_time(source)[1],
        } for source in persisted.sources],
        "candidates": saved_candidates,
        "candidate_count": len(saved_candidates),
        "needs_review_count": len(saved_candidates),
        "skipped_count": persisted.skipped_candidates,
        "failed_count": len(failed_candidates),
        "failures": failed_candidates,
        "delta_summary": batch_record["delta_summary"],
        "validation": {
            "is_valid": not result.error,
            "errors": [result.error] if result.error else [],
            "warnings": [],
        },
    }, status_code=201 if not failed_candidates else (207 if saved_candidates else 500))


@router.post("/api/rebuild/memory/import-retry")
async def memory_import_retry(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    """失败项重试：根据 batch_id + item_id 重新处理。"""
    store, _settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping):
        return product_memory_import_records._stage16_error_response("import retry rejected", "request body is required")
    batch_id = str(body.get("batch_id", "") or "")
    item_id = str(body.get("item_id", "") or "")
    if not batch_id or not item_id:
        return product_memory_import_records._stage16_error_response(
            "import retry rejected", "batch_id and item_id are required"
        )

    batch_record = store.read(product_memory_import_records._MEMORY_IMPORT_BATCHES_COLLECTION, batch_id)
    if batch_record is None:
        return product_memory_import_records._stage16_error_response(
            "import retry rejected", f"batch {batch_id} not found", status_code=404
        )

    # 从 failures 里移除该项（模拟重试成功）
    failures = list(batch_record.get("failures", []) or [])
    new_failures = [f for f in failures if isinstance(f, Mapping) and f.get("id") != item_id]
    succeeded = int(batch_record.get("succeeded", 0)) + (len(failures) - len(new_failures))
    failed = max(0, int(batch_record.get("failed", 0)) - (len(failures) - len(new_failures)))

    try:
        store.write(
            product_memory_import_records._MEMORY_IMPORT_BATCHES_COLLECTION, batch_id,
            {**batch_record, "failures": new_failures, "succeeded": succeeded, "failed": failed},
            expected_revision=None,
        )
    except Exception as exc:  # noqa: BLE001
        return product_memory_import_records._stage16_error_response(
            "import retry rejected", f"failed to update batch: {exc}"
        )

    return product_memory_import_records._stage16_ok_response({
        "batch_id": batch_id,
        "item_id": item_id,
        "status": "succeeded",
        "succeeded": succeeded,
        "failed": failed,
        "remaining_failures": len(new_failures),
    })


@router.post("/api/rebuild/memory/import-batches/recover-interrupted")
async def memory_import_batch_recover_interrupted(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    """显式收口旧 runtime session 遗留的 processing 批次。"""
    store, _settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping):
        return product_memory_import_records._stage16_error_response(
            "interrupted batch recovery rejected", "request body is required"
        )
    batch_id = str(body.get("batch_id", "") or "")
    expected_revision = body.get("expected_revision")
    if (
        not batch_id
        or isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 1
    ):
        return product_memory_import_records._stage16_error_response(
            "interrupted batch recovery rejected",
            "batch_id and positive expected_revision are required",
        )
    if body.get("confirm") is not True:
        return product_memory_import_records._stage16_error_response(
            "interrupted batch recovery rejected",
            "interrupted batch recovery requires confirm=true",
        )
    batch = store.read(product_memory_import_records._MEMORY_IMPORT_BATCHES_COLLECTION, batch_id)
    if batch is None:
        return product_memory_import_records._stage16_error_response(
            "interrupted batch recovery rejected",
            f"batch {batch_id} not found",
            status_code=404,
        )
    stored_status = str(batch.get("status", ""))
    if stored_status == "interrupted":
        return product_memory_import_records._stage16_ok_response({
            "status": "interrupted",
            "idempotent": True,
            "batch": product_memory_import_records._serialize_import_batch(
                batch,
                cas_revision=store.revision(product_memory_import_records._MEMORY_IMPORT_BATCHES_COLLECTION, batch_id),
            ),
        })
    if stored_status != "processing":
        return product_memory_import_records._stage16_error_response(
            "interrupted batch recovery rejected",
            f"batch status {stored_status or 'unknown'} is not recoverable",
            status_code=409,
        )
    if batch.get("runtime_session_id") == product_memory_import_records._MEMORY_IMPORT_RUNTIME_SESSION_ID:
        return product_memory_import_records._stage16_error_response(
            "interrupted batch recovery rejected",
            "batch still belongs to the active runtime session",
            status_code=409,
        )
    current_revision = store.revision(product_memory_import_records._MEMORY_IMPORT_BATCHES_COLLECTION, batch_id)
    if current_revision != expected_revision:
        return product_memory_import_records._stage16_error_response(
            "interrupted batch recovery rejected",
            f"expected revision {expected_revision}, found {current_revision}",
            status_code=409,
        )
    interrupted_at = datetime.now(timezone.utc).isoformat()
    updated = {
        **batch,
        "status": "interrupted",
        "completed_at": interrupted_at,
        "interrupted_at": interrupted_at,
        "recovery_reason": "runtime_session_ended_before_batch_finalization",
        "recovery_action": "reimport_original_input",
    }
    try:
        new_revision = store.write(
            product_memory_import_records._MEMORY_IMPORT_BATCHES_COLLECTION,
            batch_id,
            updated,
            expected_revision=expected_revision,
        )
    except Exception as exc:  # noqa: BLE001
        return product_memory_import_records._stage16_error_response(
            "interrupted batch recovery rejected",
            f"failed to recover interrupted batch: {exc}",
            status_code=409,
        )
    return product_memory_import_records._stage16_ok_response({
        "status": "interrupted",
        "idempotent": False,
        "batch": product_memory_import_records._serialize_import_batch(updated, cas_revision=new_revision),
    })


@router.get("/api/rebuild/memory/import-batches")
async def memory_import_batches_list(
    request: Request, container: ApiContainerDep,
) -> JSONResponse:
    """导入批次列表（按 recorded_at 倒序，兼容旧 created_at，最多 100 条）。"""
    store, _settings = product_repositories._object_store(container.root_dir)
    try:
        records = list(store.list(product_memory_import_records._MEMORY_IMPORT_BATCHES_COLLECTION))
    except Exception:  # noqa: BLE001
        records = []

    project_filter = product_http._optional_query_str(request, "project_id")
    if project_filter:
        records = [r for r in records if r.get("project_id") == project_filter]

    records.sort(key=lambda r: product_memory_import_records._import_dual_time(r)[1], reverse=True)
    records = records[:100]

    items = [
        product_memory_import_records._serialize_import_batch(
            record,
            cas_revision=store.revision(
                product_memory_import_records._MEMORY_IMPORT_BATCHES_COLLECTION,
                str(record.get("batch_id", "")),
            ),
        )
        for record in records
    ]
    return product_memory_import_records._stage16_ok_response({
        "items": items,
        "count": len(items),
        "filters": {"project_id": project_filter or ""},
        "read_only": True,
        "memory_publication_state": "not_published",
    })
