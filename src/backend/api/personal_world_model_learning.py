"""Explicit, evidence-gated proposal intake from verified World Feedback."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from backend.api.application_skill_learning_runtime import (
    ApplicationSkillLearningRuntime,
)
from backend.api.personal_world_model_runtime import (
    PersonalWorldModelRuntime,
    VerifiedWorldFeedback,
)
from backend.api.task_completion_learning import (
    CompletedTurn,
    ExplicitSkillLearningEvidence,
    ObjectStoreSkillLearningJournal,
)
from core.application_skill import SkillLearningWorkshopError
from core.memory_core import (
    MemoryCandidateRepositoryError,
    ObjectStoreMemoryCandidateRepository,
)
from core.personal_world_model import (
    PersonalWorldModelError,
    UserEvaluationVerdict,
    validate_world_narrative,
)
from core.storage_provider import ObjectStorePort


@dataclass(frozen=True, slots=True)
class LearningProposalReference:
    proposal_id: str
    status: str
    write_effect: str
    replayed: bool = False

    def to_payload(self) -> dict[str, object]:
        return {
            "proposal_id": self.proposal_id,
            "status": self.status,
            "write_effect": self.write_effect,
            "replayed": self.replayed,
        }


@dataclass(frozen=True, slots=True)
class WorldFeedbackLearningOutcome:
    project_id: str
    feedback_id: str
    turn_id: str
    memory: LearningProposalReference | None
    skill: LearningProposalReference | None

    @property
    def replayed(self) -> bool:
        references = tuple(item for item in (self.memory, self.skill) if item is not None)
        return bool(references) and all(item.replayed for item in references)

    def to_payload(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "feedback_id": self.feedback_id,
            "turn_id": self.turn_id,
            "memory": None if self.memory is None else self.memory.to_payload(),
            "skill": None if self.skill is None else self.skill.to_payload(),
            "replayed": self.replayed,
            "authority_effects": {
                "memory_publication": "not_performed",
                "skill_file_write": "not_performed",
            },
        }


@dataclass(slots=True)
class PersonalWorldModelLearningRuntime:
    """Create existing review-only artifacts from one latest FeedbackFact."""

    world: PersonalWorldModelRuntime
    object_store: ObjectStorePort
    candidates: ObjectStoreMemoryCandidateRepository
    skill_runtime: ApplicationSkillLearningRuntime | None = None

    def propose(
        self,
        *,
        project_id: str,
        feedback_id: str,
        turn_id: str,
        memory_proposal: Mapping[str, object] | None,
        skill_proposal: Mapping[str, object] | None,
    ) -> WorldFeedbackLearningOutcome:
        if (memory_proposal is None) == (skill_proposal is None):
            raise PersonalWorldModelError(
                "learning requires exactly one explicit proposal"
            )
        verified = self.world.verified_feedback(
            project_id=project_id,
            feedback_id=feedback_id,
            turn_id=turn_id,
        )
        if verified.fact.user_evaluation.verdict is UserEvaluationVerdict.NOT_PROVIDED:
            raise PersonalWorldModelError(
                "learning requires an explicit user evaluation"
            )

        memory = (
            None
            if memory_proposal is None
            else self._propose_memory(verified, memory_proposal)
        )
        skill = (
            None
            if skill_proposal is None
            else self._propose_skill(verified, skill_proposal)
        )
        return WorldFeedbackLearningOutcome(
            project_id=verified.fact.project_id,
            feedback_id=verified.fact.feedback_id,
            turn_id=verified.evidence.turn_id,
            memory=memory,
            skill=skill,
        )

    def _propose_memory(
        self,
        verified: VerifiedWorldFeedback,
        proposal: Mapping[str, object],
    ) -> LearningProposalReference:
        if not isinstance(proposal, Mapping) or set(proposal) != {
            "proposed_content", "reason",
        }:
            raise PersonalWorldModelError("Memory learning proposal shape is invalid")
        proposed_content = validate_world_narrative(
            proposal.get("proposed_content"),
            "Memory proposed content",
            maximum=4000,
        )
        reason = validate_world_narrative(
            proposal.get("reason"),
            "Memory proposal reason",
            maximum=1000,
        )
        fact = verified.fact
        event = verified.event
        evidence = verified.evidence
        candidate_id = f"world-feedback-{event.event_id}"
        candidate = {
            "schema_version": "1.0.0",
            "id": candidate_id,
            "project_id": fact.project_id,
            "target_layer": "atom",
            "candidate_type": "other",
            "status": "pending_review",
            "proposed_content": proposed_content,
            "source_refs": [
                {
                    "source_id": event.event_id,
                    "locator": (
                        f"crp://world-model/projects/{fact.project_id}/events/{event.event_id}"
                    ),
                },
                {
                    "source_id": evidence.turn_id,
                    "locator": evidence.outcome_ref,
                },
            ],
            "provenance": {
                "world_feedback_id": fact.feedback_id,
                "world_event_id": event.event_id,
                "world_feedback_turn_id": evidence.turn_id,
                "world_feedback_outcome_ref": evidence.outcome_ref,
                "input_refs": [
                    {
                        "kind": "world_feedback",
                        "object_id": event.event_id,
                        "uri": (
                            f"crp://world-model/projects/{fact.project_id}/events/{event.event_id}"
                        ),
                    },
                    {
                        "kind": "turn_receipt",
                        "object_id": evidence.turn_id,
                        "uri": evidence.outcome_ref,
                    },
                ],
            },
            "review": {
                "requires_user_confirmation": True,
                "auto_promote_allowed": False,
                "reason": reason,
                "reviewed_by": None,
                "reviewed_at": None,
            },
            "created_at": event.recorded_at,
            "updated_at": event.recorded_at,
        }
        existing = self.candidates.get(candidate_id)
        if existing is not None:
            if dict(existing) != candidate:
                raise PersonalWorldModelError(
                    "Memory learning proposal identity conflicts"
                )
            return LearningProposalReference(
                candidate_id, "pending_review", "candidate_only", replayed=True,
            )
        try:
            self.candidates.save(candidate)
        except MemoryCandidateRepositoryError as error:
            raced = self.candidates.get(candidate_id)
            if raced is None or dict(raced) != candidate:
                raise PersonalWorldModelError(
                    "Memory learning proposal identity conflicts"
                ) from error
            return LearningProposalReference(
                candidate_id, "pending_review", "candidate_only", replayed=True,
            )
        return LearningProposalReference(
            candidate_id, "pending_review", "candidate_only",
        )

    def _propose_skill(
        self,
        verified: VerifiedWorldFeedback,
        proposal: Mapping[str, object],
    ) -> LearningProposalReference:
        if self.skill_runtime is None:
            raise PersonalWorldModelError("Skill learning runtime is unavailable")
        if not isinstance(proposal, Mapping) or set(proposal) != {
            "resolution_id",
            "skill_id",
            "expected_fingerprint",
            "reusable_signal",
            "proposed_content",
        }:
            raise PersonalWorldModelError("Skill learning proposal shape is invalid")
        if not isinstance(proposal.get("reusable_signal"), Mapping) or not isinstance(
            proposal.get("proposed_content"), Mapping
        ):
            raise PersonalWorldModelError("Skill learning proposal content is invalid")
        fact = verified.fact
        evidence = verified.evidence
        completion_id = f"world-feedback-{verified.event.event_id}"
        journal = ObjectStoreSkillLearningJournal(self.object_store)
        existing = journal.get(completion_id)
        if existing is not None:
            return self._replayed_skill(
                existing,
                proposal,
                project_id=fact.project_id,
                turn_id=evidence.turn_id,
            )

        explicit = ExplicitSkillLearningEvidence(
            resolution_id=str(proposal.get("resolution_id") or ""),
            skill_id=str(proposal.get("skill_id") or ""),
            expected_fingerprint=str(proposal.get("expected_fingerprint") or ""),
            reusable_signal=proposal["reusable_signal"],
            proposed_content=proposal["proposed_content"],
        )
        try:
            result = self.skill_runtime.propose(
                turn_id=evidence.turn_id,
                resolution_id=explicit.resolution_id,
                skill_id=explicit.skill_id,
                expected_fingerprint=explicit.expected_fingerprint,
                reusable_signal=explicit.reusable_signal,
                proposed_content=explicit.proposed_content,
            )
        except SkillLearningWorkshopError as error:
            raise PersonalWorldModelError("Skill learning proposal was rejected") from error
        if result.get("status") != "pending_review" or not isinstance(
            result.get("proposal_id"), str
        ):
            raise PersonalWorldModelError("Skill learning result is not proposal-only")
        journal.record(
            completion=CompletedTurn(
                completion_id=completion_id,
                project_id=fact.project_id,
                turn_id=evidence.turn_id,
            ),
            evidence=explicit,
            result=result,
            created_at=verified.event.recorded_at,
        )
        return LearningProposalReference(
            str(result["proposal_id"]),
            "pending_review",
            "proposal_only",
        )

    def _replayed_skill(
        self,
        journal: Mapping[str, object],
        proposal: Mapping[str, object],
        *,
        project_id: str,
        turn_id: str,
    ) -> LearningProposalReference:
        proposal_id = journal.get("proposal_id")
        record = (
            self.object_store.read("application_skill_proposals", proposal_id)
            if isinstance(proposal_id, str)
            else None
        )
        payload = record.get("payload") if isinstance(record, Mapping) else None
        if (
            journal.get("project_id") != project_id
            or journal.get("turn_id") != turn_id
            or journal.get("resolution_id") != proposal.get("resolution_id")
            or journal.get("skill_id") != proposal.get("skill_id")
            or journal.get("skill_fingerprint") != proposal.get("expected_fingerprint")
            or not isinstance(payload, Mapping)
            or payload.get("reusable_signal") != dict(proposal["reusable_signal"])
            or payload.get("proposed_content") != dict(proposal["proposed_content"])
            or record.get("status") != "pending_review"
        ):
            raise PersonalWorldModelError("Skill learning proposal identity conflicts")
        return LearningProposalReference(
            str(proposal_id), "pending_review", "proposal_only", replayed=True,
        )
