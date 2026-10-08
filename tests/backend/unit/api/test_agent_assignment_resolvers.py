from __future__ import annotations

from pathlib import Path

import pytest

from backend.api.agent_assignment_resolvers import (
    AgentAssignmentResolverError,
    build_agent_assignment_resolvers,
)
from backend.api.expert_turn_binding_runtime import ExpertTurnBindingRuntime
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore


class _ExpertRuntime(ExpertTurnBindingRuntime):
    def __init__(self) -> None: pass
    def resolve_dispatch_assignment(self, project_id, proposal, kind):
        if kind != "expert" or project_id != "project-alpha": raise ValueError("rejected")
        return "crp://expert-dispatch/project-alpha/experts/test/expert-revisions/1/binding-revisions/2", 2


def _resolvers(tmp_path: Path, reviewed=lambda _project_id: ("external-skill",)):
    return build_agent_assignment_resolvers(
        expert_runtime=_ExpertRuntime(),
        capability_profiles=ProjectCapabilityProfileStore(tmp_path),
        reviewed_external_skill_ids=reviewed,
    )


def _enable(store: ProjectCapabilityProfileStore, *, revision: int = 0, skills=("selected-skill",)) -> None:
    store.update(
        "project-alpha", expected_revision=revision,
        boundary_profile_id="project-boundary-project-alpha", boundary_profile_revision=1,
        enabled_skill_ids=skills,
    )


def test_expert_is_direct_authoritative_delegate_and_skill_returns_canonical_ref(tmp_path: Path) -> None:
    store = ProjectCapabilityProfileStore(tmp_path); _enable(store)
    resolvers = _resolvers(tmp_path)
    assert resolvers.expert("project-alpha", {"expert_id": "test", "task_intents": ["research"], "budget": "small"}, "expert")[1] == 2
    ref, revision = resolvers.skill("project-alpha", {"skill_ids": ["selected-skill"]}, "skill")
    assert revision == 1
    assert ref.startswith("crp://agent-skill-dispatch/project-alpha/revisions/1/selections/")


def test_skill_rejects_wrong_kind_shape_duplicates_unknown_and_cross_project(tmp_path: Path) -> None:
    store = ProjectCapabilityProfileStore(tmp_path); _enable(store)
    skill = _resolvers(tmp_path).skill
    for proposal, kind in (({"skill_ids": ["selected-skill"]}, "expert"), ({"skill_ids": ["selected-skill"], "prompt": "no"}, "skill"), ({"skill_ids": ["selected-skill", "selected-skill"]}, "skill"), ({"skill_ids": ["other-skill"]}, "skill")):
        with pytest.raises(AgentAssignmentResolverError): skill("project-alpha", proposal, kind)
    with pytest.raises(AgentAssignmentResolverError): skill("project-beta", {"skill_ids": ["selected-skill"]}, "skill")


def test_skill_fails_closed_on_review_and_profile_revision_drift(tmp_path: Path) -> None:
    store = ProjectCapabilityProfileStore(tmp_path); _enable(store)
    resolver = _resolvers(tmp_path).skill
    first_ref, first_revision = resolver("project-alpha", {"skill_ids": ["selected-skill"]}, "skill")
    _enable(store, revision=1)
    next_ref, next_revision = resolver("project-alpha", {"skill_ids": ["selected-skill"]}, "skill")
    assert (first_ref, first_revision) != (next_ref, next_revision) and next_revision == 2
    # A project-enabled bundled/plugin Skill does not depend on an external
    # package, while a reviewed external Skill remains independently eligible.
    assert _resolvers(tmp_path, reviewed=lambda _project_id: ()).skill("project-alpha", {"skill_ids": ["selected-skill"]}, "skill")[1] == 2
    assert _resolvers(tmp_path).skill("project-alpha", {"skill_ids": ["external-skill"]}, "skill")[1] == 2
