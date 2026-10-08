"""Best-effort projection of terminal AI Turn evidence into WorldEvent.

The observer runs after the AI Kernel has committed a terminal Turn receipt.
It records only that an outcome is ready for evaluation.  A completed Turn or
Tool receipt is never treated as proof that the user's project goal succeeded.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from backend.api.personal_world_model_runtime import PersonalWorldModelRuntime
from core.ai_kernel import TurnReceipt
from core.personal_world_model import (
    OutcomeStatus,
    PersonalWorldModelError,
    WorldEvent,
    WorldEventDraft,
    WorldEventKind,
    parse_world_timestamp,
    validate_world_identifier,
)


_TERMINAL = frozenset({"completed", "failed", "cancelled"})


@dataclass(frozen=True, slots=True)
class TerminalWorldObservationOutcome:
    status: str
    event_id: str | None = None
    replayed: bool = False
    reason: str | None = None


class _TerminalReceiptReader:
    """Bind evidence verification to the exact receipt being observed."""

    def __init__(self, receipt: TurnReceipt, turn_store: object) -> None:
        self._receipt = receipt
        self._turn_store = turn_store

    def receipt_for(self, turn_id: str, *, replayed: bool = False) -> TurnReceipt:
        if turn_id != self._receipt.turn_id:
            raise KeyError(turn_id)
        return TurnReceipt(
            self._receipt.turn_id,
            self._receipt.session_id,
            self._receipt.operation_id,
            self._receipt.status,
            self._receipt.current_sequence,
            replayed,
        )

    def events_after(
        self, turn_id: str, after_sequence: int = 0,
    ) -> Iterable[Mapping[str, object]]:
        if turn_id != self._receipt.turn_id:
            return ()
        events_after = getattr(self._turn_store, "events_after", None)
        if not callable(events_after):
            return ()
        return events_after(turn_id, after_sequence)


class PersonalWorldModelTerminalObserver:
    """Append one idempotent evaluation-pending observation per governed Turn."""

    def __init__(
        self,
        *,
        root_dir: Path,
        turn_store: object,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._root_dir = Path(root_dir).expanduser().resolve(strict=False)
        self._turn_store = turn_store
        self._now = now or (lambda: datetime.now(timezone.utc))

    def __call__(self, receipt: TurnReceipt) -> TerminalWorldObservationOutcome:
        try:
            return self._observe(receipt)
        except Exception:
            # Learning and projection are downstream of the terminal Turn.  A
            # projection fault must never rewrite or weaken that authority.
            return TerminalWorldObservationOutcome(
                "failed", reason="terminal_observation_failed",
            )

    def _observe(self, receipt: TurnReceipt) -> TerminalWorldObservationOutcome:
        if not isinstance(receipt, TurnReceipt) or receipt.status not in _TERMINAL:
            return TerminalWorldObservationOutcome(
                "skipped", reason="turn_not_terminal",
            )
        get_request = getattr(self._turn_store, "get_request", None)
        request = get_request(receipt.turn_id) if callable(get_request) else None
        scope = request.get("scope") if isinstance(request, Mapping) else None
        project_id = scope.get("project_id") if isinstance(scope, Mapping) else None
        project = validate_world_identifier(project_id, "project id")

        reader = _TerminalReceiptReader(receipt, self._turn_store)
        runtime = PersonalWorldModelRuntime.for_root(
            self._root_dir,
            receipts=reader,
            turn_store=self._turn_store,
            now=self._now,
        )
        projection = runtime.project(project)
        if not any(
            action.action_id == receipt.operation_id
            for action in projection.planned_actions
        ):
            return TerminalWorldObservationOutcome(
                "skipped", reason="planned_world_action_unavailable",
            )

        outcome_ref = _latest_outcome_ref(reader.events_after(receipt.turn_id))
        if outcome_ref is None:
            return TerminalWorldObservationOutcome(
                "skipped", reason="tool_outcome_unavailable",
            )
        verified = runtime.verify_turn_outcome(
            project_id=project,
            action_id=receipt.operation_id,
            turn_id=receipt.turn_id,
            outcome_ref=outcome_ref,
            reported_outcome=OutcomeStatus.UNKNOWN,
        )
        event_id = validate_world_identifier(receipt.turn_id, "terminal observation id")
        existing = runtime.event(event_id)
        if existing is not None:
            _assert_existing_observation(
                existing,
                project_id=project,
                outcome_ref=verified.outcome_ref,
                terminal_sequence=verified.terminal_sequence,
                terminal_occurred_at=verified.terminal_occurred_at,
            )
            return TerminalWorldObservationOutcome(
                "observed", event_id=event_id, replayed=True,
            )

        project_events = runtime.events(project)
        recorded_at = _recorded_at(
            self._now(),
            terminal_occurred_at=verified.terminal_occurred_at,
            latest_world_recorded_at=project_events[-1].recorded_at,
        )
        appended = runtime.append_event(WorldEventDraft(
            event_id=event_id,
            project_id=project,
            kind=WorldEventKind.PROJECT_OBSERVATION,
            actor="effect",
            source_ref=verified.outcome_ref,
            source_revision=str(verified.terminal_sequence),
            occurred_at=verified.terminal_occurred_at,
            recorded_at=recorded_at,
            payload={
                "observation_id": event_id,
                "category": "environment",
                "summary": "AI Turn reached a terminal receipt; project outcome awaits explicit evaluation.",
                "evidence_refs": [verified.outcome_ref],
            },
        ))
        return TerminalWorldObservationOutcome(
            "observed", event_id=event_id, replayed=appended.replayed,
        )


def _latest_outcome_ref(
    events: Iterable[Mapping[str, object]],
) -> str | None:
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


def _assert_existing_observation(
    event: WorldEvent,
    *,
    project_id: str,
    outcome_ref: str,
    terminal_sequence: int,
    terminal_occurred_at: str,
) -> None:
    if (
        event.project_id != project_id
        or event.kind is not WorldEventKind.PROJECT_OBSERVATION
        or event.actor != "effect"
        or event.source_ref != outcome_ref
        or event.source_revision != str(terminal_sequence)
        or event.occurred_at != terminal_occurred_at
        or event.payload.get("observation_id") != event.event_id
        or event.payload.get("evidence_refs") != [outcome_ref]
    ):
        raise PersonalWorldModelError("terminal observation identity conflicts")


def _recorded_at(
    now: datetime,
    *,
    terminal_occurred_at: str,
    latest_world_recorded_at: str,
) -> str:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise PersonalWorldModelError("terminal observer clock must be timezone-aware")
    latest = max(
        now.astimezone(timezone.utc),
        parse_world_timestamp(terminal_occurred_at),
        parse_world_timestamp(latest_world_recorded_at),
    )
    return latest.isoformat(timespec="seconds").replace("+00:00", "Z")
