"""Deterministic, Turn-local compaction for already safe L1 memory context."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from core.ai_kernel import ContextCompaction, ContextEntry, ContextManifest, TurnPayloadStorePort


class ContextCompactionError(ValueError):
    pass


class DeterministicLocalMemoryCompactor:
    """Replace large groups of model-visible L1 atoms with a provenance brief.

    The result is an immutable Turn payload and explicitly remains a derived
    convenience projection.  It is never a Memory record or factual source.
    """

    snapshot_kind = "context-summary-v1"
    min_entries = 2
    soft_threshold_bytes = 1024

    def __init__(self, payloads: TurnPayloadStorePort) -> None:
        self._payloads = payloads

    def compact(self, manifest: ContextManifest) -> ContextManifest:
        sources = tuple(
            entry for entry in manifest.entries
            if entry.kind == "memory_r1" and entry.disclosure == "model"
        )
        input_bytes = sum(entry.content_bytes for entry in sources)
        if len(sources) < self.min_entries or input_bytes <= self.soft_threshold_bytes:
            return manifest
        if manifest.project_id is None:
            return manifest
        turn_prefix = f"crp://session/{manifest.turn_id}/"
        if any(
            entry.payload_ref is None
            or not entry.payload_ref.startswith(turn_prefix)
            or entry.source_project_id != manifest.project_id
            for entry in sources
        ):
            raise ContextCompactionError("memory compaction source binding is invalid")
        source_ids = tuple(entry.entry_id for entry in sources)
        output_id = "context-entry-memory-r1-summary"
        if (
            any(entry.entry_id == output_id for entry in manifest.entries)
            or any(
                compaction.output_entry_id == output_id
                or set(source_ids) & {
                    *compaction.source_entry_ids, compaction.output_entry_id,
                }
                for compaction in manifest.compactions
            )
        ):
            raise ContextCompactionError("memory compaction lineage conflicts")
        payload = self._payload(manifest, sources, input_bytes)
        output_bytes = len(str(payload["summary"]).encode("utf-8"))
        if output_bytes >= input_bytes:
            return manifest
        payload["output_bytes"] = output_bytes
        payload_ref = self._payloads.get_or_create_immutable_payload(
            manifest.turn_id, self.snapshot_kind, payload,
        )
        output = ContextEntry(
            entry_id=output_id,
            kind="context_summary",
            source_ref=f"crp://memory/{manifest.project_id}/context-summary",
            payload_ref=payload_ref,
            source_project_id=manifest.project_id,
            revision_identity=_revision_identity(sources),
            content_fingerprint=None,
            provenance_refs=_provenance_union(sources),
            disclosure="model",
            selection_reason="deterministic_local_memory_r1_compaction",
            content_bytes=output_bytes,
        )
        compacted = tuple(
            replace(entry, disclosure="audit_only") if entry.entry_id in source_ids else entry
            for entry in manifest.entries
        )
        return replace(
            manifest,
            entries=(*compacted, output),
            compactions=(*manifest.compactions, ContextCompaction(
                compaction_id="context-compaction-memory-r1-summary",
                strategy="deterministic_local_extractive_memory_r1_v1",
                source_entry_ids=source_ids,
                output_entry_id=output_id,
                input_bytes=input_bytes,
                output_bytes=output_bytes,
            )),
            selected_context_bytes=manifest.selected_context_bytes - input_bytes + output_bytes,
        )

    def _payload(
        self, manifest: ContextManifest, sources: Sequence[ContextEntry], input_bytes: int,
    ) -> dict[str, object]:
        budget = min(768, max(160, input_bytes // 2))
        excerpts: list[str] = []
        records: list[dict[str, str]] = []
        for entry in sources:
            assert entry.payload_ref is not None
            raw = self._payloads.get(entry.payload_ref)
            markdown, object_id, revision = _memory_payload(raw, manifest.project_id)
            records.append({"entry_id": entry.entry_id, "object_id": object_id, "revision": revision})
            excerpts.append(f"[{object_id}@{revision}] {_clip(markdown, max(48, budget // len(sources)))}")
        summary = _clip("\n".join(excerpts), budget)
        return {
            "schema_version": "1.0.0",
            "snapshot_kind": "turn_frozen_deterministic_memory_summary",
            "projection_authority": "derived_only",
            "turn_id": manifest.turn_id,
            "project_id": manifest.project_id,
            "source_entry_ids": [entry.entry_id for entry in sources],
            "source_revisions": records,
            "provenance_refs": list(_provenance_union(sources)),
            "input_bytes": input_bytes,
            "output_bytes": 0,
            "summary": summary,
        }


def _memory_payload(value: object, project_id: str) -> tuple[str, str, str]:
    if not isinstance(value, Mapping) or value.get("schema_version") != "1.0.0":
        raise ContextCompactionError("memory compaction source payload is invalid")
    if value.get("kind") != "memory_r1" or value.get("project_id") != project_id:
        raise ContextCompactionError("memory compaction source scope drifted")
    markdown, object_id, revision = value.get("markdown"), value.get("object_id"), value.get("revision")
    if not all(isinstance(item, str) and item.strip() for item in (markdown, object_id, revision)):
        raise ContextCompactionError("memory compaction source content is invalid")
    return markdown.strip(), object_id.strip(), revision.strip()


def _provenance_union(entries: Sequence[ContextEntry]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(ref for entry in entries for ref in entry.provenance_refs))


def _revision_identity(entries: Sequence[ContextEntry]) -> str:
    return "memory-r1-summary/" + ";".join(
        f"{entry.entry_id}@{entry.revision_identity or 'unknown'}" for entry in entries
    )


def _clip(value: str, maximum: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value
    fragment = encoded[:maximum]
    while fragment:
        try:
            return fragment.decode("utf-8").rstrip() + "…"
        except UnicodeDecodeError:
            fragment = fragment[:-1]
    return "…"
