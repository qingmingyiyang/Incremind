"""Route-independent admission of existing favorite batch Effects."""
from __future__ import annotations
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
import time
from backend.api.bilibili_favorite_batch import BilibiliFavoriteBatchRepository
from core.effect_log import (EFFECT_V2, NOT_APPLICABLE, V2_REVISION_KEYS, EffectClass,
    EffectIntent, GateDecision, GateDecisionFact)

EFFECT_KIND = "bilibili_favorite_batch_admission"
INTENT_SCHEMA = "bilibili-favorite-batch-admission-intent-v1"
RECEIPT_KIND = "bilibili-favorite-batch-admission-receipt-v1"
RECEIPT_SCHEMA = "bilibili-favorite-batch-admission-receipt-v1"

def admit_bilibili_favorite_batch_command(
    *, application: object, runtime_root: Path, repository: BilibiliFavoriteBatchRepository,
    batch_payload: Mapping[str, object], command_id: str, kind: str, retry_failed: bool,
) -> tuple[dict[str, object], bool]:
    """Persist an idempotent command before Core is allowed to execute it."""
    if kind not in {"admit", "continue", "retry_failed"}:
        raise ValueError("batch command kind is invalid")
    batch_id = str(batch_payload["batch_id"])
    existing = batch_payload.get("command")
    replay = isinstance(existing, Mapping) and existing.get("command_id") == command_id
    if replay:
        if existing.get("kind") != kind or existing.get("retry_failed") is not retry_failed:
            raise ValueError("batch command conflicts")
    if not replay and isinstance(existing, Mapping) and batch_payload.get("admission_status") in {"queued", "running"}:
        raise ValueError("batch command already active")
    if replay:
        stored = type("Stored", (), {"payload": dict(batch_payload)})()
    else:
        now = _now()
        staged = dict(batch_payload)
        staged["command"] = {
            "command_id": command_id, "kind": kind, "retry_failed": retry_failed,
            # The deterministic Effect id is recorded after Core accepts the plan.
            "operation_id": f"facts:bilibili-favorite-batch/pending/{batch_id}/{command_id}",
            "submitted_at": now,
        }
        staged["admission_status"] = "queued"
        staged["processing_status"] = _processing_status(staged)
        staged["status"] = "running"
        staged["updated_at"] = now
        stored = repository.save(staged)
    runtime = getattr(getattr(application, "state", object()), "effect_runtime", None)
    if runtime is None:
        raise ValueError("core_effect_runtime_unavailable")
    intent, gate = _intent_for(stored.payload)
    effect, created = runtime.log.plan_v2(
        intent, gate_decision_id=intent.gate_decision_id, gate_fact=gate, now=int(time.time()),
    )
    current = repository.get(batch_id)
    if current is None:
        raise ValueError("batch disappeared after command admission")
    projected = dict(current.payload)
    command = dict(projected["command"])
    command["operation_id"] = effect.operation_id
    projected["command"] = command
    projected["updated_at"] = _now()
    if projected == current.payload:
        return dict(current.payload), not created
    return dict(repository.save(projected).payload), not created


def _intent_for(payload: Mapping[str, object]) -> tuple[EffectIntent, GateDecisionFact]:
    batch_id, project_id = str(payload["batch_id"]), str(payload["project_id"])
    command = payload["command"]
    assert isinstance(command, Mapping)
    command_id = str(command["command_id"])
    revisions = {key: NOT_APPLICABLE for key in V2_REVISION_KEYS}
    revisions.update({
        "policy": "bilibili-favorite-batch-policy-v1", "boundary": "bilibili-favorite-batch-boundary-v1",
        "capability": "bilibili-favorite-batch-admission-v1", "bundle": "bilibili-favorite-batch-bundle-v1",
        "handler": "bilibili-favorite-batch-handler-v1", "budget": "bilibili-favorite-batch-chunk-10-v1",
        "workflow": "bilibili-favorite-batch-workflow-v1",
    })
    gate_id = f"gate:bilibili-favorite-batch/{batch_id}/{command_id}"
    intent_ref = f"intent:bilibili-favorite-batch/{batch_id}/{command_id}"
    gate = GateDecisionFact(
        decision=GateDecision.ALLOW, rule_ref="rule:bilibili-favorite-batch-public-snapshot-v1",
        scope_ref=f"scope:bilibili-favorite-batch/{project_id}",
        budget_after={"batch_ref": f"facts:bilibili-favorite-batch/{batch_id}", "item_count_budget": len(payload["items"]), "chunk_count": 10},
        secret_scope="scope:bilibili-favorite-batch-secret/not-applicable", policy_revision=revisions["policy"],
    )
    return EffectIntent(
        session_id=f"bilibili-favorite-batch:{project_id}", root_id=batch_id, parent_id=None,
        step_key=f"command:{command_id}", kind=EFFECT_KIND, effect_class=EffectClass.QUERYABLE,
        intent_ref=intent_ref, gate_decision_id=gate_id, rev_set=revisions,
        payload={"admission_ref": f"facts:bilibili-favorite-batch/{batch_id}/{command_id}", "batch_id": batch_id, "command_id": command_id, "mode": str(command["kind"])},
        idem_key=command_id, contract_version=EFFECT_V2, intent_schema_version=INTENT_SCHEMA,
        expected_receipt_kind=RECEIPT_KIND, expected_receipt_schema_version=RECEIPT_SCHEMA,
    ), gate


def _processing_status(payload: Mapping[str, object]) -> str:
    """Persist only the conservative child-work baseline.

    A batch admission Effect cannot prove that independently scheduled child
    Jobs have finished.  UI-facing truth is therefore assembled by the batch
    GET/list projection from those Job records; callers must not present this
    stored value as a terminal processing claim.
    """
    states = [item.get("state") for item in payload["items"] if isinstance(item, Mapping)]
    if not any(state == "admitted" for state in states):
        return "not_started"
    if any(state in {"pending", "processing", "admitted"} for state in states):
        return "queued"
    return "partial_failure" if any(state == "failed" for state in states) else "queued"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
