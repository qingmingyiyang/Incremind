"""Application service for governed recursive evolution.

The WorldEvent stream is the sole lifecycle ledger.  This service only
translates authenticated commands and independently verified evidence into
immutable evolution events; it never persists a second evolution state.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from core.personal_world_model import (
    PersonalWorldModelError,
    WorldEventDraft,
    WorldEventKind,
    validate_world_identifier,
)
from core.recursive_evolution import (
    EvolutionContractError,
    EvolutionEpisode,
    EvolutionEpisodeProjection,
    EvolutionEvaluation,
    EvolutionEvent,
    EvolutionPolicy,
    EvolutionProposal,
    EvolutionReview,
    EvolutionRolloutDecision,
    evolution_event_world_identity,
    project_evolution_events,
)

from .personal_world_model_runtime import PersonalWorldModelRuntime


class RecursiveEvolutionRuntimeError(ValueError):
    """Raised when an evolution command cannot be safely recorded."""


class EvidenceVerifierPort(Protocol):
    """Resolve an evaluation only from a verified, immutable Receipt."""

    def verify_evaluation(
        self,
        *,
        project_id: str,
        proposal: EvolutionProposal,
        receipt_ref: str,
    ) -> EvolutionEvaluation: ...

    def verify_canary(
        self,
        *, project_id: str, proposal: EvolutionProposal, evidence_ref: str,
    ) -> "CanaryObservation": ...


class ApprovalVerifierPort(Protocol):
    """Verify an authenticated local human confirmation."""

    def verify_local_human(
        self,
        *,
        project_id: str,
        user_id: str,
        confirmation_ref: str,
        command_id: str,
        action: str,
        proposal_id: str | None,
    ) -> None: ...


class TargetAuthorityPort(Protocol):
    """Idempotent CAS adapter for the active target authority.

    ``probe`` returns the immutable target Receipt only when this exact
    operation was durably applied.  ``apply`` must be idempotent for the
    supplied operation id and return that same Receipt reference.
    """

    def probe(self, *, operation_id: str) -> str | None: ...

    def preflight(
        self, *, project_id: str, action: str, proposal: EvolutionProposal,
    ) -> None: ...

    def apply(
        self, *, operation_id: str, action: str, proposal: EvolutionProposal,
        authorization_ref: str,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class CanaryObservation:
    """A canary result that was resolved from external evidence, not request data."""

    episode_id: str
    proposal_id: str
    passed: bool
    samples: int
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.episode_id, str) or not self.episode_id
            or not isinstance(self.proposal_id, str) or not self.proposal_id
            or not isinstance(self.passed, bool)
            or not isinstance(self.samples, int) or isinstance(self.samples, bool)
            or self.samples < 1
            or not self.evidence_refs
            or any(not isinstance(ref, str) or not ref.startswith("crp://") for ref in self.evidence_refs)
        ):
            raise RecursiveEvolutionRuntimeError("canary evidence is invalid")



@dataclass(frozen=True, slots=True)
class RecordedEvolutionCommand:
    event: EvolutionEvent
    replayed: bool
    projection: EvolutionEpisodeProjection


@dataclass(frozen=True, slots=True)
class EvolutionProjectionSummary:
    """Safe UI summary deliberately excluding artifact locators and content."""

    episode_id: str
    project_id: str
    target_kind: str
    current_generation: int
    candidate_count: int
    evaluation_count: int
    budget_used: int
    stop_reason: str | None
    is_terminal: bool
    proposal_statuses: Mapping[str, str]
    policy: EvolutionPolicy


class RecursiveEvolutionRuntime:
    """Mutate governed evolution through the existing Personal World runtime."""

    def __init__(
        self,
        *,
        world: PersonalWorldModelRuntime,
        evidence: EvidenceVerifierPort,
        approvals: ApprovalVerifierPort,
        targets: TargetAuthorityPort,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._world = world
        self._evidence = evidence
        self._approvals = approvals
        self._targets = targets
        self._now = now or (lambda: datetime.now(UTC))

    def create_episode(
        self, *, episode: EvolutionEpisode, command_id: str,
    ) -> RecordedEvolutionCommand:
        self._entity_command(command_id, episode.episode_id)
        event = EvolutionEvent.episode_created(episode, self._timestamp())
        return self._append(episode.project_id, event)

    def record_candidate(
        self, *, project_id: str, proposal: EvolutionProposal, command_id: str,
    ) -> RecordedEvolutionCommand:
        self._entity_command(command_id, proposal.proposal_id)
        event = EvolutionEvent.proposal_recorded(proposal, self._timestamp())
        replay = self._replay(project_id, event)
        if replay is not None:
            return replay
        projection = self._ready(project_id, proposal.episode_id)
        if projection.target_kind is not proposal.target_kind:
            raise RecursiveEvolutionRuntimeError("candidate target kind drifted")
        self._preflight_with_limit_stop(project_id, event, projection)
        self._target_preflight(project_id, "record_candidate", proposal)
        self._apply_target(
            project_id, proposal.episode_id, command_id, "record_candidate",
            proposal, proposal.candidate_ref,
        )
        return self._append(project_id, event)

    def record_evaluation_from_receipt(
        self,
        *,
        project_id: str,
        episode_id: str,
        proposal_id: str,
        receipt_ref: str,
        command_id: str,
    ) -> RecordedEvolutionCommand:
        self._command(command_id)
        replay = self._entity_replay(
            project_id, episode_id, command_id, "evaluation.recorded",
        )
        if replay is not None:
            if receipt_ref not in self._evaluation_evidence(project_id, episode_id, command_id):
                raise RecursiveEvolutionRuntimeError("evaluation Receipt identity conflicts")
            return replay
        projection = self._ready(project_id, episode_id)
        proposal = self._proposal(project_id, projection, proposal_id)
        try:
            evaluation = self._evidence.verify_evaluation(
                project_id=project_id, proposal=proposal, receipt_ref=receipt_ref,
            )
        except Exception as error:
            raise RecursiveEvolutionRuntimeError("evaluation Receipt is invalid") from error
        if not isinstance(evaluation, EvolutionEvaluation):
            raise RecursiveEvolutionRuntimeError("evaluation Receipt is invalid")
        if (
            evaluation.episode_id != episode_id
            or evaluation.proposal_id != proposal_id
        ):
            raise RecursiveEvolutionRuntimeError("Receipt evaluation scope drifted")
        self._entity_command(command_id, evaluation.evaluation_id)
        event = EvolutionEvent.evaluation_recorded(evaluation, self._timestamp())
        self._preflight_with_limit_stop(project_id, event, projection)
        return self._append(project_id, event)

    def record_review(
        self, *, project_id: str, review: EvolutionReview, command_id: str,
    ) -> RecordedEvolutionCommand:
        self._entity_command(command_id, review.review_id)
        event = EvolutionEvent.review_recorded(review, self._timestamp())
        replay = self._replay(project_id, event)
        if replay is not None:
            return replay
        self._ready(project_id, review.episode_id)
        return self._append(project_id, event)

    def approve_canary(
        self,
        *,
        project_id: str,
        episode_id: str,
        proposal_id: str,
        user_id: str,
        confirmation_ref: str,
        evidence_refs: Sequence[str],
        command_id: str,
    ) -> RecordedEvolutionCommand:
        self._command(command_id)
        projection = self._projection(project_id, episode_id)
        if projection is None:
            raise RecursiveEvolutionRuntimeError("evolution episode is unknown")
        proposal = self._proposal(project_id, projection, proposal_id)
        decision_evidence = self._approval_evidence(evidence_refs, confirmation_ref)
        replay = self._rollout_replay(
            project_id, episode_id, command_id, "canary.started", proposal_id,
            user_id, decision_evidence, allow_target_receipt=True,
        )
        if replay is not None:
            return replay
        decision = EvolutionRolloutDecision(
            decision_id=command_id,
            episode_id=episode_id,
            proposal_id=proposal.proposal_id,
            user_id=user_id,
            evidence_refs=decision_evidence,
        )
        event = EvolutionEvent.canary_started(decision, self._timestamp())
        self._ready(project_id, episode_id)
        self._human(
            project_id, user_id, confirmation_ref, command_id,
            action="start_canary", proposal_id=proposal_id,
        )
        self._preflight(project_id, event)
        self._target_preflight(project_id, "start_canary", proposal)
        target_receipt_ref = self._apply_target(
            project_id, episode_id, command_id, "start_canary", proposal, confirmation_ref,
        )
        decision = EvolutionRolloutDecision(
            command_id, episode_id, proposal.proposal_id, user_id,
            (*decision_evidence, target_receipt_ref),
        )
        event = EvolutionEvent.canary_started(decision, self._timestamp())
        self._preflight(project_id, event)
        return self._append(project_id, event)

    def observe_canary(
        self,
        *,
        project_id: str,
        episode_id: str,
        proposal_id: str,
        evidence_ref: str,
        command_id: str,
    ) -> RecordedEvolutionCommand:
        self._command(command_id)
        replay = self._entity_replay(
            project_id, episode_id, command_id, "canary.observed",
        )
        if replay is not None:
            if evidence_ref not in self._canary_evidence(project_id, episode_id, command_id):
                raise RecursiveEvolutionRuntimeError("canary evidence identity conflicts")
            self._automatic_rollback(project_id, episode_id, proposal_id, command_id)
            return replay
        projection = self._ready(project_id, episode_id)
        proposal = self._proposal(project_id, projection, proposal_id)
        try:
            observation = self._evidence.verify_canary(
                project_id=project_id, proposal=proposal, evidence_ref=evidence_ref,
            )
        except Exception as error:
            raise RecursiveEvolutionRuntimeError("canary evidence is invalid") from error
        if (
            not isinstance(observation, CanaryObservation)
            or observation.episode_id != episode_id
            or observation.proposal_id != proposal_id
            or evidence_ref not in observation.evidence_refs
        ):
            raise RecursiveEvolutionRuntimeError("canary evidence scope drifted")
        observed = self._append(project_id, EvolutionEvent.canary_observed(
            command_id, episode_id, proposal_id, observation.passed, observation.samples,
            observation.evidence_refs, self._timestamp(),
        ))
        if not observation.passed:
            self._automatic_rollback(project_id, episode_id, proposal_id, command_id)
        return observed

    def promote(
        self,
        *,
        project_id: str,
        episode_id: str,
        proposal_id: str,
        user_id: str,
        confirmation_ref: str,
        evidence_refs: Sequence[str],
        command_id: str,
    ) -> RecordedEvolutionCommand:
        return self._terminal(
            action="promote", project_id=project_id, episode_id=episode_id,
            proposal_id=proposal_id, user_id=user_id,
            confirmation_ref=confirmation_ref, evidence_refs=evidence_refs,
            command_id=command_id, requires_human=True,
        )

    def rollback(
        self,
        *,
        project_id: str,
        episode_id: str,
        proposal_id: str,
        user_id: str,
        confirmation_ref: str,
        evidence_refs: Sequence[str],
        command_id: str,
    ) -> RecordedEvolutionCommand:
        return self._terminal(
            action="rollback", project_id=project_id, episode_id=episode_id,
            proposal_id=proposal_id, user_id=user_id, confirmation_ref=confirmation_ref,
            evidence_refs=evidence_refs, command_id=command_id, requires_human=True,
        )

    def reject(
        self,
        *,
        project_id: str,
        episode_id: str,
        proposal_id: str,
        user_id: str,
        confirmation_ref: str,
        evidence_refs: Sequence[str],
        command_id: str,
    ) -> RecordedEvolutionCommand:
        self._command(command_id)
        projection = self._projection(project_id, episode_id)
        if projection is None:
            raise RecursiveEvolutionRuntimeError("evolution episode is unknown")
        proposal = self._proposal(project_id, projection, proposal_id)
        decision = EvolutionRolloutDecision(
            command_id,
            episode_id,
            proposal.proposal_id,
            user_id,
            self._approval_evidence(evidence_refs, confirmation_ref),
        )
        event = EvolutionEvent.rejected(decision, self._timestamp())
        replay = self._replay(project_id, event)
        if replay is not None:
            return replay
        self._ready(project_id, episode_id)
        self._human(
            project_id, user_id, confirmation_ref, command_id,
            action="reject", proposal_id=proposal_id,
        )
        return self._append(project_id, event)

    def stop(
        self,
        *, project_id: str, episode_id: str, reason: str, user_id: str,
        confirmation_ref: str, evidence_refs: Sequence[str], command_id: str,
    ) -> RecordedEvolutionCommand:
        self._command(command_id)
        if reason != "user":
            raise RecursiveEvolutionRuntimeError("public stop only accepts user reason")
        projection = self._projection(project_id, episode_id)
        if projection is None:
            raise RecursiveEvolutionRuntimeError("evolution episode is unknown")
        persisted_evidence = self._approval_evidence(evidence_refs, confirmation_ref)
        existing_stop = self._stop_replay(
            project_id, episode_id, command_id, reason, persisted_evidence,
        )
        if existing_stop is not None:
            return existing_stop
        event = EvolutionEvent.episode_stopped(
            episode_id, command_id, reason, persisted_evidence,
            projection.source_event_ids, self._timestamp(), self._timestamp(),
        )
        replay = self._replay(project_id, event)
        if replay is not None:
            return replay
        self._human(
            project_id, user_id, confirmation_ref, command_id,
            action="stop", proposal_id=None,
        )
        return self._append(project_id, event)

    def get_projection(
        self, *, project_id: str, episode_id: str,
    ) -> EvolutionEpisodeProjection | None:
        return self._projection(project_id, episode_id)

    def list_projections(self, *, project_id: str) -> tuple[EvolutionProjectionSummary, ...]:
        project = validate_world_identifier(project_id, "project id")
        episode_ids = {
            event.episode_id for event in self._events(project)
        }
        summaries = []
        for episode_id in sorted(episode_ids):
            projection = self._projection(project, episode_id)
            if projection is not None:
                summaries.append(self._summary(projection))
        return tuple(summaries)

    def _terminal(
        self, *, action: str, project_id: str, episode_id: str, proposal_id: str,
        user_id: str, confirmation_ref: str | None, evidence_refs: Sequence[str],
        command_id: str, requires_human: bool,
    ) -> RecordedEvolutionCommand:
        self._command(command_id)
        projection = self._projection(project_id, episode_id)
        if projection is None:
            raise RecursiveEvolutionRuntimeError("evolution episode is unknown")
        proposal = self._proposal(project_id, projection, proposal_id)
        decision_evidence = (
            self._approval_evidence(evidence_refs, confirmation_ref)
            if requires_human and confirmation_ref is not None
            else tuple(evidence_refs)
        )
        replay = self._rollout_replay(
            project_id, episode_id, command_id, {
                "promote": "rollout.promoted", "rollback": "rollout.rolled_back",
            }[action], proposal_id, user_id, decision_evidence,
            allow_target_receipt=True,
        )
        if replay is not None:
            return replay
        decision = EvolutionRolloutDecision(
            command_id,
            episode_id,
            proposal.proposal_id,
            user_id,
            decision_evidence,
        )
        event = {
            "promote": EvolutionEvent.promoted,
            "rollback": EvolutionEvent.rolled_back,
        }[action](decision, self._timestamp())
        self._ready(project_id, episode_id)
        if requires_human:
            if confirmation_ref is None:
                raise RecursiveEvolutionRuntimeError("human confirmation is required")
            self._human(
                project_id, user_id, confirmation_ref, command_id,
                action=action, proposal_id=proposal_id,
            )
        self._preflight(project_id, event)
        target_action = (
            "rollback_promotion" if action == "rollback"
            and projection.proposal_statuses.get(proposal_id) == "promoted"
            else "rollback_canary" if action == "rollback" else "promote"
        )
        self._target_preflight(project_id, target_action, proposal)
        authorization = confirmation_ref or decision_evidence[0]
        target_receipt_ref = self._apply_target(
            project_id, episode_id, command_id, target_action, proposal, authorization,
        )
        decision = EvolutionRolloutDecision(
            command_id, episode_id, proposal.proposal_id, user_id,
            (*decision_evidence, target_receipt_ref),
        )
        event = {
            "promote": EvolutionEvent.promoted,
            "rollback": EvolutionEvent.rolled_back,
        }[action](decision, self._timestamp())
        self._preflight(project_id, event)
        return self._append(project_id, event)

    def _automatic_rollback(self, project_id: str, episode_id: str, proposal_id: str, command_id: str) -> None:
        projection = self._projection(project_id, episode_id)
        if projection is None or projection.proposal_statuses.get(proposal_id) != "canary_failed":
            return
        self._terminal(
            action="rollback", project_id=project_id, episode_id=episode_id,
            proposal_id=proposal_id, user_id="system", confirmation_ref=None,
            evidence_refs=(f"crp://recursive-evolution/{episode_id}/{proposal_id}/canary-failed",),
            command_id=f"rollback-{proposal_id}", requires_human=False,
        )

    def recover_pending_rollbacks(self, *, project_id: str) -> None:
        for summary in self.list_projections(project_id=project_id):
            for proposal_id, status in summary.proposal_statuses.items():
                if status == "canary_failed":
                    self._automatic_rollback(
                        project_id, summary.episode_id, proposal_id, "recovery",
                    )

    def _ready(self, project_id: str, episode_id: str) -> EvolutionEpisodeProjection:
        projection = self._projection(project_id, episode_id)
        if projection is None:
            raise RecursiveEvolutionRuntimeError("evolution episode is unknown")
        if projection.is_terminal:
            raise RecursiveEvolutionRuntimeError("evolution episode is stopped")
        if projection.stop_required_reason is not None:
            self._persist_required_stop(project_id, projection)
            raise RecursiveEvolutionRuntimeError("evolution episode reached its stop policy")
        return projection

    def _persist_required_stop(self, project_id: str, projection: EvolutionEpisodeProjection) -> None:
        reason = projection.stop_required_reason
        if reason is None or projection.is_terminal:
            return
        self._persist_stop_reason(project_id, projection, reason)

    def _persist_stop_reason(
        self,
        project_id: str,
        projection: EvolutionEpisodeProjection,
        reason: str,
    ) -> None:
        event = EvolutionEvent.episode_stopped(
            projection.episode_id, f"auto-{reason}", reason,
            (f"crp://recursive-evolution/{projection.episode_id}/stop/{reason}",),
            projection.source_event_ids, self._timestamp(), self._timestamp(),
        )
        self._append(project_id, event)

    def _preflight_with_limit_stop(
        self,
        project_id: str,
        event: EvolutionEvent,
        projection: EvolutionEpisodeProjection,
    ) -> None:
        try:
            self._preflight(project_id, event)
        except RecursiveEvolutionRuntimeError as error:
            cause = error.__cause__
            reason = {
                "max_generations requires stop": "max_generations",
                "max_candidates_per_generation requires stop": "max_candidates_per_generation",
                "max_evaluations_per_candidate requires stop": "max_evaluations_per_candidate",
                "evolution budget requires stop": "budget",
            }.get(str(cause) if isinstance(cause, EvolutionContractError) else "")
            if reason is not None:
                self._persist_stop_reason(project_id, projection, reason)
                raise RecursiveEvolutionRuntimeError(
                    "evolution episode reached its stop policy"
                ) from error
            raise

    def _append(self, project_id: str, event: EvolutionEvent) -> RecordedEvolutionCommand:
        try:
            project = validate_world_identifier(project_id, "project id")
            replay = self._replay(project, event)
            if replay is not None:
                return replay
            event_id, source_ref, source_revision = evolution_event_world_identity(event)
            result = self._world.append_event(WorldEventDraft(
                event_id=event_id, project_id=project,
                kind=WorldEventKind.EVOLUTION_EVENT_RECORDED, actor="system",
                source_ref=source_ref, source_revision=source_revision,
                occurred_at=event.recorded_at, recorded_at=event.recorded_at,
                payload=event.to_payload(),
            ))
        except (EvolutionContractError, PersonalWorldModelError) as error:
            raise RecursiveEvolutionRuntimeError("evolution World append is invalid") from error
        projection = self._projection(project, event.episode_id)
        if projection is None:
            raise RecursiveEvolutionRuntimeError("evolution event did not project")
        return RecordedEvolutionCommand(event, result.replayed, projection)

    def _target_preflight(self, project_id: str, action: str, proposal: EvolutionProposal) -> None:
        try:
            self._targets.preflight(project_id=project_id, action=action, proposal=proposal)
        except Exception as error:
            raise RecursiveEvolutionRuntimeError("target baseline or resource authority drifted") from error

    def _apply_target(self, project_id: str, episode_id: str, command_id: str, action: str, proposal: EvolutionProposal, authorization_ref: str) -> str:
        operation_id = f"evolution.{project_id}.{episode_id}.{command_id}.{action}"
        try:
            receipt_ref = self._targets.probe(operation_id=operation_id)
            if receipt_ref is not None and not self._target_receipt_ref(receipt_ref):
                raise TypeError("target probe is invalid")
            if receipt_ref is None:
                receipt_ref = self._targets.apply(
                    operation_id=operation_id, action=action, proposal=proposal,
                    authorization_ref=authorization_ref,
                )
            if not self._target_receipt_ref(receipt_ref):
                raise TypeError("target Receipt is invalid")
            return receipt_ref
        except Exception as error:
            raise RecursiveEvolutionRuntimeError("target authority rejected evolution operation") from error

    def _replay(self, project_id: str, event: EvolutionEvent) -> RecordedEvolutionCommand | None:
        event_id, _source_ref, _source_revision = evolution_event_world_identity(event)
        existing = self._world.event(event_id)
        if existing is None:
            return None
        if existing.project_id != project_id or existing.kind is not WorldEventKind.EVOLUTION_EVENT_RECORDED:
            raise RecursiveEvolutionRuntimeError("evolution command identity conflicts")
        try:
            prior = EvolutionEvent.from_payload(existing.payload)
        except EvolutionContractError as error:
            raise RecursiveEvolutionRuntimeError("evolution command identity conflicts") from error
        if (
            prior.event_id != event.event_id
            or prior.kind is not event.kind
            or prior.episode_id != event.episode_id
            or prior.payload != event.payload
        ):
            raise RecursiveEvolutionRuntimeError("evolution command identity conflicts")
        projection = self._projection(project_id, event.episode_id)
        if projection is None:
            raise RecursiveEvolutionRuntimeError("evolution replay did not project")
        return RecordedEvolutionCommand(prior, True, projection)

    def _rollout_replay(
        self,
        project_id: str,
        episode_id: str,
        decision_id: str,
        kind: str,
        proposal_id: str,
        user_id: str,
        caller_evidence: tuple[str, ...],
        *,
        allow_target_receipt: bool,
    ) -> RecordedEvolutionCommand | None:
        suffix = {
            "canary.started": ".canary",
            "rollout.promoted": ".promoted",
            "rollout.rolled_back": ".rolled-back",
            "rollout.rejected": ".rejected",
        }.get(kind)
        if suffix is None:
            raise RecursiveEvolutionRuntimeError("evolution rollout kind is invalid")
        replay = self._entity_replay(
            project_id, episode_id, f"{decision_id}{suffix}", kind,
        )
        if replay is None:
            return None
        try:
            decision = EvolutionRolloutDecision.from_payload(replay.event.payload)
        except EvolutionContractError as error:
            raise RecursiveEvolutionRuntimeError(
                "evolution command identity conflicts"
            ) from error
        persisted = decision.evidence_refs
        if allow_target_receipt:
            receipts = tuple(ref for ref in persisted if self._target_receipt_ref(ref))
            persisted = tuple(ref for ref in persisted if not self._target_receipt_ref(ref))
            if len(receipts) != 1:
                raise RecursiveEvolutionRuntimeError(
                    "evolution target Receipt identity conflicts"
                )
        if (
            decision.proposal_id != proposal_id
            or decision.user_id != user_id
            or persisted != caller_evidence
        ):
            raise RecursiveEvolutionRuntimeError(
                "evolution command identity conflicts"
            )
        return replay

    def _entity_replay(
        self, project_id: str, episode_id: str, entity_id: str, kind: str,
    ) -> RecordedEvolutionCommand | None:
        existing = self._world.event(f"evolution.{episode_id}.{entity_id}")
        if existing is None:
            return None
        try:
            event = EvolutionEvent.from_payload(existing.payload)
        except EvolutionContractError as error:
            raise RecursiveEvolutionRuntimeError("evolution command identity conflicts") from error
        if (
            existing.project_id != project_id
            or existing.kind is not WorldEventKind.EVOLUTION_EVENT_RECORDED
            or event.event_id != entity_id
            or event.kind.value != kind
            or event.episode_id != episode_id
        ):
            raise RecursiveEvolutionRuntimeError("evolution command identity conflicts")
        projection = self._projection(project_id, episode_id)
        if projection is None:
            raise RecursiveEvolutionRuntimeError("evolution replay did not project")
        return RecordedEvolutionCommand(event, True, projection)

    def _stop_replay(
        self, project_id: str, episode_id: str, stop_id: str, reason: str,
        evidence_refs: tuple[str, ...],
    ) -> RecordedEvolutionCommand | None:
        existing = self._world.event(f"evolution.{episode_id}.{episode_id}.{stop_id}.stopped")
        if existing is None:
            return None
        try:
            event = EvolutionEvent.from_payload(existing.payload)
        except EvolutionContractError as error:
            raise RecursiveEvolutionRuntimeError("evolution command identity conflicts") from error
        payload = event.payload
        if (
            existing.project_id != project_id
            or event.kind.value != "episode.stopped"
            or event.episode_id != episode_id
            or payload.get("reason") != reason
            or tuple(payload.get("evidence_refs", ())) != evidence_refs
        ):
            raise RecursiveEvolutionRuntimeError("evolution command identity conflicts")
        projection = self._projection(project_id, episode_id)
        if projection is None:
            raise RecursiveEvolutionRuntimeError("evolution replay did not project")
        return RecordedEvolutionCommand(event, True, projection)

    def _preflight(self, project_id: str, event: EvolutionEvent) -> None:
        try:
            projected = project_evolution_events(
                (*(
                    item for item in self._events(project_id)
                    if item.episode_id == event.episode_id
                ), event), project_id=project_id,
                observed_at=event.recorded_at,
            )
        except EvolutionContractError as error:
            raise RecursiveEvolutionRuntimeError("evolution command is not lifecycle-valid") from error
        if projected is None:
            raise RecursiveEvolutionRuntimeError("evolution preflight did not project")

    def _projection(self, project_id: str, episode_id: str) -> EvolutionEpisodeProjection | None:
        project = validate_world_identifier(project_id, "project id")
        target = validate_world_identifier(episode_id, "episode id")
        events = tuple(event for event in self._events(project) if event.episode_id == target)
        if not events:
            return None
        try:
            return project_evolution_events(events, project_id=project, observed_at=self._timestamp())
        except EvolutionContractError as error:
            raise RecursiveEvolutionRuntimeError("evolution World stream is invalid") from error

    def _events(self, project_id: str) -> tuple[EvolutionEvent, ...]:
        result = []
        try:
            for event in self._world.events(project_id):
                if event.kind is WorldEventKind.EVOLUTION_EVENT_RECORDED:
                    result.append(EvolutionEvent.from_payload(event.payload))
        except (EvolutionContractError, PersonalWorldModelError) as error:
            raise RecursiveEvolutionRuntimeError("evolution World stream is invalid") from error
        return tuple(result)

    def _evaluation_evidence(self, project_id: str, episode_id: str, evaluation_id: str) -> tuple[str, ...]:
        for event in self._events(project_id):
            if event.episode_id == episode_id and event.event_id == evaluation_id:
                return EvolutionEvaluation.from_payload(event.payload).evidence_refs
        raise RecursiveEvolutionRuntimeError("evaluation replay is unavailable")

    def _canary_evidence(self, project_id: str, episode_id: str, observation_id: str) -> tuple[str, ...]:
        for event in self._events(project_id):
            if event.episode_id == episode_id and event.event_id == observation_id:
                refs = event.payload.get("evidence_refs")
                return tuple(refs) if isinstance(refs, tuple) else ()
        raise RecursiveEvolutionRuntimeError("canary replay is unavailable")

    def _proposal(self, project_id: str, projection: EvolutionEpisodeProjection, proposal_id: str) -> EvolutionProposal:
        identifier = validate_world_identifier(proposal_id, "proposal id")
        for event in self._events(project_id):
            if event.episode_id != projection.episode_id:
                continue
            if event.kind.value == "proposal.recorded":
                proposal = EvolutionProposal.from_payload(event.payload)
                if proposal.proposal_id == identifier:
                    return proposal
        raise RecursiveEvolutionRuntimeError("evolution proposal is unknown")

    def _human(
        self,
        project_id: str,
        user_id: str,
        confirmation_ref: str,
        command_id: str,
        *,
        action: str,
        proposal_id: str | None,
    ) -> None:
        # The verifier owns authentication.  This runtime never accepts an agent
        # identity as a replacement for a local authenticated human proof.
        if not isinstance(user_id, str) or not user_id or user_id in {"system", "agent"}:
            raise RecursiveEvolutionRuntimeError("local human identity is required")
        try:
            self._approvals.verify_local_human(
                project_id=project_id, user_id=user_id,
                confirmation_ref=confirmation_ref, command_id=command_id,
                action=action, proposal_id=proposal_id,
            )
        except Exception as error:
            raise RecursiveEvolutionRuntimeError("local human confirmation is invalid") from error

    @staticmethod
    def _approval_evidence(
        evidence_refs: Sequence[str], confirmation_ref: str,
    ) -> tuple[str, ...]:
        refs = tuple(evidence_refs)
        if any(RecursiveEvolutionRuntime._target_receipt_ref(ref) for ref in refs):
            raise RecursiveEvolutionRuntimeError(
                "target Receipt references are system-owned"
            )
        return refs if confirmation_ref in refs else (*refs, confirmation_ref)

    @staticmethod
    def _command(command_id: str) -> str:
        try:
            return validate_world_identifier(command_id, "command id")
        except PersonalWorldModelError as error:
            raise RecursiveEvolutionRuntimeError("command id is invalid") from error

    @staticmethod
    def _target_receipt_ref(value: object) -> bool:
        return (
            isinstance(value, str)
            and value.startswith("crp://recursive-evolution/target-operations/")
        )

    def _entity_command(self, command_id: str, entity_id: str) -> str:
        command = self._command(command_id)
        if command != entity_id:
            raise RecursiveEvolutionRuntimeError("command id must equal immutable entity id")
        return command

    def _timestamp(self) -> str:
        now = self._now()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise RecursiveEvolutionRuntimeError("trusted clock is invalid")
        return now.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def _summary(projection: EvolutionEpisodeProjection) -> EvolutionProjectionSummary:
        return EvolutionProjectionSummary(
            episode_id=projection.episode_id, project_id=projection.project_id,
            target_kind=projection.target_kind.value,
            current_generation=projection.current_generation,
            candidate_count=projection.candidate_count,
            evaluation_count=projection.evaluation_count,
            budget_used=projection.budget_used,
            stop_reason=projection.stop_reason, is_terminal=projection.is_terminal,
            proposal_statuses=dict(projection.proposal_statuses),
            policy=projection.policy,
        )
