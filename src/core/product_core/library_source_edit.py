from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .ports import ObjectStorePort
from .tag_index import (
    MANUAL_REF_ORIGIN,
    build_manual_tag_ref,
    build_tag_index_record,
    merge_tag_index_refs,
    tag_index_id,
)


@dataclass(frozen=True, slots=True)
class LibrarySourceEditResult:
    status: str
    source_id: str
    revision: int
    title: str
    series_name: str
    tags: tuple[str, ...]
    error: str | None = None


class UpdateLibrarySourceMetadata:
    """CAS-protected user edit for Source presentation metadata."""

    def __init__(
        self,
        store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        now: str = "2026-07-03T21:30:00+08:00",
    ) -> None:
        self._store = store
        self._namespace_id = namespace_id
        self._now = now

    def execute(
        self,
        *,
        source_id: str,
        expected_revision: int,
        title: str,
        series_name: str = "",
        tags: Sequence[str] = (),
    ) -> LibrarySourceEditResult:
        clean_id = source_id.strip()
        clean_title = title.strip()
        clean_series = series_name.strip()
        clean_tags = tuple(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))
        if not clean_id or not clean_title:
            return self._result("rejected", clean_id, expected_revision, clean_title, clean_series, clean_tags,
                                "source_id and title are required")
        if expected_revision < 1:
            return self._result("rejected", clean_id, expected_revision, clean_title, clean_series, clean_tags,
                                "expected_revision must be positive")
        if len(clean_title) > 240 or any(len(tag) > 80 for tag in clean_tags) or len(clean_tags) > 32:
            return self._result("rejected", clean_id, expected_revision, clean_title, clean_series, clean_tags,
                                "source metadata exceeds bounded limits")

        source = self._store.read("sources", clean_id)
        if not isinstance(source, Mapping):
            return self._result("not_found", clean_id, 0, clean_title, clean_series, clean_tags, "source not found")
        current_revision = self._store.revision("sources", clean_id)
        if current_revision != expected_revision:
            return self._result("conflict", clean_id, current_revision, str(source.get("title") or ""),
                                _series(source), _tags(source), "source revision changed")

        updated = dict(source)
        updated["title"] = clean_title
        metadata = dict(updated.get("metadata") or {})
        previous_tags = _tags(source)
        metadata["manual_tags"] = list(clean_tags)
        assignment = dict(metadata.get("series_assignment") or {})
        if clean_series:
            assignment.update({
                "series_name": clean_series,
                "series_id": _series_id(clean_series),
                "status": "assigned",
                "memory_publication_state": "not_published",
            })
        else:
            assignment = {}
        metadata["series_assignment"] = assignment
        updated["metadata"] = metadata
        try:
            revision = self._store.write("sources", clean_id, updated, expected_revision=expected_revision)
        except ValueError:
            if self._store.revision("sources", clean_id) == expected_revision:
                raise
            latest = self._store.read("sources", clean_id) or source
            return self._result("conflict", clean_id, self._store.revision("sources", clean_id),
                                str(latest.get("title") or ""), _series(latest), _tags(latest),
                                "source revision changed")

        self._sync_tag_index(clean_id, previous_tags, clean_tags)
        self._write_receipt(clean_id, revision, clean_title, clean_series, clean_tags)
        return self._result("updated", clean_id, revision, clean_title, clean_series, clean_tags, None)

    def _write_receipt(
        self,
        source_id: str,
        revision: int,
        title: str,
        series_name: str,
        tags: Sequence[str],
    ) -> None:
        # 内部审计投影：确定性 event id 覆盖写，同 source 后续编辑覆盖为最新。
        event_id = f"event-library-source-edited-{source_id}"
        self._store.write(
            "activity_events",
            event_id,
            {
                "schema_version": "1.0.0",
                "id": event_id,
                "type": "library_source_edited",
                "source_id": source_id,
                "status": "completed",
                "details": {
                    "title": title,
                    "series_name": series_name,
                    "manual_tags": list(tags),
                    "source_revision": revision,
                },
                "created_at": self._now,
                "ref": f"crp://{self._namespace_id}/activity/{event_id}.json",
            },
            expected_revision=None,
        )

    def _sync_tag_index(self, source_id: str, previous: Sequence[str], current: Sequence[str]) -> None:
        for tag in set(previous) | set(current):
            index_id = tag_index_id(tag)
            existing = self._store.read("tag_index", index_id)
            new_refs = [build_manual_tag_ref(source_id)] if tag in current else []
            merged_refs = merge_tag_index_refs(
                existing, source_id=source_id, origin=MANUAL_REF_ORIGIN, new_refs=new_refs,
            )
            record = build_tag_index_record(
                index_id=index_id,
                tag=tag,
                refs=merged_refs,
                namespace_id=self._namespace_id,
                updated_at=self._now,
            )
            self._store.write("tag_index", index_id, record, expected_revision=None)

    @staticmethod
    def _result(status: str, source_id: str, revision: int, title: str, series_name: str,
                tags: Sequence[str], error: str | None) -> LibrarySourceEditResult:
        return LibrarySourceEditResult(status, source_id, revision, title, series_name, tuple(tags), error)


def serialize_library_source_edit_result(result: LibrarySourceEditResult) -> dict[str, object]:
    return {
        "status": result.status, "source_id": result.source_id, "revision": result.revision,
        "title": result.title, "series_name": result.series_name, "tags": list(result.tags), "error": result.error,
    }


def _tags(source: Mapping[str, object]) -> tuple[str, ...]:
    metadata = source.get("metadata")
    values = metadata.get("manual_tags") if isinstance(metadata, Mapping) else ()
    return tuple(str(value) for value in (values or ()) if str(value).strip())


def _series(source: Mapping[str, object]) -> str:
    metadata = source.get("metadata")
    assignment = metadata.get("series_assignment") if isinstance(metadata, Mapping) else None
    return str(assignment.get("series_name") or "") if isinstance(assignment, Mapping) else ""


def _series_id(name: str) -> str:
    return f"series-{hashlib.sha256(name.lower().encode('utf-8')).hexdigest()[:12]}"
