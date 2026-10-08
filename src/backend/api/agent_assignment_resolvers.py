"""Provider-free assignment resolvers used by governed Agent dispatch.

The factory deliberately returns narrow callables compatible with
``AgentDispatchRuntime``.  It neither persists a proposal nor resolves a
provider/model; it only attests already-governed expert and application-skill
identities against their existing authorities.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import re
from uuid import NAMESPACE_URL, uuid5

from backend.api.expert_turn_binding_runtime import ExpertTurnBindingRuntime
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore


class AgentAssignmentResolverError(ValueError):
    """A dispatch assignment cannot be derived from current authority."""


_SKILL_ID = re.compile(r"^[a-z][a-z0-9_-]{1,127}$")
_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
ReviewedExternalSkillIds = Callable[[str], Sequence[str]]


@dataclass(frozen=True, slots=True)
class AgentAssignmentResolvers:
    """Narrow opaque resolver pair for ``AgentDispatchRuntime`` injection."""

    expert: Callable[[str, Mapping[str, object], str], tuple[str, int] | None]
    skill: Callable[[str, Mapping[str, object], str], tuple[str, int] | None]


def build_agent_assignment_resolvers(
    *,
    expert_runtime: ExpertTurnBindingRuntime,
    capability_profiles: ProjectCapabilityProfileStore,
    reviewed_external_skill_ids: ReviewedExternalSkillIds,
) -> AgentAssignmentResolvers:
    """Build strict resolvers without widening expert/profile authority."""
    if not isinstance(expert_runtime, ExpertTurnBindingRuntime):
        raise AgentAssignmentResolverError("expert assignment runtime is invalid")
    if not isinstance(capability_profiles, ProjectCapabilityProfileStore):
        raise AgentAssignmentResolverError("project capability authority is invalid")
    if not callable(reviewed_external_skill_ids):
        raise AgentAssignmentResolverError("reviewed external skill authority is invalid")

    def resolve_expert(
        project_id: str, proposal: Mapping[str, object], kind: str,
    ) -> tuple[str, int] | None:
        # The authoritative runtime validates exact DTO shape, active status,
        # project binding and both expert/binding revisions.
        return expert_runtime.resolve_dispatch_assignment(project_id, proposal, kind)

    def resolve_skill(
        project_id: str, proposal: Mapping[str, object], kind: str,
    ) -> tuple[str, int] | None:
        if kind != "skill":
            raise AgentAssignmentResolverError("dispatch assignment kind is invalid")
        if not isinstance(project_id, str) or _PROJECT_ID.fullmatch(project_id.strip()) is None:
            raise AgentAssignmentResolverError("dispatch skill project is invalid")
        if not isinstance(proposal, Mapping) or set(proposal) != {"skill_ids"}:
            raise AgentAssignmentResolverError("dispatch skill proposal shape is invalid")
        raw_ids = proposal.get("skill_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise AgentAssignmentResolverError("dispatch skill ids are invalid")
        skill_ids = tuple(item.strip() for item in raw_ids if isinstance(item, str))
        if len(skill_ids) != len(raw_ids) or len(skill_ids) != len(set(skill_ids)) or any(_SKILL_ID.fullmatch(item) is None for item in skill_ids):
            raise AgentAssignmentResolverError("dispatch skill ids are invalid")
        try:
            snapshot = capability_profiles.get(project_id.strip())
        except Exception as error:
            raise AgentAssignmentResolverError("dispatch project capability profile is unavailable") from error
        profile = snapshot.profile
        if profile.project_id != project_id.strip() or profile.revision < 1:
            raise AgentAssignmentResolverError("dispatch project capability profile drifted")
        try:
            reviewed = tuple(reviewed_external_skill_ids(project_id.strip()))
        except Exception as error:
            raise AgentAssignmentResolverError("reviewed external skill authority is unavailable") from error
        if any(not isinstance(item, str) or _SKILL_ID.fullmatch(item) is None for item in reviewed):
            raise AgentAssignmentResolverError("reviewed external skill authority is invalid")
        # Project-enabled bundled/plugin skills and reviewed external packages
        # are the same two sources admitted by Turn SkillSnapshot authority.
        # Requiring their intersection would accidentally make ordinary
        # project skills unusable unless duplicated by an external package.
        allowed = set(profile.enabled_skill_ids) | set(reviewed)
        if not set(skill_ids).issubset(allowed):
            raise AgentAssignmentResolverError("dispatch skills are not enabled and reviewed for project")
        stable_id = uuid5(
            NAMESPACE_URL,
            ":".join((project_id.strip(), str(profile.revision), *sorted(skill_ids))),
        ).hex
        return (
            f"crp://agent-skill-dispatch/{project_id.strip()}/revisions/{profile.revision}/selections/{stable_id}",
            profile.revision,
        )

    return AgentAssignmentResolvers(expert=resolve_expert, skill=resolve_skill)
