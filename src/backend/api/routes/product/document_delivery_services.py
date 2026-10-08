"""Document delivery services ownership for the product API."""
from __future__ import annotations

import hashlib, hmac

from fastapi import Request

from core.storage_provider import SQLiteStructuredRecordStore

from . import repositories as product_repositories


def _document_delivery_service(container, effect_runner=None):
    from core.product_core.document_delivery import DocumentDeliveryService

    store, settings = product_repositories._object_store(container.root_dir)
    return DocumentDeliveryService(
        records=SQLiteStructuredRecordStore(
            container.root_dir / ".rebuild-data" / "jobs.sqlite3"
        ),
        documents=product_repositories._document_repository(container.root_dir, store, settings),
        runtime_root=container.root_dir,
        namespace_id=settings.namespace_id,
        effect_runner=effect_runner,
    )


def _document_pdf_delivery_service(container, effect_runner=None):
    from core.product_core.document_pdf_delivery import DocumentPdfDeliveryService
    store, settings = product_repositories._object_store(container.root_dir)
    return DocumentPdfDeliveryService(
        records=SQLiteStructuredRecordStore(container.root_dir / ".rebuild-data" / "jobs.sqlite3"),
        deliveries=_document_delivery_service(container, effect_runner),
        runtime_root=container.root_dir,
        namespace_id=settings.namespace_id,
        effect_runner=effect_runner,
    )


def _path_with_query(request: Request) -> str:
    query = request.url.query
    return f"{request.url.path}?{query}" if query else request.url.path


def _document_pdf_main_authorized(request: Request) -> bool:
    """Authorize Electron main only; the renderer never receives this secret."""
    from backend.api.desktop_session import desktop_session
    try:
        session = desktop_session()
    except RuntimeError:
        return False
    signature = request.headers.get("x-chriptmas-main-signature", "")
    message = f"document-pdf:{request.method.upper()}:{_path_with_query(request)}"
    expected = hmac.new(
        session.secret.encode("utf-8") if session else b"", message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return session is not None and bool(signature) and hmac.compare_digest(signature, expected)
