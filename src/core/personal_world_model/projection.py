"""Pure project-state projection and deterministic dynamics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from core.long_horizon_runtime import (
    ProvenanceContractError,
    TrajectoryContractError,
    TrustCheckpoint,
    project_provenance_events,
    trajectory_checkpoint_world_identity,
    validate_segment_transition,
)
from core.long_horizon_runtime.task_graph import (
    TaskGraphContractError,
    TaskGraphEvent,
    project_task_graph,
    task_graph_event_identity,
    task_graph_world_identity,
)
from core.recursive_evolution import (
    EvolutionContractError,
    EvolutionEvent,
    evolution_event_world_identity,
    project_evolution_events,
)

from .models import (
    FeedbackFact,
    OutcomeStatus,
    PersonalWorldModelError,
    UserEvaluationVerdict,
    WorldEvent,
    WorldEventKind,
    parse_world_timestamp,
)


@dataclass(frozen=True, slots=True)
class ProjectGoal:
    goal_id: str
    title: str
    success_criteria: tuple[str, ...]
    target_at: str | None
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProjectTask:
    task_id: str
    title: str
    state: str
    evidence_refs: tuple[str, ...]
    observed_sequence: int


@dataclass(frozen=True, slots=True)
class ProjectBlocker:
    blocker_id: str
    summary: str
    severity: str
    evidence_refs: tuple[str, ...]
    opened_sequence: int


@dataclass(frozen=True, slots=True)
class ProjectObservation:
    observation_id: str
    category: str
    summary: str
    evidence_refs: tuple[str, ...]
    observed_sequence: int


@dataclass(frozen=True, slots=True)
class PlannedAction:
    action_id: str
    title: str
    expected_outcome: str
    effect_class: str
    gate_requirement: str
    due_at: str | None
    evidence_refs: tuple[str, ...]
    planned_sequence: int


@dataclass(frozen=True, slots=True)
class StatePrediction:
    predicted_state: str
    confidence: float
    horizon: str
    assumptions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Counterfactual:
    condition: str
    predicted_state: str
    confidence: float
    rationale: str


@dataclass(frozen=True, slots=True)
class SupervisionClaim:
    claim_id: str
    supersedes_claim_id: str | None
    action_id: str
    hypothesis: str
    expected_signals: tuple[str, ...]
    falsification_signals: tuple[str, ...]
    checkpoint_policy: str
    pivot_conditions: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    basis_sequence: int
    declared_sequence: int
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SupervisionVerification:
    verification_id: str
    claim_id: str
    action_id: str
    finding: str
    verdict: str
    checked_world_sequence: int
    recorded_sequence: int
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SupervisionDecision:
    decision_id: str
    claim_id: str
    verification_id: str
    action_id: str
    disposition: str
    rationale: str
    recorded_sequence: int
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SupervisionClaimStatus:
    claim: SupervisionClaim
    latest_verification: SupervisionVerification | None
    latest_decision: SupervisionDecision | None
    state: str


@dataclass(frozen=True, slots=True)
class ProjectSupervision:
    active_claims: tuple[SupervisionClaim, ...]
    claim_statuses: tuple[SupervisionClaimStatus, ...]
    active_claim: SupervisionClaim | None
    latest_active_claim: SupervisionClaim | None
    latest_verification: SupervisionVerification | None
    latest_decision: SupervisionDecision | None
    supervision_state: str


@dataclass(frozen=True, slots=True)
class ProjectWorldStateProjection:
    schema_version: str
    project_id: str
    through_sequence: int
    derived_at: str
    phase: str
    goal: ProjectGoal | None
    tasks: tuple[ProjectTask, ...]
    blockers: tuple[ProjectBlocker, ...]
    observations: tuple[ProjectObservation, ...]
    planned_actions: tuple[PlannedAction, ...]
    pending_action_ids: tuple[str, ...]
    feedback_facts: tuple[FeedbackFact, ...]
    latest_feedback: FeedbackFact | None
    supervision: ProjectSupervision
    confidence: float
    risk_codes: tuple[str, ...]
    predictions: tuple[StatePrediction, ...]
    counterfactuals: tuple[Counterfactual, ...]
    source_event_ids: tuple[str, ...]
    persisted_as_authority: bool = False

    def to_payload(self) -> dict[str, object]:
        """Serialize the complete read-only projection for local application APIs."""

        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "through_sequence": self.through_sequence,
            "derived_at": self.derived_at,
            "phase": self.phase,
            "goal": None if self.goal is None else {
                "goal_id": self.goal.goal_id,
                "title": self.goal.title,
                "success_criteria": list(self.goal.success_criteria),
                "target_at": self.goal.target_at,
                "evidence_refs": list(self.goal.evidence_refs),
            },
            "tasks": [
                {
                    "task_id": item.task_id,
                    "title": item.title,
                    "state": item.state,
                    "evidence_refs": list(item.evidence_refs),
                    "observed_sequence": item.observed_sequence,
                }
                for item in self.tasks
            ],
            "blockers": [
                {
                    "blocker_id": item.blocker_id,
                    "summary": item.summary,
                    "severity": item.severity,
                    "evidence_refs": list(item.evidence_refs),
                    "opened_sequence": item.opened_sequence,
                }
                for item in self.blockers
            ],
            "observations": [
                {
                    "observation_id": item.observation_id,
                    "category": item.category,
                    "summary": item.summary,
                    "evidence_refs": list(item.evidence_refs),
                    "observed_sequence": item.observed_sequence,
                }
                for item in self.observations
            ],
            "planned_actions": [
                {
                    "action_id": item.action_id,
                    "title": item.title,
                    "expected_outcome": item.expected_outcome,
                    "effect_class": item.effect_class,
                    "gate_requirement": item.gate_requirement,
                    "due_at": item.due_at,
                    "evidence_refs": list(item.evidence_refs),
                    "planned_sequence": item.planned_sequence,
                }
                for item in self.planned_actions
            ],
            "pending_action_ids": list(self.pending_action_ids),
            "feedback_facts": [item.to_payload() for item in self.feedback_facts],
            "latest_feedback_id": (
                None if self.latest_feedback is None else self.latest_feedback.feedback_id
            ),
            "supervision": _supervision_payload(self.supervision),
            "confidence": self.confidence,
            "risk_codes": list(self.risk_codes),
            "predictions": [
                {
                    "predicted_state": item.predicted_state,
                    "confidence": item.confidence,
                    "horizon": item.horizon,
                    "assumptions": list(item.assumptions),
                }
                for item in self.predictions
            ],
            "counterfactuals": [
                {
                    "condition": item.condition,
                    "predicted_state": item.predicted_state,
                    "confidence": item.confidence,
                    "rationale": item.rationale,
                }
                for item in self.counterfactuals
            ],
            "source_event_ids": list(self.source_event_ids),
            "persisted_as_authority": self.persisted_as_authority,
        }

    def planning_payload(self) -> dict[str, object]:
        """Return the bounded model-facing subset for a later Context Manifest."""

        latest = self.latest_feedback
        return {
            "schema_version": "1.0.0",
            "project_id": self.project_id,
            "through_sequence": self.through_sequence,
            "phase": self.phase,
            "goal": None if self.goal is None else {
                "goal_id": self.goal.goal_id,
                "title": self.goal.title,
                "success_criteria": list(self.goal.success_criteria),
            },
            "tasks": [
                {"task_id": item.task_id, "title": item.title, "state": item.state}
                for item in self.tasks[:24]
            ],
            "blockers": [
                {"blocker_id": item.blocker_id, "summary": item.summary, "severity": item.severity}
                for item in self.blockers[:12]
            ],
            "observations": [
                {
                    "observation_id": item.observation_id,
                    "category": item.category,
                    "summary": item.summary,
                }
                for item in self.observations[:12]
            ],
            "planned_actions": [
                {
                    "action_id": item.action_id,
                    "title": item.title,
                    "expected_outcome": item.expected_outcome,
                    "effect_class": item.effect_class,
                    "gate_requirement": item.gate_requirement,
                    "due_at": item.due_at,
                }
                for item in self.planned_actions[-12:]
            ],
            "pending_action_ids": list(self.pending_action_ids),
            "latest_feedback": None if latest is None else {
                "feedback_id": latest.feedback_id,
                "supersedes_feedback_id": latest.supersedes_feedback_id,
                "action_id": latest.action_id,
                "expected_outcome": latest.expected_outcome,
                "actual_outcome": latest.actual_outcome,
                "outcome": latest.outcome.value,
                "state_delta": [item.to_payload() for item in latest.state_delta],
                "cost": latest.cost.to_payload(),
                "user_evaluation": latest.user_evaluation.to_payload(),
                "evidence_refs": list(latest.evidence_refs),
            },
            "supervision": {
                "active_claim": _planning_claim(self.supervision.active_claim),
                "active_claims": [_planning_claim(item) for item in self.supervision.active_claims],
                "claim_statuses": [_claim_status_payload(item) for item in self.supervision.claim_statuses],
                "verdict": (
                    None if self.supervision.latest_verification is None
                    else self.supervision.latest_verification.verdict
                ),
                "disposition": (
                    None if self.supervision.latest_decision is None
                    else self.supervision.latest_decision.disposition
                ),
                "checked_sequence": (
                    None if self.supervision.latest_verification is None
                    else self.supervision.latest_verification.checked_world_sequence
                ),
                "supervision_state": self.supervision.supervision_state,
            },
            "confidence": self.confidence,
            "risk_codes": list(self.risk_codes),
            "predictions": [
                {
                    "predicted_state": item.predicted_state,
                    "confidence": item.confidence,
                    "horizon": item.horizon,
                    "assumptions": list(item.assumptions),
                }
                for item in self.predictions
            ],
            "counterfactuals": [
                {
                    "condition": item.condition,
                    "predicted_state": item.predicted_state,
                    "confidence": item.confidence,
                    "rationale": item.rationale,
                }
                for item in self.counterfactuals
            ],
            "projection_authority": "derived_only",
        }


class ProjectDynamicsEngine:
    """Rebuild one project's state and next-state hypotheses from facts only."""

    def project(
        self,
        events: tuple[WorldEvent, ...] | list[WorldEvent],
        *,
        project_id: str,
        now: str,
    ) -> ProjectWorldStateProjection:
        ordered = tuple(events)
        _validate_stream(ordered, project_id)
        try:
            project_provenance_events(ordered, project_id=project_id)
        except ProvenanceContractError as error:
            raise PersonalWorldModelError("project provenance stream is invalid") from error
        graph_events: dict[str, list[TaskGraphEvent]] = {}
        try:
            for event in ordered:
                if event.kind is WorldEventKind.TASK_GRAPH_EVENT_RECORDED:
                    graph_event = TaskGraphEvent.from_payload(event.payload)
                    if graph_event.project_id != project_id:
                        raise TaskGraphContractError("task graph event crossed project scope")
                    expected_graph_event_id = task_graph_event_identity(
                        graph_event.project_id,
                        graph_event.graph_id,
                        graph_event.kind,
                        graph_event.operation_id,
                    )
                    expected_event_id, expected_ref, expected_revision = (
                        task_graph_world_identity(graph_event)
                    )
                    if (
                        event.actor != "system"
                        or graph_event.event_id != expected_graph_event_id
                        or event.event_id != expected_event_id
                        or event.source_ref != expected_ref
                        or event.source_revision != expected_revision
                    ):
                        raise TaskGraphContractError(
                            "task graph World event authority drifted"
                        )
                    graph_events.setdefault(graph_event.graph_id, []).append(graph_event)
            for events_for_graph in graph_events.values():
                project_task_graph(events_for_graph)
        except TaskGraphContractError as error:
            raise PersonalWorldModelError("task graph stream is invalid") from error
        _validate_trajectory_checkpoints(ordered, project_id)
        _validate_evolution_events(ordered, project_id)
        current_time = parse_world_timestamp(now)
        goal: ProjectGoal | None = None
        tasks: dict[str, ProjectTask] = {}
        blockers: dict[str, ProjectBlocker] = {}
        observations: dict[str, ProjectObservation] = {}
        actions: dict[str, PlannedAction] = {}
        feedback: dict[str, FeedbackFact] = {}
        claims: dict[str, SupervisionClaim] = {}
        verifications: dict[str, SupervisionVerification] = {}
        decisions: dict[str, SupervisionDecision] = {}

        for event in ordered:
            payload = event.payload
            if event.kind is WorldEventKind.GOAL_DECLARED:
                goal = ProjectGoal(
                    goal_id=str(payload["goal_id"]),
                    title=str(payload["title"]),
                    success_criteria=tuple(payload["success_criteria"]),
                    target_at=payload["target_at"],
                    evidence_refs=tuple(payload["evidence_refs"]),
                )
            elif event.kind is WorldEventKind.PROJECT_OBSERVATION:
                observation = ProjectObservation(
                    observation_id=str(payload["observation_id"]),
                    category=str(payload["category"]),
                    summary=str(payload["summary"]),
                    evidence_refs=tuple(payload["evidence_refs"]),
                    observed_sequence=event.sequence,
                )
                observations[observation.observation_id] = observation
            elif event.kind is WorldEventKind.TASK_OBSERVED:
                task = ProjectTask(
                    task_id=str(payload["task_id"]),
                    title=str(payload["title"]),
                    state=str(payload["state"]),
                    evidence_refs=tuple(payload["evidence_refs"]),
                    observed_sequence=event.sequence,
                )
                tasks[task.task_id] = task
            elif event.kind is WorldEventKind.BLOCKER_OPENED:
                blocker = ProjectBlocker(
                    blocker_id=str(payload["blocker_id"]),
                    summary=str(payload["summary"]),
                    severity=str(payload["severity"]),
                    evidence_refs=tuple(payload["evidence_refs"]),
                    opened_sequence=event.sequence,
                )
                blockers[blocker.blocker_id] = blocker
            elif event.kind is WorldEventKind.BLOCKER_RESOLVED:
                blocker_id = str(payload["blocker_id"])
                if blocker_id not in blockers:
                    raise PersonalWorldModelError("resolved blocker was never observed open")
                blockers.pop(blocker_id)
            elif event.kind is WorldEventKind.ACTION_PLANNED:
                action = PlannedAction(
                    action_id=str(payload["action_id"]),
                    title=str(payload["title"]),
                    expected_outcome=str(payload["expected_outcome"]),
                    effect_class=str(payload["effect_class"]),
                    gate_requirement=str(payload["gate_requirement"]),
                    due_at=payload["due_at"],
                    evidence_refs=tuple(payload["evidence_refs"]),
                    planned_sequence=event.sequence,
                )
                if action.action_id in actions:
                    raise PersonalWorldModelError("planned action identity was reused")
                actions[action.action_id] = action
            elif event.kind is WorldEventKind.FEEDBACK_RECORDED:
                fact = FeedbackFact.from_payload(project_id, payload)
                action = actions.get(fact.action_id)
                if action is None:
                    raise PersonalWorldModelError("feedback references an unknown planned action")
                if action.expected_outcome != fact.expected_outcome:
                    raise PersonalWorldModelError("feedback expected outcome drifted from the plan")
                if fact.feedback_id in feedback:
                    raise PersonalWorldModelError("feedback identity was reused")
                prior_for_action = tuple(
                    item for item in feedback.values() if item.action_id == fact.action_id
                )
                if not prior_for_action and fact.supersedes_feedback_id is not None:
                    raise PersonalWorldModelError("first action feedback cannot supersede another fact")
                if prior_for_action and fact.supersedes_feedback_id != prior_for_action[-1].feedback_id:
                    raise PersonalWorldModelError("feedback correction does not supersede the latest fact")
                feedback[fact.feedback_id] = fact
            elif event.kind is WorldEventKind.SUPERVISION_CLAIM_DECLARED:
                action_id = str(payload["action_id"])
                action = actions.get(action_id)
                if action is None:
                    raise PersonalWorldModelError("supervision claim references an unknown planned action")
                claim = SupervisionClaim(
                    claim_id=str(payload["claim_id"]),
                    supersedes_claim_id=payload["supersedes_claim_id"],
                    action_id=action_id,
                    hypothesis=str(payload["hypothesis"]),
                    expected_signals=tuple(payload["expected_signals"]),
                    falsification_signals=tuple(payload["falsification_signals"]),
                    checkpoint_policy=str(payload["checkpoint_policy"]),
                    pivot_conditions=tuple(payload["pivot_conditions"]),
                    stop_conditions=tuple(payload["stop_conditions"]),
                    basis_sequence=int(payload["basis_sequence"]),
                    declared_sequence=event.sequence,
                    evidence_refs=tuple(payload["evidence_refs"]),
                )
                if claim.claim_id in claims:
                    raise PersonalWorldModelError("supervision claim identity was reused")
                if not action.planned_sequence <= claim.basis_sequence < event.sequence:
                    raise PersonalWorldModelError("supervision claim forward causality is invalid")
                if claim.supersedes_claim_id is not None:
                    prior = claims.get(claim.supersedes_claim_id)
                    if prior is None:
                        raise PersonalWorldModelError("supervision claim supersedes an unknown claim")
                    if any(item.supersedes_claim_id == prior.claim_id for item in claims.values()):
                        raise PersonalWorldModelError("supervision claim was already superseded")
                    if prior.action_id != claim.action_id:
                        prior_decision = next(
                            (
                                item for item in reversed(tuple(decisions.values()))
                                if item.claim_id == prior.claim_id
                            ),
                            None,
                        )
                        if (
                            prior_decision is None
                            or prior_decision.disposition != "replan_required"
                            or prior_decision.recorded_sequence >= action.planned_sequence
                        ):
                            raise PersonalWorldModelError(
                                "cross-action supervision supersede requires an earlier replan decision"
                            )
                claims[claim.claim_id] = claim
            elif event.kind is WorldEventKind.SUPERVISION_VERIFICATION_RECORDED:
                claim_id, action_id = str(payload["claim_id"]), str(payload["action_id"])
                claim = claims.get(claim_id)
                if claim is None:
                    raise PersonalWorldModelError("supervision verification references an unknown claim")
                if any(item.supersedes_claim_id == claim_id for item in claims.values()):
                    raise PersonalWorldModelError("supervision verification cannot use a superseded claim")
                if claim.action_id != action_id:
                    raise PersonalWorldModelError("supervision verification must use the claim same action")
                verification = SupervisionVerification(
                    verification_id=str(payload["verification_id"]),
                    claim_id=claim_id,
                    action_id=action_id,
                    finding=str(payload["finding"]),
                    verdict=str(payload["verdict"]),
                    checked_world_sequence=int(payload["checked_world_sequence"]),
                    recorded_sequence=event.sequence,
                    evidence_refs=tuple(payload["evidence_refs"]),
                )
                if verification.verification_id in verifications:
                    raise PersonalWorldModelError("supervision verification identity was reused")
                if not claim.declared_sequence <= verification.checked_world_sequence < event.sequence:
                    raise PersonalWorldModelError("supervision verification forward causality is invalid")
                verifications[verification.verification_id] = verification
            elif event.kind is WorldEventKind.SUPERVISION_DECISION_RECORDED:
                claim_id = str(payload["claim_id"])
                verification_id = str(payload["verification_id"])
                action_id = str(payload["action_id"])
                claim = claims.get(claim_id)
                verification = verifications.get(verification_id)
                if claim is None or verification is None:
                    raise PersonalWorldModelError("supervision decision references an unknown claim or verification")
                if any(item.supersedes_claim_id == claim_id for item in claims.values()):
                    raise PersonalWorldModelError("supervision decision cannot use a superseded claim")
                if claim.action_id != action_id or verification.claim_id != claim_id or verification.action_id != action_id:
                    raise PersonalWorldModelError("supervision decision must use the same action chain")
                decision = SupervisionDecision(
                    decision_id=str(payload["decision_id"]),
                    claim_id=claim_id,
                    verification_id=verification_id,
                    action_id=action_id,
                    disposition=str(payload["disposition"]),
                    rationale=str(payload["rationale"]),
                    recorded_sequence=event.sequence,
                    evidence_refs=tuple(payload["evidence_refs"]),
                )
                if decision.decision_id in decisions:
                    raise PersonalWorldModelError("supervision decision identity was reused")
                if verification.recorded_sequence >= event.sequence:
                    raise PersonalWorldModelError("supervision decision forward causality is invalid")
                if any(item.verification_id == verification_id for item in decisions.values()):
                    raise PersonalWorldModelError("supervision verification already has a decision")
                latest_for_claim = next(
                    (item for item in reversed(tuple(verifications.values())) if item.claim_id == claim_id),
                    None,
                )
                if latest_for_claim is None or latest_for_claim.verification_id != verification_id:
                    raise PersonalWorldModelError("supervision decision must use the latest claim verification")
                if verification.verdict in {"refuted", "inconclusive"} and decision.disposition == "continue":
                    raise PersonalWorldModelError("supervision verdict cannot continue")
                decisions[decision.decision_id] = decision

        feedback_actions = {item.action_id for item in feedback.values()}
        pivoted_action_ids = {
            claims[item.supersedes_claim_id].action_id
            for item in claims.values()
            if (
                item.supersedes_claim_id is not None
                and claims[item.supersedes_claim_id].action_id != item.action_id
            )
        }
        pending_action_ids = tuple(
            item.action_id
            for item in sorted(actions.values(), key=lambda value: value.planned_sequence)
            if item.action_id not in feedback_actions
            and item.action_id not in pivoted_action_ids
        )
        ordered_tasks = tuple(sorted(tasks.values(), key=lambda item: item.task_id))
        ordered_blockers = tuple(sorted(blockers.values(), key=lambda item: item.blocker_id))
        ordered_observations = tuple(
            sorted(observations.values(), key=lambda item: item.observed_sequence)
        )
        ordered_actions = tuple(sorted(actions.values(), key=lambda item: item.planned_sequence))
        ordered_feedback = tuple(feedback.values())
        latest_feedback = ordered_feedback[-1] if ordered_feedback else None
        ordered_claims = tuple(claims.values())
        ordered_verifications = tuple(verifications.values())
        ordered_decisions = tuple(decisions.values())
        superseded_claim_ids = {
            item.supersedes_claim_id for item in ordered_claims
            if item.supersedes_claim_id is not None
        }
        active_claims = tuple(
            item for item in ordered_claims if item.claim_id not in superseded_claim_ids
        )
        active_by_action = {item.action_id: item for item in active_claims}
        active_claim = max(active_by_action.values(), key=lambda item: item.declared_sequence, default=None)
        claim_statuses = tuple(
            _claim_status(claim, ordered_verifications, ordered_decisions)
            for claim in sorted(active_by_action.values(), key=lambda item: item.declared_sequence)
        )
        latest_status = max(
            claim_statuses, key=lambda item: item.claim.declared_sequence, default=None,
        )
        supervision = ProjectSupervision(
            active_claims=tuple(sorted(active_by_action.values(), key=lambda item: item.declared_sequence)),
            claim_statuses=claim_statuses,
            active_claim=active_claim,
            latest_active_claim=active_claim,
            latest_verification=None if latest_status is None else latest_status.latest_verification,
            latest_decision=None if latest_status is None else latest_status.latest_decision,
            supervision_state=_aggregate_supervision_state(claim_statuses),
        )
        phase = _phase(
            goal=goal,
            tasks=ordered_tasks,
            blockers=ordered_blockers,
            pending_action_ids=pending_action_ids,
            latest_feedback=latest_feedback,
        )
        confidence = _confidence(
            ordered,
            now=current_time,
            goal=goal,
            tasks=ordered_tasks,
            observations=ordered_observations,
            feedback=ordered_feedback,
        )
        risks = _risks(
            ordered,
            now=current_time,
            goal=goal,
            blockers=ordered_blockers,
            actions=ordered_actions,
            pending_action_ids=pending_action_ids,
            latest_feedback=latest_feedback,
            confidence=confidence,
            supervision=supervision,
        )
        predictions = _predictions(
            phase,
            confidence=confidence,
            blockers=ordered_blockers,
            latest_feedback=latest_feedback,
        )
        counterfactuals = _counterfactuals(
            phase,
            confidence=confidence,
            blockers=ordered_blockers,
            latest_feedback=latest_feedback,
        )
        return ProjectWorldStateProjection(
            schema_version="1.0.0",
            project_id=project_id,
            through_sequence=(ordered[-1].sequence if ordered else 0),
            derived_at=current_time.isoformat(timespec="seconds").replace("+00:00", "Z"),
            phase=phase,
            goal=goal,
            tasks=ordered_tasks,
            blockers=ordered_blockers,
            observations=ordered_observations,
            planned_actions=ordered_actions,
            pending_action_ids=pending_action_ids,
            feedback_facts=ordered_feedback,
            latest_feedback=latest_feedback,
            supervision=supervision,
            confidence=confidence,
            risk_codes=risks,
            predictions=predictions,
            counterfactuals=counterfactuals,
            source_event_ids=tuple(event.event_id for event in ordered),
        )


def _planning_claim(value: SupervisionClaim | None) -> dict[str, object] | None:
    return None if value is None else {
        "claim_id": value.claim_id,
        "supersedes_claim_id": value.supersedes_claim_id,
        "action_id": value.action_id,
        "hypothesis": value.hypothesis,
        "expected_signals": list(value.expected_signals),
        "falsification_signals": list(value.falsification_signals),
        "checkpoint_policy": value.checkpoint_policy,
        "pivot_conditions": list(value.pivot_conditions),
        "stop_conditions": list(value.stop_conditions),
        "basis_sequence": value.basis_sequence,
        "evidence_refs": list(value.evidence_refs),
    }


def _supervision_payload(value: ProjectSupervision) -> dict[str, object]:
    return {
        "state": value.supervision_state,
        "active_claim": _planning_claim(value.active_claim),
        "latest_active_claim": _planning_claim(value.latest_active_claim),
        "active_claims": [_planning_claim(item) for item in value.active_claims],
        "claim_statuses": [_claim_status_payload(item) for item in value.claim_statuses],
        "latest_verification": None if value.latest_verification is None else {
            "verification_id": value.latest_verification.verification_id,
            "claim_id": value.latest_verification.claim_id,
            "action_id": value.latest_verification.action_id,
            "verdict": value.latest_verification.verdict,
            "finding": value.latest_verification.finding,
            "checked_world_sequence": value.latest_verification.checked_world_sequence,
            "evidence_refs": list(value.latest_verification.evidence_refs),
        },
        "latest_decision": None if value.latest_decision is None else {
            "decision_id": value.latest_decision.decision_id,
            "claim_id": value.latest_decision.claim_id,
            "verification_id": value.latest_decision.verification_id,
            "action_id": value.latest_decision.action_id,
            "disposition": value.latest_decision.disposition,
            "rationale": value.latest_decision.rationale,
            "evidence_refs": list(value.latest_decision.evidence_refs),
        },
    }


def _claim_status_payload(value: SupervisionClaimStatus) -> dict[str, object]:
    return {
        "claim": _planning_claim(value.claim),
        "state": value.state,
        "latest_verification": None if value.latest_verification is None else {
            "verification_id": value.latest_verification.verification_id,
            "verdict": value.latest_verification.verdict,
            "checked_world_sequence": value.latest_verification.checked_world_sequence,
            "evidence_refs": list(value.latest_verification.evidence_refs),
        },
        "latest_decision": None if value.latest_decision is None else {
            "decision_id": value.latest_decision.decision_id,
            "disposition": value.latest_decision.disposition,
            "evidence_refs": list(value.latest_decision.evidence_refs),
        },
    }


def _claim_status(
    claim: SupervisionClaim,
    verifications: tuple[SupervisionVerification, ...],
    decisions: tuple[SupervisionDecision, ...],
) -> SupervisionClaimStatus:
    verification = next(
        (item for item in reversed(verifications) if item.claim_id == claim.claim_id),
        None,
    )
    decision = next(
        (item for item in reversed(decisions) if item.claim_id == claim.claim_id),
        None,
    )
    return SupervisionClaimStatus(
        claim=claim,
        latest_verification=verification,
        latest_decision=decision,
        state=_supervision_state(claim, verification, decision),
    )


def _aggregate_supervision_state(statuses: tuple[SupervisionClaimStatus, ...]) -> str:
    if not statuses:
        return "no_claim"
    priority = {
        "on_track": 0, "unverified": 1, "at_risk": 2,
        "invalidated": 3, "stop_required": 4,
    }
    return max(statuses, key=lambda item: priority[item.state]).state


def _supervision_state(
    claim: SupervisionClaim | None,
    verification: SupervisionVerification | None,
    decision: SupervisionDecision | None,
) -> str:
    if claim is None:
        return "no_claim"
    if decision is not None and decision.disposition == "stop_required":
        return "stop_required"
    if verification is None:
        return "unverified"
    if verification.verdict == "refuted":
        return "invalidated"
    if verification.verdict in {"weakened", "inconclusive"}:
        return "at_risk"
    if decision is not None and decision.disposition in {"replan_required", "escalate_user"}:
        return "at_risk"
    return "on_track"


def _validate_trajectory_checkpoints(
    events: tuple[WorldEvent, ...], project_id: str,
) -> None:
    """Validate system-owned checkpoints against the facts visible at append time."""

    graph_events: dict[str, list[TaskGraphEvent]] = {}
    previous: dict[str, TrustCheckpoint] = {}
    for index, event in enumerate(events):
        if event.kind is WorldEventKind.TASK_GRAPH_EVENT_RECORDED:
            graph = TaskGraphEvent.from_payload(event.payload)
            graph_events.setdefault(graph.graph_id, []).append(graph)
            continue
        if event.kind is not WorldEventKind.TRAJECTORY_CHECKPOINT_RECORDED:
            continue
        try:
            checkpoint = TrustCheckpoint.from_payload(event.payload)
            expected_id, expected_ref, expected_revision = (
                trajectory_checkpoint_world_identity(checkpoint)
            )
            if (
                event.actor != "system"
                or event.event_id != expected_id
                or event.source_ref != expected_ref
                or event.source_revision != expected_revision
                or checkpoint.project_id != project_id
                or checkpoint.world_cursor != event.sequence
            ):
                raise TrajectoryContractError("trajectory checkpoint World authority drifted")
            graph = project_task_graph(graph_events.get(checkpoint.graph_id, ()))
            if graph.through_sequence != checkpoint.dag_cursor:
                raise TrajectoryContractError("trajectory checkpoint graph cursor drifted")
            provenance = project_provenance_events(events[:index], project_id=project_id)
            if provenance.through_sequence != checkpoint.provenance_cursor:
                raise TrajectoryContractError("trajectory checkpoint provenance cursor drifted")
            prior = previous.get(checkpoint.graph_id)
            if prior is None:
                if checkpoint.checkpoint_revision != 1 or checkpoint.segment.revision != 1:
                    raise TrajectoryContractError("trajectory initial checkpoint revision is invalid")
            else:
                if checkpoint.checkpoint_revision != prior.checkpoint_revision + 1:
                    raise TrajectoryContractError("trajectory checkpoint revision drifted")
                validate_segment_transition(prior.segment, checkpoint.segment)
            previous[checkpoint.graph_id] = checkpoint
        except (ProvenanceContractError, TaskGraphContractError, TrajectoryContractError) as error:
            raise PersonalWorldModelError("trajectory checkpoint stream is invalid") from error


def _validate_evolution_events(
    events: tuple[WorldEvent, ...], project_id: str,
) -> None:
    """Fail closed on every recursive-evolution substream in the World stream."""

    by_episode: dict[str, list[EvolutionEvent]] = {}
    try:
        for event in events:
            if event.kind is not WorldEventKind.EVOLUTION_EVENT_RECORDED:
                continue
            evolution_event = EvolutionEvent.from_payload(event.payload)
            expected_id, expected_ref, expected_revision = evolution_event_world_identity(
                evolution_event
            )
            if (
                event.actor != "system"
                or event.event_id != expected_id
                or event.source_ref != expected_ref
                or event.source_revision != expected_revision
            ):
                raise EvolutionContractError("evolution World authority drifted")
            by_episode.setdefault(evolution_event.episode_id, []).append(evolution_event)
        for episode_events in by_episode.values():
            project_evolution_events(episode_events, project_id=project_id)
    except EvolutionContractError as error:
        raise PersonalWorldModelError("recursive evolution stream is invalid") from error


def _validate_stream(events: tuple[WorldEvent, ...], project_id: str) -> None:
    if any(not isinstance(item, WorldEvent) for item in events):
        raise PersonalWorldModelError("world projection requires validated events")
    if any(item.project_id != project_id for item in events):
        raise PersonalWorldModelError("world projection crossed project scope")
    expected = tuple(range(1, len(events) + 1))
    actual = tuple(item.sequence for item in events)
    if actual != expected:
        raise PersonalWorldModelError("world event stream has a gap or ordering drift")
    recorded_times = tuple(parse_world_timestamp(item.recorded_at) for item in events)
    if recorded_times != tuple(sorted(recorded_times)):
        raise PersonalWorldModelError("world event recorded time moved backwards")
    event_ids = tuple(item.event_id for item in events)
    if len(event_ids) != len(set(event_ids)):
        raise PersonalWorldModelError("world event stream contains duplicate identities")


def _phase(
    *,
    goal: ProjectGoal | None,
    tasks: tuple[ProjectTask, ...],
    blockers: tuple[ProjectBlocker, ...],
    pending_action_ids: tuple[str, ...],
    latest_feedback: FeedbackFact | None,
) -> str:
    if goal is None:
        return "needs_goal"
    if blockers or any(item.state == "blocked" for item in tasks):
        return "blocked"
    if pending_action_ids:
        return "awaiting_outcome"
    if (
        latest_feedback is not None
        and latest_feedback.outcome is OutcomeStatus.ACHIEVED
        and tasks
        and all(item.state == "completed" for item in tasks)
    ):
        return "achieved"
    if latest_feedback is not None and latest_feedback.outcome in {
        OutcomeStatus.FAILED,
        OutcomeStatus.PARTIAL,
        OutcomeStatus.UNKNOWN,
    }:
        return "needs_revision"
    if tasks or latest_feedback is not None:
        return "in_progress"
    return "ready"


def _confidence(
    events: tuple[WorldEvent, ...],
    *,
    now: datetime,
    goal: ProjectGoal | None,
    tasks: tuple[ProjectTask, ...],
    observations: tuple[ProjectObservation, ...],
    feedback: tuple[FeedbackFact, ...],
) -> float:
    score = 0.32
    if goal is not None:
        score += 0.20
    if tasks:
        score += 0.12
    if observations:
        score += min(0.10, len(observations) * 0.025)
    if feedback:
        score += 0.16
        if feedback[-1].user_evaluation.verdict is not UserEvaluationVerdict.NOT_PROVIDED:
            score += 0.06
    if events:
        latest = max(
            parse_world_timestamp(item.occurred_at or item.recorded_at) for item in events
        )
        age_days = max(0.0, (now - latest).total_seconds() / 86_400)
        score -= min(0.30, age_days * 0.015)
    return round(max(0.05, min(0.98, score)), 3)


def _risks(
    events: tuple[WorldEvent, ...],
    *,
    now: datetime,
    goal: ProjectGoal | None,
    blockers: tuple[ProjectBlocker, ...],
    actions: tuple[PlannedAction, ...],
    pending_action_ids: tuple[str, ...],
    latest_feedback: FeedbackFact | None,
    confidence: float,
    supervision: ProjectSupervision,
) -> tuple[str, ...]:
    risks: list[str] = []
    if goal is None:
        risks.append("goal_missing")
    if blockers:
        risks.append("active_blocker")
        if any(item.severity == "high" for item in blockers):
            risks.append("high_severity_blocker")
    pending = {item.action_id: item for item in actions if item.action_id in pending_action_ids}
    if pending:
        risks.append("outcome_feedback_missing")
        if any(
            item.due_at is not None and parse_world_timestamp(item.due_at) < now
            for item in pending.values()
        ):
            risks.append("planned_action_overdue")
    if latest_feedback is not None and latest_feedback.outcome is OutcomeStatus.FAILED:
        risks.append("latest_action_failed")
    if latest_feedback is not None and latest_feedback.outcome is OutcomeStatus.UNKNOWN:
        risks.append("latest_effect_unknown")
    for item in supervision.claim_statuses:
        verification = item.latest_verification
        decision = item.latest_decision
        if verification is not None and verification.verdict == "refuted":
            risks.append("supervision_refuted")
        if verification is not None and verification.verdict == "inconclusive":
            risks.append("supervision_inconclusive")
        if decision is not None and decision.disposition == "replan_required":
            risks.append("supervision_replan_required")
        if decision is not None and decision.disposition == "stop_required":
            risks.append("supervision_stop_required")
    if events:
        latest_time = max(parse_world_timestamp(item.occurred_at or item.recorded_at) for item in events)
        if (now - latest_time).total_seconds() > 14 * 86_400:
            risks.append("observations_stale")
    if confidence < 0.55:
        risks.append("low_projection_confidence")
    return tuple(dict.fromkeys(risks))


def _predictions(
    phase: str,
    *,
    confidence: float,
    blockers: tuple[ProjectBlocker, ...],
    latest_feedback: FeedbackFact | None,
) -> tuple[StatePrediction, ...]:
    if phase == "needs_goal":
        return (StatePrediction("ready", confidence, "next_user_decision", ("goal_is_explicitly_declared",)),)
    if phase == "blocked":
        return (
            StatePrediction(
                "ready_to_resume",
                max(0.05, round(confidence - 0.08, 3)),
                "after_blocker_resolution",
                tuple(f"resolve:{item.blocker_id}" for item in blockers),
            ),
        )
    if phase == "awaiting_outcome":
        return (StatePrediction("evaluated", confidence, "next_receipt_or_observation", ("receipt_is_verified",)),)
    if phase == "needs_revision":
        outcome = latest_feedback.outcome.value if latest_feedback is not None else "unknown"
        return (
            StatePrediction(
                "in_progress",
                max(0.05, round(confidence - 0.05, 3)),
                "next_replanned_action",
                (f"previous_outcome:{outcome}", "new_plan_addresses_state_delta"),
            ),
        )
    if phase == "achieved":
        return (StatePrediction("goal_review", confidence, "next_user_review", ("success_criteria_are_rechecked",)),)
    return (StatePrediction("in_progress", confidence, "next_governed_action", ("current_authorities_remain_valid",)),)


def _counterfactuals(
    phase: str,
    *,
    confidence: float,
    blockers: tuple[ProjectBlocker, ...],
    latest_feedback: FeedbackFact | None,
) -> tuple[Counterfactual, ...]:
    if phase == "blocked":
        return (
            Counterfactual(
                "highest_severity_blocker_resolved_first",
                "ready_to_resume",
                max(0.05, round(confidence - 0.05, 3)),
                f"open blockers: {len(blockers)}",
            ),
            Counterfactual(
                "action_continues_without_resolving_blocker",
                "higher_failure_risk",
                max(0.05, round(confidence - 0.20, 3)),
                "the observed blocker remains active",
            ),
        )
    if phase == "needs_revision":
        status = latest_feedback.outcome.value if latest_feedback is not None else "unknown"
        return (
            Counterfactual(
                "plan_is_revised_from_feedback_delta",
                "in_progress",
                confidence,
                f"latest outcome is {status}",
            ),
            Counterfactual(
                "same_action_is_retried_without_new_evidence",
                "repeated_failure_risk",
                max(0.05, round(confidence - 0.18, 3)),
                "no changed assumption or evidence is present",
            ),
        )
    if phase == "awaiting_outcome":
        return (
            Counterfactual(
                "receipt_and_real_change_are_verified",
                "evaluated",
                confidence,
                "the planned action receives outcome evidence",
            ),
            Counterfactual(
                "next_action_starts_before_feedback",
                "causal_attribution_risk",
                max(0.05, round(confidence - 0.16, 3)),
                "multiple actions would overlap before attribution",
            ),
        )
    return (
        Counterfactual(
            "next_action_uses_current_prediction",
            "in_progress" if phase != "achieved" else "goal_review",
            confidence,
            "the next plan consumes the latest derived state",
        ),
    )
