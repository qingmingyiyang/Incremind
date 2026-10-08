"""Pure durable Turn receipt projection shared by read-only consumers."""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from .ports import TurnReceipt


def receipt_from_events(
    events: Sequence[Mapping[str, object]], *, turn_id: str,
    request: Mapping[str, object], replayed: bool = False,
) -> TurnReceipt:
    """Preserve the runtime's terminal-first durable receipt projection."""
    terminal_types = {"turn.completed", "turn.failed", "turn.cancelled"}
    terminal_index = next(
        (index for index in range(len(events) - 1, -1, -1)
         if events[index].get("type") in terminal_types),
        None,
    )
    # Nested model/tool attempts have their own terminal statuses.  They do
    # not finish the enclosing Turn, whose result may not be committed yet.
    # Keep the latest nonterminal status until an actual Turn terminal event.
    status_event = events[terminal_index] if terminal_index is not None else next(
        (event for event in reversed(events)
         if event["data"]["status"] not in {"completed", "failed", "cancelled"}),
        None,
    )
    status = str(status_event["data"]["status"]) if status_event is not None else "running"
    session_event = status_event if status_event is not None else events[-1]
    return TurnReceipt(
        turn_id, str(session_event["session_id"]), str(request["operation_id"]),
        status, len(events), replayed,
    )
