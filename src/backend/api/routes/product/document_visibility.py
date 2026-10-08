"""Document visibility ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from backend.shared.document_visibility import LegacyDocumentVisibility

from core.aggregate_repository_factory import STRUCTURED_DATABASE_NAME
from core.storage_provider import SQLiteStructuredRecordStore

from . import http as product_http


def _workspace_review_sources(runtime_root: Path) -> set[str]:
    database = runtime_root / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    if not database.is_file():
        return set()
    return {
        str(row.payload["source_id"])
        for row in SQLiteStructuredRecordStore(database).list("workspace_review_intents")
        if isinstance(row.payload.get("source_id"), str)
    }


def _candidate_uses_source(candidate: Mapping[str, object], source_ids: set[str]) -> bool:
    if candidate.get("source_id") in source_ids:
        return True
    refs = candidate.get("source_refs")
    return isinstance(refs, (list, tuple)) and any(
        isinstance(ref, Mapping) and ref.get("source_id") in source_ids for ref in refs
    )


def _document_visible_in_legacy(documents: object, document: Mapping[str, object] | None) -> bool:
    return document is not None and LegacyDocumentVisibility.from_repository(documents).allows(document)


_DOCUMENT_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")


def _document_request_project(request: Request, body: object = None) -> str:
    values = request.query_params.getlist("project_id")
    if len(values) > 1:
        raise HTTPException(400, "project_id must occur once")
    query_id = values[0] if values else None
    body_has_id = isinstance(body, Mapping) and "project_id" in body
    body_id = body.get("project_id") if body_has_id else None
    if body_has_id and not isinstance(body_id, str):
        raise HTTPException(400, "project_id is invalid")
    for value in (query_id, body_id):
        if value is not None and (not isinstance(value, str) or _DOCUMENT_PROJECT_ID.fullmatch(value) is None):
            raise HTTPException(400, "project_id is invalid")
    if query_id is not None and body_id is not None and query_id != body_id:
        raise HTTPException(400, "project_id differs between query and body")
    return query_id or body_id or "default"


def _document_project_id(document: Mapping[str, object]) -> str | None:
    value = document.get("project_id")
    if value is None or value == "":
        return "default"
    return value if isinstance(value, str) else None


def _document_visible_in_project(
    documents: object, document: Mapping[str, object] | None, project_id: str,
) -> bool:
    return _document_visible_in_legacy(documents, document) and _document_project_id(document) == project_id


def _document_delivery_visible_in_project(delivery_service: object, delivery_id: str, project_id: str) -> bool:
    from core.product_core.document_delivery import DocumentDeliveryError

    try:
        scope = delivery_service.document_scope(delivery_id)
    except (DocumentDeliveryError, ValueError):
        return False
    if not isinstance(scope, Mapping) or _document_project_id(scope) != project_id:
        return False
    document_id = scope.get("document_id")
    if not isinstance(document_id, str):
        return False
    documents = delivery_service.documents
    try:
        document = documents.read(document_id)
    except ValueError:
        return False
    return _document_visible_in_project(documents, document, project_id)


def _document_pdf_visible_in_project(pdf_service: object, operation_id: str, project_id: str) -> bool:
    from core.product_core.document_pdf_delivery import DocumentPdfDeliveryError

    try:
        delivery_id = pdf_service.source_delivery_id(operation_id)
    except (DocumentPdfDeliveryError, ValueError):
        return False
    return isinstance(delivery_id, str) and _document_delivery_visible_in_project(pdf_service.deliveries, delivery_id, project_id)


def _document_not_found(document_id: str) -> JSONResponse:
    return product_http._json_response(
        404,
        {"detail": "document not found", "document_id": document_id, "actionable": True},
        product_http._no_store_headers(),
    )
