from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import time

from fastapi import FastAPI

from core.effect_log import (
    EffectClass,
    EffectHandlerAbandoned,
    EffectHandlerRegistration,
    EffectIntent,
    EffectLog,
    EffectRunner,
    EffectState,
)
from core.product_core.team_memory_source_recovery import ForgetTeamCreatedSource
from core.product_core.team_memory_source_staging import TeamMemorySourceStagingRepository
from core.storage_provider import JsonObjectStore
from backend.security.user_context import json_attribution


DEFAULT_TEAM_MEMORY_FORGET_RECOVERY_LIMIT = 100


def register_team_memory_source_forget_handler(runtime_root: Path, effect_runtime) -> None:
    store, staging = _stores(runtime_root)

    def handle(effect) -> str:
        matching = []
        for record in staging.list_forget_pending():
            receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
            if receipt.get("forget_operation_id") == effect.operation_id:
                matching.append((record, receipt))
        if len(matching) != 1:
            raise EffectHandlerAbandoned("team_memory_source_forget.operation_identity_invalid")
        record, receipt = matching[0]
        expected_revision = receipt.get("forget_requested_staging_revision")
        reason = receipt.get("forget_reason")
        if not isinstance(expected_revision, int) or not isinstance(reason, str):
            raise EffectHandlerAbandoned("team_memory_source_forget.receipt_incomplete")
        forgotten_at = datetime.fromtimestamp(
            effect.occurred_at, tz=timezone.utc,
        ).isoformat()
        ForgetTeamCreatedSource(object_store=store, staging=staging).execute(
            str(record["id"]),
            expected_staging_revision=expected_revision,
            confirmed=True,
            reason=reason,
            forgotten_at=forgotten_at,
        )
        return f"crp://default/team-memory-source-forget-receipts/{effect.operation_id}"

    effect_runtime.handlers.register(EffectHandlerRegistration(
        kind="team_memory_source_forget",
        effect_class=EffectClass.IDEMPOTENT,
        handler=handle,
    ))


@dataclass(frozen=True, slots=True)
class TeamMemoryForgetStartupRecoveryItem:
    staging_id: str
    operation_id: str | None
    initial_state: str
    outcome: str
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class TeamMemoryForgetStartupRecoveryReport:
    scanned: int
    attempted: int
    recovered: int
    failed: int
    deferred: int
    items: tuple[TeamMemoryForgetStartupRecoveryItem, ...]


def backfill_team_memory_source_forget_effects(
    runtime_root: Path, effects: EffectLog, *, max_operations: int = DEFAULT_TEAM_MEMORY_FORGET_RECOVERY_LIMIT,
) -> tuple[str, ...]:
    """Convert stranded legacy claims to expired Core Effect leases only."""
    store, staging = _stores(runtime_root)
    backfilled: list[str] = []
    now = int(time.time())
    for record in staging.list_forget_pending()[:max_operations]:
        receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
        operation_id = str(receipt.get("forget_operation_id") or "")
        if not operation_id:
            continue
        intent = EffectIntent(
            session_id=f"team-memory-forget:{record['id']}", root_id=str(record["id"]),
            step_key="hard-forget-source", kind="team_memory_source_forget",
            effect_class=EffectClass.IDEMPOTENT,
            intent_ref=f"crp://default/team-memory-source-forget-intents/{operation_id}",
            gate_decision_id="team-memory-hard-forget-confirmation",
            rev_set={
                "staging_revision": receipt["forget_requested_staging_revision"],
                "source_revision": receipt.get("source_revision", "absent"),
            },
            payload={
                "staging_id": record["id"], "reason": receipt["forget_reason"],
            },
            operation_id_override=operation_id,
        )
        effect, _created = effects.plan(intent, now=now - 2)
        if effect.state is EffectState.PLANNED:
            effects.transition(
                operation_id, expected=EffectState.PLANNED,
                target=EffectState.INFLIGHT, now=now - 2,
                lease_owner="legacy-team-memory-forget",
                lease_expires_at=now - 1, increment_attempt=True,
            )
        backfilled.append(operation_id)
    return tuple(backfilled)


def dispatch_team_memory_source_forget_effects(
    application: FastAPI,
    runtime_root: Path,
    runner: EffectRunner,
    *,
    max_operations: int = DEFAULT_TEAM_MEMORY_FORGET_RECOVERY_LIMIT,
) -> TeamMemoryForgetStartupRecoveryReport:
    """Dispatch only stranded forgets authorized as PLANNED by Core Reaper."""
    if (
        not isinstance(max_operations, int)
        or isinstance(max_operations, bool)
        or max_operations < 1
    ):
        raise ValueError("max_operations must be a positive integer")

    effects = runner.log
    store, staging = _stores(runtime_root)
    pending = tuple(
        record for record in staging.list_forget_pending()
        if _planned(effects, record)
    )
    selected = pending[:max_operations]
    items: list[TeamMemoryForgetStartupRecoveryItem] = []
    forgotten_at = datetime.now(timezone.utc).isoformat()
    for record in selected:
        staging_id = str(record.get("id") or "")
        receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
        operation_id = (
            str(receipt.get("forget_operation_id"))
            if receipt.get("forget_operation_id") is not None
            else None
        )
        try:
            expected_revision = receipt.get("forget_requested_staging_revision")
            reason = receipt.get("forget_reason")
            if not isinstance(expected_revision, int) or not isinstance(reason, str):
                raise _RecoveryRejected("receipt_incomplete")
            assert operation_id is not None
            def forget(_effect):
                ForgetTeamCreatedSource(object_store=store, staging=staging).execute(
                    staging_id,
                    expected_staging_revision=expected_revision,
                    confirmed=True,
                    reason=reason,
                    forgotten_at=forgotten_at,
                )
                return f"crp://default/team-memory-source-forget-receipts/{operation_id}"

            runner.execute_planned(
                operation_id, forget, now=int(time.time()),
                receipt_kind="team-memory-source-forget-receipt",
            )
            items.append(
                TeamMemoryForgetStartupRecoveryItem(
                    staging_id=staging_id,
                    operation_id=operation_id,
                    initial_state="forgetting",
                    outcome="recovered",
                )
            )
        except Exception as error:  # noqa: BLE001
            items.append(
                TeamMemoryForgetStartupRecoveryItem(
                    staging_id=staging_id,
                    operation_id=operation_id,
                    initial_state="forgetting",
                    outcome="failed",
                    error_code=_stable_error_code(error),
                )
            )
    report = TeamMemoryForgetStartupRecoveryReport(
        scanned=len(pending),
        attempted=len(items),
        recovered=sum(item.outcome == "recovered" for item in items),
        failed=sum(item.outcome == "failed" for item in items),
        deferred=len(pending) - len(selected),
        items=tuple(items),
    )
    application.state.team_memory_source_forget_startup_recovery = report
    return report


def _stores(runtime_root: Path) -> tuple[JsonObjectStore, TeamMemorySourceStagingRepository]:
    store = JsonObjectStore(
        runtime_root / ".rebuild-data", legacy_root=runtime_root / "library",
        namespace_id="default",
        mutation_attribution=json_attribution(runtime_root, 'default'),
    )
    return store, TeamMemorySourceStagingRepository(store)


def _planned(effects: EffectLog, record: dict | object) -> bool:
    if not isinstance(record, dict):
        return False
    receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
    operation_id = receipt.get("forget_operation_id")
    if not isinstance(operation_id, str):
        return False
    try:
        return effects.get(operation_id).state is EffectState.PLANNED
    except KeyError:
        return False


class _RecoveryRejected(RuntimeError):
    pass


def _stable_error_code(error: Exception) -> str:
    from core.product_core.team_memory_source_recovery import (
        TeamMemorySourceAuthorityConflict,
        TeamMemorySourceAuthorityError,
    )
    from core.product_core.team_memory_source_staging import (
        TeamMemorySourceStagingError,
    )

    if isinstance(error, _RecoveryRejected):
        return str(error)
    if isinstance(error, TeamMemorySourceAuthorityConflict):
        return "authority_conflict"
    if isinstance(error, TeamMemorySourceAuthorityError):
        return "authority_error"
    if isinstance(error, TeamMemorySourceStagingError):
        return "staging_error"
    return "recovery_failed"
