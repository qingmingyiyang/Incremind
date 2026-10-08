"""Compatibility exports for the shared document publication rules."""

from backend.shared.document_visibility import (
    LegacyDocumentVisibility,
    recognition_document_visible,
)

__all__ = ["LegacyDocumentVisibility", "recognition_document_visible"]
