from __future__ import annotations

from typing import Callable, Mapping

import pytest

from core.capability_packages.thought_graph_context import (
    DocumentDraftAdapter,
    LineMapProposal,
    MemoryProposalAdapter,
    PlatformProposalHandoffAdapter,
    ProjectSkillProposalAdapter,
    ResearchSynthesisAdapter,
    review_proposal,
)
from core.context_graph import (
    ContextCompiler,
    ContextGraphEdge,
    ContextGraphNode,
    ContextGraphSnapshot,
    ContextPermissionGrant,
    ContextProvenance,
    FrozenContextRevisions,
    StalenessEvaluationInput,
)


def _compiled_binding():
    nodes = (
        ContextGraphNode(
            node_id="requirement", node_type="question", title="Requirement",
            content_ref="content:requirement", content_revision="r1",
            source_refs=("source:requirement",), trust="user_authored",
            created_at="2026-08-30T00:00:00Z", updated_at="2026-08-30T00:00:00Z",
            metadata={"project_id": "project-alpha", "content": "Draft a governed LineMap output."},
        ),
        ContextGraphNode(
            node_id="evidence", node_type="evidence", title="Evidence",
            content_ref="content:evidence", content_revision="r2",
            source_refs=("source:evidence",), trust="verified",
            created_at="2026-08-30T00:00:00Z", updated_at="2026-08-30T00:00:00Z",
            metadata={"project_id": "project-alpha", "content": "Evidence supports the draft."},
        ),
        ContextGraphNode(
            node_id="decision", node_type="decision", title="Decision",
            content_ref="content:decision", content_revision="r3",
            source_refs=("source:decision",), trust="user_authored",
            created_at="2026-08-30T00:00:00Z", updated_at="2026-08-30T00:00:00Z",
            metadata={"project_id": "project-alpha", "content": "Produce a reviewable proposal."},
        ),
    )
    snapshot = ContextGraphSnapshot(
        schema_version="1.0.0", graph_id="compiled-proposal-vertical",
        graph_revision="graph-r7", project_id="project-alpha", source_type="fixture",
        source_revision="source-r9", created_at="2026-08-30T00:00:00Z", nodes=nodes,
        edges=(
            ContextGraphEdge("edge-1", "requirement", "evidence", "full_chain", 1, 0),
            ContextGraphEdge("edge-2", "evidence", "decision", "full_chain", 1, 1),
        ),
        selected_outputs=("decision",), token_estimate=40,
        provenance=ContextProvenance(
            source_type="fixture", source_revision="source-r9",
            imported_at="2026-08-30T00:00:00Z", importer_id="fixture",
            importer_revision="1", source_ref="fixture://compiled-proposal-vertical",
        ),
    )
    revisions = FrozenContextRevisions(
        capability_revision="cap-r5", boundary_revision="boundary-r4",
        provider_revision="provider-r3", model_route_revision="route-r2",
        compiler_revision="2.0.0",
    )
    return ContextCompiler().compile(
        snapshot, revisions=revisions, expected_revisions=revisions,
        permission_grant=ContextPermissionGrant(
            "project-alpha", "grant-r1", frozenset(node.content_ref for node in nodes),
        ), token_budget=700,
        staleness_input=StalenessEvaluationInput.baseline(snapshot, revisions),
    )


AdapterFactory = Callable[[], LineMapProposal]


def _document() -> LineMapProposal:
    return DocumentDraftAdapter().create(
        _compiled_binding(), project_id="project-alpha", title="Document Draft",
        content="Compiled document draft.",
        paragraphs=({"source_node_ids": ("evidence",), "text": "Compiled document draft."},),
    )


def _project_skill() -> LineMapProposal:
    return ProjectSkillProposalAdapter().create(
        _compiled_binding(), project_id="project-alpha", title="Project Skill",
        content="# Compiled Skill",
        sections={"id": "skill-alpha", "project_id": "project-alpha", "rules": ["review first"]},
    )


def _memory() -> LineMapProposal:
    return MemoryProposalAdapter().create(
        _compiled_binding(), project_id="project-alpha", title="Memory Proposal",
        content="Compiled memory candidate.",
    )


def _research() -> LineMapProposal:
    return ResearchSynthesisAdapter().create(
        _compiled_binding(), project_id="project-alpha", title="Research Synthesis",
        content="Compiled research synthesis.", open_questions=("What needs further proof?",),
        unverified_inferences=("This is still a proposal.",),
    )


@pytest.mark.parametrize(
    "factory,target_id,expected_platform_type",
    [
        (_document, "document-alpha", "document_revision_proposal"),
        (_project_skill, "skill-alpha", "project_skill_update_proposal"),
        (_memory, None, "memory_candidate_proposal"),
        (_research, "research-alpha", "document_revision_proposal"),
    ],
)
def test_compiled_graph_proposal_review_handoff_preserves_provenance(
    factory: AdapterFactory, target_id: str | None, expected_platform_type: str,
) -> None:
    proposal = factory()
    binding = _compiled_binding()
    assert proposal.status == "pending_review"
    assert proposal.source_node_ids == binding.deterministic_order
    assert proposal.source_refs == binding.source_refs
    assert (proposal.graph_revision, proposal.capability_revision) == (
        binding.graph_revision, binding.capability_revision,
    )

    rejected = review_proposal(proposal, decision="reject")
    modified = review_proposal(proposal, decision="modify", revised_content="User-modified draft.")
    handoff = PlatformProposalHandoffAdapter()
    for review_pending in (proposal, rejected, modified):
        with pytest.raises(ValueError, match="not_accepted_for_effect"):
            handoff.create_payload(review_pending, target_id=target_id)

    accepted = review_proposal(modified, decision="accept")
    payload = handoff.create_payload(accepted, target_id=target_id)
    provenance = payload["suggested_changes"]["linemap_provenance"]
    assert isinstance(provenance, Mapping)
    assert payload["proposal_type"] == expected_platform_type
    assert payload["formal_write"] is False
    assert payload["requires_user_review"] is True
    assert payload["linemap_review_status"] == "accepted_for_effect"
    assert payload["source_refs"] == list(binding.source_refs)
    assert provenance == {
        "source_node_ids": list(binding.deterministic_order),
        "graph_revision": binding.graph_revision,
        "capability_revision": binding.capability_revision,
        "compiler_revision": binding.compiler_revision,
        "boundary_revision": binding.boundary_revision,
        "provider_revision": binding.provider_revision,
        "model_route_revision": binding.model_route_revision,
        "proposal_review_status": "accepted_for_effect",
    }
    # A capability handoff must remain data for the platform's later review and
    # Effect path.  It must not smuggle an Effect identity or a write command.
    assert not {"effect_id", "operation_id", "intent_ref", "receipt_ref"} & set(payload)
