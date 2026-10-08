"""Bind a World action to the existing Agent organization entrypoint.

This adapter deliberately exposes the ordinary ``accept_and_submit`` shape so
the World workflow can keep its existing durable plan-before-admission and
retry behavior.  It does not create a receipt: the canonical receipt remains
owned by the existing AI runtime.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from backend.api.personal_world_model_workflow import PersonalWorldModelWorkflowError
from backend.api.workbench_ai_runtime import WORLD_PROJECT_SESSION_ID


class AgentOrganizationSubmitterPort(Protocol):
    def start(
        self, request: Mapping[str, object], *, agent_turn_mode: bool = False,
    ) -> Mapping[str, object]: ...


class TurnReceiptReaderPort(Protocol):
    def receipt_for(self, turn_id: str, *, replayed: bool = False) -> object: ...


class WorldActionAgentSubmitter:
    """Submit World actions through the governed main/steward organization."""

    def __init__(
        self,
        *,
        organization: AgentOrganizationSubmitterPort,
        turns: TurnReceiptReaderPort,
        turn_store: object,
    ) -> None:
        self._organization = organization
        self._turns = turns
        self._turn_store = turn_store

    def accept_and_submit(self, request: Mapping[str, object]) -> object:
        """Start the organization and return its already-persisted main receipt."""
        turn_id, operation_id, project_id = _request_binding(request)
        existing = _optional_request_for_turn(self._turn_store, turn_id)
        if existing is not None:
            _assert_same_binding(
                existing,
                turn_id=turn_id,
                operation_id=operation_id,
                project_id=project_id,
            )
            return _bound_receipt(
                self._turns,
                turn_id=turn_id,
                operation_id=operation_id,
            )
        started = self._organization.start(request, agent_turn_mode=True)
        if not isinstance(started, Mapping):
            raise PersonalWorldModelWorkflowError("Agent organization start is invalid")
        main = started.get("main")
        if not isinstance(main, Mapping) or main.get("turn_id") != turn_id:
            raise PersonalWorldModelWorkflowError(
                "Agent organization main Turn did not bind the planned action"
            )
        admitted = _request_for_turn(self._turn_store, turn_id)
        _assert_same_binding(
            admitted,
            turn_id=turn_id,
            operation_id=operation_id,
            project_id=project_id,
        )
        return _bound_receipt(
            self._turns,
            turn_id=turn_id,
            operation_id=operation_id,
        )


def _bound_receipt(
    turns: TurnReceiptReaderPort,
    *,
    turn_id: str,
    operation_id: str,
) -> object:
    receipt = turns.receipt_for(turn_id)
    if (
        getattr(receipt, "turn_id", None) != turn_id
        or getattr(receipt, "operation_id", None) != operation_id
        or not isinstance(getattr(receipt, "status", None), str)
    ):
        raise PersonalWorldModelWorkflowError(
            "Agent organization receipt did not bind the planned action"
        )
    return receipt


def _assert_same_binding(
    request: Mapping[str, object],
    *,
    turn_id: str,
    operation_id: str,
    project_id: str,
) -> None:
    admitted_turn, admitted_operation, admitted_project = _request_binding(request)
    if (
        admitted_turn != turn_id
        or admitted_operation != operation_id
        or admitted_project != project_id
    ):
        raise PersonalWorldModelWorkflowError(
            "Agent organization Turn binding drifted"
        )


def _optional_request_for_turn(
    store: object,
    turn_id: str,
) -> Mapping[str, object] | None:
    getter = getattr(store, "get_request", None)
    request = getter(turn_id) if callable(getter) else None
    if request is None:
        return None
    if not isinstance(request, Mapping):
        raise PersonalWorldModelWorkflowError("Agent organization Turn is unavailable")
    return request


def _request_for_turn(store: object, turn_id: str) -> Mapping[str, object]:
    getter = getattr(store, "get_request", None)
    request = getter(turn_id) if callable(getter) else None
    if not isinstance(request, Mapping):
        raise PersonalWorldModelWorkflowError("Agent organization Turn is unavailable")
    return request


def _request_binding(request: Mapping[str, object]) -> tuple[str, str, str]:
    turn_id = request.get("turn_id")
    operation_id = request.get("operation_id")
    scope = request.get("scope")
    project_id = scope.get("project_id") if isinstance(scope, Mapping) else None
    if (
        not isinstance(turn_id, str)
        or not turn_id
        or not isinstance(operation_id, str)
        or not operation_id
        or not isinstance(project_id, str)
        or not project_id
        or request.get("session_id") != WORLD_PROJECT_SESSION_ID
    ):
        raise PersonalWorldModelWorkflowError("Agent organization request binding is invalid")
    return turn_id, operation_id, project_id
