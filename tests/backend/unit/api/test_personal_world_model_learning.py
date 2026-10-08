from __future__ import annotations

from pathlib import Path

import pytest

from backend.api.personal_world_model_learning import (
    PersonalWorldModelLearningRuntime,
)
from backend.api.personal_world_model_runtime import (
    VerifiedTurnOutcome,
    VerifiedWorldFeedback,
)
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.personal_world_model import (
    FeedbackCost,
    FeedbackFact,
    OutcomeStatus,
    PersonalWorldModelError,
    StateDelta,
    UserEvaluation,
    UserEvaluationVerdict,
    WorldEvent,
    WorldEventKind,
)


PROJECT = "project-learning"
TURN = "turn-learning"
ACTION = "operation-learning"
FEEDBACK = "feedback-learning"
EVENT = "feedback-event-learning"
OUTCOME_REF = "crp://session/turn-learning/tool-invocation-outcome/outcome-learning"


class _World:
    def __init__(self, *, verdict: UserEvaluationVerdict = UserEvaluationVerdict.ACCEPTED) -> None:
        fact = FeedbackFact(
            feedback_id=FEEDBACK,
            supersedes_feedback_id=None,
            project_id=PROJECT,
            action_id=ACTION,
            expected_outcome="The project artifact is updated",
            actual_outcome="The updated project artifact was verified",
            outcome=OutcomeStatus.ACHIEVED,
            state_delta=(StateDelta("artifact.revision", 1, 2),),
            cost=FeedbackCost(elapsed_ms=400, external_calls=1),
            user_evaluation=UserEvaluation(
                verdict,
                rating=None if verdict is UserEvaluationVerdict.NOT_PROVIDED else 5,
                note=None if verdict is UserEvaluationVerdict.NOT_PROVIDED else "Keep this method",
            ),
            evidence_refs=(OUTCOME_REF,),
        )
        event = WorldEvent(
            event_id=EVENT,
            project_id=PROJECT,
            sequence=3,
            kind=WorldEventKind.FEEDBACK_RECORDED,
            actor="user",
            source_ref=OUTCOME_REF,
            source_revision="4",
            occurred_at="2026-09-01T10:00:00Z",
            recorded_at="2026-09-01T10:00:00Z",
            payload=fact.to_payload(),
        )
        self.verified = VerifiedWorldFeedback(
            event,
            fact,
            VerifiedTurnOutcome(
                turn_id=TURN,
                operation_id=ACTION,
                turn_status="completed",
                terminal_sequence=4,
                terminal_occurred_at="2026-09-01T09:59:59Z",
                outcome_ref=OUTCOME_REF,
                invocation_id="tool-call-learning",
                capability_id="project.write",
                tool_status="completed",
                effect_certainty="confirmed_applied",
            ),
        )

    def verified_feedback(self, *, project_id: str, feedback_id: str, turn_id: str):
        if (project_id, feedback_id, turn_id) != (PROJECT, FEEDBACK, TURN):
            raise PersonalWorldModelError("feedback scope drifted")
        return self.verified


class _SkillRuntime:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def propose(self, **values: object) -> dict[str, object]:
        self.calls.append(values)
        return {
            "status": "pending_review",
            "proposal_id": "skill-proposal-learning",
            "write_effect": "proposal_only",
        }


def _runtime(
    root: Path,
    *,
    world: _World | None = None,
    skill: _SkillRuntime | None = None,
) -> tuple[PersonalWorldModelLearningRuntime, ObjectStoreMemoryCandidateRepository]:
    store, _settings = build_rebuild_object_store(root)
    candidates = ObjectStoreMemoryCandidateRepository(store)
    return (
        PersonalWorldModelLearningRuntime(
            world=world or _World(),
            object_store=store,
            candidates=candidates,
            skill_runtime=skill,
        ),
        candidates,
    )


def _memory(content: str = "Reuse the verified two-step artifact update check") -> dict[str, str]:
    return {
        "proposed_content": content,
        "reason": "The user explicitly accepted this verified project method",
    }


def _skill() -> dict[str, object]:
    return {
        "resolution_id": "resolution-learning",
        "skill_id": "project-method",
        "expected_fingerprint": "a" * 64,
        "reusable_signal": {
            "kind": "explicit_correction",
            "evidence": "The user explicitly confirmed the corrected verification sequence.",
        },
        "proposed_content": {
            "summary": "Verify the artifact revision after each governed update.",
            "instructions": "Read the authoritative receipt, then verify the artifact revision before reporting the outcome.",
        },
    }


def test_verified_feedback_creates_only_pending_memory_candidate_and_replays(
    tmp_path: Path,
) -> None:
    runtime, candidates = _runtime(tmp_path)

    created = runtime.propose(
        project_id=PROJECT,
        feedback_id=FEEDBACK,
        turn_id=TURN,
        memory_proposal=_memory(),
        skill_proposal=None,
    )
    replayed = runtime.propose(
        project_id=PROJECT,
        feedback_id=FEEDBACK,
        turn_id=TURN,
        memory_proposal=_memory(),
        skill_proposal=None,
    )
    candidate = candidates.get(f"world-feedback-{EVENT}")

    assert created.memory is not None and created.memory.status == "pending_review"
    assert created.memory.write_effect == "candidate_only"
    assert replayed.replayed is True
    assert candidate is not None
    assert candidate["status"] == "pending_review"
    assert candidate["review"]["auto_promote_allowed"] is False
    assert candidate["provenance"]["world_feedback_id"] == FEEDBACK
    assert "application" not in candidate


def test_memory_learning_rejects_identity_drift_and_sensitive_content(
    tmp_path: Path,
) -> None:
    runtime, _candidates = _runtime(tmp_path)
    runtime.propose(
        project_id=PROJECT,
        feedback_id=FEEDBACK,
        turn_id=TURN,
        memory_proposal=_memory(),
        skill_proposal=None,
    )

    with pytest.raises(PersonalWorldModelError, match="identity conflicts"):
        runtime.propose(
            project_id=PROJECT,
            feedback_id=FEEDBACK,
            turn_id=TURN,
            memory_proposal=_memory("Use a different proposal for the same feedback"),
            skill_proposal=None,
        )
    with pytest.raises(PersonalWorldModelError, match="must remain an evidence reference"):
        other, _candidates = _runtime(tmp_path / "unsafe")
        other.propose(
            project_id=PROJECT,
            feedback_id=FEEDBACK,
            turn_id=TURN,
            memory_proposal=_memory("Read C:\\private\\artifact before continuing"),
            skill_proposal=None,
        )


def test_learning_requires_explicit_user_evaluation(tmp_path: Path) -> None:
    runtime, _candidates = _runtime(
        tmp_path,
        world=_World(verdict=UserEvaluationVerdict.NOT_PROVIDED),
    )

    with pytest.raises(PersonalWorldModelError, match="explicit user evaluation"):
        runtime.propose(
            project_id=PROJECT,
            feedback_id=FEEDBACK,
            turn_id=TURN,
            memory_proposal=_memory(),
            skill_proposal=None,
        )


def test_explicit_signal_creates_one_existing_skill_proposal_and_no_skill_write(
    tmp_path: Path,
) -> None:
    skill = _SkillRuntime()
    runtime, _candidates = _runtime(tmp_path, skill=skill)
    store = runtime.object_store
    proposal = _skill()

    created = runtime.propose(
        project_id=PROJECT,
        feedback_id=FEEDBACK,
        turn_id=TURN,
        memory_proposal=None,
        skill_proposal=proposal,
    )
    store.write("application_skill_proposals", "skill-proposal-learning", {
        "proposal_id": "skill-proposal-learning",
        "status": "pending_review",
        "payload": {
            "reusable_signal": proposal["reusable_signal"],
            "proposed_content": proposal["proposed_content"],
        },
    }, expected_revision=0)
    replayed = runtime.propose(
        project_id=PROJECT,
        feedback_id=FEEDBACK,
        turn_id=TURN,
        memory_proposal=None,
        skill_proposal=proposal,
    )

    assert created.skill is not None
    assert created.skill.status == "pending_review"
    assert created.skill.write_effect == "proposal_only"
    assert replayed.skill is not None and replayed.skill.replayed is True
    assert len(skill.calls) == 1
    assert replayed.to_payload()["authority_effects"] == {
        "memory_publication": "not_performed",
        "skill_file_write": "not_performed",
    }
