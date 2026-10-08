"""Bounded startup delivery for external-Agent publication changes.

The structured publication store and the AI Turn store do not share a
transaction.  The outbox keeps a privacy-safe, replayable handoff until this
small startup pass can deliver it.  It is intentionally one-shot and bounded:
ordinary request handling must never depend on outbox recovery.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
import time

from core.ai_kernel import SQLiteAITurnStore
from core.effect_log import (
    EffectClass, EffectHandlerAbandoned, EffectHandlerRegistration, EffectIntent, EffectLog, EffectRunner,
    backfill_interrupted_effects, is_effect_planned,
)
from core.aggregate_repository_factory import STRUCTURED_DATABASE_NAME
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore
from backend.security.user_context import json_attribution
from backend.security.audited_records import audit_records
from core.storage_provider.external_agent_publication_change import (
    ExternalAgentPublicationChangeOutbox,
    PublicationChangeDelivery,
)
from core.storage_provider.external_agent_publication_backfill import (
    PublicationOutboxBackfillResult,
    backfill_external_agent_publication_outbox,
)
from core.storage_provider.memory_invalidation_outbox import (
    MemoryInvalidationDispatchResult,
    dispatch_memory_invalidations,
)


DEFAULT_EXTERNAL_AGENT_PUBLICATION_CHANGE_RECOVERY_LIMIT = 100
LOGGER = logging.getLogger(__name__)


def register_external_agent_publication_handler(root: Path, effect_runtime) -> None:
    outbox = _outbox(root)

    def handle(effect) -> str:
        matches = tuple(
            record
            for record in outbox.pending(limit=128)
            if _effect_intent(record).operation_id == effect.operation_id
        )
        if len(matches) != 1:
            raise EffectHandlerAbandoned("external_agent_publication.outbox_identity_invalid")
        receipt = outbox.deliver(matches[0])
        return (
            f"crp://external-agent-publication/{receipt.project_id}/"
            f"{receipt.publication_identity}:cursor-{receipt.change_cursor}"
        )

    effect_runtime.handlers.register(EffectHandlerRegistration(
        kind="external_agent_publication",
        effect_class=EffectClass.IDEMPOTENT,
        handler=handle,
    ))


def backfill_external_agent_publication_effects(
    root: Path, effects: EffectLog, *,
    limit: int = DEFAULT_EXTERNAL_AGENT_PUBLICATION_CHANGE_RECOVERY_LIMIT,
) -> tuple[str, ...]:
    outbox = _outbox(root)
    intents = tuple(_effect_intent(record) for record in outbox.pending(limit=limit))
    return backfill_interrupted_effects(
        effects, intents, now=int(time.time()),
        lease_owner="legacy-external-agent-publication",
    )


def dispatch_external_agent_publication_effects(
    root: Path, runner: EffectRunner, *,
    limit: int = DEFAULT_EXTERNAL_AGENT_PUBLICATION_CHANGE_RECOVERY_LIMIT,
) -> tuple[PublicationChangeDelivery, ...]:
    outbox = _outbox(root)
    effects = runner.log
    delivered: list[PublicationChangeDelivery] = []
    for record in outbox.pending(limit=limit):
        intent = _effect_intent(record)
        if not is_effect_planned(effects, intent.operation_id):
            continue
        delivered_receipt: PublicationChangeDelivery | None = None

        def deliver(_effect):
            nonlocal delivered_receipt
            delivered_receipt = outbox.deliver(record)
            receipt = delivered_receipt
            return (
                f"crp://external-agent-publication/{receipt.project_id}/"
                f"{receipt.publication_identity}:cursor-{receipt.change_cursor}"
            )

        runner.execute_planned(
            intent.operation_id,
            deliver,
            now=int(time.time()),
            receipt_kind="external-agent-publication-receipt",
        )
        assert delivered_receipt is not None
        delivered.append(delivered_receipt)
    return tuple(delivered)


def _outbox(root: Path) -> ExternalAgentPublicationChangeOutbox:
    root = Path(root)
    return ExternalAgentPublicationChangeOutbox(
        audit_records(SQLiteStructuredRecordStore(root / ".rebuild-data" / STRUCTURED_DATABASE_NAME),root,'default'),
        SQLiteAITurnStore(root / ".rebuild-data" / "ai-turns.sqlite3"),
        now=lambda: datetime.now(timezone.utc),
    )


def _effect_intent(record) -> EffectIntent:
    event = record.payload.get("event")
    if not isinstance(event, dict):
        raise ValueError("publication outbox event is invalid")
    return EffectIntent(
        session_id=f"external-agent-publication:{event['project_id']}",
        root_id=str(event["publication_identity"]), step_key="deliver-publication-change",
        kind="external_agent_publication", effect_class=EffectClass.IDEMPOTENT,
        intent_ref=(
            f"crp://external-agent-publication/{event['project_id']}/"
            f"{event['publication_identity']}"
        ),
        gate_decision_id="publication-authority-commit",
        rev_set={
            "object_revision": event["object_revision"],
            "change_type": event["change_type"],
            "occurred_at": event["occurred_at"],
        },
        payload={"collection": record.collection, "event": dict(event)},
    )


def dispatch_memory_invalidation_changes(
    root: Path,
    *,
    limit: int = DEFAULT_EXTERNAL_AGENT_PUBLICATION_CHANGE_RECOVERY_LIMIT,
) -> MemoryInvalidationDispatchResult | None:
    """Move pending Memory invalidations into the shared publication outbox."""
    try:
        root = Path(root)
        return dispatch_memory_invalidations(
            JsonObjectStore(root / ".rebuild-data", legacy_root=root / "library",mutation_attribution=json_attribution(root,'default')),
            audit_records(SQLiteStructuredRecordStore(root / ".rebuild-data" / STRUCTURED_DATABASE_NAME),root,'default'),
            limit=limit,
        )
    except Exception:  # noqa: BLE001 - pending JSON invalidations remain recoverable.
        LOGGER.warning(
            "memory_invalidation_outbox_dispatch_failed",
            extra={"event": "memory_invalidation_outbox_dispatch_failed"},
        )
        return None


def backfill_external_agent_publication_changes(
    root: Path,
    *,
    limit: int = DEFAULT_EXTERNAL_AGENT_PUBLICATION_CHANGE_RECOVERY_LIMIT,
) -> PublicationOutboxBackfillResult | None:
    """Backfill formal current publication authorities before the outbox drain.

    Backfill is isolated from the subsequent drain.  It has no reason to make
    app startup fail, and a later restart can safely retry an interrupted or
    conflicted bounded pass.
    """
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= DEFAULT_EXTERNAL_AGENT_PUBLICATION_CHANGE_RECOVERY_LIMIT
    ):
        raise ValueError("limit must be an integer from 1 through 100")
    try:
        return backfill_external_agent_publication_outbox(
            audit_records(SQLiteStructuredRecordStore(Path(root) / ".rebuild-data" / STRUCTURED_DATABASE_NAME),root,'default'),
            limit=limit,
        )
    except Exception:  # noqa: BLE001 - historical repair must not block startup.
        LOGGER.warning(
            "external_agent_publication_backfill_failed",
            extra={"event": "external_agent_publication_backfill_failed"},
        )
        return None
