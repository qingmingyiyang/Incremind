from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from core.document_engine import document_block_text

from .library_overview import (
    GetLibraryOverview,
    LibraryOverviewItem,
    LibraryOverviewReaderPort,
    _memory_layer,
    serialize_library_overview_item,
)


class LibraryOverviewSearchError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LibraryOverviewSearchPage:
    query: str
    total: int
    offset: int
    limit: int
    items: tuple[LibraryOverviewItem, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "status": "ready" if self.total else "empty",
            "backend": "overview_scan",
            "query": self.query,
            "total": self.total,
            "offset": self.offset,
            "limit": self.limit,
            "has_more": self.offset + len(self.items) < self.total,
            "items": [serialize_library_overview_item(item) for item in self.items],
        }


class _CapturedReader:
    """Give Overview and text matching the same collection snapshots."""

    def __init__(self, reader: LibraryOverviewReaderPort) -> None:
        self._reader = reader
        self.collections = {
            "source": tuple(reader.sources()),
            "document": tuple(reader.documents()),
            "memory_candidate": tuple(reader.memory_candidates()),
            "external_agent_review_draft": tuple(reader.external_agent_review_drafts()),
            "memory": tuple(reader.memory_objects()),
        }

    def sources(self):
        return self.collections["source"]

    def documents(self):
        return self.collections["document"]

    def memory_candidates(self):
        return self.collections["memory_candidate"]

    def external_agent_review_drafts(self):
        return self.collections["external_agent_review_draft"]

    def memory_objects(self):
        return self.collections["memory"]

    def capture_job_id(self, source_id: str) -> str | None:
        method = getattr(self._reader, "capture_job_id", None)
        return method(source_id) if callable(method) else None

    def user_job_id(self, source_id: str) -> str | None:
        method = getattr(self._reader, "user_job_id", None)
        return method(source_id) if callable(method) else None


class SearchLibraryOverview:
    """Search all visible Library items before filtering, counting and paging."""

    def __init__(
        self,
        reader: LibraryOverviewReaderPort,
        *,
        namespace_id: str = "default",
        read_record: Callable[[str, str], Mapping[str, object] | None],
    ) -> None:
        self._reader = reader
        self._namespace_id = namespace_id
        self._read_record = read_record

    def execute(
        self,
        *,
        query: str,
        project_id: str | None = None,
        filter_id: str = "all",
        tag: str | None = None,
        import_batch_id: str | None = None,
        offset: int = 0,
        limit: int = 30,
    ) -> LibraryOverviewSearchPage:
        clean_query = query.strip()
        if not clean_query:
            raise LibraryOverviewSearchError("q query parameter is required")
        if offset < 0 or not 1 <= limit <= 100:
            raise LibraryOverviewSearchError("offset or limit is out of range")
        if filter_id not in {
            "all", "pending_memory", "external_draft", "link", "file", "image",
            "audio", "video", "note", "inspiration",
        } and not (filter_id.startswith("tag:") and filter_id[4:].strip()):
            raise LibraryOverviewSearchError("filter_id is invalid")
        captured = _CapturedReader(self._reader)
        overview = GetLibraryOverview(captured, namespace_id=self._namespace_id).execute(
            project_id=project_id
        )
        candidates = tuple(
            item for item in overview.items
            if _matches_filter(item, filter_id)
            and (not tag or tag.casefold() in {value.casefold() for value in item.content_tags})
            and (not import_batch_id or (
                item.item_type == "memory_candidate" and item.import_batch_id == import_batch_id
            ))
        )
        raw_by_key = _raw_items(captured.collections, {(item.item_type, item.item_id) for item in candidates})
        selected: list[LibraryOverviewItem] = []
        seen: set[tuple[str, str]] = set()
        for item in candidates:
            identity = (item.item_type, item.item_id)
            if identity in seen:
                continue
            raw = raw_by_key.get(identity, {})
            read = (
                self._read_record("source_content_reads", item.content_read_id)
                if item.item_type == "source" and item.content_read and item.content_read_id else None
            )
            if read is not None and (
                read.get("source_id") != item.item_id or read.get("status") != "completed"
            ):
                read = None
            linked_output_texts: list[object] = []
            if item.item_type == "source":
                output_ids = dict.fromkeys((
                    *item.media_output_ids,
                    item.video_transcript_output_id,
                    item.audio_transcript_output_id,
                ))
                for output_id in output_ids:
                    output = (
                        self._read_record("media_processing_outputs", output_id)
                        if isinstance(output_id, str) and output_id else None
                    )
                    if (output is not None and output.get("status") == "completed"
                            and output.get("source_id") == item.item_id):
                        linked_output_texts.append(output.get("text"))
            if clean_query.casefold() not in _search_text(item, raw, read, linked_output_texts).casefold():
                continue
            seen.add(identity)
            selected.append(item)
        return LibraryOverviewSearchPage(
            query=clean_query, total=len(selected), offset=offset, limit=limit,
            items=tuple(selected[offset:offset + limit]),
        )


def _raw_items(
    collections: Mapping[str, Sequence[Mapping[str, object]]],
    identities: set[tuple[str, str]],
) -> dict[tuple[str, str], Mapping[str, object]]:
    result: dict[tuple[str, str], Mapping[str, object]] = {}
    for kind, records in collections.items():
        for record in records:
            record_id = record.get("id")
            if not isinstance(record_id, str) or not record_id:
                continue
            item_type = _memory_layer(record) if kind == "memory" else kind
            identity = (item_type, record_id)
            if identity in identities:
                result[identity] = record
    return result


def _search_text(
    item: LibraryOverviewItem,
    raw: Mapping[str, object],
    content_read: Mapping[str, object] | None,
    linked_output_texts: Sequence[object],
) -> str:
    values: list[object] = [
        item.item_id, item.item_type, item.title, item.status, item.project_id,
        item.trust_status, item.source_media_type, item.source_content_kind,
        item.source_original_url, item.content_preview, item.media_output_preview,
        item.structured_summary, item.structured_body, item.series_candidate,
        item.series_reason, *item.structured_key_points, *item.content_tags,
    ]
    values.extend(raw.get(field) for field in (
        "title", "name", "content", "body", "text", "summary", "overview",
        "purpose", "description", "original_url", "url", "rules", "output_rules",
    ))
    if item.item_type == "document":
        values.append(document_block_text(raw))
    metadata = raw.get("metadata")
    if isinstance(metadata, Mapping):
        values.extend(metadata.get(field) for field in ("content", "url", "urls"))
    if content_read is not None:
        values.append(content_read.get("text"))
    values.extend(linked_output_texts)
    for paragraph in item.paragraph_tags:
        values.extend((paragraph.get("paragraph_id"), paragraph.get("text_preview"), paragraph.get("tags")))
    return " ".join(_text_fragments(values))


def _text_fragments(values: Sequence[object]) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            result.append(value.strip())
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            result.extend(_text_fragments(value))
        elif isinstance(value, Mapping):
            result.extend(_text_fragments(tuple(value.get(key) for key in ("text", "rule", "title"))))
    return result


def _matches_filter(item: LibraryOverviewItem, filter_id: str) -> bool:
    if filter_id == "all":
        return True
    if filter_id.startswith("tag:"):
        tag = filter_id[4:].casefold()
        return any(value.casefold() == tag for value in item.content_tags)
    if filter_id == "pending_memory":
        return item.item_type == "memory_candidate" and item.status == "pending_review"
    if filter_id == "external_draft":
        return item.item_type == "external_agent_review_draft" and item.status == "pending_review"
    media = (item.source_media_type or "").casefold()
    video = item.item_type == "source" and (media.startswith("video/") or item.source_content_kind == "video")
    if filter_id == "video":
        return video
    if filter_id == "link":
        return item.item_type == "source" and not video and ("uri" in media or "bookmark" in media)
    if filter_id == "file":
        return any(part in media for part in ("pdf", "document", "word"))
    if filter_id == "image":
        return media.startswith("image/")
    if filter_id == "audio":
        return media.startswith("audio/")
    if filter_id == "note":
        return item.item_type == "source" and not video and not any(
            ("uri" in media, "bookmark" in media, media.startswith("image/"), media.startswith("audio/"))
        )
    if filter_id == "inspiration":
        return bool(
            item.inspiration_status or item.inspiration_id or item.inspiration_ref
            or item.inspiration_summary or item.inspiration_series_name or item.inspiration_themes
        )
    raise LibraryOverviewSearchError("filter_id is invalid")
