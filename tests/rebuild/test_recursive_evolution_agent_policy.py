from __future__ import annotations

from dataclasses import replace

import pytest

from core.recursive_evolution.agent_policy import (
    AgentEvolutionPolicy, AgentEvolutionPolicyConflict, AgentEvolutionPolicyError,
    AgentPolicyCatalog, AgentPolicyContext, AgentPolicyEvaluation, AgentPolicyRouting,
    AgentPolicyScheduler, VerifiedRolloutEvidence, is_in_canary_cohort,
)
from core.storage_provider import SQLiteStructuredRecordStore


class _Evidence:
    def __init__(self) -> None:
        self.items: dict[str, VerifiedRolloutEvidence] = {}

    def resolve(self, evidence_ref: str) -> VerifiedRolloutEvidence | None:
        return self.items.get(evidence_ref)

    def approve(self, revision: int, *, turns: int = 3, qualified: bool = True, reviewed: bool = True) -> str:
        ref = f"crp://evolution/evidence/workbench.default/r{revision}-{turns}-{int(qualified)}-{int(reviewed)}"
        self.items[ref] = VerifiedRolloutEvidence(ref, "workbench.default", revision, qualified, reviewed, turns)
        return ref


def _policy(revision: int = 1, *, parent_revision: int | None = None, **changes) -> AgentEvolutionPolicy:
    value = AgentEvolutionPolicy(
        policy_id="workbench.default", revision=revision, status="candidate", parent_revision=parent_revision,
        target_roles=("main", "subagent"), routing=AgentPolicyRouting(("main.orchestrator", "subagent.explorer")),
        scheduler=AgentPolicyScheduler("expert_cluster", 3, 2, False, ("research",), ("project-skill",)),
        context=AgentPolicyContext(True, True, True, 32_000), evaluation=AgentPolicyEvaluation(10, 3, True),
    )
    return replace(value, **changes)


def _catalog(tmp_path, evidence: _Evidence, baseline: AgentEvolutionPolicy | None = None) -> AgentPolicyCatalog:
    return AgentPolicyCatalog(
        SQLiteStructuredRecordStore(tmp_path / "records.sqlite3"),
        profile_ids=("main.orchestrator", "subagent.explorer"), expert_ids=("research",), skill_ids=("project-skill",),
        trusted_baseline=baseline or _policy(), rollout_evidence=evidence,
    )


def _candidate_two() -> AgentEvolutionPolicy:
    return _policy(2, parent_revision=1, context=AgentPolicyContext(False, True, True, 24_000), evaluation=AgentPolicyEvaluation(10, 3, True))


def test_policy_allowlist_cardinality_and_payload_limit():
    payload = _policy().to_payload()
    assert AgentEvolutionPolicy.from_payload(payload) == _policy()
    payload["prompt"] = "forbidden"
    with pytest.raises(AgentEvolutionPolicyError, match="fields are not allowed"):
        AgentEvolutionPolicy.from_payload(payload)
    with pytest.raises(AgentEvolutionPolicyError, match="routing profile_ids"):
        AgentPolicyRouting(tuple(f"profile.{i}" for i in range(9)))


def test_trusted_baseline_bootstraps_active_and_normal_candidate_cannot_be_root(tmp_path):
    evidence = _Evidence()
    catalog = _catalog(tmp_path, evidence)
    assert catalog.active("workbench.default") == _policy()
    with pytest.raises(AgentEvolutionPolicyConflict, match="original command"):
        catalog.create_candidate(_policy(), command_id="candidate-0001")


def test_evidence_required_for_canary_and_activation(tmp_path):
    evidence = _Evidence()
    catalog = _catalog(tmp_path, evidence)
    candidate = _candidate_two()
    catalog.create_candidate(candidate, command_id="candidate-0002")
    with pytest.raises(AgentEvolutionPolicyError, match="unavailable"):
        catalog.start_canary("workbench.default", 2, percent=10, evidence_ref="crp://evolution/evidence/missing", expected_head_revision=2, command_id="canary-000002")
    rejected = evidence.approve(2, qualified=False)
    with pytest.raises(AgentEvolutionPolicyConflict, match="qualified human review"):
        catalog.start_canary("workbench.default", 2, percent=10, evidence_ref=rejected, expected_head_revision=2, command_id="canary-000003")
    approved = evidence.approve(2)
    catalog.start_canary("workbench.default", 2, percent=10, evidence_ref=approved, expected_head_revision=2, command_id="canary-000004")
    insufficient = evidence.approve(2, turns=2)
    with pytest.raises(AgentEvolutionPolicyConflict, match="insufficient completed turns"):
        catalog.activate("workbench.default", 2, evidence_ref=insufficient, expected_head_revision=3, command_id="activate-00002")
    catalog.activate("workbench.default", 2, evidence_ref=approved, expected_head_revision=3, command_id="activate-00003")


def test_canary_rejects_existing_canary_and_active_reopen(tmp_path):
    evidence = _Evidence()
    catalog = _catalog(tmp_path, evidence)
    catalog.create_candidate(_candidate_two(), command_id="candidate-0002")
    ref = evidence.approve(2)
    catalog.start_canary("workbench.default", 2, percent=10, evidence_ref=ref, expected_head_revision=2, command_id="canary-000002")
    with pytest.raises(AgentEvolutionPolicyConflict, match="already active"):
        catalog.start_canary("workbench.default", 2, percent=10, evidence_ref=ref, expected_head_revision=3, command_id="canary-000003")
    catalog.activate("workbench.default", 2, evidence_ref=ref, expected_head_revision=3, command_id="activate-00002")
    with pytest.raises(AgentEvolutionPolicyConflict, match="active revision cannot reopen"):
        catalog.start_canary("workbench.default", 2, percent=10, evidence_ref=ref, expected_head_revision=4, command_id="canary-000004")


def test_candidate_cannot_expand_modes_or_evaluation_constraints(tmp_path):
    evidence = _Evidence()
    baseline = _policy(
        scheduler=AgentPolicyScheduler("main_only", 0, 0, True, (), ()), routing=AgentPolicyRouting(("main.orchestrator",)), context=AgentPolicyContext(False, False, True, 8_000),
    )
    catalog = _catalog(tmp_path, evidence, baseline)
    expanded = _policy(2, parent_revision=1, scheduler=AgentPolicyScheduler("expert_cluster", 0, 0, True, (), ()), routing=baseline.routing, context=baseline.context)
    with pytest.raises(AgentEvolutionPolicyError, match="expands active authority"):
        catalog.create_candidate(expanded, command_id="candidate-0002")
    loosened = _policy(2, parent_revision=1, scheduler=baseline.scheduler, routing=baseline.routing, context=baseline.context, evaluation=AgentPolicyEvaluation(11, 2, True))
    with pytest.raises(AgentEvolutionPolicyError, match="expands active authority"):
        catalog.create_candidate(loosened, command_id="candidate-0003")


def test_canary_cohort_and_turn_snapshot_freeze(tmp_path):
    evidence = _Evidence()
    catalog = _catalog(tmp_path, evidence)
    assert not is_in_canary_cohort("project.one", "turn.one", "workbench.default", 0)
    assert is_in_canary_cohort("project.one", "turn.one", "workbench.default", 100)
    assert is_in_canary_cohort("project.one", "turn.one", "workbench.default", 10) == is_in_canary_cohort("project.one", "turn.one", "workbench.default", 10)
    first = catalog.freeze_for_new_turn("workbench.default", project_id="project.one", turn_id="turn.one")
    assert first.selected_revision == 1
    assert catalog.freeze_for_new_turn("workbench.default", project_id="project.one", turn_id="turn.one") == first
    catalog.create_candidate(_candidate_two(), command_id="candidate-0002")
    ref = evidence.approve(2)
    catalog.start_canary("workbench.default", 2, percent=10, evidence_ref=ref, expected_head_revision=2, command_id="canary-000002")
    assert catalog.load_turn_snapshot("workbench.default", project_id="project.one", turn_id="turn.one") == first


def test_failed_candidate_allows_a_newer_generation_from_same_active(tmp_path):
    evidence = _Evidence()
    catalog = _catalog(tmp_path, evidence)
    candidate_two = _candidate_two()
    catalog.create_candidate(candidate_two, command_id="candidate-0002")
    ref = evidence.approve(2)
    catalog.start_canary("workbench.default", 2, percent=10, evidence_ref=ref, expected_head_revision=2, command_id="canary-000002")
    catalog.rollback("workbench.default", expected_head_revision=3, command_id="rollback-00002")
    candidate_three = _policy(3, parent_revision=1, context=AgentPolicyContext(True, False, True, 20_000), evaluation=AgentPolicyEvaluation(10, 3, True))
    assert catalog.create_candidate(candidate_three, command_id="candidate-0003") == candidate_three
