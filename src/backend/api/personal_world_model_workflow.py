"""Ordinary-user workflow facade for one governed project-world loop.

The facade generates internal identities and binds a World action to the
existing AI Turn runtime.  It owns no State, Effect, Receipt, Memory, or Skill
authority; every returned status is a projection over those existing stores.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from backend.api.personal_world_model_context import (
    PersonalWorldModelContextError,
    frozen_world_state_planning,
)
from backend.api.personal_world_model_runtime import PersonalWorldModelRuntime
from backend.api.project_provenance_runtime import ProjectProvenanceRuntime
from backend.api.world_supervision_runtime import WorldSupervisionRuntime
from backend.api.workbench_ai_runtime import (
    WORKBENCH_QUESTION_CAPABILITY,
    WORKBENCH_QUESTION_OUTCOME,
    WORLD_PROJECT_SESSION_ID,
)
from core.personal_world_model import (
    FeedbackFact,
    OutcomeStatus,
    PersonalWorldModelError,
    WorldEvent,
    WorldEventDraft,
    WorldEventKind,
    parse_world_timestamp,
    validate_world_identifier,
)
from core.long_horizon_runtime import (
    TraceLink,
    TraceSubject,
    ValidationFact,
    VersionBinding,
)
from core.ai_kernel.context_manifest import ContextManifestError, context_manifest_from_payload


_TERMINAL = frozenset({"completed", "failed", "cancelled"})
_COMMAND_LIMIT = 64


class PersonalWorldModelWorkflowError(ValueError):
    """Raised when the ordinary-user workflow cannot preserve its bindings."""


class TurnRuntimePort(Protocol):
    def receipt_for(self, turn_id: str, *, replayed: bool = False) -> object: ...

    def events_after(
        self, turn_id: str, after_sequence: int = 0,
    ) -> Iterable[Mapping[str, object]]: ...

    def presentation_for(self, turn_id: str) -> Mapping[str, object] | None: ...


class TurnStorePort(Protocol):
    def get_request(self, turn_id: str) -> Mapping[str, object] | None: ...

    def get(self, payload_ref: str) -> object: ...


class TurnRunnerPort(Protocol):
    def accept_and_submit(self, request: Mapping[str, object]) -> object: ...


@dataclass(frozen=True, slots=True)
class WorkflowGoalResult:
    goal_id: str
    replayed: bool
    state: Mapping[str, object]

    def to_payload(self) -> dict[str, object]:
        return {
            "goal_id": self.goal_id,
            "replayed": self.replayed,
            "state": dict(self.state),
        }


@dataclass(frozen=True, slots=True)
class WorkflowActionResult:
    turn_id: str
    action_id: str
    status: str
    replayed: bool
    state: Mapping[str, object]

    def to_payload(self) -> dict[str, object]:
        return {
            "turn_id": self.turn_id,
            "action_id": self.action_id,
            "status": self.status,
            "replayed": self.replayed,
            "state": dict(self.state),
        }


class PersonalWorldModelWorkflow:
    """Bind user-level project progress commands to existing authorities."""

    def __init__(
        self,
        *,
        world: PersonalWorldModelRuntime,
        turns: TurnRuntimePort,
        turn_store: TurnStorePort,
        runner: TurnRunnerPort,
        organization_submitter: TurnRunnerPort | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._world = world
        self._turns = turns
        self._turn_store = turn_store
        self._runner = runner
        self._organization_submitter = organization_submitter
        self._supervision = WorldSupervisionRuntime(world=world)
        self._provenance = ProjectProvenanceRuntime(world=world)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def declare_goal(
        self,
        *,
        project_id: str,
        command_id: str,
        title: object,
        success_criteria: object,
    ) -> WorkflowGoalResult:
        project = validate_world_identifier(project_id, "project id")
        command = _command(command_id)
        event_id = _identity("world-goal-event", command)
        goal_id = _identity("world-goal", command)
        existing = self._world.event(event_id)
        recorded_at = (
            existing.recorded_at
            if existing is not None
            else self._next_recorded_at(project)
        )
        draft = WorldEventDraft(
            event_id=event_id,
            project_id=project,
            kind=WorldEventKind.GOAL_DECLARED,
            actor="user",
            source_ref=f"crp://world-model/{project}/commands/{command}/goal",
            source_revision="1",
            occurred_at=recorded_at,
            recorded_at=recorded_at,
            payload={
                "goal_id": goal_id,
                "title": title,
                "success_criteria": success_criteria,
                "target_at": None,
                "evidence_refs": [
                    f"crp://world-model/{project}/commands/{command}/goal"
                ],
            },
        )
        appended = self._world.append_event(draft)
        state = self._world.project(project).to_payload()
        return WorkflowGoalResult(goal_id, appended.replayed, state)

    def submit_action(
        self,
        *,
        project_id: str,
        command_id: str,
        title: object,
        expected_outcome: object,
        question: object,
    ) -> WorkflowActionResult:
        project = validate_world_identifier(project_id, "project id")
        command = _command(command_id)
        event_id = _identity("world-plan-event", command)
        action_id = _identity("world-action", command)
        turn_id = _identity("world-turn", command)
        existing = self._world.event(event_id)
        current = self._world.project(project)
        if current.goal is None:
            raise PersonalWorldModelWorkflowError(
                "a project goal is required before an action can run"
            )
        if (
            existing is None
            and current.pending_action_ids
            and action_id not in current.pending_action_ids
        ):
            raise PersonalWorldModelWorkflowError(
                "the pending project action requires feedback first"
            )
        if existing is None:
            _require_fresh_supervision(current)
        recorded_at = (
            existing.recorded_at
            if existing is not None
            else self._next_recorded_at(project)
        )
        source_ref = f"crp://world-model/{project}/commands/{command}/action"
        draft = WorldEventDraft(
            event_id=event_id,
            project_id=project,
            kind=WorldEventKind.ACTION_PLANNED,
            actor="user",
            source_ref=source_ref,
            source_revision="1",
            occurred_at=recorded_at,
            recorded_at=recorded_at,
            payload={
                "action_id": action_id,
                "title": title,
                "expected_outcome": expected_outcome,
                "effect_class": "PURE",
                "gate_requirement": "none",
                "due_at": None,
                "evidence_refs": [source_ref],
            },
        )
        text = question.strip() if isinstance(question, str) else ""
        if not text:
            raise PersonalWorldModelWorkflowError("project action question is required")
        request = _turn_request(
            project_id=project,
            command_id=command,
            turn_id=turn_id,
            action_id=action_id,
            question=text,
            created_at=recorded_at,
        )
        appended = self._world.append_event(draft)
        planned_payload = appended.event.payload
        self._supervision.declare_default_claim(
            project_id=project,
            action_id=action_id,
            expected_outcome=str(planned_payload["expected_outcome"]),
            action_sequence=appended.event.sequence,
            recorded_at=recorded_at,
        )
        self._record_action_provenance(
            project_id=project,
            action_id=action_id,
        )
        submitter = self._organization_submitter or self._runner
        receipt = submitter.accept_and_submit(request)
        if (
            getattr(receipt, "turn_id", None) != turn_id
            or getattr(receipt, "operation_id", None) != action_id
            or not isinstance(getattr(receipt, "status", None), str)
        ):
            raise PersonalWorldModelWorkflowError(
                "AI Turn admission did not bind the planned action"
            )
        state = self._world.project(project).to_payload()
        return WorkflowActionResult(
            turn_id,
            action_id,
            str(receipt.status),
            appended.replayed or getattr(receipt, "replayed", False) is True,
            state,
        )

    def pivot_action(
        self,
        *,
        project_id: str,
        command_id: str,
        supersedes_action_id: str,
        title: object,
        expected_outcome: object,
        question: object,
    ) -> WorkflowActionResult:
        """Replace one refuted direction through an explicit, replayable saga."""

        project = validate_world_identifier(project_id, "project id")
        command = _command(command_id)
        superseded_action = validate_world_identifier(
            supersedes_action_id, "superseded action id"
        )
        event_id = _identity("world-plan-event", command)
        action_id = _identity("world-action", command)
        turn_id = _identity("world-turn", command)
        claim_event_id = _identity("world-supervision-claim-event", action_id)
        existing = self._world.event(event_id)
        existing_claim = self._world.event(claim_event_id)
        current = self._world.project(project)
        if current.goal is None:
            raise PersonalWorldModelWorkflowError(
                "a project goal is required before an action can run"
            )
        prior_claim_id = _identity("world-supervision-claim", superseded_action)
        if existing_claim is None:
            _require_pivotable_supervision(current, superseded_action, prior_claim_id)
        recorded_at = (
            existing.recorded_at
            if existing is not None
            else self._next_recorded_at(project)
        )
        source_ref = (
            f"crp://world-model/{project}/commands/{command}/"
            f"pivot/{superseded_action}"
        )
        draft = WorldEventDraft(
            event_id=event_id,
            project_id=project,
            kind=WorldEventKind.ACTION_PLANNED,
            actor="user",
            source_ref=source_ref,
            source_revision="1",
            occurred_at=recorded_at,
            recorded_at=recorded_at,
            payload={
                "action_id": action_id,
                "title": title,
                "expected_outcome": expected_outcome,
                "effect_class": "PURE",
                "gate_requirement": "none",
                "due_at": None,
                "evidence_refs": [source_ref],
            },
        )
        text = question.strip() if isinstance(question, str) else ""
        if not text:
            raise PersonalWorldModelWorkflowError("project action question is required")
        request = _turn_request(
            project_id=project,
            command_id=command,
            turn_id=turn_id,
            action_id=action_id,
            question=text,
            created_at=recorded_at,
        )
        appended = self._world.append_event(draft)
        planned_payload = appended.event.payload
        claim = self._supervision.declare_default_claim(
            project_id=project,
            action_id=action_id,
            expected_outcome=str(planned_payload["expected_outcome"]),
            action_sequence=appended.event.sequence,
            recorded_at=recorded_at,
            supersedes_claim_id=prior_claim_id,
        )
        self._record_action_provenance(
            project_id=project,
            action_id=action_id,
        )
        submitter = self._organization_submitter or self._runner
        receipt = submitter.accept_and_submit(request)
        if (
            getattr(receipt, "turn_id", None) != turn_id
            or getattr(receipt, "operation_id", None) != action_id
            or not isinstance(getattr(receipt, "status", None), str)
        ):
            raise PersonalWorldModelWorkflowError(
                "AI Turn admission did not bind the planned action"
            )
        state = self._world.project(project).to_payload()
        return WorkflowActionResult(
            turn_id,
            action_id,
            str(receipt.status),
            appended.replayed
            or claim.replayed
            or getattr(receipt, "replayed", False) is True,
            state,
        )

    def overview(self, *, project_id: str) -> dict[str, object]:
        project = validate_world_identifier(project_id, "project id")
        state = self._world.project(project)
        actions = []
        for planned in state.planned_actions[-8:]:
            turn_id = _turn_id_for_action(planned.action_id)
            if turn_id is None:
                continue
            actions.append(self.action_status(project_id=project, turn_id=turn_id))
        return {
            "state": state.to_payload(),
            "recent_actions": actions,
            "authority": {
                "state": "derived_only",
                "action": "ai_turn_gate_effect_receipt",
                "learning": "proposal_only",
            },
        }

    def action_status(self, *, project_id: str, turn_id: str) -> dict[str, object]:
        project = validate_world_identifier(project_id, "project id")
        turn = validate_world_identifier(turn_id, "turn id")
        request = self._turn_store.get_request(turn)
        if request is None:
            action_id = _action_id_for_turn(turn)
            return {
                "turn_id": turn,
                "action_id": action_id,
                "status": "not_admitted",
                "terminal": False,
                "ready_for_feedback": False,
                "feedback_id": None,
                "answer": "",
                "context": None,
                "governance": {
                    "gate": "not_started",
                    "effect": "not_started",
                    "handler": "not_started",
                    "receipt": "not_started",
                    "feedback": "not_recorded",
                },
            }
        _assert_turn_project(request, project)
        receipt = self._turns.receipt_for(turn)
        action_id = validate_world_identifier(
            getattr(receipt, "operation_id", None), "action id"
        )
        state = self._world.project(project)
        if not any(item.action_id == action_id for item in state.planned_actions):
            raise PersonalWorldModelWorkflowError(
                "AI Turn is not bound to a planned project action"
            )
        events = tuple(self._turns.events_after(turn))
        event_types = tuple(str(item.get("type") or "") for item in events)
        status = str(getattr(receipt, "status", ""))
        terminal = status in _TERMINAL
        feedback = next(
            (
                item
                for item in reversed(state.feedback_facts)
                if item.action_id == action_id
            ),
            None,
        )
        context = _context_summary(
            events,
            self._turn_store,
            turn_id=turn,
            project_id=project,
        )
        presentation = self._turns.presentation_for(turn)
        return {
            "turn_id": turn,
            "action_id": action_id,
            "status": status,
            "terminal": terminal,
            "ready_for_feedback": terminal and feedback is None and _latest_outcome_ref(events) is not None,
            "feedback_id": None if feedback is None else feedback.feedback_id,
            "answer": _answer_text(presentation),
            "context": context,
            "governance": {
                "gate": (
                    "passed"
                    if "tool.intent.recorded" in event_types
                    else ("evaluating" if "tool.requested" in event_types else "not_started")
                ),
                "effect": (
                    "settled"
                    if "tool.outcome.recorded" in event_types
                    else ("running" if "tool.intent.recorded" in event_types else "not_started")
                ),
                "handler": (
                    "completed"
                    if "tool.completed" in event_types
                    else (
                        "failed"
                        if "tool.failed" in event_types
                        else ("running" if "tool.intent.recorded" in event_types else "not_started")
                    )
                ),
                "receipt": "terminal" if terminal else "pending",
                "feedback": "recorded" if feedback is not None else "not_recorded",
            },
        }

    def record_feedback(
        self,
        *,
        project_id: str,
        turn_id: str,
        command_id: str,
        actual_outcome: object,
        outcome: object,
        state_delta: object,
        user_evaluation: object,
    ) -> dict[str, object]:
        project = validate_world_identifier(project_id, "project id")
        turn = validate_world_identifier(turn_id, "turn id")
        command = _command(command_id)
        feedback_id = _identity("world-feedback", command)
        event_id = _identity("world-feedback-event", command)
        request = self._turn_store.get_request(turn)
        if request is None:
            raise PersonalWorldModelWorkflowError("AI Turn is unavailable")
        _assert_turn_project(request, project)
        receipt = self._turns.receipt_for(turn)
        action_id = validate_world_identifier(
            getattr(receipt, "operation_id", None), "action id"
        )
        status = outcome if isinstance(outcome, OutcomeStatus) else OutcomeStatus(outcome)
        if not isinstance(state_delta, Sequence) or isinstance(state_delta, (str, bytes)):
            raise PersonalWorldModelWorkflowError("state delta must be a list")
        if not isinstance(user_evaluation, Mapping):
            raise PersonalWorldModelWorkflowError("user evaluation must be an object")
        existing = self._world.event(event_id)
        if existing is not None:
            fact = _matching_feedback_replay(
                existing,
                project_id=project,
                feedback_id=feedback_id,
                action_id=action_id,
                actual_outcome=actual_outcome,
                outcome=status,
                state_delta=state_delta,
                user_evaluation=user_evaluation,
            )
            verified = self._world.verified_feedback(
                project_id=project,
                feedback_id=feedback_id,
                turn_id=turn,
            )
            self._record_feedback_provenance(
                project_id=project,
                action_id=action_id,
                fact=fact,
                evidence=verified.evidence,
                recorded_at=existing.recorded_at,
            )
            return _feedback_result(
                fact=fact,
                turn_id=turn,
                replayed=True,
                evidence=verified.evidence,
                state=self._world.project(project).to_payload(),
            )
        events = tuple(self._turns.events_after(turn))
        outcome_ref = _latest_outcome_ref(events)
        if outcome_ref is None:
            raise PersonalWorldModelWorkflowError(
                "terminal Tool outcome Receipt is unavailable"
            )
        current = self._world.project(project)
        planned = next(
            (item for item in current.planned_actions if item.action_id == action_id),
            None,
        )
        if planned is None:
            raise PersonalWorldModelWorkflowError("planned project action is unavailable")
        prior = next(
            (
                item
                for item in reversed(current.feedback_facts)
                if item.action_id == action_id
            ),
            None,
        )
        terminal_at = _terminal_occurred_at(events, receipt)
        recorded_at = self._next_recorded_at(project, minimum=terminal_at)
        cost = {
            "elapsed_ms": _elapsed_ms(request.get("created_at"), terminal_at),
            "model_input_tokens": 0,
            "model_output_tokens": 0,
            "external_calls": 0,
            "human_attention_seconds": 0,
        }
        recorded = self._world.record_feedback(
            project_id=project,
            event_id=event_id,
            feedback_id=feedback_id,
            supersedes_feedback_id=None if prior is None else prior.feedback_id,
            action_id=action_id,
            turn_id=turn,
            outcome_ref=outcome_ref,
            expected_outcome=planned.expected_outcome,
            actual_outcome=actual_outcome,
            outcome=status,
            state_delta=state_delta,
            cost=cost,
            user_evaluation=user_evaluation,
            observed_evidence_refs=(),
            actor="user",
            occurred_at=recorded_at,
            recorded_at=recorded_at,
        )
        fact = FeedbackFact.from_payload(project, recorded.append.event.payload)
        self._record_feedback_provenance(
            project_id=project,
            action_id=action_id,
            fact=fact,
            evidence=recorded.evidence,
            recorded_at=recorded.append.event.recorded_at,
        )
        return _feedback_result(
            fact=fact,
            turn_id=turn,
            replayed=recorded.append.replayed,
            evidence=recorded.evidence,
            state=self._world.project(project).to_payload(),
        )

    def _record_action_provenance(
        self,
        *,
        project_id: str,
        action_id: str,
    ) -> None:
        """Complete the deterministic World action/claim trace before admission."""

        events = self._world.events(project_id)
        action_event = next(
            (
                event
                for event in events
                if event.kind is WorldEventKind.ACTION_PLANNED
                and event.payload.get("action_id") == action_id
            ),
            None,
        )
        claim_event = next(
            (
                event
                for event in events
                if event.kind is WorldEventKind.SUPERVISION_CLAIM_DECLARED
                and event.payload.get("action_id") == action_id
                and not any(
                    later.kind is WorldEventKind.SUPERVISION_CLAIM_DECLARED
                    and later.payload.get("supersedes_claim_id")
                    == event.payload.get("claim_id")
                    for later in events
                )
            ),
            None,
        )
        if action_event is None or claim_event is None:
            raise PersonalWorldModelWorkflowError("action supervision provenance is unavailable")
        action_refs = action_event.payload.get("evidence_refs")
        claim_refs = claim_event.payload.get("evidence_refs")
        claim_id = claim_event.payload.get("claim_id")
        if (
            not isinstance(action_refs, Sequence)
            or isinstance(action_refs, (str, bytes))
            or not action_refs
            or not isinstance(claim_refs, Sequence)
            or isinstance(claim_refs, (str, bytes))
            or not claim_refs
            or not isinstance(claim_id, str)
        ):
            raise PersonalWorldModelWorkflowError("action supervision provenance is unavailable")
        action_subject = TraceSubject(project_id, "world_action", action_id)
        action_version = VersionBinding(
            str(action_refs[0]), f"world-sequence-{action_event.sequence}", None,
        )
        hypothesis_subject = TraceSubject(project_id, "hypothesis", claim_id)
        hypothesis_version = VersionBinding(
            str(claim_refs[0]), f"world-sequence-{claim_event.sequence}", None,
        )
        self._provenance.record_subject(
            subject=action_subject,
            version=action_version,
            recorded_at=action_event.recorded_at,
        )
        self._provenance.record_subject(
            subject=hypothesis_subject,
            version=hypothesis_version,
            recorded_at=claim_event.recorded_at,
        )
        self._provenance.record_link(
            link=TraceLink(
                project_id, action_subject, action_version, hypothesis_subject,
                hypothesis_version, "depends_on",
            ),
            recorded_at=claim_event.recorded_at,
        )

    def _record_feedback_provenance(
        self,
        *,
        project_id: str,
        action_id: str,
        fact: FeedbackFact,
        evidence: object,
        recorded_at: str,
    ) -> None:
        self._record_action_provenance(
            project_id=project_id,
            action_id=action_id,
        )
        state = self._world.project(project_id, now=recorded_at)
        action = next(
            (item for item in state.planned_actions if item.action_id == action_id), None,
        )
        if action is None:
            raise PersonalWorldModelWorkflowError("feedback provenance action is unavailable")
        subject = TraceSubject(project_id, "world_action", action_id)
        version = VersionBinding(
            action.evidence_refs[0], f"world-sequence-{action.planned_sequence}", None,
        )
        outcome_refs = tuple(dict.fromkeys((
            str(getattr(evidence, "outcome_ref", "")), *fact.evidence_refs,
        )))
        if not outcome_refs or not all(reference.startswith("crp://") for reference in outcome_refs):
            raise PersonalWorldModelWorkflowError("feedback provenance evidence is unavailable")
        verdict = {
            OutcomeStatus.ACHIEVED: "verified",
            OutcomeStatus.FAILED: "rejected",
            OutcomeStatus.PARTIAL: "inconclusive",
            OutcomeStatus.UNKNOWN: "inconclusive",
        }[fact.outcome]
        terminal_sequence = getattr(evidence, "terminal_sequence", None)
        if not isinstance(terminal_sequence, int) or terminal_sequence < 1:
            raise PersonalWorldModelWorkflowError("feedback provenance receipt is unavailable")
        self._provenance.record_validation(
            validation=ValidationFact(
                project_id, _identity("world-feedback-validation", fact.feedback_id),
                subject, version, verdict, "turn_receipt", str(terminal_sequence), outcome_refs,
            ),
            recorded_at=recorded_at,
        )

    def _next_recorded_at(self, project_id: str, *, minimum: str | None = None) -> str:
        now = self._now()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise PersonalWorldModelWorkflowError("workflow clock must be timezone-aware")
        candidates = [now.astimezone(timezone.utc)]
        events = self._world.events(project_id)
        if events:
            candidates.append(parse_world_timestamp(events[-1].recorded_at))
        if minimum is not None:
            candidates.append(parse_world_timestamp(minimum))
        return max(candidates).isoformat(timespec="seconds").replace("+00:00", "Z")


def _turn_request(
    *,
    project_id: str,
    command_id: str,
    turn_id: str,
    action_id: str,
    question: str,
    created_at: str,
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "turn_id": turn_id,
        "session_id": WORLD_PROJECT_SESSION_ID,
        "operation_id": action_id,
        "idempotency_key": _identity("world-turn-request", command_id),
        "scope": {"kind": "project", "project_id": project_id, "series_id": None},
        "input": {"kind": "text", "text": question, "refs": []},
        "desired_outcome": WORKBENCH_QUESTION_OUTCOME,
        "privacy": {
            "mode": "local_only",
            "allow_remote": False,
            "pii": "possible",
            "consent_refs": [],
            "retention": "local_durable",
        },
        "capability_policy": {
            "allowed": [WORKBENCH_QUESTION_CAPABILITY],
            "denied": [],
            "require_approval": [],
        },
        "context_policy": {
            "include_project_skill": True,
            "include_memory": True,
            "include_session_history": False,
            "max_context_bytes": 262_144,
        },
        "approval_policy": {
            "mode": "risk_based",
            "auto_approve_read_only": True,
        },
        "created_at": created_at,
    }


def _command(value: object) -> str:
    command = validate_world_identifier(value, "workflow command id")
    if len(command) > _COMMAND_LIMIT:
        raise PersonalWorldModelWorkflowError("workflow command id is over budget")
    return command


def _require_fresh_supervision(state: object) -> None:
    supervision = getattr(state, "supervision", None)
    statuses = getattr(supervision, "claim_statuses", ())
    dispositions = {
        getattr(item.latest_decision, "disposition", None)
        for item in statuses
        if getattr(item, "latest_decision", None) is not None
    }
    if "stop_required" in dispositions:
        raise PersonalWorldModelWorkflowError(
            "active supervision requires stopping before another action"
        )
    if "replan_required" in dispositions:
        raise PersonalWorldModelWorkflowError(
            "active supervision requires replanning before another action"
        )
    if "escalate_user" in dispositions:
        raise PersonalWorldModelWorkflowError(
            "active supervision requires user escalation before another action"
        )


def _require_pivotable_supervision(
    state: object,
    supersedes_action_id: str,
    supersedes_claim_id: str,
) -> None:
    supervision = getattr(state, "supervision", None)
    statuses = tuple(getattr(supervision, "claim_statuses", ()))
    blocking = tuple(
        item
        for item in statuses
        if getattr(getattr(item, "latest_decision", None), "disposition", None)
        in {"replan_required", "stop_required", "escalate_user"}
    )
    if any(
        getattr(item.latest_decision, "disposition", None) == "stop_required"
        for item in blocking
    ):
        raise PersonalWorldModelWorkflowError(
            "active supervision requires stopping and cannot be pivoted"
        )
    if any(
        getattr(item.latest_decision, "disposition", None) == "escalate_user"
        for item in blocking
    ):
        raise PersonalWorldModelWorkflowError(
            "active supervision requires user escalation before pivoting"
        )
    matches = tuple(
        item
        for item in blocking
        if (
            item.claim.action_id == supersedes_action_id
            and item.claim.claim_id == supersedes_claim_id
            and getattr(item.latest_decision, "disposition", None)
            == "replan_required"
        )
    )
    if len(matches) != 1 or len(blocking) != 1:
        raise PersonalWorldModelWorkflowError(
            "pivot requires exactly one active replan decision"
        )


def _identity(prefix: str, command_id: str) -> str:
    return validate_world_identifier(f"{prefix}-{command_id}", prefix)


def _turn_id_for_action(action_id: str) -> str | None:
    prefix = "world-action-"
    if not action_id.startswith(prefix):
        return None
    return _identity("world-turn", action_id.removeprefix(prefix))


def _action_id_for_turn(turn_id: str) -> str | None:
    prefix = "world-turn-"
    if not turn_id.startswith(prefix):
        return None
    return _identity("world-action", turn_id.removeprefix(prefix))


def _assert_turn_project(request: Mapping[str, object], project_id: str) -> None:
    scope = request.get("scope")
    if not isinstance(scope, Mapping) or scope.get("project_id") != project_id:
        raise PersonalWorldModelWorkflowError("AI Turn project scope drifted")
    if request.get("session_id") != WORLD_PROJECT_SESSION_ID:
        raise PersonalWorldModelWorkflowError("AI Turn is not a project-world action")


def _latest_outcome_ref(events: Sequence[Mapping[str, object]]) -> str | None:
    candidates: list[tuple[int, str]] = []
    for event in events:
        data = event.get("data")
        sequence = event.get("sequence")
        payload_ref = data.get("payload_ref") if isinstance(data, Mapping) else None
        if (
            event.get("type") == "tool.outcome.recorded"
            and isinstance(sequence, int)
            and not isinstance(sequence, bool)
            and isinstance(payload_ref, str)
        ):
            candidates.append((sequence, payload_ref))
    return max(candidates)[1] if candidates else None


def _terminal_occurred_at(events: Sequence[Mapping[str, object]], receipt: object) -> str:
    sequence = getattr(receipt, "current_sequence", None)
    status = getattr(receipt, "status", None)
    expected = {
        "completed": "turn.completed",
        "failed": "turn.failed",
        "cancelled": "turn.cancelled",
    }.get(status)
    matches = tuple(
        event
        for event in events
        if event.get("sequence") == sequence and event.get("type") == expected
    )
    occurred_at = matches[0].get("occurred_at") if len(matches) == 1 else None
    if not isinstance(occurred_at, str):
        raise PersonalWorldModelWorkflowError("terminal Turn time is unavailable")
    return parse_world_timestamp(occurred_at).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _elapsed_ms(created_at: object, terminal_at: str) -> int:
    if not isinstance(created_at, str):
        return 0
    try:
        delta = parse_world_timestamp(terminal_at) - parse_world_timestamp(created_at)
    except (PersonalWorldModelError, TypeError, ValueError):
        return 0
    return max(0, int(delta.total_seconds() * 1000))


def _answer_text(presentation: Mapping[str, object] | None) -> str:
    if not isinstance(presentation, Mapping):
        return ""
    answer = presentation.get("answer")
    if isinstance(answer, Mapping) and isinstance(answer.get("text"), str):
        return str(answer["text"]).strip()
    preview = presentation.get("answer_preview")
    return str(preview).strip() if isinstance(preview, str) else ""


def _context_summary(
    events: Sequence[Mapping[str, object]],
    payloads: TurnStorePort,
    *,
    turn_id: str,
    project_id: str,
) -> dict[str, object] | None:
    try:
        planning = frozen_world_state_planning(
            events,
            payloads,  # type: ignore[arg-type]
            turn_id=turn_id,
            project_id=project_id,
        )
    except PersonalWorldModelContextError as error:
        raise PersonalWorldModelWorkflowError(
            "frozen WorldState context cannot be verified"
        ) from error
    if planning is None:
        return None
    latest = planning.get("latest_feedback")
    summary = {
        "through_sequence": planning.get("through_sequence"),
        "phase": planning.get("phase"),
        "latest_feedback_consumed": isinstance(latest, Mapping),
        "latest_feedback_outcome": (
            latest.get("outcome") if isinstance(latest, Mapping) else None
        ),
        "latest_feedback_actual_outcome": (
            latest.get("actual_outcome") if isinstance(latest, Mapping) else None
        ),
        "projection_authority": "frozen_derived_only",
    }
    summary["compaction"] = _context_compaction_summary(
        events, payloads, turn_id=turn_id, project_id=project_id,
    )
    return summary


def _context_compaction_summary(
    events: Sequence[Mapping[str, object]],
    payloads: TurnStorePort,
    *,
    turn_id: str,
    project_id: str,
) -> dict[str, object]:
    """Return aggregate Manifest compaction facts without its lineage/content."""
    context_events = tuple(event for event in events if event.get("type") == "context.resolved")
    if not context_events:
        return _empty_compaction_summary()
    if len(context_events) != 1:
        raise PersonalWorldModelWorkflowError("Turn context authority is ambiguous")
    data = context_events[0].get("data")
    manifest_ref = data.get("payload_ref") if isinstance(data, Mapping) else None
    if not isinstance(manifest_ref, str):
        raise PersonalWorldModelWorkflowError("Turn context manifest ref is unavailable")
    try:
        manifest = context_manifest_from_payload(payloads.get(manifest_ref))
    except (ContextManifestError, KeyError, TypeError, ValueError) as error:
        raise PersonalWorldModelWorkflowError("Turn context manifest is invalid") from error
    if manifest.turn_id != turn_id or manifest.project_id != project_id:
        raise PersonalWorldModelWorkflowError("Turn context manifest scope drifted")
    input_bytes = sum(item.input_bytes for item in manifest.compactions)
    output_bytes = sum(item.output_bytes for item in manifest.compactions)
    labels = tuple(dict.fromkeys(_compaction_strategy_label(item.strategy) for item in manifest.compactions))
    return {
        "applied": bool(manifest.compactions),
        "count": len(manifest.compactions),
        "input_bytes": input_bytes,
        "output_bytes": output_bytes,
        "saved_bytes": input_bytes - output_bytes,
        "strategy_label": "、".join(labels) if labels else "未使用压缩",
    }


def _empty_compaction_summary() -> dict[str, object]:
    return {
        "applied": False,
        "count": 0,
        "input_bytes": 0,
        "output_bytes": 0,
        "saved_bytes": 0,
        "strategy_label": "未使用压缩",
    }


def _compaction_strategy_label(value: object) -> str:
    labels = {
        "semantic_summary": "语义摘要",
        "conversation_summary": "对话摘要",
        "world_state_compaction": "状态摘要",
        "deterministic_local_extractive_memory_r1_v1": "本地记忆摘要",
    }
    return labels.get(value if isinstance(value, str) else "", "上下文摘要")


def _matching_feedback_replay(
    event: WorldEvent,
    *,
    project_id: str,
    feedback_id: str,
    action_id: str,
    actual_outcome: object,
    outcome: OutcomeStatus,
    state_delta: Sequence[object],
    user_evaluation: Mapping[str, object],
) -> FeedbackFact:
    if event.project_id != project_id or event.kind is not WorldEventKind.FEEDBACK_RECORDED:
        raise PersonalWorldModelWorkflowError("feedback command identity conflicts")
    fact = FeedbackFact.from_payload(project_id, event.payload)
    requested_delta = [dict(item) if isinstance(item, Mapping) else item for item in state_delta]
    if (
        fact.feedback_id != feedback_id
        or fact.action_id != action_id
        or fact.actual_outcome != actual_outcome
        or fact.outcome is not outcome
        or [item.to_payload() for item in fact.state_delta] != requested_delta
        or fact.user_evaluation.to_payload() != dict(user_evaluation)
    ):
        raise PersonalWorldModelWorkflowError("feedback command payload drifted")
    return fact


def _feedback_result(
    *,
    fact: FeedbackFact,
    turn_id: str,
    replayed: bool,
    evidence: object,
    state: Mapping[str, object],
) -> dict[str, object]:
    return {
        "feedback_id": fact.feedback_id,
        "turn_id": turn_id,
        "replayed": replayed,
        "outcome": fact.outcome.value,
        "governance": {
            "capability_id": getattr(evidence, "capability_id", None),
            "tool_status": getattr(evidence, "tool_status", None),
            "effect_certainty": getattr(evidence, "effect_certainty", None),
            "receipt": "verified",
        },
        "state": dict(state),
    }
