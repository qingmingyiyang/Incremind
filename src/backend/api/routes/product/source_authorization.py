"""Source authorization ownership for the product API."""
from __future__ import annotations

from core.product_core.source_file_authorization import (
    AuthorizeLocalAudioFileForSource,
    AuthorizeLocalDocumentFileForSource,
    AuthorizeLocalImageFileForSource,
    AuthorizeLocalTextFileForSource,
    AuthorizeLocalVideoFileForSource,
)
from core.storage_provider import JsonObjectStore


def _source_file_authorizer(store: JsonObjectStore, *, namespace_id: str):
    def authorize_file(*, source_id: str, file_path: str):
        source = store.read("sources", source_id)
        if source is None:
            return AuthorizeLocalTextFileForSource(store, namespace_id=namespace_id).execute(
                source_id=source_id,
                file_path=file_path,
            )

        source_type = str(source.get("type") or "").strip().lower()
        media_type = str(source.get("media_type") or "").strip().lower()
        if source_type == "image":
            return AuthorizeLocalImageFileForSource(store, namespace_id=namespace_id).execute(
                source_id=source_id,
                file_path=file_path,
            )
        if source_type == "audio":
            return AuthorizeLocalAudioFileForSource(store, namespace_id=namespace_id).execute(
                source_id=source_id,
                file_path=file_path,
            )
        if source_type == "video":
            return AuthorizeLocalVideoFileForSource(store, namespace_id=namespace_id).execute(
                source_id=source_id,
                file_path=file_path,
            )
        if media_type in {
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }:
            return AuthorizeLocalDocumentFileForSource(store, namespace_id=namespace_id).execute(
                source_id=source_id,
                file_path=file_path,
            )
        return AuthorizeLocalTextFileForSource(store, namespace_id=namespace_id).execute(
            source_id=source_id,
            file_path=file_path,
        )

    return authorize_file
