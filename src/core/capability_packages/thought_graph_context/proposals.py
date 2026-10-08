from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, Mapping

from ...context_graph.models import ContextBinding

ProposalType = Literal["document_draft", "project_skill_proposal", "memory_proposal", "research_synthesis_draft"]
ProposalStatus = Literal["pending_review", "accepted_for_effect", "rejected", "modified_pending_review"]

# This package owns the review record only.  ``accepted_for_effect`` means a
# user has explicitly accepted the LineMap proposal and it may be handed to the
# platform's *separate* review/effect path.  It never means the package can
# write an authority object itself.
_REVIEW_TRANSITIONS: Mapping[ProposalStatus, frozenset[str]] = {
    "pending_review": frozenset({"accept", "reject", "modify"}),
    "modified_pending_review": frozenset({"accept", "reject", "modify"}),
    "accepted_for_effect": frozenset(),
    "rejected": frozenset(),
}
_HANDOFFABLE_STATUS: ProposalStatus = "accepted_for_effect"


@dataclass(frozen=True, slots=True)
class LineMapProposal:
    proposal_id: str
    proposal_type: ProposalType
    status: ProposalStatus
    project_id: str
    title: str
    content: str
    source_node_ids: tuple[str, ...]
    source_refs: tuple[str, ...]
    graph_revision: str
    capability_revision: str
    compiler_revision: str
    boundary_revision: str
    provider_revision: str
    model_route_revision: str
    metadata: Mapping[str, object]
    lineage: tuple[str, ...] = ()
    redaction: Literal["none", "soft", "hard"] = "none"
    rollback_of: str | None = None


def _proposal(binding: ContextBinding, *, project_id: str, proposal_type: ProposalType, title: str, content: str, metadata: Mapping[str, object] | None = None) -> LineMapProposal:
    if not content.strip():
        raise ValueError("proposal_content_required")
    return LineMapProposal(
        proposal_id=f"linemap-{proposal_type}-{binding.graph_id}-{binding.graph_revision}",
        proposal_type=proposal_type, status="pending_review", project_id=project_id,
        title=title.strip() or proposal_type, content=content,
        source_node_ids=binding.deterministic_order, source_refs=binding.source_refs,
        graph_revision=binding.graph_revision, capability_revision=binding.capability_revision,
        compiler_revision=binding.compiler_revision, boundary_revision=binding.boundary_revision,
        provider_revision=binding.provider_revision, model_route_revision=binding.model_route_revision,
        metadata={"formal_write": False, "requires_user_confirmation": True,
                  "trimmed_nodes": tuple(item["node_id"] for item in binding.trimmed_nodes),
                  "stale_nodes": binding.stale_nodes, **dict(metadata or {})},
    )


class DocumentDraftAdapter:
    def create(self, binding: ContextBinding, *, project_id: str, title: str, content: str, paragraphs: tuple[Mapping[str, object], ...] = ()) -> LineMapProposal:
        allowed = set(binding.deterministic_order)
        for paragraph in paragraphs:
            source_nodes = paragraph.get("source_node_ids")
            if not isinstance(source_nodes, (list, tuple)) or not source_nodes or not set(source_nodes).issubset(allowed):
                raise ValueError("invalid_paragraph_provenance")
        return _proposal(binding, project_id=project_id, proposal_type="document_draft", title=title, content=content,
                         metadata={"paragraph_provenance": paragraphs})


class ProjectSkillProposalAdapter:
    def create(self, binding: ContextBinding, *, project_id: str, title: str, content: str, sections: Mapping[str, object] | None = None) -> LineMapProposal:
        return _proposal(binding, project_id=project_id, proposal_type="project_skill_proposal", title=title, content=content,
                         metadata={"sections": dict(sections or {})})


class MemoryProposalAdapter:
    def create(self, binding: ContextBinding, *, project_id: str, title: str, content: str) -> LineMapProposal:
        return _proposal(binding, project_id=project_id, proposal_type="memory_proposal", title=title, content=content)


class ResearchSynthesisAdapter:
    def create(self, binding: ContextBinding, *, project_id: str, title: str, content: str, open_questions: tuple[str, ...] = (), unverified_inferences: tuple[str, ...] = ()) -> LineMapProposal:
        return _proposal(binding, project_id=project_id, proposal_type="research_synthesis_draft", title=title, content=content,
                         metadata={"open_questions": open_questions, "unverified_inferences": unverified_inferences})


class PlatformProposalHandoffAdapter:
    """Map a reviewable LineMap proposal to the platform's generic proposal contract.

    The adapter is deliberately transport-free.  Its result still requires the
    platform proposal importer, user review, Gate and Effect Runner before a
    formal object can change.
    """

    _PLATFORM_TYPES = {
        "memory_proposal": "memory_candidate_proposal",
        "project_skill_proposal": "project_skill_update_proposal",
        "document_draft": "document_revision_proposal",
        "research_synthesis_draft": "document_revision_proposal",
    }

    def create_payload(
        self,
        proposal: LineMapProposal,
        *,
        target_id: str | None = None,
    ) -> Mapping[str, object]:
        if proposal.status != _HANDOFFABLE_STATUS:
            raise ValueError("proposal_not_accepted_for_effect")
        if proposal.redaction == "hard" or not proposal.content.strip():
            raise ValueError("proposal_content_unavailable")
        if not proposal.source_refs:
            raise ValueError("proposal_source_refs_required")

        platform_type = self._PLATFORM_TYPES[proposal.proposal_type]
        suggested_changes: dict[str, object] = {
            "proposed_content": proposal.content,
            "linemap_provenance": {
                "source_node_ids": list(proposal.source_node_ids),
                "graph_revision": proposal.graph_revision,
                "capability_revision": proposal.capability_revision,
                "compiler_revision": proposal.compiler_revision,
                "boundary_revision": proposal.boundary_revision,
                "provider_revision": proposal.provider_revision,
                "model_route_revision": proposal.model_route_revision,
                "proposal_review_status": proposal.status,
            },
        }
        if platform_type == "memory_candidate_proposal":
            suggested_changes.update({"target_layer": "atom", "candidate_type": "linemap_memory_proposal"})
        elif platform_type == "project_skill_update_proposal":
            skill_id = self._required_target(proposal, target_id)
            structured = proposal.metadata.get("sections")
            if not isinstance(structured, Mapping) or not structured:
                raise ValueError("project_skill_structured_contract_required")
            if structured.get("id") != skill_id or structured.get("project_id") != proposal.project_id:
                raise ValueError("project_skill_structured_identity_mismatch")
            suggested_changes.update({
                "project_skill_id": skill_id,
                "structured": dict(structured),
                "markdown": proposal.content,
            })
        else:
            suggested_changes["document_id"] = self._required_target(proposal, target_id)

        return {
            "proposal_id": proposal.proposal_id,
            "proposal_type": platform_type,
            "project_id": proposal.project_id,
            "summary": proposal.title,
            "source_refs": list(proposal.source_refs),
            "evidence_refs": [
                {"locator": f"linemap-node:{node_id}", "graph_revision": proposal.graph_revision}
                for node_id in proposal.source_node_ids
            ],
            "suggested_changes": suggested_changes,
            # This is an importable platform proposal/draft, not an Effect
            # Intent and not an authority write.  The receiving platform still
            # presents its own review and, only after confirmation, creates the
            # governed Effect.
            "linemap_review_status": proposal.status,
            "formal_write": False,
            "requires_user_review": True,
        }

    @staticmethod
    def _required_target(proposal: LineMapProposal, explicit: str | None) -> str:
        metadata_keys = (
            ("project_skill_id", "target_project_skill_id")
            if proposal.proposal_type == "project_skill_proposal"
            else ("document_id", "target_document_id")
        )
        candidates = (explicit, *(proposal.metadata.get(key) for key in metadata_keys))
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        raise ValueError("proposal_target_id_required")


def review_proposal(proposal: LineMapProposal, *, decision: Literal["accept", "reject", "modify"], revised_content: str | None = None) -> LineMapProposal:
    if decision not in _REVIEW_TRANSITIONS[proposal.status]:
        raise ValueError("proposal_already_reviewed")
    if decision == "accept":
        return replace(proposal, status="accepted_for_effect")
    if decision == "reject":
        return replace(proposal, status="rejected")
    if decision == "modify" and revised_content and revised_content.strip():
        return replace(proposal, status="modified_pending_review", content=revised_content)
    raise ValueError("invalid_proposal_review")


def supersede_proposal(previous: LineMapProposal, replacement: LineMapProposal) -> LineMapProposal:
    if previous.project_id != replacement.project_id or previous.proposal_type != replacement.proposal_type:
        raise ValueError("proposal_lineage_scope_mismatch")
    return replace(replacement, lineage=previous.lineage + (previous.proposal_id,))


def redact_proposal(proposal: LineMapProposal, *, mode: Literal["soft", "hard"]) -> LineMapProposal:
    if mode == "soft":
        return replace(proposal, redaction="soft", metadata={**proposal.metadata, "redacted": True})
    return replace(proposal, redaction="hard", content="", source_refs=(), source_node_ids=(),
                   metadata={"formal_write": False, "requires_user_confirmation": True, "hard_redacted": True})


def rollback_proposal(current: LineMapProposal, previous: LineMapProposal) -> LineMapProposal:
    if current.project_id != previous.project_id or current.proposal_type != previous.proposal_type:
        raise ValueError("proposal_rollback_scope_mismatch")
    return replace(previous, proposal_id=f"{previous.proposal_id}-rollback", status="modified_pending_review",
                   lineage=current.lineage + (current.proposal_id,), rollback_of=current.proposal_id)
