"""Read-only World Action task projection without runtime composition."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from backend.api.personal_world_model_runtime import PersonalWorldModelRuntime
from backend.api.lightworld_action_contract import WORLD_PROJECT_SESSION_ID
from core.ai_kernel.contracts import AIKernelContractError, validate_turn_request
from core.ai_kernel.turn_receipt_projection import receipt_from_events
from core.personal_world_model import validate_world_identifier


_TERMINAL = frozenset({"completed", "failed", "cancelled"})
_STATUSES = frozenset({"accepted", "running", "waiting_approval", *_TERMINAL})


class TaskWorldActionReaderError(ValueError):
    pass


class TaskWorldActionReader:
    """Expose only World Action ownership/status required by TaskReference."""

    def __init__(self, *, root_dir: Path, turn_store: object) -> None:
        if not callable(getattr(turn_store, "get_request", None)) or not callable(getattr(turn_store, "events_after", None)):
            raise TaskWorldActionReaderError("durable Turn read store is unavailable")
        self._world = PersonalWorldModelRuntime.for_root(root_dir)
        self._turns = turn_store

    def overview(self, *, project_id: str) -> dict[str, object]:
        project = validate_world_identifier(project_id, "project id")
        return {"state": self._world.project(project).to_payload()}

    def action_status(self, *, project_id: str, turn_id: str) -> dict[str, object]:
        project = validate_world_identifier(project_id, "project id")
        turn = validate_world_identifier(turn_id, "turn id")
        action_id = _action_id_for_turn(turn)
        request = self._turns.get_request(turn)
        if request is None:
            return {"turn_id": turn, "action_id": action_id, "status": "not_admitted"}
        if not isinstance(request, Mapping):
            raise TaskWorldActionReaderError("durable Turn request is invalid")
        try:
            frozen = validate_turn_request(request)
        except AIKernelContractError as error:
            raise TaskWorldActionReaderError("durable Turn request is invalid") from error
        scope = frozen["scope"]
        if (
            frozen.get("turn_id") != turn
            or scope.get("kind") != "project" or scope.get("project_id") != project
            or frozen.get("session_id") != WORLD_PROJECT_SESSION_ID
            or frozen.get("operation_id") != action_id
        ):
            raise TaskWorldActionReaderError("World Action Turn binding drifted")
        state = self._world.project(project)
        if not any(item.action_id == action_id for item in state.planned_actions):
            raise TaskWorldActionReaderError("World Action plan binding drifted")
        events = tuple(self._turns.events_after(turn))
        if not events:
            raise TaskWorldActionReaderError("World Action receipt is unavailable")
        try:
            _validate_event_bindings(events, turn_id=turn, operation_id=action_id)
            receipt = receipt_from_events(events, turn_id=turn, request=frozen)
        except (AttributeError, KeyError, TypeError, IndexError) as error:
            raise TaskWorldActionReaderError("World Action receipt is invalid") from error
        if receipt.session_id != WORLD_PROJECT_SESSION_ID or receipt.operation_id != action_id or receipt.status not in _STATUSES:
            raise TaskWorldActionReaderError("World Action receipt binding drifted")
        feedback = any(item.action_id == action_id for item in state.feedback_facts)
        return {
            "turn_id": turn,
            "action_id": action_id,
            "status": receipt.status,
            "terminal": receipt.status in _TERMINAL,
            "ready_for_feedback": receipt.status in _TERMINAL and not feedback and _latest_outcome_ref(events) is not None,
        }


def _action_id_for_turn(turn_id: str) -> str:
    prefix = "world-turn-"
    if not turn_id.startswith(prefix) or not turn_id.removeprefix(prefix):
        raise TaskWorldActionReaderError("World Action Turn identity is invalid")
    return f"world-action-{turn_id.removeprefix(prefix)}"


def _latest_outcome_ref(events: tuple[Mapping[str, object], ...]) -> str | None:
    values: list[tuple[int, str]] = []
    for event in events:
        data, sequence = event.get("data"), event.get("sequence")
        payload_ref = data.get("payload_ref") if isinstance(data, Mapping) else None
        if event.get("type") == "tool.outcome.recorded" and isinstance(sequence, int) and not isinstance(sequence, bool) and isinstance(payload_ref, str):
            values.append((sequence, payload_ref))
    return max(values)[1] if values else None


def _validate_event_bindings(
    events: tuple[Mapping[str, object], ...], *, turn_id: str, operation_id: str,
) -> None:
    for event in events:
        if not isinstance(event, Mapping):
            raise TypeError("durable Turn event is invalid")
        correlation = event.get("correlation")
        if (
            event.get("turn_id") != turn_id
            or event.get("session_id") != WORLD_PROJECT_SESSION_ID
            or not isinstance(correlation, Mapping)
            or correlation.get("operation_id") != operation_id
        ):
            raise TaskWorldActionReaderError("World Action event binding drifted")
