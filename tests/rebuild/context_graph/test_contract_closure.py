from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path

import pytest

from core.capability_packages.thought_graph_context import (
    ContextPreviewAdapter, DocumentDraftAdapter, GraphCanvasAdapter, MemoryProposalAdapter,
    MarkdownGraphExporter, MarkdownGraphImporter, ThoughtDAGExporter, ThoughtDAGImporter,
    redact_proposal, rollback_proposal, supersede_proposal,
)
from core.context_graph import (
    ContextBinding, ContextGraphAdapterRegistry, ContextGraphValidationError, ProposalRouter, snapshot_from_dict,
)


def _binding() -> ContextBinding:
    return ContextBinding(
        "1.0.0", "g", "r1", "cap1", "compiler1", "boundary1", "provider1", "model1",
        ({"role": "assistant", "content": "content"},),
        {"materials": (), "references": (), "conversation": ({"node_id": "n1", "content": "content"},)},
        {"materials": 0, "references": 0, "conversation": 2}, 2, (), (), (), ("source:n1",), ("n1",),
        {"hard_budget": 10},
    )


def _snapshot_payload(version: str = "1.0.0") -> dict:
    return {
        "schema_version": version, "graph_id": "g", "graph_revision": "r1", "project_id": "p1",
        "source_type": "fixture", "source_revision": "s1", "created_at": "2026-08-29T00:00:00Z",
        "nodes": [{"node_id": "n1", "node_type": "note", "title": "N", "content_ref": "c:n1",
                   "content_revision": "c1", "source_refs": ["source:n1"], "trust": "untrusted",
                   "created_at": "2026-08-29T00:00:00Z", "updated_at": "2026-08-29T00:00:00Z",
                   **({} if version == "0.9.0" else {"stale": False, "stale_reason": None}),
                   "metadata": {"project_id": "p1", "content": "content", "position": {"x": 1, "y": 2}}}],
        "edges": [], "selected_outputs": ["n1"], "token_estimate": 2,
        "provenance": {"source_type": "fixture", "source_revision": "s1", "imported_at": "2026-08-29T00:00:00Z",
                       "importer_id": "fixture", "importer_revision": "1", "source_ref": "fixture.json",
                       **({} if version == "0.9.0" else {"untrusted_external_text": True})},
        **({} if version == "0.9.0" else {"integrity_issues": []}),
    }


def test_old_snapshot_migrates_without_executing_content_and_future_version_fails() -> None:
    migrated = snapshot_from_dict(_snapshot_payload("0.9.0"))
    assert migrated.schema_version == "1.0.0"
    assert migrated.integrity_issues[0].code == "migrated_from_0_9"
    with pytest.raises(ContextGraphValidationError, match="unsupported"):
        snapshot_from_dict({**_snapshot_payload(), "schema_version": "2.0.0"})


def test_preview_and_canvas_are_read_only_standard_projections() -> None:
    snapshot = snapshot_from_dict(_snapshot_payload())
    preview = ContextPreviewAdapter().preview(snapshot, _binding())
    canvas = GraphCanvasAdapter().to_canvas(snapshot)
    assert preview.formal_write is False and preview.message_count == 1
    assert canvas["product_name"] == "LineMap" and canvas["read_only_execution"] is True
    assert "model" not in canvas and "secret" not in canvas and "effect" not in canvas


def test_generic_proposal_router_has_no_thoughtdag_branch() -> None:
    router = ProposalRouter()
    router.register("document_draft", DocumentDraftAdapter())
    proposal = router.route("document_draft", _binding(), project_id="p1", title="D", content="Draft",
                            options={"paragraphs": ({"paragraph_id": "p1", "source_node_ids": ("n1",)},)})
    assert proposal.status == "pending_review"
    assert proposal.metadata["paragraph_provenance"][0]["source_node_ids"] == ("n1",)


def test_memory_proposal_lineage_redaction_and_rollback_remain_proposal_only() -> None:
    first = MemoryProposalAdapter().create(_binding(), project_id="p1", title="M", content="One")
    second = MemoryProposalAdapter().create(_binding(), project_id="p1", title="M", content="Two")
    superseded = supersede_proposal(first, second)
    assert superseded.lineage == (first.proposal_id,)
    assert redact_proposal(superseded, mode="soft").redaction == "soft"
    hard = redact_proposal(superseded, mode="hard")
    assert hard.redaction == "hard" and hard.content == "" and hard.source_refs == ()
    rolled = rollback_proposal(superseded, first)
    assert rolled.rollback_of == superseded.proposal_id and rolled.status == "modified_pending_review"
    assert rolled.metadata["formal_write"] is False


def test_project_scope_violation_is_rejected_by_platform_validator() -> None:
    payload = _snapshot_payload()
    payload["nodes"][0]["metadata"]["project_id"] = "other"
    with pytest.raises(ContextGraphValidationError, match="project_scope_violation"):
        snapshot_from_dict(payload)


def test_second_importer_and_exporter_register_without_core_format_branch() -> None:
    registry = ContextGraphAdapterRegistry()
    registry.register_importer("thoughtdag", ThoughtDAGImporter())
    registry.register_importer("markdown", MarkdownGraphImporter())
    registry.register_exporter("thoughtdag", ThoughtDAGExporter())
    registry.register_exporter("markdown", MarkdownGraphExporter())
    assert registry.registered() == {"importers": ("markdown", "thoughtdag"), "exporters": ("markdown", "thoughtdag")}
    assert registry.importer("markdown").importer_id.endswith("MarkdownGraphImporter")


def test_core_execution_paths_have_no_thoughtdag_specific_branch() -> None:
    root = Path(__file__).resolve().parents[3]
    paths = [root / "src/core/ai_kernel", root / "src/core/job_runner", root / "src/core/effect_log",
             root / "src/backend/api"]
    hits = []
    for directory in paths:
        for path in directory.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "thoughtdag" in text.lower():
                hits.append(path.relative_to(root).as_posix())
    assert hits == []


def test_context_binding_language_neutral_schema_is_valid() -> None:
    from jsonschema import Draft202012Validator

    root = Path(__file__).resolve().parents[3]
    schema = json.loads((root / "core-contracts/rebuild/schemas/context-binding.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(json.loads(json.dumps(asdict(_binding()))))
