from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from .ports import ObjectStorePort


class SourceContentReadError(ValueError):
    """Raised when a Source cannot be read by the restricted text reader."""


@dataclass(frozen=True, slots=True)
class SourceContentReadResult:
    status: str
    source_id: str
    media_type: str | None
    content_read: bool
    char_count: int
    byte_count: int
    preview: str
    read_ref: str | None
    error: str | None
    activity_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BookmarkCollectionWebContentReadResult:
    status: str
    source_id: str
    collection_item_count: int
    completed_count: int
    failed_count: int
    child_source_ids: tuple[str, ...]
    child_results: tuple[SourceContentReadResult, ...]
    memory_publication_state: str
    blocked_operations: tuple[str, ...]


class ReadSourceTextContent:
    """Read already captured text Source content into a traceable local record."""

    _ALLOWED_MEDIA_TYPES = frozenset({"text/plain", "text/markdown", "application/json"})
    _ALLOWED_DOCUMENT_MEDIA_TYPES = frozenset(
        {
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
    )

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        now: str = "2026-07-01T16:00:00+08:00",
        max_bytes: int = 262_144,
        preview_chars: int = 240,
        document_extractors: Mapping[str, Callable[[Path, str], str]] | None = None,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if preview_chars <= 0:
            raise ValueError("preview_chars must be positive")
        self._object_store = object_store
        self._namespace_id = namespace_id
        self._now = now
        self._max_bytes = max_bytes
        self._preview_chars = preview_chars
        self._document_extractors = dict(document_extractors or {})

    def execute(self, *, source_id: str) -> SourceContentReadResult:
        clean_source_id = source_id.strip()
        if not clean_source_id:
            raise SourceContentReadError("source_id is required")
        source = self._object_store.read("sources", clean_source_id)
        if source is None:
            raise SourceContentReadError("source not found")
        if source.get("identity_method") == "workspace_confirmation":
            # This Source was reviewed with its extracted text already frozen.
            # A second content-read record would calculate a new fingerprint
            # and mutate the confirmation Source without adding evidence.
            metadata = source.get("metadata")
            text = metadata.get("content_snapshot") if isinstance(metadata, Mapping) else None
            if not isinstance(text, str) or not text:
                raise SourceContentReadError("confirmed source snapshot is unavailable")
            encoded_size = len(text.encode("utf-8"))
            if encoded_size > self._max_bytes:
                raise SourceContentReadError(f"source content exceeds max_bytes {self._max_bytes}")
            return SourceContentReadResult(
                status="completed", source_id=clean_source_id,
                media_type=_optional_str(source.get("media_type")),
                content_read=True, char_count=len(text), byte_count=encoded_size,
                preview=_preview(text, self._preview_chars), read_ref=None,
                error=None, activity_refs=(),
            )

        started_event = self._write_event(
            clean_source_id,
            event_type="content_read_started",
            status="started",
            details={
                "max_bytes": self._max_bytes,
                "allowed_media_types": sorted(self._ALLOWED_MEDIA_TYPES),
                "allowed_document_media_types": sorted(self._ALLOWED_DOCUMENT_MEDIA_TYPES),
            },
        )
        try:
            text = self._extract_allowed_text(source)
            if text == "":
                raise SourceContentReadError("source content is empty")
            encoded = text.encode("utf-8")
            if len(encoded) > self._max_bytes:
                raise SourceContentReadError(
                    f"source content exceeds max_bytes {self._max_bytes}"
                )
            preview = _preview(text, self._preview_chars)
            read_id = f"content-read-{clean_source_id}"
            read_ref = self._read_uri(read_id)
            read_record = {
                "schema_version": "1.0.0",
                "id": read_id,
                "source_id": clean_source_id,
                "status": "completed",
                "media_type": _optional_str(source.get("media_type")),
                "encoding": "utf-8",
                "char_count": len(text),
                "byte_count": len(encoded),
                "text_sha256": hashlib.sha256(encoded).hexdigest(),
                "preview": preview,
                "text": text,
                "created_at": self._now,
            }
            self._object_store.write("source_content_reads", read_id, read_record, expected_revision=None)
            completed_event = self._write_event(
                clean_source_id,
                event_type="content_read_completed",
                status="completed",
                details={
                    "read_ref": read_ref,
                    "char_count": len(text),
                    "byte_count": len(encoded),
                },
            )
            self._update_source_content_read(
                source,
                status="completed",
                content_read=True,
                char_count=len(text),
                byte_count=len(encoded),
                preview=preview,
                read_ref=read_ref,
                error=None,
                activity_refs=(started_event, completed_event),
            )
            return SourceContentReadResult(
                status="completed",
                source_id=clean_source_id,
                media_type=_optional_str(source.get("media_type")),
                content_read=True,
                char_count=len(text),
                byte_count=len(encoded),
                preview=preview,
                read_ref=read_ref,
                error=None,
                activity_refs=(started_event, completed_event),
            )
        except SourceContentReadError as error:
            failed_event = self._write_event(
                clean_source_id,
                event_type="content_read_failed",
                status="failed",
                details={"reason": str(error)},
            )
            self._update_source_content_read(
                source,
                status="failed",
                content_read=False,
                char_count=0,
                byte_count=0,
                preview="",
                read_ref=None,
                error=str(error),
                activity_refs=(started_event, failed_event),
            )
            return SourceContentReadResult(
                status="failed",
                source_id=clean_source_id,
                media_type=_optional_str(source.get("media_type")),
                content_read=False,
                char_count=0,
                byte_count=0,
                preview="",
                read_ref=None,
                error=str(error),
                activity_refs=(started_event, failed_event),
            )

    def _extract_allowed_text(self, source: Mapping[str, object]) -> str:
        media_type = _optional_str(source.get("media_type"))
        if media_type not in self._ALLOWED_MEDIA_TYPES and media_type not in self._ALLOWED_DOCUMENT_MEDIA_TYPES:
            raise SourceContentReadError(f"unsupported media_type for text read: {media_type}")
        metadata = source.get("metadata")
        if not isinstance(metadata, Mapping):
            raise SourceContentReadError("source metadata is required")
        content = metadata.get("content")
        if isinstance(content, str) and content:
            return content
        if media_type in self._ALLOWED_DOCUMENT_MEDIA_TYPES:
            return self._read_authorized_document_file(source, metadata, media_type)
        return self._read_authorized_text_file(source, metadata)

    def _read_authorized_text_file(
        self,
        source: Mapping[str, object],
        metadata: Mapping[str, object],
    ) -> str:
        authorization = metadata.get("file_authorization")
        if not isinstance(authorization, Mapping):
            raise SourceContentReadError("source has no captured text content or file authorization")
        authorization_id = _optional_str(authorization.get("authorization_id"))
        if authorization_id is None:
            raise SourceContentReadError("source file authorization id is required")
        record = self._object_store.read("authorized_file_refs", authorization_id)
        if record is None:
            raise SourceContentReadError("source file authorization record not found")
        if record.get("source_id") != source.get("id"):
            raise SourceContentReadError("source file authorization does not match source")
        path_value = _optional_str(record.get("path"))
        if path_value is None:
            raise SourceContentReadError("authorized file path is required")
        path = Path(path_value).expanduser().resolve(strict=False)
        if not path.exists():
            raise SourceContentReadError("authorized file does not exist")
        if not path.is_file():
            raise SourceContentReadError("authorized path is not a file")
        if path.stat().st_size > self._max_bytes:
            raise SourceContentReadError(f"source content exceeds max_bytes {self._max_bytes}")
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise SourceContentReadError("authorized file is not valid utf-8 text") from error

    def _read_authorized_document_file(
        self,
        source: Mapping[str, object],
        metadata: Mapping[str, object],
        media_type: str,
    ) -> str:
        authorization = metadata.get("document_authorization")
        if not isinstance(authorization, Mapping):
            raise SourceContentReadError("source has no document authorization")
        authorization_id = _optional_str(authorization.get("authorization_id"))
        if authorization_id is None:
            raise SourceContentReadError("source document authorization id is required")
        record = self._object_store.read("authorized_file_refs", authorization_id)
        if record is None:
            raise SourceContentReadError("source document authorization record not found")
        if record.get("source_id") != source.get("id"):
            raise SourceContentReadError("source document authorization does not match source")
        path_value = _optional_str(record.get("path"))
        if path_value is None:
            raise SourceContentReadError("authorized document path is required")
        path = Path(path_value).expanduser().resolve(strict=False)
        if not path.exists():
            raise SourceContentReadError("authorized document does not exist")
        if not path.is_file():
            raise SourceContentReadError("authorized document path is not a file")
        if path.stat().st_size > self._max_bytes:
            raise SourceContentReadError(f"source content exceeds max_bytes {self._max_bytes}")
        extractor = self._document_extractors.get(media_type)
        if extractor is None:
            raise SourceContentReadError(f"document text extractor is not configured for media_type: {media_type}")
        extracted = extractor(path, media_type)
        if not isinstance(extracted, str) or not extracted.strip():
            raise SourceContentReadError("document text extractor returned no text")
        return extracted

    def _update_source_content_read(
        self,
        source: Mapping[str, object],
        *,
        status: str,
        content_read: bool,
        char_count: int,
        byte_count: int,
        preview: str,
        read_ref: str | None,
        error: str | None,
        activity_refs: tuple[str, ...],
    ) -> None:
        source_id = _required_str(source, "id")
        metadata = dict(source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {})
        metadata["content_read"] = {
            "status": status,
            "content_read": content_read,
            "char_count": char_count,
            "byte_count": byte_count,
            "preview": preview,
            "read_ref": read_ref,
            "error": error,
            "activity_refs": list(activity_refs),
            "updated_at": self._now,
        }
        updated = dict(source)
        updated["metadata"] = metadata
        if status == "completed":
            updated["processing_state"] = "ready"
        self._object_store.write("sources", source_id, updated, expected_revision=None)

    def _write_event(
        self,
        source_id: str,
        *,
        event_type: str,
        status: str,
        details: Mapping[str, object],
    ) -> str:
        event_id = f"event-{event_type.replace('_', '-')}-{source_id}"
        event_ref = self._event_uri(event_id)
        self._object_store.write(
            "activity_events",
            event_id,
            {
                "schema_version": "1.0.0",
                "id": event_id,
                "type": event_type,
                "source_id": source_id,
                "status": status,
                "contentRead": event_type == "content_read_completed",
                "memoryPublication": "not_started",
                "details": dict(details),
                "created_at": self._now,
                "ref": event_ref,
            },
            expected_revision=None,
        )
        return event_ref

    def _read_uri(self, read_id: str) -> str:
        return f"crp://{self._namespace_id}/source-content-reads/{read_id}.json"

    def _event_uri(self, event_id: str) -> str:
        return f"crp://{self._namespace_id}/activity/{event_id}.json"


class ReadLinkWebContent:
    """Fetch a captured link Source and persist extracted webpage text as source_content_read."""

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        fetch_url: Callable[[str], str],
        namespace_id: str = "default",
        now: str = "2026-07-02T15:00:00+08:00",
        max_bytes: int = 262_144,
        preview_chars: int = 240,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if preview_chars <= 0:
            raise ValueError("preview_chars must be positive")
        self._object_store = object_store
        self._fetch_url = fetch_url
        self._namespace_id = namespace_id
        self._now = now
        self._max_bytes = max_bytes
        self._preview_chars = preview_chars

    def execute(self, *, source_id: str) -> SourceContentReadResult:
        clean_source_id = source_id.strip()
        if not clean_source_id:
            raise SourceContentReadError("source_id is required")
        source = self._object_store.read("sources", clean_source_id)
        if source is None:
            raise SourceContentReadError("source not found")
        if source.get("type") != "link":
            raise SourceContentReadError("web content read requires link source")
        original_url = _optional_str(source.get("original_url"))
        if original_url is None:
            raise SourceContentReadError("link source original_url is required")

        started_event = self._write_event(
            clean_source_id,
            event_type="web_content_read_started",
            status="started",
            details={
                "original_url": original_url,
                "max_bytes": self._max_bytes,
                "remote_fetch": True,
                "memoryPublication": "not_started",
            },
        )
        try:
            html = self._fetch_url(original_url)
            if not isinstance(html, str) or not html.strip():
                raise SourceContentReadError("web fetch returned no text")
            encoded_html = html.encode("utf-8")
            if len(encoded_html) > self._max_bytes:
                raise SourceContentReadError(f"web content exceeds max_bytes {self._max_bytes}")
            text = _html_to_text(html)
            if not text:
                raise SourceContentReadError("web content extractor returned no text")
            encoded = text.encode("utf-8")
            if len(encoded) > self._max_bytes:
                raise SourceContentReadError(f"extracted web text exceeds max_bytes {self._max_bytes}")
            preview = _preview(text, self._preview_chars)
            read_id = f"content-read-{clean_source_id}"
            read_ref = self._read_uri(read_id)
            read_record = {
                "schema_version": "1.0.0",
                "id": read_id,
                "source_id": clean_source_id,
                "status": "completed",
                "media_type": "text/html",
                "encoding": "utf-8",
                "char_count": len(text),
                "byte_count": len(encoded),
                "text_sha256": hashlib.sha256(encoded).hexdigest(),
                "preview": preview,
                "text": text,
                "original_url": original_url,
                "fetch_provider": "local-urllib-web-content",
                "remote_fetch": True,
                "created_at": self._now,
            }
            self._object_store.write("source_content_reads", read_id, read_record, expected_revision=None)
            completed_event = self._write_event(
                clean_source_id,
                event_type="web_content_read_completed",
                status="completed",
                details={
                    "read_ref": read_ref,
                    "char_count": len(text),
                    "byte_count": len(encoded),
                    "original_url": original_url,
                },
            )
            self._update_source_content_read(
                source,
                status="completed",
                content_read=True,
                char_count=len(text),
                byte_count=len(encoded),
                preview=preview,
                read_ref=read_ref,
                error=None,
                activity_refs=(started_event, completed_event),
            )
            return SourceContentReadResult(
                status="completed",
                source_id=clean_source_id,
                media_type="text/html",
                content_read=True,
                char_count=len(text),
                byte_count=len(encoded),
                preview=preview,
                read_ref=read_ref,
                error=None,
                activity_refs=(started_event, completed_event),
            )
        except SourceContentReadError as error:
            failed_event = self._write_event(
                clean_source_id,
                event_type="web_content_read_failed",
                status="failed",
                details={"reason": str(error), "original_url": original_url},
            )
            self._update_source_content_read(
                source,
                status="failed",
                content_read=False,
                char_count=0,
                byte_count=0,
                preview="",
                read_ref=None,
                error=str(error),
                activity_refs=(started_event, failed_event),
            )
            return SourceContentReadResult(
                status="failed",
                source_id=clean_source_id,
                media_type="text/html",
                content_read=False,
                char_count=0,
                byte_count=0,
                preview="",
                read_ref=None,
                error=str(error),
                activity_refs=(started_event, failed_event),
            )

    def _update_source_content_read(
        self,
        source: Mapping[str, object],
        *,
        status: str,
        content_read: bool,
        char_count: int,
        byte_count: int,
        preview: str,
        read_ref: str | None,
        error: str | None,
        activity_refs: tuple[str, ...],
    ) -> None:
        source_id = _required_str(source, "id")
        metadata = dict(source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {})
        metadata["content_read"] = {
            "status": status,
            "content_read": content_read,
            "char_count": char_count,
            "byte_count": byte_count,
            "preview": preview,
            "read_ref": read_ref,
            "error": error,
            "activity_refs": list(activity_refs),
            "reader": "link_web_content",
            "updated_at": self._now,
        }
        updated = dict(source)
        updated["metadata"] = metadata
        if status == "completed":
            updated["processing_state"] = "ready"
        self._object_store.write("sources", source_id, updated, expected_revision=None)

    def _write_event(
        self,
        source_id: str,
        *,
        event_type: str,
        status: str,
        details: Mapping[str, object],
    ) -> str:
        event_id = f"event-{event_type.replace('_', '-')}-{source_id}"
        event_ref = self._event_uri(event_id)
        self._object_store.write(
            "activity_events",
            event_id,
            {
                "schema_version": "1.0.0",
                "id": event_id,
                "type": event_type,
                "source_id": source_id,
                "status": status,
                "contentRead": event_type == "web_content_read_completed",
                "memoryPublication": "not_started",
                "details": dict(details),
                "created_at": self._now,
                "ref": event_ref,
            },
            expected_revision=None,
        )
        return event_ref

    def _read_uri(self, read_id: str) -> str:
        return f"crp://{self._namespace_id}/source-content-reads/{read_id}.json"

    def _event_uri(self, event_id: str) -> str:
        return f"crp://{self._namespace_id}/activity/{event_id}.json"


class ReadBookmarkCollectionWebContent:
    """Read webpage text for every link Source captured by a bookmark collection Source."""

    _BLOCKED_OPERATIONS = (
        "cookie_read",
        "video_downloader_execution",
        "model_provider_execution",
        "memory_candidate_auto_creation",
        "long_term_memory_publication",
    )

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        fetch_url: Callable[[str], str],
        namespace_id: str = "default",
        now: str = "2026-07-02T16:00:00+08:00",
        max_bytes: int = 262_144,
        preview_chars: int = 240,
    ) -> None:
        self._object_store = object_store
        self._fetch_url = fetch_url
        self._namespace_id = namespace_id
        self._now = now
        self._max_bytes = max_bytes
        self._preview_chars = preview_chars

    def execute(self, *, source_id: str) -> BookmarkCollectionWebContentReadResult:
        clean_source_id = source_id.strip()
        if not clean_source_id:
            raise SourceContentReadError("source_id is required")
        source = self._object_store.read("sources", clean_source_id)
        if source is None:
            raise SourceContentReadError("source not found")
        if source.get("type") != "collection":
            raise SourceContentReadError("bookmark collection web content read requires collection source")
        metadata = source.get("metadata")
        if not isinstance(metadata, Mapping) or metadata.get("collection_type") != "bookmark_collection":
            raise SourceContentReadError("source is not a bookmark collection")

        urls = _collection_urls(metadata.get("urls"))
        if not urls:
            raise SourceContentReadError("bookmark collection has no urls")
        reader = ReadLinkWebContent(
            self._object_store,
            fetch_url=self._fetch_url,
            namespace_id=self._namespace_id,
            now=self._now,
            max_bytes=self._max_bytes,
            preview_chars=self._preview_chars,
        )
        child_source_ids = tuple(_link_source_id_for_url(url) for url in urls)
        child_results: list[SourceContentReadResult] = []
        for child_source_id in child_source_ids:
            if self._object_store.read("sources", child_source_id) is None:
                child_results.append(
                    SourceContentReadResult(
                        status="failed",
                        source_id=child_source_id,
                        media_type="text/html",
                        content_read=False,
                        char_count=0,
                        byte_count=0,
                        preview="",
                        read_ref=None,
                        error="child link source not found",
                        activity_refs=(),
                    )
                )
                continue
            child_results.append(reader.execute(source_id=child_source_id))

        completed_count = sum(1 for result in child_results if result.status == "completed")
        failed_count = len(child_results) - completed_count
        if completed_count == len(child_results):
            status = "completed"
        elif completed_count > 0:
            status = "partial"
        else:
            status = "failed"
        return BookmarkCollectionWebContentReadResult(
            status=status,
            source_id=clean_source_id,
            collection_item_count=len(urls),
            completed_count=completed_count,
            failed_count=failed_count,
            child_source_ids=child_source_ids,
            child_results=tuple(child_results),
            memory_publication_state="not_published",
            blocked_operations=self._BLOCKED_OPERATIONS,
        )


def serialize_source_content_read_result(result: SourceContentReadResult) -> dict[str, object]:
    return {
        "status": result.status,
        "source_id": result.source_id,
        "media_type": result.media_type,
        "content_read": result.content_read,
        "char_count": result.char_count,
        "byte_count": result.byte_count,
        "preview": result.preview,
        "content_read_id": _content_read_id_from_ref(result.read_ref),
        "read_ref": result.read_ref,
        "error": result.error,
        "activity_refs": list(result.activity_refs),
    }


def serialize_bookmark_collection_web_content_read_result(
    result: BookmarkCollectionWebContentReadResult,
) -> dict[str, object]:
    return {
        "status": result.status,
        "source_id": result.source_id,
        "collection_item_count": result.collection_item_count,
        "completed_count": result.completed_count,
        "failed_count": result.failed_count,
        "child_source_ids": list(result.child_source_ids),
        "child_results": [
            serialize_source_content_read_result(child_result)
            for child_result in result.child_results
        ],
        "memory_publication_state": result.memory_publication_state,
        "blocked_operations": list(result.blocked_operations),
    }


def _content_read_id_from_ref(read_ref: str | None) -> str | None:
    if not isinstance(read_ref, str) or not read_ref.strip():
        return None
    marker = "/source-content-reads/"
    if marker not in read_ref:
        return None
    tail = read_ref.rsplit(marker, 1)[1]
    return tail[:-5] if tail.endswith(".json") else tail


def _preview(text: str, limit: int) -> str:
    compact = " ".join(text.strip().split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 1]}..."


def _html_to_text(html: str) -> str:
    parser = _ReadableHtmlTextParser()
    parser.feed(html)
    parser.close()
    return " ".join(" ".join(parser.parts).split())


class _ReadableHtmlTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self._ignored_depth > 0:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth > 0:
            return
        clean = data.strip()
        if clean:
            self.parts.append(clean)


def _required_str(source: Mapping[str, object], key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value:
        raise SourceContentReadError(f"source requires {key}")
    return value


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _collection_urls(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _link_source_id_for_url(url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return f"source-link-{digest}"
