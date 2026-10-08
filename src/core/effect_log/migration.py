from __future__ import annotations

from collections.abc import Iterable

from .core import EffectIntent, EffectLeaseFence, EffectLog, EffectState


def backfill_interrupted_effects(
    effects: EffectLog,
    intents: Iterable[EffectIntent],
    *,
    now: int,
    lease_owner: str,
) -> tuple[str, ...]:
    """Backfill legacy started operations without executing domain work."""
    operation_ids: list[str] = []
    for intent in intents:
        effect, _created = effects.plan(intent, now=now - 2)
        if effect.state is EffectState.PLANNED:
            effects.transition(
                effect.operation_id, expected=EffectState.PLANNED,
                target=EffectState.INFLIGHT, now=now - 2,
                lease_owner=lease_owner, lease_expires_at=now - 1,
                increment_attempt=True,
            )
        operation_ids.append(effect.operation_id)
    return tuple(operation_ids)


def is_effect_planned(effects: EffectLog, operation_id: str) -> bool:
    try:
        return effects.get(operation_id).state is EffectState.PLANNED
    except KeyError:
        return False


def claim_planned_effect(
    effects: EffectLog, operation_id: str, *, now: int,
    lease_owner: str, lease_seconds: int = 30,
) -> None:
    effects.transition(
        operation_id, expected=EffectState.PLANNED,
        target=EffectState.INFLIGHT, now=now,
        lease_owner=lease_owner, lease_expires_at=now + lease_seconds,
        increment_attempt=True,
    )


def settle_effect_receipt(
    effects: EffectLog, operation_id: str, *, receipt_ref: str,
    receipt_kind: str, now: int,
) -> None:
    inflight = effects.get(operation_id)
    effects.settle_ok_with_receipt(
        operation_id, expected=EffectState.INFLIGHT,
        receipt_ref=receipt_ref, receipt_kind=receipt_kind, now=now,
        fence=EffectLeaseFence.from_effect(inflight),
    )
