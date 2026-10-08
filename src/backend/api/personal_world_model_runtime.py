"""Application composition for the Personal Memory World Model.

This module observes existing AI Turn and Tool outcome authorities.  It does
not execute an Effect, create a Receipt, or write Memory/Skill state.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from core.ai_kernel.contracts import AIKernelContractError
from core.ai_kernel.tool_invocation import ToolInvocationOutcome, outcome_from_payload
from core.personal_world_model import (
    FeedbackCost,
    FeedbackFact,
    OutcomeStatus,
    PersonalWorldModelError,
    ProjectDynamicsEngine,
    ProjectWorldStateProjection,
    SQLiteWorldEventRepository,
    StateDelta,
    UserEvaluation,
    WorldEvent,
    WorldEventAppendResult,
    WorldEventDraft,
    WorldEventKind,
    feedback_event_draft,
    parse_world_timestamp,
    validate_world_evidence_ref,
    validate_world_identifier,
)
from core.long_horizon_runtime import TrustCheckpoint
from core.storage_provider import SQLiteStructuredRecordStore


_TERMINAL_TURN_TYPES = {
    "completed": "turn.completed",
    "failed": "turn.failed",
    "cancelled": "turn.cancelled",
}
_TERMINAL_TOOL_TYPES = {
    "completed": "tool.completed",
    "failed": "tool.failed",
    "timed_out": "tool.failed",
    "cancelled": "tool.cancelled",
    "unknown_effect": "tool.failed",
}


class TurnReceiptPort(Protocol):
    turn_id: str
    operation_id: str
    status: str
    current_sequence: int


class TurnReceiptReaderPort(Protocol):
    def receipt_for(self, turn_id: str, *, replayed: bool = False) -> TurnReceiptPort: ...

    def events_after(
        self, turn_id: str, after_sequence: int = 0,
    ) -> Iterable[Mapping[str, object]]: ...


class TurnEvidenceStorePort(Protocol):
    def get_request(self, turn_id: str) -> Mapping[str, object] | None: ...

    def get(self, payload_ref: str) -> object: ...


class TrajectoryCheckpointAuthorityPort(Protocol):
    def validate_checkpoint(
        self,
        checkpoint: TrustCheckpoint,
        previous: TrustCheckpoint | None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class VerifiedTurnOutcome:
    turn_id: str
    operation_id: str
    turn_status: str
    terminal_sequence: int
    terminal_occurred_at: str
    outcome_ref: str
    invocation_id: str
    capability_id: str
    tool_status: str
    effect_certainty: str

    def to_payload(self) -> dict[str, object]:
        return {
            "turn_id": self.turn_id,
            "operation_id": self.operation_id,
            "turn_status": self.turn_status,
            "terminal_sequence": self.terminal_sequence,
            "outcome_ref": self.outcome_ref,
            "invocation_id": self.invocation_id,
            "capability_id": self.capability_id,
            "tool_status": self.tool_status,
            "effect_certainty": self.effect_certainty,
        }


@dataclass(frozen=True, slots=True)
class RecordedWorldFeedback:
    append: WorldEventAppendResult
    evidence: VerifiedTurnOutcome


@dataclass(frozen=True, slots=True)
class VerifiedWorldFeedback:
    event: WorldEvent
    fact: FeedbackFact
    evidence: VerifiedTurnOutcome


class TurnOutcomeEvidenceAuthority:
    """Verify one feedback claim against the existing Turn outcome authority."""

    def __init__(
        self,
        *,
        receipts: TurnReceiptReaderPort,
        store: TurnEvidenceStorePort,
    ) -> None:
        self._receipts = receipts
        self._store = store

    def verify(
        self,
        *,
        project_id: str,
        action_id: str,
        turn_id: str,
        outcome_ref: str,
        reported_outcome: OutcomeStatus,
    ) -> VerifiedTurnOutcome:
        project = validate_world_identifier(project_id, "project id")
        action = validate_world_identifier(action_id, "action id")
        turn = validate_world_identifier(turn_id, "turn id")
        outcome_reference = validate_world_evidence_ref(outcome_ref)
        outcome_status = (
            reported_outcome
            if isinstance(reported_outcome, OutcomeStatus)
            else OutcomeStatus(reported_outcome)
        )

        request = self._store.get_request(turn)
        scope = request.get("scope") if isinstance(request, Mapping) else None
        scoped_project = scope.get("project_id") if isinstance(scope, Mapping) else None
        if scoped_project != project:
            raise PersonalWorldModelError("Turn project authority does not match world project")

        try:
            receipt = self._receipts.receipt_for(turn)
        except Exception as error:
            raise PersonalWorldModelError("terminal AI Turn receipt is unavailable") from error
        if (
            getattr(receipt, "turn_id", None) != turn
            or getattr(receipt, "operation_id", None) != action
            or getattr(receipt, "status", None) not in _TERMINAL_TURN_TYPES
            or not isinstance(getattr(receipt, "current_sequence", None), int)
        ):
            raise PersonalWorldModelError("AI Turn receipt does not bind the planned action")

        events = tuple(self._receipts.events_after(turn))
        terminal_sequence = int(receipt.current_sequence)
        terminal = _event_at_sequence(events, terminal_sequence)
        if terminal is None or terminal.get("type") != _TERMINAL_TURN_TYPES[receipt.status]:
            raise PersonalWorldModelError("AI Turn terminal event drifted from its receipt")
        terminal_time = terminal.get("occurred_at")
        if not isinstance(terminal_time, str):
            raise PersonalWorldModelError("AI Turn terminal time is unavailable")
        terminal_occurred_at = _world_time(terminal_time)

        outcome_event = _unique_outcome_event(events, outcome_reference)
        outcome_data = outcome_event.get("data")
        correlation = outcome_event.get("correlation")
        if not isinstance(outcome_data, Mapping) or not isinstance(correlation, Mapping):
            raise PersonalWorldModelError("Tool outcome event is malformed")
        tool_call_id = correlation.get("tool_call_id")
        capability_id = outcome_data.get("capability_id")
        if not isinstance(tool_call_id, str) or not isinstance(capability_id, str):
            raise PersonalWorldModelError("Tool outcome identity is unavailable")

        try:
            tool_outcome = outcome_from_payload(self._store.get(outcome_reference))
        except (AIKernelContractError, KeyError, TypeError, ValueError) as error:
            raise PersonalWorldModelError("Tool outcome Receipt is unavailable or invalid") from error
        _verify_tool_outcome_identity(
            tool_outcome,
            turn_id=turn,
            tool_call_id=tool_call_id,
            capability_id=capability_id,
        )
        tool_terminal = _matching_tool_terminal(events, tool_outcome)
        if (
            int(outcome_event.get("sequence", 0)) >= int(tool_terminal.get("sequence", 0))
            or int(tool_terminal.get("sequence", 0)) >= terminal_sequence
        ):
            raise PersonalWorldModelError("Tool and Turn outcome ordering is invalid")
        _verify_reported_outcome(outcome_status, tool_outcome, turn_status=receipt.status)

        return VerifiedTurnOutcome(
            turn_id=turn,
            operation_id=action,
            turn_status=receipt.status,
            terminal_sequence=terminal_sequence,
            terminal_occurred_at=terminal_occurred_at,
            outcome_ref=outcome_reference,
            invocation_id=tool_outcome.invocation_id,
            capability_id=tool_outcome.capability_id,
            tool_status=tool_outcome.status,
            effect_certainty=tool_outcome.effect_certainty,
        )


class PersonalWorldModelRuntime:
    """Append facts and rebuild one project projection from the shared DB."""

    def __init__(
        self,
        *,
        repository: SQLiteWorldEventRepository,
        evidence: TurnOutcomeEvidenceAuthority | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._evidence = evidence
        self._dynamics = ProjectDynamicsEngine()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._trajectory_authority: TrajectoryCheckpointAuthorityPort | None = None

    @classmethod
    def for_root(
        cls,
        root_dir: Path,
        *,
        receipts: TurnReceiptReaderPort | None = None,
        turn_store: TurnEvidenceStorePort | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> PersonalWorldModelRuntime:
        root = Path(root_dir).expanduser().resolve(strict=False)
        repository = SQLiteWorldEventRepository(
            SQLiteStructuredRecordStore(root / ".rebuild-data" / "jobs.sqlite3")
        )
        if (receipts is None) != (turn_store is None):
            raise PersonalWorldModelError("Turn evidence composition is incomplete")
        evidence = (
            None
            if receipts is None or turn_store is None
            else TurnOutcomeEvidenceAuthority(receipts=receipts, store=turn_store)
        )
        return cls(repository=repository, evidence=evidence, now=now)

    def append_event(self, draft: WorldEventDraft) -> WorldEventAppendResult:
        def validate_stream(events: tuple[WorldEvent, ...]) -> None:
            self._dynamics.project(
                events,
                project_id=draft.project_id,
                now=draft.recorded_at,
            )
            if draft.kind is not WorldEventKind.TRAJECTORY_CHECKPOINT_RECORDED:
                return
            authority = self._trajectory_authority
            if authority is None:
                raise PersonalWorldModelError(
                    "trajectory checkpoint authority is unavailable"
                )
            checkpoint = TrustCheckpoint.from_payload(events[-1].payload)
            previous = next(
                (
                    TrustCheckpoint.from_payload(event.payload)
                    for event in reversed(events[:-1])
                    if event.kind is WorldEventKind.TRAJECTORY_CHECKPOINT_RECORDED
                    and event.payload.get("graph_id") == checkpoint.graph_id
                ),
                None,
            )
            authority.validate_checkpoint(checkpoint, previous)

        return self._repository.append(
            draft,
            stream_validator=validate_stream,
        )

    def bind_trajectory_authority(
        self, authority: TrajectoryCheckpointAuthorityPort,
    ) -> None:
        """Bind the one append-time authority used by this World runtime."""

        if self._trajectory_authority is not None and self._trajectory_authority is not authority:
            raise PersonalWorldModelError("trajectory checkpoint authority is already bound")
        if not callable(getattr(authority, "validate_checkpoint", None)):
            raise PersonalWorldModelError("trajectory checkpoint authority is invalid")
        self._trajectory_authority = authority

    def project(
        self, project_id: str, *, now: str | None = None,
    ) -> ProjectWorldStateProjection:
        project = validate_world_identifier(project_id, "project id")
        observed_at = _world_time(now if now is not None else self._now())
        return self._dynamics.project(
            self._repository.list_project(project),
            project_id=project,
            now=observed_at,
        )

    def events(self, project_id: str):
        project = validate_world_identifier(project_id, "project id")
        return self._repository.list_project(project)

    def project_ids(self) -> tuple[str, ...]:
        """Return the durable World stream scopes used by bounded recovery."""

        return self._repository.project_ids()

    def event(self, event_id: str):
        return self._repository.get(validate_world_identifier(event_id, "event id"))

    def verify_turn_outcome(
        self,
        *,
        project_id: str,
        action_id: str,
        turn_id: str,
        outcome_ref: str,
        reported_outcome: OutcomeStatus | str,
    ) -> VerifiedTurnOutcome:
        if self._evidence is None:
            raise PersonalWorldModelError("Turn outcome evidence authority is unavailable")
        status = (
            reported_outcome
            if isinstance(reported_outcome, OutcomeStatus)
            else OutcomeStatus(reported_outcome)
        )
        return self._evidence.verify(
            project_id=project_id,
            action_id=action_id,
            turn_id=turn_id,
            outcome_ref=outcome_ref,
            reported_outcome=status,
        )

    def verified_feedback(
        self,
        *,
        project_id: str,
        feedback_id: str,
        turn_id: str,
    ) -> VerifiedWorldFeedback:
        if self._evidence is None:
            raise PersonalWorldModelError("Turn outcome evidence authority is unavailable")
        project = validate_world_identifier(project_id, "project id")
        feedback = validate_world_identifier(feedback_id, "feedback id")
        matches = tuple(
            event
            for event in self._repository.list_project(project)
            if event.kind.value == "feedback.recorded"
            and event.payload.get("feedback_id") == feedback
        )
        if len(matches) != 1:
            raise PersonalWorldModelError("verified FeedbackFact is unavailable")
        event = matches[0]
        fact = FeedbackFact.from_payload(project, event.payload)
        projection = self.project(project)
        latest_for_action = tuple(
            item for item in projection.feedback_facts if item.action_id == fact.action_id
        )
        if not latest_for_action or latest_for_action[-1].feedback_id != fact.feedback_id:
            raise PersonalWorldModelError("superseded FeedbackFact cannot produce learning")
        evidence = self.verify_turn_outcome(
            project_id=project,
            action_id=fact.action_id,
            turn_id=turn_id,
            outcome_ref=event.source_ref,
            reported_outcome=fact.outcome,
        )
        if (
            event.source_revision != str(evidence.terminal_sequence)
            or evidence.outcome_ref not in fact.evidence_refs
        ):
            raise PersonalWorldModelError("FeedbackFact evidence binding drifted")
        return VerifiedWorldFeedback(event, fact, evidence)

    def record_feedback(
        self,
        *,
        project_id: str,
        event_id: str,
        feedback_id: str,
        supersedes_feedback_id: str | None,
        action_id: str,
        turn_id: str,
        outcome_ref: str,
        expected_outcome: str,
        actual_outcome: str,
        outcome: OutcomeStatus | str,
        state_delta: Sequence[object],
        cost: object,
        user_evaluation: object,
        observed_evidence_refs: Sequence[object],
        actor: str,
        occurred_at: str,
        recorded_at: str,
    ) -> RecordedWorldFeedback:
        if self._evidence is None:
            raise PersonalWorldModelError("Turn outcome evidence authority is unavailable")
        status = outcome if isinstance(outcome, OutcomeStatus) else OutcomeStatus(outcome)
        current = self.project(project_id, now=recorded_at)
        planned = next(
            (item for item in current.planned_actions if item.action_id == action_id),
            None,
        )
        if planned is None:
            raise PersonalWorldModelError("feedback references an unknown planned action")
        if planned.expected_outcome != expected_outcome:
            raise PersonalWorldModelError("feedback expected outcome drifted from the plan")
        verified = self.verify_turn_outcome(
            project_id=project_id,
            action_id=action_id,
            turn_id=turn_id,
            outcome_ref=outcome_ref,
            reported_outcome=status,
        )
        if parse_world_timestamp(occurred_at) < parse_world_timestamp(
            verified.terminal_occurred_at
        ):
            raise PersonalWorldModelError("feedback cannot predate its terminal Turn evidence")
        observed_refs = tuple(
            validate_world_evidence_ref(item) for item in observed_evidence_refs
        )
        fact = FeedbackFact(
            feedback_id=feedback_id,
            supersedes_feedback_id=supersedes_feedback_id,
            project_id=project_id,
            action_id=action_id,
            expected_outcome=expected_outcome,
            actual_outcome=actual_outcome,
            outcome=status,
            state_delta=tuple(StateDelta.from_payload(item) for item in state_delta),
            cost=FeedbackCost.from_payload(cost),
            user_evaluation=UserEvaluation.from_payload(user_evaluation),
            evidence_refs=tuple(dict.fromkeys((verified.outcome_ref, *observed_refs))),
        )
        result = self.append_event(
            feedback_event_draft(
                fact,
                event_id=event_id,
                source_ref=verified.outcome_ref,
                source_revision=str(verified.terminal_sequence),
                occurred_at=occurred_at,
                recorded_at=recorded_at,
                actor=actor,
            )
        )
        return RecordedWorldFeedback(result, verified)


def _event_at_sequence(
    events: tuple[Mapping[str, object], ...], sequence: int,
) -> Mapping[str, object] | None:
    matches = [event for event in events if event.get("sequence") == sequence]
    return matches[0] if len(matches) == 1 else None


def _unique_outcome_event(
    events: tuple[Mapping[str, object], ...], outcome_ref: str,
) -> Mapping[str, object]:
    matches = []
    for event in events:
        data = event.get("data")
        if (
            event.get("type") == "tool.outcome.recorded"
            and isinstance(data, Mapping)
            and data.get("payload_ref") == outcome_ref
        ):
            matches.append(event)
    if len(matches) != 1:
        raise PersonalWorldModelError("Tool outcome Receipt is not bound to this Turn")
    return matches[0]


def _verify_tool_outcome_identity(
    outcome: ToolInvocationOutcome,
    *,
    turn_id: str,
    tool_call_id: str,
    capability_id: str,
) -> None:
    if (
        outcome.turn_id != turn_id
        or outcome.invocation_id != tool_call_id
        or outcome.capability_id != capability_id
    ):
        raise PersonalWorldModelError("Tool outcome Receipt identity drifted")


def _matching_tool_terminal(
    events: tuple[Mapping[str, object], ...], outcome: ToolInvocationOutcome,
) -> Mapping[str, object]:
    expected_type = _TERMINAL_TOOL_TYPES[outcome.status]
    matches = []
    for event in events:
        correlation = event.get("correlation")
        data = event.get("data")
        if (
            event.get("type") == expected_type
            and isinstance(correlation, Mapping)
            and correlation.get("tool_call_id") == outcome.invocation_id
            and isinstance(data, Mapping)
            and data.get("capability_id") == outcome.capability_id
        ):
            matches.append(event)
    if len(matches) != 1:
        raise PersonalWorldModelError("Tool terminal event drifted from its outcome Receipt")
    return matches[0]


def _verify_reported_outcome(
    reported: OutcomeStatus,
    tool: ToolInvocationOutcome,
    *,
    turn_status: str,
) -> None:
    if tool.effect_certainty == "unknown" and reported is not OutcomeStatus.UNKNOWN:
        raise PersonalWorldModelError("unconfirmed Effect can only produce unknown feedback")
    if reported in {OutcomeStatus.ACHIEVED, OutcomeStatus.PARTIAL} and (
        tool.status != "completed" or turn_status != "completed"
    ):
        raise PersonalWorldModelError("positive feedback requires a completed Tool and Turn")
    if reported is OutcomeStatus.FAILED and tool.status == "unknown_effect":
        raise PersonalWorldModelError("unknown Effect cannot be asserted as failed")


def _world_time(value: datetime | str) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise PersonalWorldModelError("world-model clock must be timezone-aware")
        value = value.astimezone(timezone.utc).isoformat(timespec="seconds")
    return parse_world_timestamp(value).isoformat(timespec="seconds").replace("+00:00", "Z")
