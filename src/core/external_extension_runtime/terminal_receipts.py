"""Immutable lifecycle intent and terminal Receipt authority for extensions.

Core EffectLog owns execution state. This module owns only immutable domain
intents and Receipts, plus Handler/probe strategies that let Core Reaper close
the receipt-before-settlement crash window.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Protocol

from core.effect_log import (
    EFFECT_V2,
    Effect,
    EffectClass,
    EffectHandlerRegistration,
    EffectHandlerRegistry,
    EffectIntent,
    EffectLog,
    EffectPurpose,
    EffectReceipt,
    EffectState,
)
from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict


class ExternalExtensionTerminalReceiptError(ValueError):
    """Raised when a lifecycle intent or Receipt is malformed."""


class ExternalExtensionTerminalReceiptConflict(ExternalExtensionTerminalReceiptError):
    """Raised when immutable lifecycle evidence or Effect identity drifts."""


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~:-]{7,159}$")
_ROOT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,159}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_INTENTS = "external_extension_lifecycle_intents"
_RECEIPTS = "external_extension_terminal_receipts"
_INTENT_PREFIX = "crp://external-extension-lifecycle-intents/"
_RECEIPT_PREFIX = "crp://external-extension-terminal-receipts/"
LIFECYCLE_INTENT_SCHEMA = "external-extension-lifecycle-intent/v1"


@dataclass(frozen=True, slots=True)
class LifecycleEffectContract:
    action: str
    effect_kind: str
    receipt_kind: str
    receipt_schema: str


_CONTRACTS = {
    "health": LifecycleEffectContract(
        "health", "external_extension_health", "external_extension_health_receipt",
        "external-extension-health-receipt/v1",
    ),
    "activation": LifecycleEffectContract(
        "activation", "external_extension_activation", "external_extension_activation_receipt",
        "external-extension-activation-receipt/v1",
    ),
    "disable": LifecycleEffectContract(
        "disable", "external_extension_disable", "external_extension_disable_receipt",
        "external-extension-disable-receipt/v1",
    ),
    "rollback": LifecycleEffectContract(
        "rollback", "external_extension_rollback", "external_extension_rollback_receipt",
        "external-extension-rollback-receipt/v1",
    ),
    "uninstall": LifecycleEffectContract(
        "uninstall", "external_extension_uninstall", "external_extension_uninstall_receipt",
        "external-extension-uninstall-receipt/v1",
    ),
}


@dataclass(frozen=True, slots=True)
class ExternalExtensionLifecycleIntent:
    intent_id: str
    action: str
    root_id: str
    revision_ref: str
    intake_ref: str
    artifact_ref: str
    artifact_receipt_ref: str
    artifact_content_sha256: str
    manifest_identity: str
    review_plan_identity: str
    activation_plan_identity: str
    health_plan_identity: str
    health_checks: tuple[str, ...] = ()
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        _id(self.intent_id, "lifecycle intent id")
        lifecycle_contract(self.action)
        _root(self.root_id)
        for value in (self.revision_ref, self.intake_ref, self.artifact_ref, self.artifact_receipt_ref):
            _ref(value)
        for value in (
            self.artifact_content_sha256, self.manifest_identity,
            self.review_plan_identity, self.activation_plan_identity,
            self.health_plan_identity,
        ):
            _sha(value)
        object.__setattr__(self, "health_checks", _checks(self.health_checks))
        if self.action == "health":
            if not self.health_checks:
                raise ExternalExtensionTerminalReceiptError("health lifecycle intent requires checks")
        elif self.health_checks:
            raise ExternalExtensionTerminalReceiptError("non-health lifecycle intent carries checks")
        if self.schema_version != "1.0.0":
            raise ExternalExtensionTerminalReceiptError("lifecycle intent schema is invalid")

    @property
    def intent_ref(self) -> str:
        return lifecycle_intent_ref(self.intent_id)

    @property
    def effect_payload(self) -> dict[str, object]:
        return _canonical({
            "schema_revision": self.schema_version,
            "kind": self.action,
            "root_id": self.root_id,
            "revision_digest": hashlib.sha256(self.revision_ref.encode("utf-8")).hexdigest(),
            "intake_ref": self.intake_ref,
            "artifact_ref": self.artifact_ref,
            "artifact_receipt_ref": self.artifact_receipt_ref,
            "artifact_content_digest": self.artifact_content_sha256,
            "manifest_digest": self.manifest_identity,
            "review_plan_digest": self.review_plan_identity,
            "activation_plan_digest": self.activation_plan_identity,
            "health_plan_digest": self.health_plan_identity,
            "health_check_ids": list(self.health_checks),
        })


@dataclass(frozen=True, slots=True)
class ExternalExtensionTerminalReceipt:
    effect_operation_id: str
    action: str
    intent_ref: str
    intent_digest: str
    root_id: str
    revision_ref: str
    intake_ref: str
    artifact_ref: str
    artifact_receipt_ref: str
    artifact_content_sha256: str
    manifest_identity: str
    review_plan_identity: str
    activation_plan_identity: str
    health_plan_identity: str
    passed: bool | None = None
    observed_checks: tuple[str, ...] = ()
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        _id(self.effect_operation_id, "effect operation id")
        lifecycle_contract(self.action)
        _lifecycle_intent_id(self.intent_ref)
        _sha(self.intent_digest)
        _root(self.root_id)
        for value in (self.revision_ref, self.intake_ref, self.artifact_ref, self.artifact_receipt_ref):
            _ref(value)
        for value in (
            self.artifact_content_sha256, self.manifest_identity,
            self.review_plan_identity, self.activation_plan_identity,
            self.health_plan_identity,
        ):
            _sha(value)
        object.__setattr__(self, "observed_checks", _checks(self.observed_checks))
        if self.action == "health":
            if not isinstance(self.passed, bool) or not self.observed_checks:
                raise ExternalExtensionTerminalReceiptError("health receipt outcome is invalid")
        elif self.passed is not None or self.observed_checks:
            raise ExternalExtensionTerminalReceiptError("non-health receipt carries health outcome")
        if self.schema_version != "1.0.0":
            raise ExternalExtensionTerminalReceiptError("terminal receipt schema is invalid")

    @property
    def receipt_ref(self) -> str:
        return terminal_receipt_ref(self.effect_operation_id)

    @property
    def receipt_kind(self) -> str:
        return lifecycle_contract(self.action).receipt_kind

    @property
    def receipt_schema(self) -> str:
        return lifecycle_contract(self.action).receipt_schema


@dataclass(frozen=True, slots=True)
class ExternalExtensionReceiptExpectation:
    action: str
    root_id: str
    revision_ref: str
    intake_ref: str
    artifact_ref: str
    artifact_receipt_ref: str
    artifact_content_sha256: str
    manifest_identity: str
    review_plan_identity: str
    activation_plan_identity: str
    health_plan_identity: str


@dataclass(frozen=True, slots=True)
class LifecycleOutcome:
    passed: bool | None = None
    observed_checks: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LifecycleProbeOutcome:
    state: EffectState
    evidence_ref: str
    passed: bool | None = None
    observed_checks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.state not in {
            EffectState.PLANNED,
            EffectState.SETTLED_OK,
            EffectState.SETTLED_ERR,
            EffectState.UNKNOWN,
        }:
            raise ExternalExtensionTerminalReceiptError("lifecycle probe state is invalid")
        _internal_ref(self.evidence_ref, "lifecycle probe evidence reference")
        object.__setattr__(self, "observed_checks", _checks(self.observed_checks))
        if self.state is not EffectState.SETTLED_OK and (
            self.passed is not None or self.observed_checks
        ):
            raise ExternalExtensionTerminalReceiptError(
                "non-success lifecycle probe carries a terminal outcome"
            )


class LifecycleExecutor(Protocol):
    def execute(self, intent: ExternalExtensionLifecycleIntent, effect: Effect) -> LifecycleOutcome: ...

    def probe(
        self, intent: ExternalExtensionLifecycleIntent, effect: Effect,
    ) -> LifecycleProbeOutcome: ...


class ExternalExtensionTerminalReceiptStore:
    """Immutable lifecycle facts; never an execution-state authority."""

    def __init__(self, records: SQLiteStructuredRecordStore) -> None:
        if not isinstance(records, SQLiteStructuredRecordStore):
            raise TypeError("terminal receipt store requires structured records")
        self._records = records

    def record_intent(self, intent: ExternalExtensionLifecycleIntent) -> str:
        if not isinstance(intent, ExternalExtensionLifecycleIntent):
            raise TypeError("lifecycle intent is invalid")
        payload = _canonical({
            **asdict(intent), "health_checks": list(intent.health_checks), "intent_ref": intent.intent_ref,
        })
        self._put_immutable(_INTENTS, intent.intent_id, payload, "lifecycle intent")
        return intent.intent_ref

    def load_intent(self, reference: str) -> ExternalExtensionLifecycleIntent:
        identity = _lifecycle_intent_id(reference)
        record = self._records.read(_INTENTS, identity)
        if record is None:
            raise ExternalExtensionTerminalReceiptError("lifecycle intent is missing")
        payload = record.payload
        try:
            if payload.get("intent_ref") != lifecycle_intent_ref(identity):
                raise ValueError
            values = {key: value for key, value in payload.items() if key != "intent_ref"}
            values["health_checks"] = tuple(values.get("health_checks", ()))
            intent = ExternalExtensionLifecycleIntent(**values)
        except (TypeError, ValueError) as error:
            raise ExternalExtensionTerminalReceiptConflict("lifecycle intent is invalid") from error
        if intent.intent_id != identity:
            raise ExternalExtensionTerminalReceiptConflict("lifecycle intent identity drifted")
        return intent

    def record_effect_receipt(
        self,
        effect: Effect,
        *,
        passed: bool | None = None,
        observed_checks: tuple[str, ...] = (),
    ) -> ExternalExtensionTerminalReceipt:
        if not isinstance(effect, Effect):
            raise TypeError("effect is invalid")
        intent = self.load_intent(effect.intent_ref)
        _require_effect_identity(effect, intent)
        if effect.state is not EffectState.INFLIGHT:
            raise ExternalExtensionTerminalReceiptConflict("receipt requires an inflight lifecycle Effect")
        checks = _checks(observed_checks)
        if intent.action == "health":
            if not isinstance(passed, bool) or checks != intent.health_checks:
                raise ExternalExtensionTerminalReceiptConflict("health outcome does not bind lifecycle intent")
        elif passed is not None or checks:
            raise ExternalExtensionTerminalReceiptConflict("non-health outcome is invalid")
        receipt = ExternalExtensionTerminalReceipt(
            effect_operation_id=effect.operation_id,
            action=intent.action,
            intent_ref=intent.intent_ref,
            intent_digest=effect.intent_digest,
            root_id=intent.root_id,
            revision_ref=intent.revision_ref,
            intake_ref=intent.intake_ref,
            artifact_ref=intent.artifact_ref,
            artifact_receipt_ref=intent.artifact_receipt_ref,
            artifact_content_sha256=intent.artifact_content_sha256,
            manifest_identity=intent.manifest_identity,
            review_plan_identity=intent.review_plan_identity,
            activation_plan_identity=intent.activation_plan_identity,
            health_plan_identity=intent.health_plan_identity,
            passed=passed,
            observed_checks=checks,
        )
        payload = _canonical({
            **asdict(receipt),
            "observed_checks": list(receipt.observed_checks),
            "receipt_ref": receipt.receipt_ref,
            "receipt_kind": receipt.receipt_kind,
            "receipt_schema": receipt.receipt_schema,
        })
        self._put_immutable(_RECEIPTS, receipt.effect_operation_id, payload, "terminal receipt")
        return receipt

    def find_receipt(self, effect_operation_id: str) -> ExternalExtensionTerminalReceipt | None:
        operation = _id(effect_operation_id, "effect operation id")
        return self.load_receipt(operation) if self._records.read(_RECEIPTS, operation) is not None else None

    def load_receipt(self, effect_operation_id: str) -> ExternalExtensionTerminalReceipt:
        operation = _id(effect_operation_id, "effect operation id")
        record = self._records.read(_RECEIPTS, operation)
        if record is None:
            raise ExternalExtensionTerminalReceiptError("terminal receipt is missing")
        payload = record.payload
        try:
            values = {
                key: value for key, value in payload.items()
                if key not in {"receipt_ref", "receipt_kind", "receipt_schema"}
            }
            values["observed_checks"] = tuple(values.get("observed_checks", ()))
            receipt = ExternalExtensionTerminalReceipt(**values)
        except (TypeError, ValueError) as error:
            raise ExternalExtensionTerminalReceiptConflict("terminal receipt is invalid") from error
        if (
            receipt.effect_operation_id != operation
            or payload.get("receipt_ref") != receipt.receipt_ref
            or payload.get("receipt_kind") != receipt.receipt_kind
            or payload.get("receipt_schema") != receipt.receipt_schema
        ):
            raise ExternalExtensionTerminalReceiptConflict("terminal receipt contract drifted")
        intent = self.load_intent(receipt.intent_ref)
        _require_receipt_intent(receipt, intent)
        return receipt

    def completed_receipt(
        self, effect: Effect,
    ) -> ExternalExtensionTerminalReceipt | None:
        intent = self.load_intent(effect.intent_ref)
        _require_effect_identity(effect, intent)
        receipt = self.find_receipt(effect.operation_id)
        if receipt is None:
            return None
        _require_receipt_effect(receipt, effect)
        return receipt

    def _put_immutable(
        self, collection: str, identity: str, payload: Mapping[str, object], label: str,
    ) -> None:
        canonical = _canonical(payload)
        try:
            with self._records.begin() as uow:
                existing = uow.read(collection, identity)
                if existing is None:
                    uow.put(collection, identity, canonical, expected_revision=0)
                elif _canonical(existing.payload) != canonical:
                    raise ExternalExtensionTerminalReceiptConflict(f"{label} drifted")
                uow.commit()
        except SQLiteUnitOfWorkConflict as error:
            raise ExternalExtensionTerminalReceiptConflict(str(error)) from error


class ExternalExtensionLifecycleHandler:
    """Effect Handler strategy; execution transport is supplied as a narrow port."""

    def __init__(
        self,
        receipts: ExternalExtensionTerminalReceiptStore,
        executor: LifecycleExecutor,
        *,
        frozen_identity_validator: Callable[[Effect], None] | None = None,
    ) -> None:
        self._receipts = receipts
        self._executor = executor
        self._frozen_identity_validator = frozen_identity_validator

    def _require_frozen_identity(self, effect: Effect) -> None:
        """Block Handler/Reaper IO unless durable command authority validates it."""
        if self._frozen_identity_validator is None:
            raise ExternalExtensionTerminalReceiptConflict(
                "lifecycle Handler has no frozen identity authority"
            )
        self._frozen_identity_validator(effect)

    def __call__(self, effect: Effect) -> EffectReceipt:
        self._require_frozen_identity(effect)
        intent = self._receipts.load_intent(effect.intent_ref)
        _require_effect_identity(effect, intent)
        existing = self._receipts.find_receipt(effect.operation_id)
        if existing is None:
            outcome = self._executor.execute(intent, effect)
            if not isinstance(outcome, LifecycleOutcome):
                raise ExternalExtensionTerminalReceiptError("lifecycle executor returned invalid outcome")
            existing = self._receipts.record_effect_receipt(
                effect, passed=outcome.passed, observed_checks=outcome.observed_checks,
            )
        _require_receipt_effect(existing, effect)
        return EffectReceipt(
            existing.receipt_ref, existing.receipt_kind, existing.receipt_schema,
            LIFECYCLE_INTENT_SCHEMA,
        )

    def probe(self, effect: Effect) -> tuple[EffectState, str | None]:
        self._require_frozen_identity(effect)
        intent = self._receipts.load_intent(effect.intent_ref)
        _require_effect_identity(effect, intent)
        completed = self._receipts.completed_receipt(effect)
        if completed is not None:
            return EffectState.SETTLED_OK, completed.receipt_ref
        outcome = self._executor.probe(intent, effect)
        if not isinstance(outcome, LifecycleProbeOutcome):
            raise ExternalExtensionTerminalReceiptError(
                "lifecycle executor returned an invalid probe outcome"
            )
        if outcome.state is EffectState.SETTLED_OK:
            completed = self._receipts.record_effect_receipt(
                effect,
                passed=outcome.passed,
                observed_checks=outcome.observed_checks,
            )
            return EffectState.SETTLED_OK, completed.receipt_ref
        return outcome.state, outcome.evidence_ref


def register_external_extension_lifecycle_handlers(
    registry: EffectHandlerRegistry,
    receipts: ExternalExtensionTerminalReceiptStore,
    executor: LifecycleExecutor,
    *,
    frozen_identity_validator: Callable[[Effect], None] | None = None,
) -> ExternalExtensionLifecycleHandler:
    """Register the closed lifecycle contract on the Core-owned registry."""

    if not isinstance(registry, EffectHandlerRegistry):
        raise TypeError("lifecycle registration requires the Core handler registry")
    handler = ExternalExtensionLifecycleHandler(
        receipts, executor, frozen_identity_validator=frozen_identity_validator,
    )
    for contract in _CONTRACTS.values():
        registry.register(
            EffectHandlerRegistration(
                kind=contract.effect_kind,
                effect_class=EffectClass.QUERYABLE,
                handler=handler,
                probe=handler.probe,
                contract_version=EFFECT_V2,
                intent_schema_version=LIFECYCLE_INTENT_SCHEMA,
                receipt_kind=contract.receipt_kind,
                receipt_schema_version=contract.receipt_schema,
            )
        )
    return handler


class ExternalExtensionTerminalReceiptVerifier:
    """Join Core terminal state with immutable lifecycle intent and Receipt facts."""

    def __init__(self, effects: EffectLog, receipts: ExternalExtensionTerminalReceiptStore) -> None:
        if not isinstance(effects, EffectLog) or not isinstance(receipts, ExternalExtensionTerminalReceiptStore):
            raise TypeError("terminal verifier requires Core EffectLog and receipt store")
        self._effects = effects
        self._receipts = receipts

    def verify(
        self, effect_operation_id: str, *, expected: ExternalExtensionReceiptExpectation,
    ) -> ExternalExtensionTerminalReceipt:
        if not isinstance(expected, ExternalExtensionReceiptExpectation):
            raise TypeError("terminal receipt expectation is invalid")
        effect = self._effects.get(_id(effect_operation_id, "effect operation id"))
        if effect.contract_version != EFFECT_V2 or effect.state is not EffectState.SETTLED_OK:
            raise ExternalExtensionTerminalReceiptConflict("effect is not settled Effect v2")
        intent = self._receipts.load_intent(effect.intent_ref)
        _require_effect_identity(effect, intent)
        receipt = self._receipts.load_receipt(effect.operation_id)
        _require_receipt_effect(receipt, effect)
        contract = lifecycle_contract(expected.action)
        if (
            effect.result_ref != receipt.receipt_ref
            or effect.expected_receipt_kind != contract.receipt_kind
            or effect.expected_receipt_schema_version != contract.receipt_schema
        ):
            raise ExternalExtensionTerminalReceiptConflict("effect terminal Receipt binding is invalid")
        expected_fields = (
            expected.action, expected.root_id, expected.revision_ref, expected.intake_ref,
            expected.artifact_ref, expected.artifact_receipt_ref,
            expected.artifact_content_sha256, expected.manifest_identity,
            expected.review_plan_identity, expected.activation_plan_identity,
            expected.health_plan_identity,
        )
        actual_fields = (
            receipt.action, receipt.root_id, receipt.revision_ref, receipt.intake_ref,
            receipt.artifact_ref, receipt.artifact_receipt_ref,
            receipt.artifact_content_sha256, receipt.manifest_identity,
            receipt.review_plan_identity, receipt.activation_plan_identity,
            receipt.health_plan_identity,
        )
        if actual_fields != expected_fields:
            raise ExternalExtensionTerminalReceiptConflict(
                "terminal Receipt does not bind installation revision"
            )
        return receipt


def build_lifecycle_effect_intent(
    intent: ExternalExtensionLifecycleIntent,
    *,
    session_id: str,
    step_key: str,
    gate_decision_id: str,
    rev_set: Mapping[str, object],
    turn_id: str | None = None,
    parent_id: str | None = None,
    idem_key: str | None = None,
) -> EffectIntent:
    if not isinstance(intent, ExternalExtensionLifecycleIntent):
        raise TypeError("lifecycle intent is invalid")
    contract = lifecycle_contract(intent.action)
    return EffectIntent(
        session_id=session_id,
        turn_id=turn_id,
        root_id=intent.root_id,
        parent_id=parent_id,
        step_key=step_key,
        kind=contract.effect_kind,
        effect_class=EffectClass.QUERYABLE,
        purpose=EffectPurpose.PRIMARY,
        intent_ref=intent.intent_ref,
        gate_decision_id=gate_decision_id,
        rev_set=rev_set,
        idem_key=idem_key,
        payload=intent.effect_payload,
        contract_version=EFFECT_V2,
        intent_schema_version=LIFECYCLE_INTENT_SCHEMA,
        expected_receipt_kind=contract.receipt_kind,
        expected_receipt_schema_version=contract.receipt_schema,
    )


def lifecycle_contract(action: str) -> LifecycleEffectContract:
    try:
        return _CONTRACTS[action]
    except (KeyError, TypeError) as error:
        raise ExternalExtensionTerminalReceiptError("lifecycle action is invalid") from error


def lifecycle_intent_ref(intent_id: str) -> str:
    return _INTENT_PREFIX + _id(intent_id, "lifecycle intent id")


def terminal_receipt_ref(effect_operation_id: str) -> str:
    return _RECEIPT_PREFIX + _id(effect_operation_id, "effect operation id")


def terminal_receipt_operation_id(reference: str) -> str:
    if not isinstance(reference, str) or not reference.startswith(_RECEIPT_PREFIX):
        raise ExternalExtensionTerminalReceiptError("terminal receipt reference is invalid")
    return _id(reference.removeprefix(_RECEIPT_PREFIX), "effect operation id")


def _require_effect_identity(effect: Effect, intent: ExternalExtensionLifecycleIntent) -> None:
    expected = build_lifecycle_effect_intent(
        intent,
        session_id=effect.session_id,
        turn_id=effect.turn_id,
        parent_id=effect.parent_id,
        step_key=effect.step_key,
        gate_decision_id=effect.gate_decision_id,
        rev_set=effect.rev_set,
        idem_key=effect.idem_key,
    )
    fields = (
        "operation_id", "session_id", "turn_id", "root_id", "parent_id", "step_key",
        "kind", "effect_class", "purpose", "intent_ref", "intent_digest",
        "gate_decision_id", "idem_key", "contract_version", "intent_schema_version",
        "expected_receipt_kind", "expected_receipt_schema_version",
    )
    for field in fields:
        expected_value = expected.operation_id if field == "operation_id" else getattr(expected, field)
        if getattr(effect, field) != expected_value:
            raise ExternalExtensionTerminalReceiptConflict("effect does not bind lifecycle intent")
    if effect.authority_set_id != expected.authority_set_id:
        raise ExternalExtensionTerminalReceiptConflict("effect authority revision set drifted")


def _require_receipt_intent(
    receipt: ExternalExtensionTerminalReceipt, intent: ExternalExtensionLifecycleIntent,
) -> None:
    if (
        receipt.action, receipt.intent_ref, receipt.root_id, receipt.revision_ref,
        receipt.intake_ref, receipt.artifact_ref, receipt.artifact_receipt_ref,
        receipt.artifact_content_sha256, receipt.manifest_identity,
        receipt.review_plan_identity, receipt.activation_plan_identity,
        receipt.health_plan_identity,
    ) != (
        intent.action, intent.intent_ref, intent.root_id, intent.revision_ref,
        intent.intake_ref, intent.artifact_ref, intent.artifact_receipt_ref,
        intent.artifact_content_sha256, intent.manifest_identity,
        intent.review_plan_identity, intent.activation_plan_identity,
        intent.health_plan_identity,
    ):
        raise ExternalExtensionTerminalReceiptConflict("terminal Receipt intent binding drifted")
    if intent.action == "health" and receipt.observed_checks != intent.health_checks:
        raise ExternalExtensionTerminalReceiptConflict("health Receipt checks drifted")


def _require_receipt_effect(receipt: ExternalExtensionTerminalReceipt, effect: Effect) -> None:
    if (
        receipt.effect_operation_id != effect.operation_id
        or receipt.intent_ref != effect.intent_ref
        or receipt.intent_digest != effect.intent_digest
        or receipt.root_id != effect.root_id
    ):
        raise ExternalExtensionTerminalReceiptConflict("terminal Receipt Effect identity drifted")


def _lifecycle_intent_id(reference: str) -> str:
    if not isinstance(reference, str) or not reference.startswith(_INTENT_PREFIX):
        raise ExternalExtensionTerminalReceiptError("lifecycle intent reference is invalid")
    return _id(reference.removeprefix(_INTENT_PREFIX), "lifecycle intent id")


def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ExternalExtensionTerminalReceiptError(f"{label} is invalid")
    return value


def _ref(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 224
        or not re.fullmatch(r"crp:[A-Za-z0-9._:/@+\-~]{1,200}", value)
    ):
        raise ExternalExtensionTerminalReceiptError("lifecycle reference is invalid")
    return value


def _internal_ref(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 176
        or any(char.isspace() or ord(char) < 32 for char in value)
        or not re.fullmatch(
            r"(?:crp|facts|lease|intent|gate|receipt|error|decision|rule|scope|policy|budget|boundary|capability|provider|model|bundle|handler):[A-Za-z0-9._:/@+\-]{1,160}",
            value,
        )
    ):
        raise ExternalExtensionTerminalReceiptError(f"{label} is invalid")
    return value


def _root(value: object) -> str:
    if not isinstance(value, str) or not _ROOT.fullmatch(value):
        raise ExternalExtensionTerminalReceiptError("lifecycle root id is invalid")
    return value


def _sha(value: object) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ExternalExtensionTerminalReceiptError("lifecycle digest is invalid")
    return value


def _checks(value: object) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise ExternalExtensionTerminalReceiptError("lifecycle checks are invalid")
    checks = tuple(value)
    if (
        tuple(sorted(set(checks))) != checks
        or any(not isinstance(item, str) or not _ID.fullmatch(item) for item in checks)
    ):
        raise ExternalExtensionTerminalReceiptError("lifecycle checks are invalid")
    return checks


def _canonical(value: Mapping[str, object]) -> dict[str, object]:
    try:
        result = json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    except (TypeError, ValueError) as error:
        raise ExternalExtensionTerminalReceiptError("lifecycle fact is not serializable") from error
    if not isinstance(result, dict):
        raise ExternalExtensionTerminalReceiptError("lifecycle fact is invalid")
    return result
