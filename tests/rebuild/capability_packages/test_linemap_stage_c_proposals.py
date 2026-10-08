from __future__ import annotations

import pytest

from core.capability_packages.thought_graph_context import (
    DocumentDraftAdapter, MemoryProposalAdapter, ProjectSkillProposalAdapter,
    PlatformProposalHandoffAdapter, ResearchSynthesisAdapter, review_proposal,
)
from core.context_graph import ContextBinding


def _binding() -> ContextBinding:
    return ContextBinding(
        schema_version="1.0.0", graph_id="vertical-fixture", graph_revision="g2",
        capability_revision="cap-1", compiler_revision="1.0.0", boundary_revision="b1",
        provider_revision="p1", model_route_revision="m1",
        messages=({"role": "assistant", "content": "draft"},),
        layers={"materials": (), "references": (), "conversation": ()},
        layer_token_costs={"materials": 0, "references": 0, "conversation": 2}, total_token_cost=2,
        trimmed_nodes=(), excluded_nodes=("rejected",), stale_nodes=(),
        source_refs=("source:req", "source:evidence"), deterministic_order=("requirement", "evidence", "decision"),
        budget_explanation={"hard_budget": 100},
    )


@pytest.mark.parametrize(
    "adapter, expected",
    [
        (ProjectSkillProposalAdapter(), "project_skill_proposal"),
        (DocumentDraftAdapter(), "document_draft"),
        (MemoryProposalAdapter(), "memory_proposal"),
        (ResearchSynthesisAdapter(), "research_synthesis_draft"),
    ],
)
def test_all_outputs_are_review_only_with_full_provenance(adapter, expected: str) -> None:
    proposal = adapter.create(_binding(), project_id="p1", title="Vertical", content="Draft content")
    assert proposal.proposal_type == expected
    assert proposal.status == "pending_review"
    assert proposal.metadata["formal_write"] is False
    assert proposal.metadata["requires_user_confirmation"] is True
    assert proposal.source_refs == ("source:req", "source:evidence")
    assert proposal.graph_revision == "g2" and proposal.compiler_revision == "1.0.0"


def test_user_can_accept_reject_or_modify_without_formal_write() -> None:
    proposal = MemoryProposalAdapter().create(_binding(), project_id="p1", title="Memory", content="Candidate")
    assert review_proposal(proposal, decision="accept").status == "accepted_for_effect"
    assert review_proposal(proposal, decision="reject").status == "rejected"
    modified = review_proposal(proposal, decision="modify", revised_content="User revised")
    assert modified.status == "modified_pending_review" and modified.content == "User revised"
    assert modified.metadata["formal_write"] is False


def test_accepted_proposal_is_not_a_writer_and_cannot_be_reviewed_twice() -> None:
    proposal = DocumentDraftAdapter().create(_binding(), project_id="p1", title="Doc", content="Draft")
    accepted = review_proposal(proposal, decision="accept")
    with pytest.raises(ValueError, match="already_reviewed"):
        review_proposal(accepted, decision="accept")
    assert not hasattr(accepted, "write") and not hasattr(accepted, "publish")


def test_only_accepted_review_state_can_cross_to_platform_handoff() -> None:
    proposal = MemoryProposalAdapter().create(
        _binding(), project_id="p1", title="Memory", content="Candidate",
    )
    handoff = PlatformProposalHandoffAdapter()
    for rejected in (
        proposal,
        review_proposal(proposal, decision="modify", revised_content="Edited candidate"),
        review_proposal(proposal, decision="reject"),
    ):
        with pytest.raises(ValueError, match="not_accepted_for_effect"):
            handoff.create_payload(rejected)

    payload = handoff.create_payload(review_proposal(proposal, decision="accept"))
    assert payload["formal_write"] is False
    assert payload["requires_user_review"] is True
