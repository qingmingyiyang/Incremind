from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.api.personal_world_model_runtime import PersonalWorldModelRuntime
from core.ai_kernel.tool_invocation import ToolInvocationOutcome, outcome_to_payload
from core.personal_world_model import (
    PersonalWorldModelError,
    WorldEventDraft,
    WorldEventKind,
)


PROJECT = "project-alpha"
ACTION = "operation-alpha"
TURN = "turn-alpha"
OUTCOME_REF = "crp://session/turn-alpha/tool-invocation-outcome/outcome-alpha"
NOW = "2026-09-01T10:00:00Z"


@dataclass(frozen=True)
class _Receipt:
    turn_id: str = TURN
    operation_id: str = ACTION
    status: str = "completed"
    current_sequence: int = 4


class _TurnAuthority:
    def __init__(
        self,
        *,
        project_id: str = PROJECT,
        operation_id: str = ACTION,
        tool_status: str = "completed",
        effect_certainty: str = "confirmed_applied",
        turn_status: str = "completed",
    ) -> None:
        self.project_id = project_id
        self.operation_id = operation_id
        self.tool_status = tool_status
        self.effect_certainty = effect_certainty
        self.turn_status = turn_status
        self.outcome_ref = OUTCOME_REF
        self.outcome = outcome_to_payload(
            ToolInvocationOutcome(
                invocation_id="tool-call-alpha",
                turn_id=TURN,
                capability_id="project.write",
                attempt=1,
                status=tool_status,
                effect_certainty=effect_certainty,
                payload_ref=None,
                receipt_ref="crp://receipts/project-write-alpha" if tool_status == "completed" else None,
                evidence_refs=(),
                error_code=None if tool_status == "completed" else "tool.failed",
                retryable=False,
            )
        )

    def get_request(self, turn_id: str):
        if turn_id != TURN:
            return None
        return {"scope": {"kind": "project", "project_id": self.project_id}}

    def receipt_for(self, turn_id: str, *, replayed: bool = False):
        if turn_id != TURN:
            raise KeyError(turn_id)
        return _Receipt(operation_id=self.operation_id, status=self.turn_status)

    def events_after(self, turn_id: str, after_sequence: int = 0):
        terminal_tool = {
            "completed": "tool.completed",
            "failed": "tool.failed",
            "timed_out": "tool.failed",
            "cancelled": "tool.cancelled",
            "unknown_effect": "tool.failed",
        }[self.tool_status]
        terminal_turn = {
            "completed": "turn.completed",
            "failed": "turn.failed",
            "cancelled": "turn.cancelled",
        }[self.turn_status]
        events = (
            {
                "sequence": 1,
                "type": "turn.accepted",
                "data": {},
                "correlation": {},
                "occurred_at": "2026-09-01T09:59:55Z",
            },
            {
                "sequence": 2,
                "type": "tool.outcome.recorded",
                "data": {
                    "payload_ref": self.outcome_ref,
                    "capability_id": "project.write",
                },
                "correlation": {"tool_call_id": "tool-call-alpha"},
                "occurred_at": "2026-09-01T09:59:57Z",
            },
            {
                "sequence": 3,
                "type": terminal_tool,
                "data": {"capability_id": "project.write"},
                "correlation": {"tool_call_id": "tool-call-alpha"},
                "occurred_at": "2026-09-01T09:59:58Z",
            },
            {
                "sequence": 4,
                "type": terminal_turn,
                "data": {"status": self.turn_status},
                "correlation": {"operation_id": self.operation_id},
                "occurred_at": "2026-09-01T09:59:59Z",
            },
        )
        return tuple(event for event in events if int(event["sequence"]) > after_sequence)

    def get(self, payload_ref: str):
        if payload_ref != self.outcome_ref:
            raise KeyError(payload_ref)
        return dict(self.outcome)


def _runtime(tmp_path: Path, authority: _TurnAuthority) -> PersonalWorldModelRuntime:
    runtime = PersonalWorldModelRuntime.for_root(
        tmp_path,
        receipts=authority,
        turn_store=authority,
        now=lambda: datetime(2026, 9, 1, 10, tzinfo=timezone.utc),
    )
    runtime.append_event(_event("goal-alpha", WorldEventKind.GOAL_DECLARED, {
        "goal_id": "goal-alpha",
        "title": "Complete the governed project slice",
        "success_criteria": ["A verified outcome changes the next projection"],
        "target_at": None,
        "evidence_refs": ["crp://projects/project-alpha/goals/goal-alpha"],
    }))
    runtime.append_event(_event("plan-alpha", WorldEventKind.ACTION_PLANNED, {
        "action_id": ACTION,
        "title": "Apply one governed project change",
        "expected_outcome": "The project artifact is updated",
        "effect_class": "QUERYABLE",
        "gate_requirement": "approval",
        "due_at": None,
        "evidence_refs": ["crp://plans/project-alpha/operation-alpha"],
    }))
    return runtime


def _event(event_id: str, kind: WorldEventKind, payload: dict[str, object]) -> WorldEventDraft:
    return WorldEventDraft(
        event_id=event_id,
        project_id=PROJECT,
        kind=kind,
        actor="user",
        source_ref=f"crp://world-events/{event_id}",
        source_revision="1",
        occurred_at=NOW,
        recorded_at=NOW,
        payload=payload,
    )


def _record(runtime: PersonalWorldModelRuntime, **overrides: object):
    values = {
        "project_id": PROJECT,
        "event_id": "feedback-event-alpha",
        "feedback_id": "feedback-alpha",
        "supersedes_feedback_id": None,
        "action_id": ACTION,
        "turn_id": TURN,
        "outcome_ref": OUTCOME_REF,
        "expected_outcome": "The project artifact is updated",
        "actual_outcome": "The artifact update was verified",
        "outcome": "achieved",
        "state_delta": [{"field": "artifact.status", "before": "stale", "after": "updated"}],
        "cost": {
            "elapsed_ms": 1200,
            "model_input_tokens": 30,
            "model_output_tokens": 12,
            "external_calls": 1,
            "human_attention_seconds": 4,
        },
        "user_evaluation": {"verdict": "accepted", "rating": 5, "note": "Verified"},
        "observed_evidence_refs": ["crp://artifacts/project-alpha/revision-2"],
        "actor": "user",
        "occurred_at": NOW,
        "recorded_at": NOW,
    }
    values.update(overrides)
    return runtime.record_feedback(**values)


def test_verified_feedback_rebuilds_after_restart_and_replays_exactly(tmp_path: Path) -> None:
    authority = _TurnAuthority()
    runtime = _runtime(tmp_path, authority)

    recorded = _record(runtime)
    replayed = _record(runtime)
    restarted = PersonalWorldModelRuntime.for_root(tmp_path).project(PROJECT, now=NOW)

    assert recorded.append.replayed is False
    assert replayed.append.replayed is True
    assert recorded.evidence.effect_certainty == "confirmed_applied"
    assert restarted.phase == "in_progress"
    assert restarted.latest_feedback is not None
    assert restarted.latest_feedback.evidence_refs == (
        OUTCOME_REF,
        "crp://artifacts/project-alpha/revision-2",
    )
    assert restarted.persisted_as_authority is False


@pytest.mark.parametrize(
    ("authority", "overrides", "message"),
    [
        (_TurnAuthority(project_id="project-other"), {}, "project authority"),
        (_TurnAuthority(operation_id="operation-other"), {}, "planned action"),
        (_TurnAuthority(), {"outcome_ref": "crp://session/turn-alpha/tool-invocation-outcome/other"}, "not bound"),
    ],
)
def test_feedback_fails_closed_on_project_action_or_receipt_drift(
    tmp_path: Path,
    authority: _TurnAuthority,
    overrides: dict[str, object],
    message: str,
) -> None:
    runtime = _runtime(tmp_path, authority)

    with pytest.raises(PersonalWorldModelError, match=message):
        _record(runtime, **overrides)


def test_unknown_effect_can_only_be_recorded_as_unknown(tmp_path: Path) -> None:
    authority = _TurnAuthority(
        tool_status="unknown_effect",
        effect_certainty="unknown",
        turn_status="failed",
    )
    runtime = _runtime(tmp_path, authority)

    with pytest.raises(PersonalWorldModelError, match="only produce unknown"):
        _record(runtime)

    recorded = _record(
        runtime,
        outcome="unknown",
        actual_outcome="The applied state cannot be confirmed",
        event_id="feedback-event-unknown",
        feedback_id="feedback-unknown",
        user_evaluation={"verdict": "not_provided", "rating": None, "note": None},
    )
    assert recorded.append.event.payload["outcome"] == "unknown"


def test_feedback_correction_must_supersede_latest_fact(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, _TurnAuthority())
    _record(runtime)

    with pytest.raises(PersonalWorldModelError, match="does not supersede"):
        _record(
            runtime,
            event_id="feedback-event-correction-bad",
            feedback_id="feedback-correction-bad",
            outcome="partial",
        )

    corrected = _record(
        runtime,
        event_id="feedback-event-correction",
        feedback_id="feedback-correction",
        supersedes_feedback_id="feedback-alpha",
        actual_outcome="The artifact changed but one acceptance check remains",
        outcome="partial",
        state_delta=[{"field": "acceptance.status", "before": "assumed", "after": "pending"}],
        user_evaluation={"verdict": "corrected", "rating": 3, "note": "One check remains"},
    )
    projection = runtime.project(PROJECT, now=NOW)
    assert corrected.append.replayed is False
    assert projection.latest_feedback is not None
    assert projection.latest_feedback.feedback_id == "feedback-correction"
    assert projection.phase == "needs_revision"

    with pytest.raises(PersonalWorldModelError, match="superseded FeedbackFact"):
        runtime.verified_feedback(
            project_id=PROJECT,
            feedback_id="feedback-alpha",
            turn_id=TURN,
        )
    verified = runtime.verified_feedback(
        project_id=PROJECT,
        feedback_id="feedback-correction",
        turn_id=TURN,
    )
    assert verified.event.event_id == "feedback-event-correction"
    assert verified.evidence.outcome_ref == OUTCOME_REF
