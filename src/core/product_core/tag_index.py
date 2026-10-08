from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .library_item_deletion import source_is_deleted
from .ports import ObjectStorePort


class TagIndexError(ValueError):
    """Raised when Source tag indexing cannot be completed."""


STRUCTURE_REF_ORIGIN = "structure"
MANUAL_REF_ORIGIN = "manual"


def tag_index_id(tag: str) -> str:
    digest = hashlib.sha256(tag.lower().encode("utf-8")).hexdigest()[:16]
    return f"tag-{digest}"


def build_manual_tag_ref(source_id: str) -> dict[str, object]:
    return {
        "source_id": source_id,
        "origin": MANUAL_REF_ORIGIN,
        "content_read_id": None,
        "paragraph_id": None,
        "text_preview": None,
    }


def merge_tag_index_refs(
    existing: Mapping[str, object] | None,
    *,
    source_id: str,
    origin: str,
    new_refs: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Replace same-origin refs of one source, preserving other origins and sources."""
    existing_refs = _refs_list(existing.get("refs")) if existing else []
    merged = [
        ref
        for ref in existing_refs
        if not (
            _optional_str(ref.get("source_id")) == source_id
            and _ref_origin(ref) == origin
        )
    ]
    merged.extend(new_refs)
    return merged


def build_tag_index_record(
    *,
    index_id: str,
    tag: str,
    refs: list[dict[str, object]],
    namespace_id: str,
    updated_at: str,
) -> dict[str, object]:
    source_ids = sorted(
        {
            ref_id
            for ref_id in (_optional_str(ref.get("source_id")) for ref in refs)
            if ref_id
        }
    )
    return {
        "schema_version": "1.0.0",
        "id": index_id,
        "tag": tag,
        "refs": refs,
        "source_ids": source_ids,
        "source_count": len(source_ids),
        "ref_count": len(refs),
        "updated_at": updated_at,
        "ref": f"crp://{namespace_id}/tag-index/{index_id}.json",
    }


def _ref_origin(ref: Mapping[str, object]) -> str:
    return _optional_str(ref.get("origin")) or STRUCTURE_REF_ORIGIN


@dataclass(frozen=True, slots=True)
class TagIndexResult:
    status: str
    source_id: str
    structure_id: str
    indexed_tags: tuple[str, ...]
    ref_count: int
    tag_index_refs: tuple[str, ...]
    activity_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TagFacet:
    tag: str
    ref_count: int
    source_count: int


@dataclass(frozen=True, slots=True)
class TagFacetsResult:
    status: str
    facets: tuple[TagFacet, ...]
    total_tags: int


@dataclass(frozen=True, slots=True)
class TaggedSourceHit:
    source_id: str
    source_uri: str
    title: str
    media_type: str
    series_name: str
    series_id: str
    matched_tags: tuple[str, ...]
    matched_paragraphs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TaggedSourcesResult:
    status: str
    tag: str
    hits: tuple[TaggedSourceHit, ...]
    total_hits: int


class IndexSourceTags:
    """Build a tag index from a completed Source structure's paragraph tags.

    The tag index maps each tag to the Source, content read and paragraph refs it
    appeared on, so the library can filter and facet by tag without rescanning
    every source structure.
    """

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        now: str = "2026-07-03T21:30:00+08:00",
    ) -> None:
        self._object_store = object_store
        self._namespace_id = namespace_id
        self._now = now

    def execute(
        self,
        *,
        source_id: str,
        structure_id: str | None = None,
    ) -> TagIndexResult:
        clean_source_id = source_id.strip()
        if not clean_source_id:
            raise TagIndexError("source_id is required")
        clean_structure_id = (structure_id or f"structure-{clean_source_id}").strip()
        structure = self._object_store.read("source_structures", clean_structure_id)
        if structure is None or structure.get("source_id") != clean_source_id:
            raise TagIndexError("completed source structure not found for source")
        paragraph_tags = structure.get("paragraph_tags")
        if not isinstance(paragraph_tags, list):
            raise TagIndexError("source structure has no paragraph_tags")
        content_read_id = _optional_str(structure.get("content_read_id")) or f"content-read-{clean_source_id}"

        refs_by_tag: dict[str, list[dict[str, object]]] = defaultdict(list)
        for paragraph in paragraph_tags:
            if not isinstance(paragraph, Mapping):
                continue
            paragraph_id = _optional_str(paragraph.get("paragraph_id")) or "p000"
            text_preview = _optional_str(paragraph.get("text_preview")) or ""
            tags_value = paragraph.get("tags")
            tags = _str_tuple(tags_value)
            for tag in tags:
                refs_by_tag[tag].append(
                    {
                        "source_id": clean_source_id,
                        "origin": STRUCTURE_REF_ORIGIN,
                        "content_read_id": content_read_id,
                        "paragraph_id": paragraph_id,
                        "text_preview": text_preview,
                    }
                )

        tag_index_refs: list[str] = []
        for tag, refs in refs_by_tag.items():
            index_id = tag_index_id(tag)
            index_ref = self._tag_index_uri(index_id)
            existing = self._object_store.read("tag_index", index_id)
            merged_refs = merge_tag_index_refs(
                existing,
                source_id=clean_source_id,
                origin=STRUCTURE_REF_ORIGIN,
                new_refs=refs,
            )
            record = build_tag_index_record(
                index_id=index_id,
                tag=tag,
                refs=merged_refs,
                namespace_id=self._namespace_id,
                updated_at=self._now,
            )
            self._object_store.write("tag_index", index_id, record, expected_revision=None)
            tag_index_refs.append(index_ref)

        event_ref = self._write_event(
            source_id=clean_source_id,
            structure_id=clean_structure_id,
            indexed_tags=tuple(refs_by_tag.keys()),
            ref_count=sum(len(refs) for refs in refs_by_tag.values()),
        )
        return TagIndexResult(
            status="indexed",
            source_id=clean_source_id,
            structure_id=clean_structure_id,
            indexed_tags=tuple(refs_by_tag.keys()),
            ref_count=sum(len(refs) for refs in refs_by_tag.values()),
            tag_index_refs=tuple(tag_index_refs),
            activity_refs=(event_ref,),
        )

    def _write_event(
        self,
        *,
        source_id: str,
        structure_id: str,
        indexed_tags: tuple[str, ...],
        ref_count: int,
    ) -> str:
        event_id = f"event-tags-indexed-{source_id}"
        event_ref = f"crp://{self._namespace_id}/activity/{event_id}.json"
        self._object_store.write(
            "activity_events",
            event_id,
            {
                "schema_version": "1.0.0",
                "id": event_id,
                "type": "source_tags_indexed",
                "source_id": source_id,
                "status": "completed",
                "contentRead": True,
                "memoryPublication": "not_started",
                "details": {
                    "structure_id": structure_id,
                    "indexed_tags": list(indexed_tags),
                    "ref_count": ref_count,
                },
                "created_at": self._now,
                "ref": event_ref,
            },
            expected_revision=None,
        )
        return event_ref

    def _tag_index_uri(self, index_id: str) -> str:
        return f"crp://{self._namespace_id}/tag-index/{index_id}.json"


class QueryTagFacets:
    """Return tag facets for the library overview."""

    def __init__(self, object_store: ObjectStorePort) -> None:
        self._object_store = object_store

    def execute(self, *, limit: int = 50, project_id: str | None = None) -> TagFacetsResult:
        clean_project_id = _project_id(project_id)
        records = self._object_store.list("tag_index")
        eligible_source_ids = {
            source_id
            for source in self._object_store.list("sources")
            if (source_id := _optional_str(source.get("id")))
            and _source_matches_project(source, clean_project_id)
        }
        facets: list[TagFacet] = []
        for record in records:
            tag = _optional_str(record.get("tag"))
            if not tag:
                continue
            refs = [
                ref for ref in _refs_list(record.get("refs"))
                if _optional_str(ref.get("source_id")) in eligible_source_ids
            ]
            ref_count = len(refs)
            source_count = len({_optional_str(ref.get("source_id")) for ref in refs})
            if not ref_count:
                continue
            facets.append(TagFacet(tag=tag, ref_count=ref_count, source_count=source_count))
        facets.sort(key=lambda item: (item.ref_count, item.source_count, item.tag), reverse=True)
        trimmed = facets[: max(1, limit)]
        return TagFacetsResult(status="completed", facets=tuple(trimmed), total_tags=len(facets))


class QueryTaggedSources:
    """Return Source hits that match a given tag."""

    def __init__(self, object_store: ObjectStorePort) -> None:
        self._object_store = object_store

    def execute(
        self, *, tag: str, limit: int = 100, project_id: str | None = None,
    ) -> TaggedSourcesResult:
        clean_tag = (tag or "").strip()
        if not clean_tag:
            raise TagIndexError("tag is required")
        clean_project_id = _project_id(project_id)
        index_id = tag_index_id(clean_tag)
        record = self._object_store.read("tag_index", index_id)
        if record is None:
            return TaggedSourcesResult(status="completed", tag=clean_tag, hits=(), total_hits=0)
        refs = _refs_list(record.get("refs"))
        refs_by_source: dict[str, list[dict[str, object]]] = defaultdict(list)
        for ref in refs:
            source_id = _optional_str(ref.get("source_id"))
            if source_id:
                refs_by_source[source_id].append(ref)
        hits: list[TaggedSourceHit] = []
        total_hits = 0
        for source_id, source_refs in refs_by_source.items():
            source = self._object_store.read("sources", source_id)
            if not _source_matches_project(source, clean_project_id):
                continue
            total_hits += 1
            if len(hits) >= max(1, limit):
                continue
            matched_tags = tuple(sorted({clean_tag} | _source_tags(source)))
            matched_paragraphs = tuple(
                _optional_str(ref.get("paragraph_id")) or ""
                for ref in source_refs
                if _optional_str(ref.get("paragraph_id"))
            )
            series = source.get("metadata") if isinstance(source.get("metadata"), Mapping) else None
            series_name = _nested_str(series, "series_assignment", "series_name") if series else ""
            series_id = _nested_str(series, "series_assignment", "series_id") if series else ""
            hits.append(
                TaggedSourceHit(
                    source_id=source_id,
                    source_uri=_optional_str(source.get("storage_uri")) or "",
                    title=_optional_str(source.get("title")) or "",
                    media_type=_optional_str(source.get("media_type")) or "",
                    series_name=series_name,
                    series_id=series_id,
                    matched_tags=matched_tags,
                    matched_paragraphs=matched_paragraphs,
                )
            )
        return TaggedSourcesResult(
            status="completed",
            tag=clean_tag,
            hits=tuple(hits),
            total_hits=total_hits,
        )


def serialize_tag_index_result(result: TagIndexResult) -> dict[str, object]:
    return {
        "status": result.status,
        "source_id": result.source_id,
        "structure_id": result.structure_id,
        "indexed_tags": list(result.indexed_tags),
        "ref_count": result.ref_count,
        "tag_index_refs": list(result.tag_index_refs),
        "activity_refs": list(result.activity_refs),
    }


def serialize_tag_facets_result(result: TagFacetsResult) -> dict[str, object]:
    return {
        "status": result.status,
        "facets": [
            {"tag": facet.tag, "ref_count": facet.ref_count, "source_count": facet.source_count}
            for facet in result.facets
        ],
        "total_tags": result.total_tags,
    }


def serialize_tagged_sources_result(result: TaggedSourcesResult) -> dict[str, object]:
    return {
        "status": result.status,
        "tag": result.tag,
        "hits": [
            {
                "source_id": hit.source_id,
                "source_uri": hit.source_uri,
                "title": hit.title,
                "media_type": hit.media_type,
                "series_name": hit.series_name,
                "series_id": hit.series_id,
                "matched_tags": list(hit.matched_tags),
                "matched_paragraphs": list(hit.matched_paragraphs),
            }
            for hit in result.hits
        ],
        "total_hits": result.total_hits,
    }


def _refs_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, Mapping):
            result.append({str(key): val for key, val in item.items()})
    return result


def _str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _int_value(value: object) -> int:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _project_id(value: str | None) -> str | None:
    if value is None:
        return None
    clean = value.strip()
    if not clean or len(clean) > 191 or any(ord(char) < 32 or ord(char) == 127 for char in clean):
        raise TagIndexError("project_id is invalid")
    return clean


def _source_matches_project(source: object, project_id: str | None) -> bool:
    if not isinstance(source, Mapping) or source_is_deleted(source):
        return False
    source_project_id = _optional_str(source.get("project_id"))
    return project_id is None or source_project_id in {None, project_id}


def _nested_str(mapping: Mapping[str, object] | None, *keys: str) -> str:
    current: object = mapping
    for key in keys:
        if not isinstance(current, Mapping):
            return ""
        current = current.get(key)
    return _optional_str(current) or ""


def _source_tags(source: Mapping[str, object]) -> set[str]:
    metadata = source.get("metadata")
    if not isinstance(metadata, Mapping):
        return set()
    structure = metadata.get("content_structure")
    if not isinstance(structure, Mapping):
        return set()
    tags = structure.get("tags")
    return set(_str_tuple(tags))
