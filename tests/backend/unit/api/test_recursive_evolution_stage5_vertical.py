"""Stage 5 vertical crash/replay invariants for recursive evolution."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.api.personal_world_model_runtime import PersonalWorldModelRuntime
from backend.api.recursive_evolution_runtime import CanaryObservation, RecursiveEvolutionRuntime, RecursiveEvolutionRuntimeError
from core.recursive_evolution import EvaluationVerdict, EvolutionEpisode, EvolutionEvaluation, EvolutionPolicy, EvolutionProposal, EvolutionResourceEnvelope, EvolutionReview, EvolutionTargetKind, ReviewVerdict


PROJECT = "project-alpha"
EPISODE = "episode-alpha"
PROPOSAL = "proposal-alpha"
NOW = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)


class Evidence:
    def verify_evaluation(self, *, project_id, proposal, receipt_ref):
        return EvolutionEvaluation("evaluation-alpha", proposal.episode_id, proposal.proposal_id, "evaluator-alpha", .5, .7, "metric-v1", "crp://input/eval", "input-v1", "crp://result/base", "crp://result/candidate", 1, EvaluationVerdict.QUALIFIED, (receipt_ref,))

    def verify_canary(self, *, project_id, proposal, evidence_ref):
        return CanaryObservation(proposal.episode_id, proposal.proposal_id, evidence_ref != "crp://canary/fail", 1, (evidence_ref,))


class Approvals:
    def verify_local_human(self, **kwargs):
        return None


class FaultTarget:
    def __init__(self):
        self.applied: dict[str, str] = {}
        self.calls: list[str] = []
        self.fail_after_native_once = False

    def probe(self, *, operation_id):
        return self.applied.get(operation_id)

    def preflight(self, *, project_id, action, proposal):
        assert project_id == PROJECT

    def apply(self, *, operation_id, action, proposal, authorization_ref):
        self.calls.append(action)
        ref = f"crp://recursive-evolution/target-operations/{operation_id}"
        self.applied[operation_id] = ref
        if self.fail_after_native_once:
            self.fail_after_native_once = False
            raise OSError("receipt store interrupted after native apply")
        return ref


def episode():
    return EvolutionEpisode(EPISODE, PROJECT, EvolutionTargetKind.AGENT_PROFILE, EvolutionPolicy(2, 2, 2, 10, 2, "2026-09-05T00:00:00Z"))


def proposal(*, expand=False):
    baseline = EvolutionResourceEnvelope(("read",), 1, 0, 0, ())
    candidate = EvolutionResourceEnvelope(("read", "write"), 1, 0, 0, ()) if expand else baseline
    return EvolutionProposal(PROPOSAL, EPISODE, 1, EvolutionTargetKind.AGENT_PROFILE, "proposer-alpha", "executor-alpha", "crp://target/base", "base-v1", None, "crp://target/candidate", "candidate-v1", baseline, candidate)


def runtime(root, target):
    return RecursiveEvolutionRuntime(world=PersonalWorldModelRuntime.for_root(root, now=lambda: NOW), evidence=Evidence(), approvals=Approvals(), targets=target, now=lambda: NOW)


def ready(service):
    service.create_episode(episode=episode(), command_id=EPISODE)
    service.record_candidate(project_id=PROJECT, proposal=proposal(), command_id=PROPOSAL)
    service.record_evaluation_from_receipt(project_id=PROJECT, episode_id=EPISODE, proposal_id=PROPOSAL, receipt_ref="crp://receipt/eval", command_id="evaluation-alpha")
    service.record_review(project_id=PROJECT, review=EvolutionReview("review-alpha", EPISODE, PROPOSAL, "reviewer-alpha", ReviewVerdict.QUALIFIED, ("evaluation-alpha",), ("crp://review/alpha",)), command_id="review-alpha")


def canary(service):
    service.approve_canary(project_id=PROJECT, episode_id=EPISODE, proposal_id=PROPOSAL, user_id="human-alpha", confirmation_ref="crp://approval/canary", evidence_refs=("crp://review/alpha",), command_id="canary-alpha")


def test_native_apply_then_receipt_crash_retries_without_second_native_apply(tmp_path):
    target = FaultTarget()
    service = runtime(tmp_path, target)
    target.fail_after_native_once = True
    with pytest.raises(RecursiveEvolutionRuntimeError, match="target authority"):
        service.create_episode(episode=episode(), command_id=EPISODE)
        service.record_candidate(project_id=PROJECT, proposal=proposal(), command_id=PROPOSAL)
    # Candidate lifecycle was not appended, while native operation was.  The
    # retry sees probe and does not execute it again.
    service.record_candidate(project_id=PROJECT, proposal=proposal(), command_id=PROPOSAL)
    assert target.calls.count("record_candidate") == 1


def test_receipt_then_world_append_retry_replays_without_second_target_apply(tmp_path):
    target = FaultTarget()
    service = runtime(tmp_path, target)
    service.create_episode(episode=episode(), command_id=EPISODE)
    first = service.record_candidate(project_id=PROJECT, proposal=proposal(), command_id=PROPOSAL)
    second = service.record_candidate(project_id=PROJECT, proposal=proposal(), command_id=PROPOSAL)
    assert first.replayed is False and second.replayed is True
    assert target.calls == ["record_candidate"]


def test_failed_canary_rollback_is_idempotent_across_restart(tmp_path):
    target = FaultTarget()
    service = runtime(tmp_path, target)
    ready(service)
    canary(service)
    service.observe_canary(project_id=PROJECT, episode_id=EPISODE, proposal_id=PROPOSAL, evidence_ref="crp://canary/fail", command_id="observation-failed")
    restarted = runtime(tmp_path, target)
    restarted.recover_pending_rollbacks(project_id=PROJECT)
    assert target.calls.count("rollback_canary") == 1


def test_restart_keeps_active_target_and_never_auto_promotes(tmp_path):
    target = FaultTarget()
    service = runtime(tmp_path, target)
    ready(service)
    canary(service)
    service.observe_canary(project_id=PROJECT, episode_id=EPISODE, proposal_id=PROPOSAL, evidence_ref="crp://canary/pass", command_id="observation-pass")
    restarted = runtime(tmp_path, target)
    assert restarted.get_projection(project_id=PROJECT, episode_id=EPISODE).proposal_statuses[PROPOSAL] == "canary_passed"
    assert "promote" not in target.calls


def test_capability_expansion_is_rejected_before_any_target_side_effect(tmp_path):
    target = FaultTarget()
    service = runtime(tmp_path, target)
    service.create_episode(episode=episode(), command_id=EPISODE)
    with pytest.raises(Exception, match="cannot expand"):
        service.record_candidate(project_id=PROJECT, proposal=proposal(expand=True), command_id=PROPOSAL)
    assert target.calls == []
