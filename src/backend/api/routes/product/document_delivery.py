"""Document delivery ownership for the product API."""
from __future__ import annotations

import base64, binascii, json
from collections.abc import Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from backend.api.container import ApiContainerDep

from . import document_delivery_services as product_document_delivery_services
from . import document_visibility as product_document_visibility
from . import http as product_http
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


@router.post("/api/rebuild/documents/{document_id:path}/html-export")
async def rebuild_document_html_export(
    request: Request,
    container: ApiContainerDep,
    document_id: str,
) -> JSONResponse:
    """Compatibility adapter into the governed Document Delivery authority."""
    from core.product_core.document_delivery import (
        DocumentDeliveryConflict,
        DocumentDeliveryError,
    )

    body = await product_http._json_body(request)
    project_id = product_document_visibility._document_request_project(request, body)
    store, settings = product_repositories._object_store(container.root_dir)
    documents = product_repositories._document_repository(container.root_dir, store, settings)
    document = documents.read(document_id)
    if not product_document_visibility._document_visible_in_project(documents, document, project_id):
        return product_http._json_response(
            404,
            {"detail": "document not found", "document_id": document_id, "actionable": True},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    revision = document.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        return product_http._json_response(
            409,
            {"detail": "document html export conflict", "reason": "document revision is invalid", "actionable": True},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    try:
        delivery = product_document_delivery_services._document_delivery_service(container).create_or_resume(
            document_id=document_id,
            expected_document_revision=revision,
            formats=["html"],
        )
    except DocumentDeliveryConflict as error:
        return product_http._json_response(
            409,
            {"detail": "document html export conflict", "reason": str(error), "actionable": True},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    except DocumentDeliveryError as error:
        return product_http._json_response(
            400,
            {"detail": "document html export rejected", "reason": str(error), "actionable": True},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    artifact = delivery["artifacts"][0]
    return product_http._json_response(
        200,
        {
            "status": "exported",
            "delivery_id": delivery["delivery_id"],
            "document_id": delivery["document_id"],
            "revision": delivery["document_revision"],
            "file_name": artifact["file_name"],
            "output_ref": artifact["output_ref"],
            "receipt_ref": delivery["receipt_ref"],
            "replayed": delivery["replayed"],
        },
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.post("/api/rebuild/document-deliveries")
async def rebuild_document_delivery_create(
    request: Request,
    container: ApiContainerDep,
) -> JSONResponse:
    """Create or recover one governed delivery of an exact Document revision."""
    from core.product_core.document_delivery import (
        DocumentDeliveryConflict,
        DocumentDeliveryError,
    )

    body = await product_http._json_body(request)
    project_id = product_document_visibility._document_request_project(request, body)
    if not isinstance(body, Mapping) or set(body) != {
        "document_id", "expected_document_revision", "formats",
    }:
        return product_http._json_response(
            400,
            {"detail": "document delivery request must contain exact fields", "actionable": True},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    document_id = str(body.get("document_id") or "")
    store, settings = product_repositories._object_store(container.root_dir)
    documents = product_repositories._document_repository(container.root_dir, store, settings)
    try:
        document = documents.read(document_id)
    except ValueError as error:
        return product_http._json_response(400, {"detail": "document delivery rejected", "reason": str(error), "actionable": True}, product_http._no_store_headers())
    if not product_document_visibility._document_visible_in_project(documents, document, project_id):
        return product_document_visibility._document_not_found(document_id)
    formats = body.get("formats")
    if not isinstance(formats, list):
        return product_http._json_response(
            400,
            {"detail": "document delivery formats must be a list", "actionable": True},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    try:
        result = product_document_delivery_services._document_delivery_service(container).create_or_resume(
            document_id=str(body.get("document_id") or ""),
            expected_document_revision=product_http._required_body_int(body, "expected_document_revision"),
            formats=formats,
        )
    except DocumentDeliveryConflict as error:
        return product_http._json_response(
            409,
            {"detail": "document delivery conflict", "reason": str(error), "actionable": True},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    except (DocumentDeliveryError, ValueError) as error:
        return product_http._json_response(
            400,
            {"detail": "document delivery rejected", "reason": str(error), "actionable": True},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    return product_http._json_response(
        200,
        result,
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.get("/api/rebuild/document-deliveries/{delivery_id}/artifacts/{format_name}")
async def rebuild_document_delivery_artifact(
    request: Request,
    container: ApiContainerDep,
    delivery_id: str,
    format_name: str,
) -> Response:
    """Read one verified internal artifact without exposing its local path."""
    from core.product_core.document_delivery import (
        DocumentDeliveryConflict,
        DocumentDeliveryError,
    )

    project_id = product_document_visibility._document_request_project(request)
    delivery_service = product_document_delivery_services._document_delivery_service(container)
    if not product_document_visibility._document_delivery_visible_in_project(delivery_service, delivery_id, project_id):
        return product_http._json_response(404, {"detail": "document delivery artifact not found"}, product_http._no_store_headers())
    try:
        file_name, data = delivery_service.artifact(
            delivery_id, format_name
        )
    except DocumentDeliveryConflict as error:
        return product_http._json_response(
            409,
            {"detail": "document delivery artifact unavailable", "reason": str(error), "actionable": True},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    except DocumentDeliveryError as error:
        return product_http._json_response(
            404,
            {"detail": "document delivery artifact not found", "reason": str(error), "actionable": True},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    media_types = {
        "markdown": "text/markdown; charset=utf-8",
        "html": "text/html; charset=utf-8",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
    media_type = media_types.get(format_name, "application/octet-stream")
    return Response(
        data,
        media_type=media_type,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'attachment; filename="{file_name}"',
        },
    )


@router.post("/api/rebuild/document-deliveries/{html_delivery_id}/pdf-operations")
async def rebuild_document_pdf_prepare(
    request: Request, container: ApiContainerDep, html_delivery_id: str,
) -> JSONResponse:
    from core.product_core.document_pdf_delivery import DocumentPdfDeliveryConflict, DocumentPdfDeliveryError
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping) or body.get("profile_id") not in {
        "builtin.a4-document", "builtin.slide-document",
    } or set(body) != {"profile_id"}:
        return product_http._json_response(400, {"detail": "PDF request must contain the fixed profile"}, product_http._no_store_headers())
    project_id = product_document_visibility._document_request_project(request, body)
    pdf_service = product_document_delivery_services._document_pdf_delivery_service(container)
    if not product_document_visibility._document_delivery_visible_in_project(pdf_service.deliveries, html_delivery_id, project_id):
        return product_http._json_response(404, {"detail": "document delivery not found"}, product_http._no_store_headers())
    try:
        result = pdf_service.prepare(html_delivery_id, body["profile_id"])
    except DocumentPdfDeliveryConflict as error:
        return product_http._json_response(409, {"detail": "PDF operation conflict", "reason": str(error), "actionable": True}, product_http._no_store_headers())
    except DocumentPdfDeliveryError as error:
        return product_http._json_response(400, {"detail": "PDF operation rejected", "reason": str(error), "actionable": True}, product_http._no_store_headers())
    return product_http._json_response(200, result, product_http._no_store_headers())


@router.get("/api/rebuild/document-pdf-operations/{operation_id}")
async def rebuild_document_pdf_projection(request: Request, operation_id: str, container: ApiContainerDep) -> JSONResponse:
    from core.product_core.document_pdf_delivery import DocumentPdfDeliveryConflict

    project_id = product_document_visibility._document_request_project(request)
    pdf_service = product_document_delivery_services._document_pdf_delivery_service(container)
    if not product_document_visibility._document_pdf_visible_in_project(pdf_service, operation_id, project_id):
        return product_http._json_response(404, {"detail": "PDF operation not found"}, product_http._no_store_headers())
    try:
        result = pdf_service.projection(operation_id)
    except DocumentPdfDeliveryConflict as error:
        return product_http._json_response(409, {"detail": "PDF operation unavailable", "reason": str(error), "actionable": True}, product_http._no_store_headers())
    if result is None:
        return product_http._json_response(404, {"detail": "PDF operation not found"}, product_http._no_store_headers())
    return product_http._json_response(200, result, product_http._no_store_headers())


@router.get("/api/rebuild/document-pdf-operations/{operation_id}/artifact")
async def rebuild_document_pdf_artifact(request: Request, operation_id: str, container: ApiContainerDep) -> Response:
    from core.product_core.document_pdf_delivery import DocumentPdfDeliveryConflict, DocumentPdfDeliveryError

    project_id = product_document_visibility._document_request_project(request)
    pdf_service = product_document_delivery_services._document_pdf_delivery_service(container)
    if not product_document_visibility._document_pdf_visible_in_project(pdf_service, operation_id, project_id):
        return product_http._json_response(404, {"detail": "PDF artifact not found"}, product_http._no_store_headers())
    try:
        file_name, data = pdf_service.artifact(operation_id)
    except DocumentPdfDeliveryConflict as error:
        return product_http._json_response(409, {"detail": "PDF artifact unavailable", "reason": str(error), "actionable": True}, product_http._no_store_headers())
    except DocumentPdfDeliveryError as error:
        return product_http._json_response(404, {"detail": "PDF artifact not found"}, product_http._no_store_headers())
    return Response(data, media_type="application/pdf", headers={"Cache-Control": "private, no-store", "Content-Disposition": f'attachment; filename="{file_name}"'})


@router.get("/api/rebuild/desktop/document-pdf-operations/waiting")
async def rebuild_desktop_document_pdf_waiting(request: Request, container: ApiContainerDep) -> JSONResponse:
    if not product_document_delivery_services._document_pdf_main_authorized(request):
        return product_http._json_response(403, {"detail": "desktop main authorization required"}, product_http._no_store_headers())
    try:
        limit = int(request.query_params.get("limit", "8"))
        result = product_document_delivery_services._document_pdf_delivery_service(container).waiting(limit)
    except Exception as error:
        return product_http._json_response(400, {"detail": "PDF waiting request rejected", "reason": str(error)}, product_http._no_store_headers())
    return product_http._json_response(200, {"operations": result}, product_http._no_store_headers())


@router.post("/api/rebuild/desktop/document-pdf-operations/{operation_id}/claim")
async def rebuild_desktop_document_pdf_claim(request: Request, container: ApiContainerDep, operation_id: str) -> JSONResponse:
    from core.product_core.document_pdf_delivery import DocumentPdfDeliveryConflict, DocumentPdfDeliveryError
    if not product_document_delivery_services._document_pdf_main_authorized(request): return product_http._json_response(403, {"detail": "desktop main authorization required"}, product_http._no_store_headers())
    body = await product_http._json_body(request)
    try:
        result = product_document_delivery_services._document_pdf_delivery_service(container).claim(operation_id, body or {})
    except DocumentPdfDeliveryConflict as error: return product_http._json_response(409, {"detail": "PDF claim conflict", "reason": str(error)}, product_http._no_store_headers())
    except DocumentPdfDeliveryError as error: return product_http._json_response(400, {"detail": "PDF claim rejected", "reason": str(error)}, product_http._no_store_headers())
    return product_http._json_response(200, result, product_http._no_store_headers())


@router.get("/api/rebuild/desktop/document-pdf-operations/{operation_id}/print-input")
async def rebuild_desktop_document_pdf_print_input(request: Request, container: ApiContainerDep, operation_id: str) -> JSONResponse:
    from core.product_core.document_pdf_delivery import DocumentPdfDeliveryConflict, DocumentPdfDeliveryError
    if not product_document_delivery_services._document_pdf_main_authorized(request): return product_http._json_response(403, {"detail": "desktop main authorization required"}, product_http._no_store_headers())
    try:
        result = product_document_delivery_services._document_pdf_delivery_service(container).print_input(operation_id, request.headers.get("x-chriptmas-pdf-claim", ""))
    except DocumentPdfDeliveryConflict as error: return product_http._json_response(409, {"detail": "PDF print input unavailable", "reason": str(error)}, product_http._no_store_headers())
    except DocumentPdfDeliveryError as error: return product_http._json_response(400, {"detail": "PDF print input rejected", "reason": str(error)}, product_http._no_store_headers())
    return product_http._json_response(200, result, product_http._no_store_headers())


@router.post("/api/rebuild/desktop/document-pdf-operations/{operation_id}/complete")
async def rebuild_desktop_document_pdf_complete(request: Request, container: ApiContainerDep, operation_id: str) -> JSONResponse:
    from core.product_core.document_pdf_delivery import DocumentPdfDeliveryConflict, DocumentPdfDeliveryError
    if not product_document_delivery_services._document_pdf_main_authorized(request): return product_http._json_response(403, {"detail": "desktop main authorization required"}, product_http._no_store_headers())
    maximum_body_bytes = 45 * 1024 * 1024
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > maximum_body_bytes:
            return product_http._json_response(413, {"detail": "PDF completion payload is too large"}, product_http._no_store_headers())
    try:
        body = json.loads(bytes(raw))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return product_http._json_response(400, {"detail": "PDF completion request is invalid JSON"}, product_http._no_store_headers())
    if not isinstance(body, Mapping) or set(body) != {"electron_version", "chrome_version", "pdf_base64"}:
        return product_http._json_response(400, {"detail": "PDF completion request fields are invalid"}, product_http._no_store_headers())
    try:
        encoded = body["pdf_base64"]
        if not isinstance(encoded, str): raise ValueError("PDF payload is invalid")
        pdf_bytes = base64.b64decode(encoded, validate=True)
        result = product_document_delivery_services._document_pdf_delivery_service(container).complete(operation_id, request.headers.get("x-chriptmas-pdf-claim", ""), electron_version=str(body["electron_version"]), chrome_version=str(body["chrome_version"]), pdf_bytes=pdf_bytes)
    except DocumentPdfDeliveryConflict as error:
        return product_http._json_response(409, {"detail": "PDF completion conflict", "reason": str(error)}, product_http._no_store_headers())
    except (DocumentPdfDeliveryError, ValueError, binascii.Error) as error:
        return product_http._json_response(400, {"detail": "PDF completion rejected", "reason": str(error)}, product_http._no_store_headers())
    return product_http._json_response(200, result, product_http._no_store_headers())
