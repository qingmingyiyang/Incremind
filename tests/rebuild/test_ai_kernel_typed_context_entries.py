from __future__ import annotations

from dataclasses import replace

import pytest

from core.ai_kernel import (
    ContextCompaction,
    ContextEntry,
    ContextManifest,
    InMemoryTurnPayloadStore,
    ModelPlannerError,
    context_manifest_to_payload,
    record_typed_context_entry,
    typed_context_raw_result,
)
from core.ai_kernel.context_manifest import compacted_source_entry_ids
from core.ai_kernel.model_planner import _materialize_model_context


TURN_ID = "turn-0123456789abcdef0123456789abcdef"
PROJECT_ID = "project-alpha"


def test_typed_entries_retain_raw_evidence_and_disclose_only_projections() -> None:
    payloads = InMemoryTurnPayloadStore()
    entries = _entries(payloads)

    assert [item.kind for item in entries] == [
        "tool_artifact", "task_graph_change", "agent_message",
    ]
    assert typed_context_raw_result(payloads, entries[0]) == {
        "files": ["report.md"], "status": "completed",
    }
    assert typed_context_raw_result(payloads, entries[1]) == {
        "event": "node.result_observed", "result_ref": "crp://results/project-alpha/node-a",
    }
    assert typed_context_raw_result(payloads, entries[2]) == {
        "kind": "result", "message": "child completed",
    }

    manifest_ref = payloads.put(TURN_ID, "context-manifest", context_manifest_to_payload(_manifest(payloads, entries)))
    selected = _materialize_model_context(
        [{"type": "context.resolved", "data": {"payload_ref": manifest_ref}}], payloads,
    )

    assert [item["kind"] for item in selected] == [
        "tool_artifact", "task_graph_change", "agent_message",
    ]
    assert [item["content"] for item in selected] == [
        {"summary": "report is ready"},
        {"summary": "node-a completed"},
        {"summary": "child completion received"},
    ]
    assert "report.md" not in str(selected)


def test_compaction_hides_typed_evidence_but_keeps_raw_result_recoverable() -> None:
    payloads = InMemoryTurnPayloadStore()
    tool_entry = _entries(payloads)[0]
    summary = _summary_entry(payloads, source_entry_id=tool_entry.entry_id)
    manifest = _manifest(
        payloads, (tool_entry, summary),
        compactions=(ContextCompaction(
            "compact-tool", "deterministic", (tool_entry.entry_id,), summary.entry_id,
            tool_entry.content_bytes, summary.content_bytes,
        ),),
    )

    assert compacted_source_entry_ids(manifest) == frozenset({tool_entry.entry_id})
    assert typed_context_raw_result(payloads, tool_entry)["status"] == "completed"
    manifest_ref = payloads.put(TURN_ID, "context-manifest", context_manifest_to_payload(manifest))
    selected = _materialize_model_context(
        [{"type": "context.resolved", "data": {"payload_ref": manifest_ref}}], payloads,
    )
    assert [item["kind"] for item in selected] == ["context_summary"]


def test_invalidated_typed_evidence_cannot_be_reintroduced_by_disclosure_drift() -> None:
    payloads = InMemoryTurnPayloadStore()
    invalidated = record_typed_context_entry(
        payloads=payloads, turn_id=TURN_ID, project_id=PROJECT_ID,
        entry_id="agent-message-invalidated", kind="agent_message",
        source_ref="crp://agent-messages/project-alpha/message-old",
        revision_identity="message-r1", raw_result={"message": "obsolete"},
        model_projection={"summary": "obsolete"},
        provenance_refs=("crp://agent-messages/project-alpha/message-old",),
        status="invalidated",
    )
    assert invalidated.disclosure == "audit_only"
    resurrected = replace(invalidated, disclosure="model")
    manifest_ref = payloads.put(
        TURN_ID, "context-manifest", context_manifest_to_payload(_manifest(payloads, (resurrected,))),
    )

    with pytest.raises(ModelPlannerError, match="typed context model content is invalid"):
        _materialize_model_context(
            [{"type": "context.resolved", "data": {"payload_ref": manifest_ref}}], payloads,
        )
    assert typed_context_raw_result(payloads, invalidated) == {"message": "obsolete"}


def _entries(payloads: InMemoryTurnPayloadStore) -> tuple[ContextEntry, ...]:
    return (
        record_typed_context_entry(
            payloads=payloads, turn_id=TURN_ID, project_id=PROJECT_ID,
            entry_id="tool-artifact-a", kind="tool_artifact",
            source_ref="crp://tool-artifacts/project-alpha/artifact-a",
            revision_identity="artifact-r2",
            raw_result={"files": ["report.md"], "status": "completed"},
            model_projection={"summary": "report is ready"},
            provenance_refs=("crp://effects/project-alpha/effect-a",),
        ),
        record_typed_context_entry(
            payloads=payloads, turn_id=TURN_ID, project_id=PROJECT_ID,
            entry_id="task-graph-change-a", kind="task_graph_change",
            source_ref="crp://task-graphs/project-alpha/graph-a/event-a",
            revision_identity="graph-a:3",
            raw_result={"event": "node.result_observed", "result_ref": "crp://results/project-alpha/node-a"},
            model_projection={"summary": "node-a completed"},
            provenance_refs=("crp://task-graphs/project-alpha/graph-a/event-a",),
        ),
        record_typed_context_entry(
            payloads=payloads, turn_id=TURN_ID, project_id=PROJECT_ID,
            entry_id="agent-message-a", kind="agent_message",
            source_ref="crp://agent-messages/project-alpha/message-a",
            revision_identity="message-r3",
            raw_result={"kind": "result", "message": "child completed"},
            model_projection={"summary": "child completion received"},
            provenance_refs=("crp://agent-messages/project-alpha/message-a",),
        ),
    )


def _summary_entry(payloads: InMemoryTurnPayloadStore, *, source_entry_id: str) -> ContextEntry:
    summary = "tool summary"
    payload_ref = payloads.put(TURN_ID, "context-summary", {
        "schema_version": "1.0.0",
        "snapshot_kind": "turn_frozen_deterministic_memory_summary",
        "projection_authority": "derived_only",
        "turn_id": TURN_ID,
        "project_id": PROJECT_ID,
        "source_entry_ids": [source_entry_id],
        "source_revisions": [{"entry_id": source_entry_id, "object_id": "artifact-a", "revision": "artifact-r2"}],
        "provenance_refs": ["crp://effects/project-alpha/effect-a"],
        "input_bytes": len('{"summary":"report is ready"}'.encode("utf-8")),
        "output_bytes": len(summary.encode("utf-8")),
        "summary": summary,
    })
    return ContextEntry(
        entry_id="tool-artifact-summary", kind="context_summary",
        source_ref="crp://tool-artifacts/project-alpha/artifact-a", payload_ref=payload_ref,
        source_project_id=PROJECT_ID, revision_identity="artifact-r2-summary",
        content_fingerprint=None, provenance_refs=("crp://effects/project-alpha/effect-a",),
        disclosure="model", selection_reason="typed_tool_compaction",
        content_bytes=len(summary.encode("utf-8")),
    )


def _manifest(
    payloads: InMemoryTurnPayloadStore, entries: tuple[ContextEntry, ...],
    *, compactions: tuple[ContextCompaction, ...] = (),
) -> ContextManifest:
    capability_ref = payloads.put(TURN_ID, "capability-manifest", {"capability_ids": []})
    return ContextManifest(
        manifest_id=f"context-manifest-{TURN_ID}", turn_id=TURN_ID,
        resolver_id="typed-context-fixture", project_id=PROJECT_ID, series_id=None,
        project_profile_id="profile-a", project_profile_revision=1,
        boundary_profile_id="boundary-a", boundary_profile_revision=1,
        capability_manifest_ref=capability_ref, entries=entries, compactions=compactions,
        excluded_reason_counts=(), max_context_bytes=4096,
        selected_context_bytes=sum(item.content_bytes for item in entries if item.disclosure == "model"),
    )
