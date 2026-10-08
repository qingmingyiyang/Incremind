from __future__ import annotations

import pytest
from core.ai_kernel.turn_receipt_projection import receipt_from_events


def _event(sequence, event_type, status):
    return {
        "turn_id": "turn-a", "session_id": "session-a", "sequence": sequence,
        "type": event_type, "data": {"status": status},
    }


def test_receipt_projection_prefers_latest_terminal_event_over_later_nonterminal_event():
    receipt = receipt_from_events(
        (
            _event(1, "turn.accepted", "accepted"),
            _event(2, "turn.completed", "completed"),
            _event(3, "model.completed", "running"),
        ),
        turn_id="turn-a", request={"operation_id": "operation-a"}, replayed=True,
    )

    assert receipt.turn_id == "turn-a"
    assert receipt.session_id == "session-a"
    assert receipt.operation_id == "operation-a"
    assert receipt.status == "completed"
    assert receipt.current_sequence == 3
    assert receipt.replayed is True


@pytest.mark.parametrize("attempt_status", ["completed", "failed", "cancelled"])
def test_nested_attempt_terminal_does_not_finish_the_turn(attempt_status):
    receipt = receipt_from_events((
        _event(1, "turn.accepted", "accepted"),
        _event(2, "model.attempt.dispatched", "running"),
        _event(3, "model.attempt.terminal", attempt_status),
    ), turn_id="turn-a", request={"operation_id": "operation-a"})
    assert receipt.status == "running"
    assert receipt.current_sequence == 3


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_actual_turn_terminal_still_finishes_after_nested_attempt(status):
    receipt = receipt_from_events((
        _event(1, "model.attempt.dispatched", "running"),
        _event(2, "model.attempt.terminal", "completed"),
        _event(3, "turn." + status, status),
    ), turn_id="turn-a", request={"operation_id": "operation-a"})
    assert receipt.status == status
