from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from core.storage_provider import ObjectStorePort

from .ports import RecallIndexEntry


_MEMORY_COLLECTIONS = (
    ("memory_atoms", "l1_atom"),
    ("memory_scenarios", "l2_scenario"),
    ("memory_series_memory", "l3_series_memory"),
    ("project_skills", "l3_project_skill"),
)

_MEMORY_LAYERS = (
    ("atom", "l1_atom"),
    ("scenario", "l2_scenario"),
    ("series_memory", "l3_series_memory"),
)


class CurrentMemoryReaderPort(Protocol):
    def list(self, layer: str) -> Sequence[Mapping[str, object]]: ...


class CurrentProjectSkillReaderPort(Protocol):
    def list_all(self) -> tuple[Mapping[str, object], ...]: ...


def build_recall_entries_from_object_store(object_store: ObjectStorePort) -> tuple[RecallIndexEntry, ...]:
    """Project current, traceable Library authorities into deterministic Recall entries."""

    entries = _source_entries(object_store)
    for collection, layer in _MEMORY_COLLECTIONS:
        for item in object_store.list(collection):
            entry = _memory_entry(item, layer=layer)
            if entry is not None:
                entries.append(entry)
    return _sorted_entries(entries)


def build_recall_entries_from_authorities(
    object_store: ObjectStorePort,
    *,
    memory: CurrentMemoryReaderPort,
    project_skills: CurrentProjectSkillReaderPort,
) -> tuple[RecallIndexEntry, ...]:
    """Build Recall entries from the resolved current authorities.

    Sources remain ObjectStore-owned. Generic Memory and Project Skill may be
    JSON or compound SQLite, so callers must resolve those authorities before
    invoking this function. This prevents a stale JSON compatibility tree from
    being indexed after the formal authority has moved to SQLite.
    """

    entries = _source_entries(object_store)
    for domain_layer, recall_layer in _MEMORY_LAYERS:
        for item in memory.list(domain_layer):
            entry = _memory_entry(item, layer=recall_layer)
            if entry is not None:
                entries.append(entry)
    for item in project_skills.list_all():
        entry = _memory_entry(item, layer="l3_project_skill")
        if entry is not None:
            entries.append(entry)
    return _sorted_entries(entries)


def _source_entries(object_store: ObjectStorePort) -> list[RecallIndexEntry]:
    content_reads = {
        item.get("source_id"): item
        for item in object_store.list("source_content_reads")
        if isinstance(item, Mapping)
        and isinstance(item.get("source_id"), str)
        and item.get("status") == "completed"
    }
    entries: list[RecallIndexEntry] = []
    for source in object_store.list("sources"):
        if not isinstance(source, Mapping) or _source_deleted(source):
            continue
        source_id = _text(source.get("id"))
        if not source_id:
            continue
        content = _source_content(source, content_reads.get(source_id))
        if not content:
            continue
        entries.append(
            RecallIndexEntry(
                object_id=source_id,
                project_id=_project_id(source),
                layer="l1_atom",
                content=content,
                source_refs=(f"{source_id}#source:content",),
                trust_status=_trust_status(source),
                base_score=0.7,
            )
        )

    return entries


def _sorted_entries(entries: Sequence[RecallIndexEntry]) -> tuple[RecallIndexEntry, ...]:
    return tuple(sorted(entries, key=lambda entry: (entry.layer, entry.project_id, entry.object_id)))


def _source_deleted(source: Mapping[str, object]) -> bool:
    lifecycle = source.get("library_lifecycle")
    if not isinstance(lifecycle, Mapping):
        return False
    # ``deleted_at`` is retained as audit evidence after a successful undo.
    # Current visibility is owned by the explicit lifecycle status, matching
    # JsonObjectStore and the Library overview reader. Treating the historical
    # timestamp as a tombstone kept restored Sources out of Recall forever.
    return lifecycle.get("status") in {"deleted", "soft_deleted"}


def _source_content(source: Mapping[str, object], content_read: Mapping[str, object] | None) -> str:
    metadata = source.get("metadata")
    values: list[str] = [_text(source.get("title")), _text(source.get("original_url"))]
    if isinstance(metadata, Mapping):
        values.extend((_text(metadata.get("content")), _text(metadata.get("url"))))
        urls = metadata.get("urls")
        if isinstance(urls, Sequence) and not isinstance(urls, (str, bytes)):
            values.extend(_text(item) for item in urls)
    if isinstance(content_read, Mapping):
        values.append(_text(content_read.get("text")))
    return _joined(values)


def _memory_entry(item: object, *, layer: str) -> RecallIndexEntry | None:
    if not isinstance(item, Mapping):
        return None
    if layer == "l3_project_skill" and item.get("status") != "active":
        return None
    if item.get("status") in {"withdrawn", "rolled_back", "deleted"} or item.get("stale") is True:
        return None
    conflict = item.get("conflict")
    if isinstance(conflict, Mapping) and conflict.get("status") not in {None, "none", "resolved"}:
        return None
    confidence = item.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and confidence < 0.7:
        return None
    object_id = _text(item.get("id") or item.get("skill_id"))
    source_refs = _source_refs(item.get("source_refs") or item.get("evidence_refs"))
    content = _joined(
        (
            _text(item.get("title")),
            _text(item.get("name")),
            _text(item.get("content")),
            _text(item.get("summary")),
            _text(item.get("overview")),
            _text(item.get("purpose")),
            _sequence_text(item.get("rules")),
            _sequence_text(item.get("output_rules")),
        )
    )
    if not object_id or not source_refs or not content:
        return None
    return RecallIndexEntry(
        object_id=object_id,
        project_id=_project_id(item),
        layer=layer,
        content=content,
        source_refs=source_refs,
        trust_status=_trust_status(item),
        base_score=0.8 if layer == "l3_project_skill" else 0.65,
    )


def _source_refs(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    refs: list[str] = []
    for item in value:
        if isinstance(item, str) and "#" in item:
            ref = item
        elif isinstance(item, Mapping):
            source_id = _text(item.get("source_id"))
            locator = _text(item.get("locator"))
            ref = f"{source_id}#{locator}" if source_id and locator else ""
        else:
            ref = ""
        if ref and ref not in refs:
            refs.append(ref)
    return tuple(refs)


def _project_id(item: Mapping[str, object]) -> str:
    metadata = item.get("metadata")
    return (
        _text(item.get("project_id"))
        or (_text(metadata.get("project_id")) if isinstance(metadata, Mapping) else "")
        or "default"
    )


def _trust_status(item: Mapping[str, object]) -> str:
    return _text(item.get("trust_status")) or "user_confirmed"


def _sequence_text(value: object) -> str:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ""
    return _joined(_text(item.get("text") or item.get("rule")) if isinstance(item, Mapping) else _text(item) for item in value)


def _joined(values) -> str:
    result: list[str] = []
    for value in values:
        clean = _text(value)
        if clean and clean not in result:
            result.append(clean)
    return "\n".join(result)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""
