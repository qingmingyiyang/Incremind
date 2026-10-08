from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from core.product_core.expert_catalog import ExpertCatalog, ExpertProjectBindingStore
from core.product_core.expert_memory_proposal import build_expert_memory_proposal
from core.product_core.external_agent_proposal_import import ImportExternalAgentProposal


class ExpertMemoryProposalRuntime:
    """Production adapter from a frozen Expert Receipt to pending review only."""

    def __init__(self, root_dir: Path, object_store: object, *, namespace_id: str) -> None:
        self._catalog = ExpertCatalog(Path(root_dir))
        self._bindings = ExpertProjectBindingStore(Path(root_dir))
        self._importer = ImportExternalAgentProposal(
            object_store, namespace_id=namespace_id,
        )

    def prepare(
        self,
        snapshot: Mapping[str, object],
        expert_receipt: Mapping[str, object],
        expert_result: Mapping[str, object],
    ) -> Mapping[str, object]:
        summary = expert_result.get("summary")
        evidence_refs = expert_result.get("evidence_refs")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("expert result summary is unavailable")
        if not isinstance(evidence_refs, list) or not evidence_refs:
            raise ValueError("expert result evidence is unavailable")
        proposal = build_expert_memory_proposal(
            snapshot=snapshot,
            catalog=self._catalog,
            bindings=self._bindings,
            context_manifest_revision=snapshot["context_manifest_revision"],
            boundary_revision=snapshot["boundary_revision"],
            model_route_revision=snapshot["model_route_revision"],
            tool_capability_revisions=snapshot["tool_capability_revisions"],
            expert_receipt=expert_receipt,
            suggested_changes={
                "target_layer": "atom",
                "candidate_type": "answer_fact",
                "content": summary.strip(),
            },
            evidence_refs=list(evidence_refs),
            proposal_type="memory_candidate_proposal",
        )
        self._importer.validate(proposal)
        return proposal

    def commit(self, proposal: Mapping[str, object], expert_receipt: Mapping[str, object]) -> Mapping[str, object]:
        result = self._importer.execute(
            proposal=proposal,
            project_id=str(proposal.get("project_id") or "").strip() or None,
        )
        if result.status != "pending_review" or result.review_state != "pending_review":
            raise ValueError("expert memory proposal escaped pending review")
        return {
            "schema_version": "1.0.0",
            "proposal_id": result.proposal_id,
            "proposal_type": result.proposal_type,
            "project_id": result.project_id,
            "memory_candidate_id": result.memory_candidate_id,
            "status": result.status,
            "review_state": result.review_state,
            "memory_publication_state": result.memory_publication_state,
            "blocked_operations": list(result.blocked_operations),
            "expert_receipt_id": expert_receipt.get("receipt_id"),
        }
