from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.aggregate_repository_factory import (
    AggregateRepositoryFactory,
    AggregateRepositoryFactoryError,
)
from core.memory_core import ObjectStoreMemoryStore, SQLiteMemoryReader
from core.product_core.memory_export_framework import (
    ExportPayload,
    ExportableEvidenceLink,
    ExportableMemory,
    ExportableSource,
    ExportableSourceAsset,
    ExportableTag,
)
from core.product_core.memory_quality_gate import redact_content
from core.product_core.workbench_original_asset import ResolveWorkbenchOriginalAsset
from core.storage_provider import JsonObjectStore


MAX_EXPORT_RECORDS = 20_000
MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_PORTABLE_SOURCE_ASSET_BYTES = 48 * 1024 * 1024
_LAYERS = (("atom", "L1"), ("scenario", "L2"), ("series_memory", "L3"))


class MemoryExportAssemblyError(RuntimeError):
    """Raised when a complete export cannot resolve one current authority."""


@dataclass(frozen=True, slots=True)
class MemoryExportAssembly:
    payload: ExportPayload
    authority_identity: str
    candidate_count: int
    published_count: int


def assemble_memory_export(
    *,
    runtime_root: Path,
    namespace_id: str,
    store: JsonObjectStore,
    project_id: str = "",
) -> MemoryExportAssembly:
    factory = AggregateRepositoryFactory(
        runtime_root=runtime_root,
        namespace_id=namespace_id,
        json_store=store,
    )
    try:
        resolution = factory.memory_publication_authority_resolution()
        memory = (
            SQLiteMemoryReader(resolution.records)
            if resolution.records is not None
            else ObjectStoreMemoryStore(store)
        )
        skills = factory.project_skill_repository_resolution().repository
    except AggregateRepositoryFactoryError as exc:
        raise MemoryExportAssemblyError(str(exc)) from exc

    published_records: list[tuple[str, Mapping[str, object]]] = []
    if project_id:
        for record in memory.list_by_project(project_id):
            published_records.append((_infer_layer(record), record))
    else:
        for layer, exported_layer in _LAYERS:
            published_records.extend((exported_layer, record) for record in memory.list(layer))
    persona_memories = _confirmed_persona_memories(
        store,
        project_id=project_id,
    )

    candidate_records = [
        dict(item)
        for item in store.list("memory_candidates")
        if not project_id or item.get("project_id") == project_id
    ]
    _bounded(len(published_records) + len(candidate_records), "memory records")

    source_project_ids = {
        str(source.get("id", "")): _source_project_id(source)
        for source in store.list("sources")
    }
    memories_by_id: dict[str, ExportableMemory] = {}
    for layer, record in published_records:
        exported = _published_memory(
            layer,
            record,
            fallback_project_id=(
                project_id
                or source_project_ids.get(str(record.get("source_id", "")), "")
            ),
        )
        memories_by_id[exported.memory_id] = exported
    for exported in persona_memories:
        memories_by_id[exported.memory_id] = exported
    published_ids = set(memories_by_id)
    for record in candidate_records:
        exported = _candidate_memory(record)
        if exported.memory_id not in published_ids:
            memories_by_id[exported.memory_id] = exported
    memories = tuple(sorted(memories_by_id.values(), key=lambda item: (item.layer, item.memory_id)))

    source_ids = {
        source_id
        for memory_item in memories
        for source_id in _memory_source_ids(memory_item)
    }
    source_records = [
        dict(item)
        for item in store.list("sources")
        if not project_id
        or str(item.get("id", "")) in source_ids
        or _source_matches_project(item, project_id)
    ]
    _bounded(len(source_records), "source records")
    sources = tuple(
        sorted((_source(item) for item in source_records), key=lambda item: item.source_id)
    )
    source_assets = _verified_source_assets(
        runtime_root=runtime_root,
        store=store,
        source_ids={source.source_id for source in sources},
    )
    source_asset_expected_count = sum(
        1
        for link in store.list("source_asset_links")
        if link.get("source_id") in {source.source_id for source in sources}
        and link.get("role") == "original"
    )

    tags_to_memories: dict[str, list[str]] = defaultdict(list)
    evidence_links: list[ExportableEvidenceLink] = []
    for memory_item in memories:
        for tag in memory_item.tags:
            if memory_item.memory_id not in tags_to_memories[tag]:
                tags_to_memories[tag].append(memory_item.memory_id)
        for evidence_ref in memory_item.evidence_refs:
            source_id = memory_item.source_ref or _source_id_from_ref(evidence_ref)
            evidence_links.append(ExportableEvidenceLink(
                memory_id=memory_item.memory_id,
                source_ref=source_id,
                evidence_ref=evidence_ref,
                relation="supports",
            ))
    tags = tuple(
        ExportableTag(tag=tag, memory_ids=tuple(sorted(memory_ids)))
        for tag, memory_ids in sorted(tags_to_memories.items())
    )

    project_skills = (
        skills.list_by_project(project_id)
        if project_id
        else skills.list_all()
    )
    import_batches = tuple(
        _redact_tree(item)
        for item in store.list("memory_import_batches")
        if not project_id or item.get("project_id") == project_id
    )
    jobs = tuple(
        _task_projection(item)
        for item in store.list("jobs")
        if not project_id
        or item.get("project_id") == project_id
        or item.get("source_id") in source_ids
    )
    _bounded(len(project_skills) + len(import_batches) + len(jobs), "operational records")

    series_summaries = [item.content for item in memories if item.layer == "L3" and item.content]
    persona_summaries = [item.content for item in memories if item.layer == "L4" and item.confirmed and item.content]
    payload = ExportPayload(
        memories=memories,
        sources=sources,
        tags=tags,
        evidence_links=tuple(evidence_links),
        source_assets=source_assets,
        source_asset_expected_count=source_asset_expected_count,
        persona_summary="\n".join(persona_summaries[:100]),
        series_summary="\n\n".join(series_summaries[:100]),
        project_skill_cards=tuple(_redact_tree(item) for item in project_skills),
        import_batches=import_batches,
        export_batches=(),
        audit_summary={
            "authority_identity": resolution.authority_identity,
            "published_memory_count": len(published_records) + len(persona_memories),
            "candidate_memory_count": len(candidate_records),
            "source_count": len(sources),
            "portable_source_asset_count": len(source_assets),
            "portable_source_asset_expected_count": source_asset_expected_count,
            "project_skill_count": len(project_skills),
        },
        task_history=jobs,
    )
    metadata_size = len(json.dumps({
        "project_skills": payload.project_skill_cards,
        "import_batches": payload.import_batches,
        "audit": payload.audit_summary,
        "tasks": payload.task_history,
    }, ensure_ascii=False).encode("utf-8"))
    if metadata_size > MAX_METADATA_BYTES:
        raise MemoryExportAssemblyError("memory export metadata exceeds the bounded limit")
    return MemoryExportAssembly(
        payload=payload,
        authority_identity=resolution.authority_identity,
        candidate_count=len(candidate_records),
        published_count=len(published_records) + len(persona_memories),
    )


def _verified_source_assets(
    *,
    runtime_root: Path,
    store: JsonObjectStore,
    source_ids: set[str],
) -> tuple[ExportableSourceAsset, ...]:
    resolver = ResolveWorkbenchOriginalAsset(
        object_store=store,
        library_root=runtime_root / "library",
    )
    result: list[ExportableSourceAsset] = []
    total_bytes = 0
    for source_id in sorted(source_ids):
        availability = resolver.for_source(source_id)
        if availability is None or availability.status != "available" or availability.path is None:
            continue
        path = availability.path
        if path.is_symlink():
            raise MemoryExportAssemblyError(
                f"source {source_id} original asset cannot be a symbolic link"
            )
        content = path.read_bytes()
        total_bytes += len(content)
        if total_bytes > MAX_PORTABLE_SOURCE_ASSET_BYTES:
            raise MemoryExportAssemblyError(
                "portable source assets exceed the 48 MiB package budget"
            )
        digest = hashlib.sha256(content).hexdigest()
        asset_record = store.read("workbench_original_assets", availability.asset_id) or {}
        if digest != asset_record.get("sha256") or len(content) != availability.byte_count:
            raise MemoryExportAssemblyError(
                f"source {source_id} original asset changed during export"
            )
        result.append(ExportableSourceAsset(
            source_id=source_id,
            asset_id=availability.asset_id,
            display_name=availability.display_name,
            media_type=availability.media_type,
            byte_count=len(content),
            sha256=digest,
            content=content,
            is_audio_visual=availability.media_type.startswith(("audio/", "video/")),
        ))
    return tuple(result)


def _published_memory(
    layer: str,
    item: Mapping[str, object],
    *,
    fallback_project_id: str = "",
) -> ExportableMemory:
    memory_id = _required_text(item, "id")
    if layer == "L1":
        content = _required_text(item, "content")
        summary = ""
        memory_type = str(item.get("atom_type", "fact"))
    elif layer == "L2":
        content = _required_text(item, "summary")
        summary = str(item.get("title", ""))
        memory_type = "scenario"
    else:
        content = _required_text(item, "overview")
        summary = ""
        memory_type = "series"
    refs = _source_refs(item.get("source_refs"))
    source_id = str(item.get("source_id", "")) or (refs[0][0] if refs else "")
    return ExportableMemory(
        memory_id=memory_id,
        layer=layer,
        type=memory_type,
        content=content,
        summary=summary,
        tags=_strings(item.get("tags")),
        confidence=_confidence(item.get("confidence"), default=1.0),
        trust_level=_trust_level(item.get("trust_status")),
        source_ref=source_id,
        evidence_refs=tuple(_evidence_ref(source, locator) for source, locator in refs),
        created_at=str(item.get("created_at", "")),
        updated_at=str(item.get("updated_at", "")),
        occurred_at=_occurred_at(item),
        recorded_at=_recorded_at(item),
        privacy_level="private",
        confirmed=True,
        status="confirmed",
        project_id=(
            str(item.get("project_id", ""))
            or next(iter(_strings(item.get("project_ids"))), "")
            or fallback_project_id
        ),
        series_id=str(item.get("series_id", "")),
        atom_ids=_strings(item.get("atom_ids")),
        scenario_ids=_strings(item.get("scenario_ids")),
    )


def _candidate_memory(item: Mapping[str, object]) -> ExportableMemory:
    memory_id = str(item.get("memory_id") or item.get("id") or "")
    if not memory_id:
        raise MemoryExportAssemblyError("memory candidate requires an id")
    layer = str(item.get("layer", "L1"))
    if layer not in {"L1", "L2", "L3", "L4"}:
        raise MemoryExportAssemblyError(
            f"memory candidate {memory_id} has unsupported layer {layer!r}"
        )
    return ExportableMemory(
        memory_id=memory_id,
        layer=layer,
        type=str(item.get("type", "other")),
        content=str(item.get("content", "")),
        summary=str(item.get("summary", "")),
        tags=_strings(item.get("tags")),
        confidence=_confidence(item.get("confidence"), default=0.0),
        trust_level=str(item.get("trust_level", "unverified")),
        source_ref=str(item.get("source_ref", "")),
        evidence_refs=_strings(item.get("evidence_refs")),
        created_at=str(item.get("created_at", "")),
        updated_at=str(item.get("updated_at", "")),
        occurred_at=_occurred_at(item),
        recorded_at=_recorded_at(item),
        privacy_level=str(item.get("privacy_level", "private")),
        confirmed=str(item.get("status", "")) == "confirmed",
        status=str(item.get("status", "candidate")),
        project_id=str(item.get("project_id", "")),
        series_id=str(item.get("series_id", "")),
        atom_ids=_strings(item.get("atom_ids")),
        scenario_ids=_strings(item.get("scenario_ids")),
    )


def _confirmed_persona_memories(
    store: JsonObjectStore,
    *,
    project_id: str,
) -> tuple[ExportableMemory, ...]:
    result: list[ExportableMemory] = []
    for record in store.list("memory_persona"):
        if not _is_confirmed_persona(record):
            continue
        scope = str(record.get("scope", "global"))
        if project_id and scope not in {"global", "project"}:
            continue
        persona_id = _required_text(record, "id")
        evidence_refs = _persona_evidence_refs(record.get("evidence_refs"))
        source_ref = next(
            (_source_id_from_ref(ref) for ref in evidence_refs if "#" in ref),
            "",
        )
        statements = record.get("statements")
        if not isinstance(statements, Sequence) or isinstance(statements, (str, bytes)):
            continue
        for statement in statements:
            if not isinstance(statement, Mapping):
                continue
            statement_id = str(statement.get("id", "")).strip()
            content = str(statement.get("content", "")).strip()
            if not statement_id or not content:
                continue
            result.append(ExportableMemory(
                memory_id=f"{persona_id}~{statement_id}",
                layer="L4",
                type=str(statement.get("category", "preference")),
                content=content,
                confidence=_confidence(statement.get("confidence"), default=1.0),
                trust_level="high",
                source_ref=source_ref,
                evidence_refs=evidence_refs,
                created_at=str(record.get("created_at", "")),
                updated_at=str(record.get("updated_at", "")),
                occurred_at=_occurred_at(record),
                recorded_at=_recorded_at(record),
                privacy_level="private",
                confirmed=True,
                status="confirmed",
            ))
    return tuple(result)


def _is_confirmed_persona(record: Mapping[str, object]) -> bool:
    confirmation = record.get("confirmation")
    return (
        isinstance(confirmation, Mapping)
        and confirmation.get("status") == "confirmed"
        and confirmation.get("actor") == "user"
        and record.get("trust_status") == "user_confirmed"
    )


def _persona_evidence_refs(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    refs: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        object_type = str(item.get("object_type", "")).strip()
        object_id = str(item.get("object_id", "")).strip()
        if object_type and object_id:
            refs.append(f"{object_type}:{object_id}")
        source_refs = item.get("source_refs")
        if not isinstance(source_refs, Sequence) or isinstance(source_refs, (str, bytes)):
            continue
        for source_ref in source_refs:
            if not isinstance(source_ref, Mapping):
                continue
            source_id = str(source_ref.get("source_id", "")).strip()
            locator = str(source_ref.get("locator", "")).strip()
            if source_id:
                refs.append(_evidence_ref(source_id, locator or "source"))
    return tuple(dict.fromkeys(refs))


def _source(item: Mapping[str, object]) -> ExportableSource:
    source_id = _required_text(item, "id")
    source_type = str(item.get("type", "other"))
    content_ref = str(item.get("storage_uri") or item.get("original_url") or source_id)
    return ExportableSource(
        source_id=source_id,
        source_type=source_type,
        title=redact_content(str(item.get("title", ""))),
        content_ref=redact_content(content_ref),
        media_type=str(item.get("media_type") or ""),
        created_at=str(item.get("created_at", "")),
        occurred_at=_occurred_at(item),
        recorded_at=_recorded_at(item),
        is_audio_visual=source_type in {"audio", "video"},
    )


def _occurred_at(item: Mapping[str, object]) -> str | None:
    """Read new time semantics while remaining compatible with legacy rows."""
    value = item.get("occurred_at")
    if value is None:
        legacy = item.get("created_at")
        return legacy if isinstance(legacy, str) and legacy else None
    return value if isinstance(value, str) and value else None


def _recorded_at(item: Mapping[str, object]) -> str:
    value = item.get("recorded_at")
    if isinstance(value, str) and value:
        return value
    legacy = item.get("created_at")
    if isinstance(legacy, str) and legacy:
        return legacy
    # A legacy derived row without a creation timestamp still needs a truthful
    # local recording time in a portable package. It is not an occurrence.
    return datetime.now(timezone.utc).isoformat()


def _source_refs(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    refs = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        source_id = str(item.get("source_id", "")).strip()
        locator = str(item.get("locator", "")).strip()
        if source_id and locator:
            refs.append((source_id, locator))
    return tuple(refs)


def _evidence_ref(source_id: str, locator: str) -> str:
    return f"{source_id}#{locator}"


def _memory_source_ids(memory: ExportableMemory) -> tuple[str, ...]:
    values = [memory.source_ref] if memory.source_ref else []
    values.extend(_source_id_from_ref(item) for item in memory.evidence_refs)
    return tuple(dict.fromkeys(item for item in values if item))


def _source_id_from_ref(value: str) -> str:
    return value.split("#", 1)[0].removeprefix("crp://").rsplit("/", 1)[-1]


def _source_matches_project(item: Mapping[str, object], project_id: str) -> bool:
    metadata = item.get("metadata")
    if not isinstance(metadata, Mapping):
        return False
    return metadata.get("project_id") == project_id or project_id in _strings(metadata.get("project_ids"))


def _source_project_id(item: Mapping[str, object]) -> str:
    metadata = item.get("metadata")
    if not isinstance(metadata, Mapping):
        return ""
    return str(metadata.get("project_id", "")) or next(
        iter(_strings(metadata.get("project_ids"))),
        "",
    )


def _task_projection(item: Mapping[str, object]) -> Mapping[str, object]:
    return _redact_tree({
        key: item[key]
        for key in (
            "id", "kind", "status", "source_id", "project_id", "created_at",
            "updated_at", "started_at", "completed_at", "error",
        )
        if key in item
    })


def _redact_tree(value: object, *, depth: int = 0) -> Any:
    if depth > 8:
        return "[TRUNCATED]"
    if isinstance(value, str):
        return redact_content(value[:32_000])
    if isinstance(value, Mapping):
        return {
            str(key)[:128]: _redact_tree(item, depth=depth + 1)
            for key, item in list(value.items())[:256]
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_redact_tree(item, depth=depth + 1) for item in list(value)[:1_000]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1_000]


def _infer_layer(item: Mapping[str, object]) -> str:
    if "atom_type" in item:
        return "L1"
    if "overview" in item:
        return "L3"
    if "summary" in item:
        return "L2"
    raise MemoryExportAssemblyError("published memory layer cannot be inferred")


def _trust_level(value: object) -> str:
    return {
        "trusted": "high",
        "user_confirmed": "high",
        "system_generated": "medium",
        "imported_unverified": "unverified",
        "failed": "low",
    }.get(str(value), "unverified")


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str) and item)


def _confidence(value: object, *, default: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0.0, min(1.0, float(value)))
    return default


def _required_text(item: Mapping[str, object], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise MemoryExportAssemblyError(f"published memory requires {key}")
    return value


def _bounded(count: int, label: str) -> None:
    if count > MAX_EXPORT_RECORDS:
        raise MemoryExportAssemblyError(f"{label} exceeds the bounded limit")
