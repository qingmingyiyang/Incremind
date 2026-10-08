from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from core.aggregate_repository_factory import STRUCTURED_DATABASE_NAME
from core.effect_log import Effect, EffectClass, EffectHandlerRegistration, EffectIntent, EffectPurpose, EffectReceipt, EffectState
from core.effect_log.core import EFFECT_V2, NOT_APPLICABLE, GateDecision, GateDecisionFact, V2_REVISION_KEYS
from core.memory_core.publication_trust_audit_uow import (
    SQLiteMemoryPublicationTrustAuditUnitOfWork,
)
from core.project_skill_core import SQLiteProjectSkillPublicationCompositeUnitOfWork
from core.storage_provider import SQLiteStructuredRecordStore


RECEIPTS = "memory_publication_effect_receipts"
INTENTS = "memory_publication_effect_intents"
EFFECT_KIND = "formal_memory_publication"
INTENT_SCHEMA = "formal-memory-publication-intent-v2"
RECEIPT_KIND = "formal-memory-publication"
RECEIPT_SCHEMA = "formal-memory-publication-receipt-v2"


def _receipt_ref(operation_id: str) -> str:
    return f"receipt:formal-memory-publication/{operation_id}"


def register_memory_publication_handler(runtime_root: Path, effect_runtime) -> None:
    database = runtime_root / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    effect_database = Path(effect_runtime.log.database)

    def execute(effect: Effect) -> str:
        payload = _read_intent(database, effect_database, effect)
        namespace_id = str(payload["namespace_id"])
        action = str(payload["action"])
        object_id = str(payload["object_id"])
        reason = str(payload["reason"])
        if action in {"memory_publish", "memory_rollback"}:
            with SQLiteMemoryPublicationTrustAuditUnitOfWork(
                database, namespace_id=namespace_id,
            ).begin() as transaction:
                if action == "memory_publish":
                    result = transaction.publish_user_confirmed(
                        layer=str(payload["layer"]),
                        staged_id=object_id,
                        published_at=str(payload["occurred_at"]),
                    )
                    receipt = {
                        "status": "published", "layer": result.layer,
                        "object_id": result.object_id,
                        "publication_id": result.publication_id,
                        "transition_id": result.transition_id,
                    }
                else:
                    result = transaction.rollback_user_confirmed(
                        publication_id=object_id,
                        reason=reason,
                        rolled_back_at=str(payload["occurred_at"]),
                    )
                    receipt = {
                        "status": "rolled_back", "layer": result.layer,
                        "object_id": result.object_id,
                        "publication_id": result.publication_id,
                        "transition_id": result.transition_id,
                    }
                transaction.commit()
        else:
            with SQLiteProjectSkillPublicationCompositeUnitOfWork(
                database, namespace_id=namespace_id,
            ).begin() as transaction:
                if action == "project_skill_publish":
                    result = transaction.publish(draft=transaction.staged_draft(object_id))
                    receipt = {
                        "status": "published", "layer": "project_skill",
                        "publication_id": result.publication_id,
                        "publication_revision": result.publication_revision,
                        "transition_id": result.transition_id,
                        "project_skill_revision": result.project_skill_revision,
                    }
                elif action == "project_skill_rollback":
                    result = transaction.rollback(
                        publication_id=object_id,
                        expected_publication_revision=int(payload["expected_publication_revision"]),
                        expected_project_skill_revision=int(payload["expected_project_skill_revision"]),
                        reason=reason,
                    )
                    receipt = {
                        "status": "rolled_back", "layer": "project_skill",
                        "publication_id": result.publication_id,
                        "publication_revision": result.publication_revision,
                        "transition_id": result.transition_id,
                        "project_skill_revision": result.project_skill_revision,
                    }
                else:
                    raise ValueError("memory publication Effect action is invalid")
                transaction.commit()
        _write_receipt(database, effect.operation_id, receipt)
        return f"crp://{namespace_id}/memory-publication-effect-receipts/{effect.operation_id}"

    def handle_v2(effect: Effect) -> EffectReceipt:
        existing = _validated_receipt(database, effect_database, effect)
        if existing is None:
            execute(effect)
            _validated_receipt(database, effect_database, effect)
        return EffectReceipt(_receipt_ref(effect.operation_id), RECEIPT_KIND, RECEIPT_SCHEMA, INTENT_SCHEMA)

    def handle_legacy(effect: Effect) -> str:
        # Historical v1 entries remain dispatchable during startup recovery.
        # New writers below are v2-only and cannot enter this path.
        return execute(effect)

    def probe_v2(effect: Effect) -> tuple[EffectState, str | None]:
        receipt = _validated_receipt(database, effect_database, effect)
        if receipt is None:
            # The UoW commits the publication/transition atomically, while
            # the immutable Effect receipt is recorded afterwards.  Recover a
            # receipt from that committed domain evidence instead of invoking
            # the writer again after a process-kill in that narrow window.
            recovered = _recover_receipt_from_publication(database, effect_database, effect)
            if recovered is not None:
                _write_receipt(database, effect.operation_id, recovered)
                receipt = _validated_receipt(database, effect_database, effect)
        if receipt is not None:
            return EffectState.SETTLED_OK, _receipt_ref(effect.operation_id)
        return EffectState.PLANNED, None

    effect_runtime.handlers.register(EffectHandlerRegistration(
        kind=EFFECT_KIND,
        effect_class=EffectClass.IDEMPOTENT,
        handler=handle_legacy,
    ))
    effect_runtime.handlers.register(EffectHandlerRegistration(
        kind=EFFECT_KIND,
        effect_class=EffectClass.QUERYABLE,
        handler=handle_v2,
        probe=probe_v2,
        contract_version=EFFECT_V2,
        intent_schema_version=INTENT_SCHEMA,
        receipt_kind=RECEIPT_KIND,
        receipt_schema_version=RECEIPT_SCHEMA,
    ))


def plan_memory_publication(
    effect_runtime,
    *,
    namespace_id: str,
    action: str,
    object_id: str,
    reason: str,
    layer: str | None = None,
    expected_publication_revision: int | None = None,
    expected_project_skill_revision: int | None = None,
) -> str:
    identity = {
        "namespace_id": namespace_id,
        "action": action,
        "object_id": object_id,
        "reason": reason,
        "layer": layer,
        "expected_publication_revision": expected_publication_revision,
        "expected_project_skill_revision": expected_project_skill_revision,
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(),
    ).hexdigest()
    intent_ref = f"crp://{namespace_id}/memory-publication-intents/{digest}"
    stored_intent = {
        **identity,
        "intent_ref": intent_ref,
        "schema_version": INTENT_SCHEMA,
        "revisions": _revisions(action),
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_intent(
        Path(effect_runtime.log.database).parent / STRUCTURED_DATABASE_NAME,
        digest,
        stored_intent,
    )
    revisions = _revisions(action)
    decision_id, gate_fact = _user_confirmation_gate(
        namespace_id=namespace_id,
        intent_id=digest,
        policy_revision=revisions["policy"],
    )
    intent = EffectIntent(
        session_id="formal-memory-publication",
        root_id=f"memory-publication-{digest}",
        step_key=action,
        kind=EFFECT_KIND,
        effect_class=EffectClass.QUERYABLE,
        purpose=EffectPurpose.PRIMARY,
        intent_ref=intent_ref,
        gate_decision_id=decision_id,
        rev_set=revisions,
        payload={
            "publication_intent_ref": intent_ref,
            "publication_intent_id": digest,
            "namespace_id": namespace_id,
            "mode": action,
        },
        idem_key=f"formal-memory-publication/{digest}",
        contract_version=EFFECT_V2,
        intent_schema_version=INTENT_SCHEMA,
        expected_receipt_kind=RECEIPT_KIND,
        expected_receipt_schema_version=RECEIPT_SCHEMA,
    )
    planned, _ = effect_runtime.log.plan_v2(
        intent, gate_decision_id=decision_id, gate_fact=gate_fact, now=int(time.time()),
    )
    return planned.operation_id


def read_memory_publication_receipt(runtime_root: Path, operation_id: str) -> Mapping[str, object]:
    record = SQLiteStructuredRecordStore(
        runtime_root / ".rebuild-data" / STRUCTURED_DATABASE_NAME,
    ).read(RECEIPTS, operation_id)
    if record is None:
        raise ValueError("memory publication Effect receipt is unavailable")
    return dict(record.payload)


def _write_receipt(database: Path, operation_id: str, payload: Mapping[str, object]) -> None:
    _write_immutable(database, RECEIPTS, operation_id, {
        **payload,
        "operation_id": operation_id,
        "receipt_ref": _receipt_ref(operation_id),
        "receipt_kind": RECEIPT_KIND,
        "receipt_schema_version": RECEIPT_SCHEMA,
        "intent_schema_version": INTENT_SCHEMA,
    })


def _read_intent(
    database: Path, effect_database: Path, effect: Effect,
) -> Mapping[str, object]:
    if effect.contract_version == EFFECT_V2:
        envelope = _v2_envelope(effect_database, effect)
        ref = envelope.get("publication_intent_ref")
        key = envelope.get("publication_intent_id")
        if not isinstance(ref, str) or ref != effect.intent_ref or not isinstance(key, str):
            raise ValueError("memory publication v2 intent reference drifted")
    else:
        key = effect.operation_id
    record = SQLiteStructuredRecordStore(database).read(INTENTS, key)
    if record is None:
        raise ValueError("memory publication Effect intent is unavailable")
    payload = dict(record.payload)
    if effect.contract_version == EFFECT_V2:
        if dict(effect.rev_set) != _revisions(str(payload["action"])):
            raise ValueError("memory publication v2 policy revision drifted")
        if (payload.get("intent_ref") != effect.intent_ref
                or payload.get("schema_version") != INTENT_SCHEMA
                or payload.get("revisions") != dict(effect.rev_set)
                or payload.get("action") != envelope.get("mode")):
            raise ValueError("memory publication v2 authority drifted")
    return payload


def _v2_envelope(effect_database: Path, effect: Effect) -> Mapping[str, object]:
    """Read Core's immutable v2 fact from the Effect database, never domain DB."""
    with sqlite3.connect(effect_database) as connection:
        row = connection.execute(
            "SELECT intent_ref,intent_digest,payload_json,schema_version "
            "FROM effect_intent_fact WHERE operation_id=?", (effect.operation_id,),
        ).fetchone()
    if row is None or row[0] != effect.intent_ref or row[1] != effect.intent_digest or row[3] != INTENT_SCHEMA:
        raise ValueError("memory publication v2 immutable intent fact drifted")
    try:
        envelope = json.loads(row[2])
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("memory publication v2 immutable intent fact is invalid") from error
    if not isinstance(envelope, Mapping):
        raise ValueError("memory publication v2 immutable intent fact is invalid")
    return envelope


def _validated_receipt(
    database: Path, effect_database: Path, effect: Effect,
) -> Mapping[str, object] | None:
    record = SQLiteStructuredRecordStore(database).read(RECEIPTS, effect.operation_id)
    if record is None:
        return None
    payload = dict(record.payload)
    if effect.contract_version != EFFECT_V2:
        return payload
    expected = {
        "operation_id": effect.operation_id,
        "receipt_ref": _receipt_ref(effect.operation_id),
        "receipt_kind": RECEIPT_KIND,
        "receipt_schema_version": RECEIPT_SCHEMA,
        "intent_schema_version": INTENT_SCHEMA,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError("memory publication v2 receipt drifted")
    intent = _read_intent(database, effect_database, effect)
    _validate_receipt_identity(database, intent, payload)
    return payload


def _validate_receipt_identity(
    database: Path, intent: Mapping[str, object], receipt: Mapping[str, object],
) -> None:
    """Reject a receipt that is syntactically valid but names another write."""
    action = str(intent.get("action"))
    base_keys = {
        "operation_id", "receipt_ref", "receipt_kind", "receipt_schema_version",
        "intent_schema_version", "status", "layer", "publication_id", "transition_id",
    }
    expected_keys = {
        "memory_publish": base_keys | {"object_id"},
        "memory_rollback": base_keys | {"object_id"},
        "project_skill_publish": base_keys | {"publication_revision", "project_skill_revision"},
        "project_skill_rollback": base_keys | {"publication_revision", "project_skill_revision"},
    }.get(action)
    if expected_keys is None or set(receipt) != expected_keys:
        raise ValueError("memory publication v2 receipt schema drifted")
    status = receipt.get("status")
    publication_id = receipt.get("publication_id")
    transition_id = receipt.get("transition_id")
    if not isinstance(publication_id, str) or not publication_id or not isinstance(transition_id, str) or not transition_id:
        raise ValueError("memory publication v2 receipt identity is invalid")
    if action == "memory_publish":
        if (status != "published" or receipt.get("layer") != intent.get("layer")
                or receipt.get("object_id") != intent.get("object_id")):
            raise ValueError("memory publication v2 receipt content drifted")
    elif action == "memory_rollback":
        if status != "rolled_back" or publication_id != intent.get("object_id"):
            raise ValueError("memory publication v2 rollback receipt content drifted")
    elif action == "project_skill_publish":
        expected_publication_id = f"memory-publication-project-skill-{intent.get('object_id')}"
        if (status != "published" or receipt.get("layer") != "project_skill"
                or publication_id != expected_publication_id):
            raise ValueError("Project Skill publication receipt content drifted")
    elif action == "project_skill_rollback":
        if status != "rolled_back" or receipt.get("layer") != "project_skill" or publication_id != intent.get("object_id"):
            raise ValueError("Project Skill rollback receipt content drifted")
    else:
        raise ValueError("memory publication v2 receipt action is invalid")
    for key in ("publication_revision", "project_skill_revision"):
        if key in receipt and (not isinstance(receipt[key], int) or isinstance(receipt[key], bool) or receipt[key] < 1):
            raise ValueError("memory publication v2 receipt revision is invalid")

    # A receipt is valid only when the formal UoW's committed publication and
    # transition remain available as its exact durable authority.
    record = SQLiteStructuredRecordStore(database).read("memory_publications", publication_id)
    if record is None:
        raise ValueError("memory publication v2 receipt publication authority is unavailable")
    publication = dict(record.payload)
    if publication.get("status") != status or publication.get("id") != publication_id:
        raise ValueError("memory publication v2 receipt publication authority drifted")
    transition = SQLiteStructuredRecordStore(database).read("memory_transitions", transition_id)
    if transition is None or transition.payload.get("id") != transition_id:
        raise ValueError("memory publication v2 receipt transition authority drifted")
    if action.startswith("memory_"):
        if (publication.get("layer") != receipt.get("layer")
                or publication.get("published_object_id") != receipt.get("object_id")
                or transition.payload.get("object_id") != receipt.get("object_id")):
            raise ValueError("memory publication v2 receipt object authority drifted")
        if action == "memory_publish" and publication.get("published_at") != intent.get("occurred_at"):
            raise ValueError("memory publication v2 receipt publication time drifted")
        if action == "memory_rollback":
            if (publication.get("rollback_reason") != intent.get("reason")
                    or publication.get("rolled_back_at") != intent.get("occurred_at")
                    or transition.payload.get("reason") != intent.get("reason")
                    or transition.payload.get("created_at") != intent.get("occurred_at")):
                raise ValueError("memory publication v2 rollback authority drifted")
    else:
        if publication.get("layer") != "project_skill" or transition.payload.get("object_id") != publication.get("published_object_id"):
            raise ValueError("Project Skill publication receipt authority drifted")
        if action == "project_skill_rollback" and (
            publication.get("rollback_reason") != intent.get("reason")
            or transition.payload.get("reason") != intent.get("reason")
        ):
            raise ValueError("Project Skill rollback authority drifted")
        if receipt.get("project_skill_revision") != _project_skill_revision(SQLiteStructuredRecordStore(database), publication):
            raise ValueError("Project Skill receipt revision authority drifted")


def _recover_receipt_from_publication(
    database: Path, effect_database: Path, effect: Effect,
) -> Mapping[str, object] | None:
    """Rebuild only a missing receipt from committed formal UoW evidence."""
    intent = _read_intent(database, effect_database, effect)
    action = str(intent["action"])
    records = SQLiteStructuredRecordStore(database)
    if action == "memory_publish":
        layer, object_id = str(intent["layer"]), str(intent["object_id"])
        candidates = [
            dict(record.payload) for record in records.list("memory_publications")
            if record.payload.get("status") == "published"
            and record.payload.get("layer") == layer
            and record.payload.get("published_object_id") == object_id
            and record.payload.get("published_at") == intent.get("occurred_at")
        ]
        if len(candidates) != 1:
            return None
        publication = candidates[0]
        transition_id = _transition_id_from_ref(publication.get("transition_ref"))
        if transition_id is None:
            return None
        return {
            "status": "published", "layer": layer, "object_id": object_id,
            "publication_id": publication.get("id"), "transition_id": transition_id,
        }
    publication_id = (
        f"memory-publication-project-skill-{intent['object_id']}"
        if action == "project_skill_publish" else str(intent["object_id"])
    )
    record = records.read("memory_publications", publication_id)
    if record is None:
        return None
    publication = dict(record.payload)
    if action == "memory_rollback":
        transition_id = f"transition-memory-rollback-{publication_id}"
        if publication.get("status") != "rolled_back" or records.read("memory_transitions", transition_id) is None:
            return None
        return {
            "status": "rolled_back", "layer": publication.get("layer"),
            "object_id": publication.get("published_object_id"), "publication_id": publication_id,
            "transition_id": transition_id,
        }
    if action == "project_skill_rollback":
        transition_id = f"transition-project-skill-rollback-{publication_id}"
        if publication.get("status") != "rolled_back" or records.read("memory_transitions", transition_id) is None:
            return None
        return {
            "status": "rolled_back", "layer": "project_skill", "publication_id": publication_id,
            "publication_revision": record.revision, "transition_id": transition_id,
            "project_skill_revision": _project_skill_revision(records, publication),
        }
    if action == "project_skill_publish":
        # A staged draft id maps to one deterministic formal publication id.
        if publication.get("status") != "published":
            return None
        transition_id = _transition_id_from_ref(publication.get("transition_ref"))
        if transition_id is None:
            return None
        return {
            "status": "published", "layer": "project_skill", "publication_id": publication_id,
            "publication_revision": record.revision, "transition_id": transition_id,
            "project_skill_revision": _project_skill_revision(records, publication),
        }
    return None


def _transition_id_from_ref(value: object) -> str | None:
    if not isinstance(value, str) or "/memory-transitions/" not in value:
        return None
    candidate = value.rsplit("/", 1)[-1]
    return candidate.removesuffix(".json") or None


def _project_skill_revision(records: SQLiteStructuredRecordStore, publication: Mapping[str, object]) -> int:
    skill_id = publication.get("published_object_id")
    if not isinstance(skill_id, str):
        raise ValueError("Project Skill publication authority is invalid")
    record = records.read("project_skills", skill_id)
    if record is None or not isinstance(record.payload.get("revision"), int):
        raise ValueError("Project Skill revision authority is unavailable")
    return int(record.payload["revision"])


def _write_intent(database: Path, intent_id: str, payload: Mapping[str, object]) -> None:
    records = SQLiteStructuredRecordStore(database)
    existing = records.read(INTENTS, intent_id)
    if existing is not None:
        frozen = dict(existing.payload)
        comparable = {key: value for key, value in payload.items() if key != "occurred_at"}
        if {key: value for key, value in frozen.items() if key != "occurred_at"} != comparable:
            raise ValueError("memory publication Effect intent conflicts")
        return
    _write_immutable(database, INTENTS, intent_id, payload)


def _revisions(action: str) -> dict[str, str]:
    values = {key: NOT_APPLICABLE for key in V2_REVISION_KEYS}
    values.update({
        "policy": "formal-memory-publication-policy-v2",
        "boundary": "formal-memory-publication-boundary-v2",
        "capability": f"formal-memory-publication-{action}-v2",
        "handler": "formal-memory-publication-handler-v2",
        "workflow": "formal-memory-publication-workflow-v2",
    })
    return values


def _user_confirmation_gate(
    *, namespace_id: str, intent_id: str, policy_revision: str,
) -> tuple[str, GateDecisionFact]:
    decision_id = f"decision:formal-memory-publication/{intent_id}"
    return decision_id, GateDecisionFact(
        decision=GateDecision.ALLOW,
        rule_ref="rule:formal-memory-publication/user-confirmation-v2",
        # The immutable Gate fact is scoped to the exact frozen publication
        # intent.  Replays keep the same digest while different objects,
        # reasons, or expected revisions cannot collide on decision_digest.
        scope_ref=f"scope:formal-memory-publication/{namespace_id}/{intent_id}",
        budget_after={"publication_count": 1},
        secret_scope="scope:formal-memory-publication-local",
        policy_revision=policy_revision,
    )


def _write_immutable(
    database: Path,
    collection: str,
    operation_id: str,
    payload: Mapping[str, object],
) -> None:
    records = SQLiteStructuredRecordStore(database)
    existing = records.read(collection, operation_id)
    if existing is not None:
        if dict(existing.payload) != dict(payload):
            raise ValueError("memory publication Effect receipt conflicts")
        return
    with records.begin() as transaction:
        transaction.put(collection, operation_id, dict(payload), expected_revision=0)
        transaction.commit()
