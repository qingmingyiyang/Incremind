from __future__ import annotations

import pytest

from backend.api.personal_world_model_workflow import PersonalWorldModelWorkflowError
from backend.api.world_action_agent_submitter import WorldActionAgentSubmitter
from core.ai_kernel import TurnReceipt


TURN = "world-turn-12345678-1234-1234-1234-123456789abc"
ACTION = "world-action-12345678-1234-1234-1234-123456789abc"
PROJECT = "project-workflow"


def _request() -> dict[str, object]:
    return {
        "turn_id": TURN,
        "session_id": "world-project",
        "operation_id": ACTION,
        "scope": {"kind": "project", "project_id": PROJECT},
    }


class _Turns:
    def receipt_for(self, turn_id: str, *, replayed: bool = False) -> TurnReceipt:
        assert turn_id == TURN
        return TurnReceipt(TURN, "world-project", ACTION, "accepted", 1, replayed)


class _Store:
    def __init__(self, request: dict[str, object] | None) -> None:
        self.request = request

    def get_request(self, turn_id: str):
        return self.request if turn_id == TURN else None


class _Organization:
    def __init__(self, *, main_turn_id: str = TURN, store: _Store | None = None) -> None:
        self.main_turn_id = main_turn_id
        self.store = store
        self.calls: list[tuple[dict[str, object], bool]] = []

    def start(self, request, *, agent_turn_mode: bool):
        self.calls.append((dict(request), agent_turn_mode))
        if self.store is not None:
            self.store.request = dict(request)
        return {"status": "accepted", "main": {"turn_id": self.main_turn_id}}


def _submitter(*, organization=None, store=None) -> WorldActionAgentSubmitter:
    selected_store = store or _Store(None)
    selected_organization = organization or _Organization(store=selected_store)
    return WorldActionAgentSubmitter(
        organization=selected_organization,
        turns=_Turns(),
        turn_store=selected_store,
    )


def test_world_action_agent_submitter_starts_organization_and_returns_canonical_receipt() -> None:
    store = _Store(None)
    organization = _Organization(store=store)

    receipt = _submitter(organization=organization, store=store).accept_and_submit(_request())

    assert receipt == TurnReceipt(TURN, "world-project", ACTION, "accepted", 1, False)
    assert organization.calls == [(_request(), True)]


def test_world_action_agent_submitter_replays_an_existing_bound_turn_without_restarting() -> None:
    organization = _Organization()

    receipt = _submitter(
        organization=organization,
        store=_Store(_request()),
    ).accept_and_submit(_request())

    assert receipt.status == "accepted"
    assert organization.calls == []


def test_world_action_agent_submitter_rejects_main_or_admitted_binding_drift() -> None:
    with pytest.raises(PersonalWorldModelWorkflowError, match="main Turn"):
        _submitter(organization=_Organization(main_turn_id="other-turn")).accept_and_submit(_request())

    drifted = _request()
    drifted["scope"] = {"kind": "project", "project_id": "project-other"}
    with pytest.raises(PersonalWorldModelWorkflowError, match="binding drifted"):
        _submitter(store=_Store(drifted)).accept_and_submit(_request())
