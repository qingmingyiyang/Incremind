from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.ai_boundary import BoundaryPolicyEngine, BoundaryRequest
from core.ai_kernel import (
    AgentAdapterProfile,
    ExternalAgentAdmissionSnapshot,
    ExternalAgentAuthoritySnapshot,
    ExternalAgentContextBridge,
    ExternalAgentContextError,
    SQLiteAITurnStore,
    generate_client_templates,
)
from core.product_core.external_agent_proposal_import import (
    ImportExternalAgentProposal,
    serialize_external_agent_proposal_import,
)


EXTERNAL_AGENT_ADAPTERS = (
    AgentAdapterProfile(
        adapter_id="codex",
        revision=1,
        template_revision="openai-skill-map-v1",
        maximum_context_bytes=64 * 1024,
        supported_purposes=("project_assistance",),
    ),
    AgentAdapterProfile(
        adapter_id="claude",
        revision=1,
        template_revision="claude-project-map-v1",
        maximum_context_bytes=64 * 1024,
        supported_purposes=("project_assistance",),
    ),
    AgentAdapterProfile(
        adapter_id="workbuddy",
        revision=1,
        template_revision="workbuddy-memory-map-v1",
        maximum_context_bytes=64 * 1024,
        supported_purposes=("project_assistance",),
    ),
)


def generate_external_agent_client_templates(
    *, target_ids: Mapping[str, str],
) -> Mapping[str, object]:
    """Generate client instructions only from the production Bridge profiles."""
    return generate_client_templates(EXTERNAL_AGENT_ADAPTERS, target_ids=target_ids)


def authorize_external_agent_context_start(
    root_dir: Path, *, operation_id: str, turn_id: str, project_id: str,
    adapter_id: str,
) -> ExternalAgentAdmissionSnapshot:
    """Evaluate one explicit, local, project-scoped read through Boundary."""
    boundary = ProjectBoundaryProfileStore(Path(root_dir).resolve()).get(project_id).profile
    decision = BoundaryPolicyEngine().evaluate(
        BoundaryRequest(
            request_id=operation_id,
            turn_id=turn_id,
            project_id=project_id,
            actor_id=f"external-agent-{adapter_id}",
            target_id=f"external-agent-context-{adapter_id}",
            operation_id=operation_id,
            idempotency_key=operation_id,
            effect="read",
            destination_kind="local",
            destination_id=adapter_id,
            data_classes=("project_instructions",),
            scan_state="local",
            reversible=True,
            same_project=True,
            requires_receipt=True,
        ),
        boundary,
    )
    if decision.outcome != "allow":
        raise ExternalAgentContextError("external agent admission was not allowed")
    return ExternalAgentAdmissionSnapshot(
        admission_id=operation_id,
        project_id=project_id,
        adapter_id=adapter_id,
        outcome=decision.outcome,
        policy_revision=decision.policy_revision,
        reason_codes=decision.reason_codes,
    )


def authorize_external_agent_memory_proposal(
    root_dir: Path, *, operation_id: str, turn_id: str, project_id: str,
    adapter_id: str,
) -> ExternalAgentAdmissionSnapshot:
    """Evaluate one explicit proposal-only write through the project Boundary."""
    boundary = ProjectBoundaryProfileStore(Path(root_dir).resolve()).get(project_id).profile
    decision = BoundaryPolicyEngine().evaluate(
        BoundaryRequest(
            request_id=operation_id,
            turn_id=turn_id,
            project_id=project_id,
            actor_id=f"external-agent-{adapter_id}",
            target_id=f"external-agent-memory-proposal-{adapter_id}",
            operation_id=operation_id,
            idempotency_key=operation_id,
            effect="write",
            destination_kind="local",
            destination_id="memory-candidate-review",
            data_classes=("memory_candidate",),
            scan_state="local",
            reversible=True,
            same_project=True,
            requires_receipt=True,
        ),
        boundary,
    )
    if decision.outcome != "allow":
        raise ExternalAgentContextError("external agent proposal admission was not allowed")
    return ExternalAgentAdmissionSnapshot(
        admission_id=operation_id,
        project_id=project_id,
        adapter_id=adapter_id,
        outcome=decision.outcome,
        policy_revision=decision.policy_revision,
        reason_codes=decision.reason_codes,
    )


def build_external_agent_context_bridge(root_dir: Path) -> ExternalAgentContextBridge:
    """Compose the read Bridge without provisioning the full AI/Hands runtime."""
    root = Path(root_dir).resolve()
    capability_profiles = ProjectCapabilityProfileStore(root)
    boundary_profiles = ProjectBoundaryProfileStore(root)
    object_store, settings = build_rebuild_object_store(root)

    def submit_proposal(
        proposal: Mapping[str, object], project_id: str,
    ) -> Mapping[str, object]:
        result = ImportExternalAgentProposal(
            object_store, namespace_id=settings.namespace_id,
        ).execute(proposal=proposal, project_id=project_id)
        return serialize_external_agent_proposal_import(result)

    def authority(project_id: str) -> ExternalAgentAuthoritySnapshot:
        capability = capability_profiles.get(project_id).profile
        boundary = boundary_profiles.get(project_id).profile
        if capability.project_id != project_id or boundary.project_id != project_id:
            raise ExternalAgentContextError("external agent project authority is unavailable")
        if (
            capability.boundary_profile_id != boundary.profile_id
            or capability.boundary_profile_revision != boundary.revision
        ):
            raise ExternalAgentContextError("external agent project authority is unavailable")
        return ExternalAgentAuthoritySnapshot(
            project_id=project_id,
            project_profile_id=capability.profile_id,
            project_profile_revision=capability.revision,
            boundary_profile_id=boundary.profile_id,
            boundary_profile_revision=boundary.revision,
        )

    bridge = ExternalAgentContextBridge(
        store=SQLiteAITurnStore(root / ".rebuild-data" / "ai-turns.sqlite3"),
        adapters=EXTERNAL_AGENT_ADAPTERS,
        authority=authority,
        proposal_sink=submit_proposal,
    )
    bridge.recover_prepared_memory_proposals(limit=32)
    return bridge
