from __future__ import annotations

import pytest

from core.capability_packages.thought_graph_context import (
    DocumentDraftAdapter,
    MemoryProposalAdapter,
    PlatformProposalHandoffAdapter,
    ProjectSkillProposalAdapter,
    ResearchSynthesisAdapter,
    review_proposal,
)
from core.context_graph import ContextBinding
from core.product_core import ImportExternalAgentProposal
from core.storage_provider import JsonObjectStore


def _binding() -> ContextBinding:
    return ContextBinding(
        schema_version="1.0.0", graph_id="handoff", graph_revision="g3",
        capability_revision="cap-2", compiler_revision="1.0.0", boundary_revision="b2",
        provider_revision="p2", model_route_revision="m2",
        messages=({"role": "assistant", "content": "draft"},),
        layers={"materials": (), "references": (), "conversation": ()},
        layer_token_costs={"materials": 0, "references": 0, "conversation": 2}, total_token_cost=2,
        trimmed_nodes=(), excluded_nodes=(), stale_nodes=(),
        source_refs=("source:req", "source:evidence"), deterministic_order=("req", "evidence", "decision"),
        budget_explanation={"hard_budget": 100},
    )


def _skill_proposal():
    return ProjectSkillProposalAdapter().create(
        _binding(), project_id="p1", title="Skill", content="Rule",
        sections={"id": "skill-1", "project_id": "p1"},
    )


@pytest.mark.parametrize(
    "proposal, target_id, expected_type",
    [
        (MemoryProposalAdapter().create(_binding(), project_id="p1", title="Memory", content="Candidate"), None, "memory_candidate_proposal"),
        (_skill_proposal(), "skill-1", "project_skill_update_proposal"),
        (DocumentDraftAdapter().create(_binding(), project_id="p1", title="Doc", content="Paragraph"), "doc-1", "document_revision_proposal"),
        (ResearchSynthesisAdapter().create(_binding(), project_id="p1", title="Research", content="Synthesis"), "research-1", "document_revision_proposal"),
    ],
)
def test_handoff_maps_to_existing_generic_pending_review_contract(tmp_path, proposal, target_id, expected_type) -> None:
    accepted = review_proposal(proposal, decision="accept")
    payload = PlatformProposalHandoffAdapter().create_payload(accepted, target_id=target_id)
    assert payload["proposal_type"] == expected_type
    assert payload["requires_user_review"] is True
    assert payload["formal_write"] is False
    assert payload["linemap_review_status"] == "accepted_for_effect"
    assert payload["suggested_changes"]["linemap_provenance"]["graph_revision"] == "g3"

    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    result = ImportExternalAgentProposal(store).execute(proposal=payload, project_id="p1")
    assert result.status == "pending_review"
    assert result.memory_publication_state == "not_published"
    assert store.list("memory_atoms") == ()
    assert store.list("project_skills") == ()


def test_handoff_requires_accepted_state_target_and_provenance() -> None:
    adapter = PlatformProposalHandoffAdapter()
    document = DocumentDraftAdapter().create(_binding(), project_id="p1", title="Doc", content="Paragraph")
    with pytest.raises(ValueError, match="not_accepted_for_effect"):
        adapter.create_payload(document, target_id="doc-1")
    modified = review_proposal(document, decision="modify", revised_content="User changed draft")
    with pytest.raises(ValueError, match="not_accepted_for_effect"):
        adapter.create_payload(modified, target_id="doc-1")
    with pytest.raises(ValueError, match="target_id_required"):
        adapter.create_payload(review_proposal(document, decision="accept"))
    with pytest.raises(ValueError, match="not_accepted_for_effect"):
        adapter.create_payload(review_proposal(document, decision="reject"), target_id="doc-1")


def test_project_skill_handoff_requires_structured_authority_contract() -> None:
    proposal = ProjectSkillProposalAdapter().create(
        _binding(), project_id="p1", title="Skill", content="Rule",
    )
    with pytest.raises(ValueError, match="structured_contract_required"):
        PlatformProposalHandoffAdapter().create_payload(
            review_proposal(proposal, decision="accept"), target_id="skill-1",
        )

    drifted = ProjectSkillProposalAdapter().create(
        _binding(), project_id="p1", title="Skill", content="Rule",
        sections={"id": "other", "project_id": "p1"},
    )
    with pytest.raises(ValueError, match="structured_identity_mismatch"):
        PlatformProposalHandoffAdapter().create_payload(
            review_proposal(drifted, decision="accept"), target_id="skill-1",
        )
