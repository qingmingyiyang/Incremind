from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from backend.api.ai_turn_runner import AITurnRunner
from backend.api.personal_world_model_runtime import PersonalWorldModelRuntime
from backend.api.personal_world_model_terminal_observer import (
    PersonalWorldModelTerminalObserver,
)
from core.ai_kernel import TurnReceipt
from core.ai_kernel.tool_invocation import ToolInvocationOutcome, outcome_to_payload
from core.personal_world_model import WorldEventDraft, WorldEventKind


PROJECT = "project-terminal"
ACTION = "operation-terminal"
TURN = "turn-terminal"
OUTCOME_REF = "crp://session/turn-terminal/tool-invocation-outcome/outcome-terminal"
ROOT = Path(__file__).resolve().parents[4]


class _TurnStore:
    def __init__(self, *, unknown_effect: bool = False) -> None:
        self.unknown_effect = unknown_effect
        status = "unknown_effect" if unknown_effect else "completed"
        self.outcome = outcome_to_payload(ToolInvocationOutcome(
            invocation_id="tool-call-terminal",
            turn_id=TURN,
            capability_id="project.write",
            attempt=1,
            status=status,
            effect_certainty="unknown" if unknown_effect else "confirmed_applied",
            payload_ref=None,
            receipt_ref=(
                None
                if unknown_effect
                else "crp://receipts/project-terminal-write"
            ),
            evidence_refs=(),
            error_code="tool.effect_unknown" if unknown_effect else None,
            retryable=False,
        ))

    def get_request(self, turn_id: str):
        return (
            {"scope": {"kind": "project", "project_id": PROJECT}}
            if turn_id == TURN
            else None
        )

    def events_after(self, turn_id: str, after_sequence: int = 0):
        if turn_id != TURN:
            return ()
        terminal_tool = "tool.failed" if self.unknown_effect else "tool.completed"
        terminal_turn = "turn.failed" if self.unknown_effect else "turn.completed"
        events = (
            {
                "sequence": 1,
                "type": "turn.accepted",
                "data": {},
                "correlation": {},
                "occurred_at": "2026-09-01T09:00:00Z",
            },
            {
                "sequence": 2,
                "type": "tool.outcome.recorded",
                "data": {
                    "payload_ref": OUTCOME_REF,
                    "capability_id": "project.write",
                },
                "correlation": {"tool_call_id": "tool-call-terminal"},
                "occurred_at": "2026-09-01T09:00:01Z",
            },
            {
                "sequence": 3,
                "type": terminal_tool,
                "data": {"capability_id": "project.write"},
                "correlation": {"tool_call_id": "tool-call-terminal"},
                "occurred_at": "2026-09-01T09:00:02Z",
            },
            {
                "sequence": 4,
                "type": terminal_turn,
                "data": {},
                "correlation": {"operation_id": ACTION},
                "occurred_at": "2026-09-01T09:00:03Z",
            },
        )
        return tuple(item for item in events if int(item["sequence"]) > after_sequence)

    def get(self, payload_ref: str):
        if payload_ref != OUTCOME_REF:
            raise KeyError(payload_ref)
        return dict(self.outcome)


def _seed_action(root: Path) -> None:
    runtime = PersonalWorldModelRuntime.for_root(root)
    runtime.append_event(WorldEventDraft(
        event_id="plan-terminal",
        project_id=PROJECT,
        kind=WorldEventKind.ACTION_PLANNED,
        actor="user",
        source_ref="crp://plans/project-terminal/operation-terminal",
        source_revision="1",
        occurred_at="2026-09-01T08:59:00Z",
        recorded_at="2026-09-01T08:59:00Z",
        payload={
            "action_id": ACTION,
            "title": "Apply one governed project change",
            "expected_outcome": "The governed project artifact changes",
            "effect_class": "QUERYABLE",
            "gate_requirement": "approval",
            "due_at": None,
            "evidence_refs": ["crp://plans/project-terminal/operation-terminal"],
        },
    ))


def _receipt(*, failed: bool = False) -> TurnReceipt:
    return TurnReceipt(
        TURN,
        "session-terminal",
        ACTION,
        "failed" if failed else "completed",
        4,
        False,
    )


def test_terminal_observer_records_only_evaluation_pending_and_replays(
    tmp_path: Path,
) -> None:
    _seed_action(tmp_path)
    store = _TurnStore()
    observer = PersonalWorldModelTerminalObserver(
        root_dir=tmp_path,
        turn_store=store,
        now=lambda: datetime(2026, 9, 1, 9, 1, tzinfo=timezone.utc),
    )

    created = observer(_receipt())
    replayed = observer(_receipt())
    projection = PersonalWorldModelRuntime.for_root(tmp_path).project(
        PROJECT, now="2026-09-01T09:01:00Z",
    )

    assert created.status == "observed" and created.replayed is False
    assert replayed.status == "observed" and replayed.replayed is True
    assert projection.latest_feedback is None
    assert projection.pending_action_ids == (ACTION,)
    assert projection.observations[-1].evidence_refs == (OUTCOME_REF,)
    assert "awaits explicit evaluation" in projection.observations[-1].summary


def test_unknown_effect_terminal_observation_never_asserts_failure_or_success(
    tmp_path: Path,
) -> None:
    _seed_action(tmp_path)
    observer = PersonalWorldModelTerminalObserver(
        root_dir=tmp_path,
        turn_store=_TurnStore(unknown_effect=True),
        now=lambda: datetime(2026, 9, 1, 9, 1, tzinfo=timezone.utc),
    )

    outcome = observer(_receipt(failed=True))
    projection = PersonalWorldModelRuntime.for_root(tmp_path).project(
        PROJECT, now="2026-09-01T09:01:00Z",
    )

    assert outcome.status == "observed"
    assert projection.latest_feedback is None
    assert projection.pending_action_ids == (ACTION,)


def test_terminal_observer_skips_turn_without_a_planned_world_action(
    tmp_path: Path,
) -> None:
    outcome = PersonalWorldModelTerminalObserver(
        root_dir=tmp_path,
        turn_store=_TurnStore(),
    )(_receipt())

    assert outcome.status == "skipped"
    assert outcome.reason == "planned_world_action_unavailable"


def test_runner_keeps_terminal_receipt_when_observer_fails() -> None:
    observed: list[TurnReceipt] = []

    class Runtime:
        def accept_turn(self, request):
            return TurnReceipt(
                str(request["turn_id"]),
                str(request["session_id"]),
                str(request["operation_id"]),
                "completed",
                4,
                False,
            )

    def failing_observer(receipt: TurnReceipt) -> None:
        observed.append(receipt)
        raise RuntimeError("derived observer failed")

    runner = AITurnRunner(Runtime(), max_workers=1, terminal_observer=failing_observer)
    request = json.loads(
        (ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(
            encoding="utf-8"
        )
    )
    receipt = runner.accept_and_submit(request)
    runner.shutdown()

    assert receipt.status == "completed"
    assert observed == [receipt]
