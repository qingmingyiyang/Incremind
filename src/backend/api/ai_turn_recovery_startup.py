from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from core.ai_kernel import classify_recovery


def scan_due_ai_turn_recovery(
    store: object,
    *,
    limit: int = 256,
    clock: Callable[[], datetime] | None = None,
) -> int:
    """Classify due leases only; this path never takes over or invokes tools."""
    try:
        now = clock or (lambda: datetime.now(timezone.utc))
        claimed = store.claim_due_run_leases(now=now(), limit=limit)
    except Exception:
        return 0
    recorded = 0
    for lease in claimed:
        try:
            events = tuple(store.events_after(lease.token.turn_id))
            decision = classify_recovery(
                lease.token.turn_id,
                lease.token.generation,
                events,
                payload_loader=store.get,
            )
            if store.record_recovery_decision(decision, observed_at=now()):
                recorded += 1
        except Exception:
            continue
    return recorded
