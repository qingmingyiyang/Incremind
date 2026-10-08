"""Documents ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from backend.api.container import ApiContainerDep
from backend.shared.document_visibility import LegacyDocumentVisibility

from core.aggregate_repository_factory import AggregateRepositoryFactoryError
from core.document_engine import DocumentExpectedRevisionError, DocumentRepositoryError, SQLiteDocumentRepository

from . import document_serialization as product_document_serialization
from . import document_visibility as product_document_visibility
from . import http as product_http
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


@router.get("/api/rebuild/documents-archived")
async def rebuild_archived_documents(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    """List archived Documents from the currently active authority."""
    project_id = product_document_visibility._document_request_project(request)
    store, settings = product_repositories._object_store(container.root_dir)
    try:
        documents = product_repositories._document_repository(container.root_dir, store, settings)
        visibility = LegacyDocumentVisibility.from_repository(documents)
        items = [
            product_document_serialization._serialize_document_detail(
                document,
                documents.markdown(str(document["id"])) or "",
                status="document_archived",
            )
            for document in documents.list(include_archived=True)
            if document.get("status") == "archived" and visibility.allows(document)
            and product_document_visibility._document_project_id(document) == project_id
        ]
    except (AggregateRepositoryFactoryError, DocumentRepositoryError) as error:
        return product_http._json_response(
            409,
            {"detail": "archived documents rejected", "reason": str(error), "actionable": True},
            product_http._no_store_headers(),
        )
    return product_http._json_response(
        200,
        {"status": "archived_documents", "items": items},
        product_http._no_store_headers(),
    )


async def _document_lifecycle_mutation(
    request: Request,
    container: ApiContainerDep,
    document_id: str,
    *,
    operation: str,
) -> JSONResponse:
    store, settings = product_repositories._object_store(container.root_dir)
    body = await product_http._json_body(request)
    project_id = product_document_visibility._document_request_project(request, body)
    expected_revision = product_http._optional_body_int(body, "expected_revision")
    if expected_revision is None:
        return product_http._json_response(
            400,
            {
                "detail": f"document {operation} rejected",
                "reason": "expected_revision is required",
                "actionable": True,
            },
            product_http._no_store_headers(),
        )
    try:
        documents = product_repositories._document_repository(container.root_dir, store, settings)
        previous = documents.read(document_id)
        if not product_document_visibility._document_visible_in_project(documents, previous, project_id):
            return product_document_visibility._document_not_found(document_id)
        updated = getattr(documents, operation)(document_id, expected_revision=expected_revision)
        if (operation == 'archive' and previous.get('status') != 'archived'
                and isinstance(documents, SQLiteDocumentRepository) and documents.namespace_id == 'default'):
            from backend.shared.memory_sidecars import record_activity
            record_activity = getattr(request.app.state, "document_record_activity", record_activity)
            if record_activity is not None:
                record_activity(documents.records, 'forget', project_id, document_id,
                                event_id='forget-document-' + document_id + '-' + str(updated['revision']))
        if (operation == 'restore' and previous.get('status') == 'archived'
                and isinstance(documents, SQLiteDocumentRepository) and documents.namespace_id == 'default'):
            from backend.shared.memory_sidecars import safe_record_document_usage
            safe_record_usage = getattr(request.app.state, "document_record_usage", safe_record_document_usage)
            if safe_record_usage is not None:
                safe_record_usage(documents.records, 'document', document_id, project_id, 1.0, reset=True)
    except DocumentExpectedRevisionError as error:
        current = documents.read(document_id)
        return product_http._json_response(
            409,
            {
                "detail": "document revision conflict",
                "reason": str(error),
                "actionable": True,
                "document_id": document_id,
                "current_revision": current.get("revision") if current else None,
            },
            product_http._no_store_headers(),
        )
    except (AggregateRepositoryFactoryError, DocumentRepositoryError) as error:
        reason = str(error)
        status_code = 404 if "not found" in reason else 409
        return product_http._json_response(
            status_code,
            {"detail": f"document {operation} rejected", "reason": reason, "actionable": True},
            product_http._no_store_headers(),
        )
    return product_http._json_response(
        200,
        product_document_serialization._serialize_document_detail(
            updated,
            documents.markdown(document_id) or "",
            status=f"document_{'archived' if operation == 'archive' else 'restored'}",
        ),
        product_http._no_store_headers(),
    )


@router.post("/api/rebuild/documents/{document_id:path}/archive")
async def rebuild_document_archive(
    request: Request,
    container: ApiContainerDep,
    document_id: str,
) -> JSONResponse:
    return await _document_lifecycle_mutation(request, container, document_id, operation="archive")


@router.post("/api/rebuild/documents/{document_id:path}/restore")
async def rebuild_document_restore(
    request: Request,
    container: ApiContainerDep,
    document_id: str,
) -> JSONResponse:
    return await _document_lifecycle_mutation(request, container, document_id, operation="restore")


@router.get("/api/rebuild/documents/{document_id:path}/revisions")
async def rebuild_document_revisions(
    request: Request,
    container: ApiContainerDep,
    document_id: str,
) -> JSONResponse:
    """列出 document 的全部 revision 历史，包含 operation / author / conflict 摘要。

    Note: MUST be registered before the catch-all
    ``GET /api/rebuild/documents/{document_id:path}`` route, otherwise the
    ``document_id`` path parameter would greedily swallow ``/revisions``.
    """
    project_id = product_document_visibility._document_request_project(request)
    store, _settings = product_repositories._object_store(container.root_dir)
    documents = product_repositories._document_repository(container.root_dir, store, _settings)
    document = documents.read(document_id)
    if not product_document_visibility._document_visible_in_project(documents, document, project_id):
        return product_http._json_response(
            404,
            {"detail": "document not found", "document_id": document_id, "actionable": True},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    revisions = documents.revisions(document_id)
    items = []
    for item in revisions:
        items.append(
            {
                "revision": item.get("revision"),
                "operation": item.get("operation"),
                "author": item.get("author"),
                "reason": item.get("reason"),
                "status": item.get("status"),
                "conflict": dict(item["conflict"]) if isinstance(item.get("conflict"), Mapping) else None,
                "changed_block_count": len(item["changed_blocks"]) if isinstance(item.get("changed_blocks"), list) else 0,
                "created_at": item.get("created_at"),
            }
        )
    return product_http._json_response(
        200,
        {
            "document_id": document_id,
            "current_revision": document.get("revision"),
            "document_status": document.get("status"),
            "revisions": items,
        },
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.get("/api/rebuild/documents/{document_id:path}/html")
async def rebuild_document_html(
    request: Request,
    container: ApiContainerDep,
    document_id: str,
) -> PlainTextResponse:
    """Render a Document's current Markdown revision as unified-style HTML.

    Returns the HTML as a text/html response for iframe/preview embedding.
    Does not write a file to disk; use POST /html-export for that.

    Note: This route MUST be registered before the catch-all
    ``GET /api/rebuild/documents/{document_id:path}`` route, otherwise the
    catch-all would swallow ``/html`` as part of the path parameter.
    """
    from core.product_core.document_html_render import (
        DocumentHtmlRenderError,
        DocumentHtmlRenderer,
    )

    project_id = product_document_visibility._document_request_project(request)
    store, _settings = product_repositories._object_store(container.root_dir)
    documents = product_repositories._document_repository(container.root_dir, store, _settings)
    document = documents.read(document_id)
    if not product_document_visibility._document_visible_in_project(documents, document, project_id):
        return product_http._json_response(
            404,
            {"detail": "document not found", "document_id": document_id, "actionable": True},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    markdown = documents.markdown(document_id) or ""
    try:
        result = DocumentHtmlRenderer().render(document, markdown)
    except DocumentHtmlRenderError as error:
        return product_http._json_response(
            400,
            {"detail": "document html render rejected", "reason": str(error), "actionable": True},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    return PlainTextResponse(
        result.html,
        status_code=200,
        headers={
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "private, max-age=5",
        },
    )


@router.get("/api/rebuild/documents/{document_id:path}")
@router.put("/api/rebuild/documents/{document_id:path}")
async def rebuild_document_detail(
    request: Request,
    container: ApiContainerDep,
    document_id: str,
) -> JSONResponse:
    project_id = product_document_visibility._document_request_project(request)
    store, _settings = product_repositories._object_store(container.root_dir)
    documents = product_repositories._document_repository(container.root_dir, store, _settings)
    if request.method.upper() == "GET":
        document = documents.read(document_id)
        if not product_document_visibility._document_visible_in_project(documents, document, project_id):
            return product_http._json_response(
                404,
                {"detail": "document not found", "document_id": document_id, "actionable": True},
                {"Content-Type": "application/json", "Cache-Control": "no-store"},
            )
        markdown = documents.markdown(document_id) or ""
        return product_http._json_response(
            200,
            product_document_serialization._serialize_document_detail(
                document, markdown,
                source_task_ref=product_document_serialization._document_source_task_ref(container.root_dir, store, document, documents),
            ),
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )

    body = await product_http._json_body(request)
    project_id = product_document_visibility._document_request_project(request, body)
    if not product_document_visibility._document_visible_in_project(documents, documents.read(document_id), project_id):
        return product_document_visibility._document_not_found(document_id)
    expected_revision = product_http._optional_body_int(body, "expected_revision")
    markdown = product_http._optional_body_text(body, "markdown")
    title = product_http._optional_body_str(body, "title")
    if expected_revision is None or markdown is None:
        return product_http._json_response(
            400,
            {
                "detail": "document save rejected",
                "reason": "expected_revision and markdown are required",
                "actionable": True,
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    try:
        updated = documents.save_user_edit(
            document_id,
            markdown=markdown,
            expected_revision=expected_revision,
            title=title,
        )
    except DocumentExpectedRevisionError as error:
        current_document = documents.read(document_id)
        current_detail = (
            product_document_serialization._serialize_document_detail(
                current_document,
                documents.markdown(document_id) or "",
                status="document_current_after_conflict",
                source_task_ref=product_document_serialization._document_source_task_ref(
                    container.root_dir, store, current_document, documents,
                ),
            )
            if current_document is not None
            else None
        )
        return product_http._json_response(
            409,
            {
                "detail": "document revision conflict",
                "reason": str(error),
                "actionable": True,
                "document_id": document_id,
                "current_revision": (
                    current_document.get("revision") if current_document is not None else None
                ),
                "current_document": current_detail,
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    except DocumentRepositoryError as error:
        return product_http._json_response(
            400,
            {"detail": "document save rejected", "reason": str(error), "actionable": True},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    return product_http._json_response(
        200,
        product_document_serialization._serialize_document_detail(
            updated, documents.markdown(document_id) or markdown, status="document_saved",
            source_task_ref=product_document_serialization._document_source_task_ref(container.root_dir, store, updated, documents),
        ),
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.patch("/api/rebuild/documents/{document_id:path}")
async def rebuild_document_ai_patch(
    request: Request,
    container: ApiContainerDep,
    document_id: str,
) -> JSONResponse:
    """Apply an AI patch with user-wins semantics: 用户手改 blocks 不被覆盖，冲突写入 conflicted revision。"""
    store, _settings = product_repositories._object_store(container.root_dir)
    documents = product_repositories._document_repository(container.root_dir, store, _settings)
    body = await product_http._json_body(request)
    project_id = product_document_visibility._document_request_project(request, body)
    if not product_document_visibility._document_visible_in_project(documents, documents.read(document_id), project_id):
        return product_document_visibility._document_not_found(document_id)
    expected_revision = product_http._optional_body_int(body, "expected_revision")
    blocks_payload = body.get("blocks") if isinstance(body, Mapping) else None
    reason = product_http._optional_body_str(body, "reason") or "AI patch document blocks"
    source_refs_payload = body.get("source_refs") if isinstance(body, Mapping) else None
    if expected_revision is None or not isinstance(blocks_payload, list) or not blocks_payload:
        return product_http._json_response(
            400,
            {
                "detail": "document ai patch rejected",
                "reason": "expected_revision and non-empty blocks are required",
                "actionable": True,
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    blocks = [item for item in blocks_payload if isinstance(item, Mapping)]
    if not blocks:
        return product_http._json_response(
            400,
            {
                "detail": "document ai patch rejected",
                "reason": "blocks must be a non-empty list of objects",
                "actionable": True,
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    source_refs = (
        [item for item in source_refs_payload if isinstance(item, Mapping)]
        if isinstance(source_refs_payload, list)
        else None
    )
    try:
        updated = documents.apply_ai_patch(
            document_id,
            blocks=blocks,
            expected_revision=expected_revision,
            reason=reason,
            source_refs=source_refs,
        )
    except DocumentExpectedRevisionError as error:
        return product_http._json_response(
            409,
            {
                "detail": "document revision conflict",
                "reason": str(error),
                "actionable": True,
                "document_id": document_id,
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    except DocumentRepositoryError as error:
        return product_http._json_response(
            400,
            {"detail": "document ai patch rejected", "reason": str(error), "actionable": True},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    status_label = (
        "ai_patch_conflict" if str(updated.get("status")) == "conflicted" else "ai_patch_applied"
    )
    payload = product_document_serialization._serialize_document_detail(
        updated, documents.markdown(document_id) or "", status=status_label
    )
    current_revision = updated.get("revision")
    if not isinstance(current_revision, int) or isinstance(current_revision, bool):
        return product_http._json_response(
            500,
            {"detail": "document ai patch rejected", "reason": "revision missing after patch", "actionable": False},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    revision_payload = documents.revision(document_id, current_revision)
    if revision_payload is not None:
        conflict_payload = revision_payload.get("conflict")
        if isinstance(conflict_payload, Mapping):
            payload["conflict"] = dict(conflict_payload)
        else:
            payload["conflict"] = {"status": "none", "conflict_blocks": [], "resolution": None}
    else:
        payload["conflict"] = {"status": "none", "conflict_blocks": [], "resolution": None}
    payload["changed_blocks"] = (
        revision_payload.get("changed_blocks") if revision_payload is not None else []
    )
    return product_http._json_response(
        200,
        payload,
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )
