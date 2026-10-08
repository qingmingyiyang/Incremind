from __future__ import annotations

from collections.abc import Mapping, Sequence
from threading import RLock

from .contracts import AIKernelContractError, validate_event_transition
from .ports import RunLeaseToken


class TurnEventConflict(AIKernelContractError):
    pass


class RunLeaseRevoked(AIKernelContractError):
    def __init__(self) -> None:
        super().__init__("run lease revoked")


class InMemoryTurnEventStore:
    """Reference store for tests, not a concurrent atomic lease fence.

    Runtime performs its state assertion before using this store. Production
    cross-process fencing requires SQLite's transaction-bound lease check.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._events: dict[str, list[dict[str, object]]] = {}

    def append(self, event: Mapping[str, object], *, expected_sequence: int, run_lease: RunLeaseToken | None = None) -> dict[str, object]:
        turn_id = str(event.get("turn_id", ""))
        with self._lock:
            stream = self._events.setdefault(turn_id, [])
            if len(stream) != expected_sequence:
                raise TurnEventConflict("turn event expected sequence conflict")
            validated = validate_event_transition(stream[-1] if stream else None, event)
            stream.append(validated)
            return dict(validated)

    def events_after(self, turn_id: str, after_sequence: int = 0) -> Sequence[Mapping[str, object]]:
        with self._lock:
            return tuple(dict(event) for event in self._events.get(turn_id, ()) if int(event["sequence"]) > after_sequence)
