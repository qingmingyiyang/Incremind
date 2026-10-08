from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
from typing import Literal

from core.ai_tooling import (
    ToolContractError,
    ToolDefinition,
    tool_contract_identity,
    validate_tool_contract_identity,
)

from .contracts import AIKernelContractError, validate_governed_payload


InvocationStatus = Literal["completed", "failed", "cancelled", "timed_out", "unknown_effect"]
EffectCertainty = Literal["confirmed_none", "confirmed_applied", "unknown"]


_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_SESSION_REF_RE = re.compile(r"^crp://session/[A-Za-z0-9._~:/-]+$")


@dataclass(frozen=True, slots=True)
class ToolInvocationIntent:
    invocation_id: str
    turn_id: str
    step_id: str
    capability_id: str
    capability_version: int
    operation_id: str
    idempotency_key: str
    execution_mode: str
    resource_locks: tuple[str, ...]
    idempotency: str
    max_attempts: int
    retry_backoff_ms: int
    retryable_error_codes: tuple[str, ...]
    timeout_ms: int
    tool_contract: Mapping[str, object] | None
    requires_approval: bool | None
    arguments: Mapping[str, object]
    authorization_facts_ref: str | None = None
    authorization_facts_revision: str | None = None
    approval_fact_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ToolInvocationOutcome:
    invocation_id: str
    turn_id: str
    capability_id: str
    attempt: int
    status: InvocationStatus
    effect_certainty: EffectCertainty
    payload_ref: str | None
    receipt_ref: str | None
    evidence_refs: tuple[str, ...]
    error_code: str | None
    retryable: bool


@dataclass(frozen=True, slots=True)
class ToolAttemptFailure:
    invocation_id: str
    turn_id: str
    capability_id: str
    attempt: int
    error_code: str
    effect_certainty: Literal["confirmed_none"]
    backoff_ms: int


def build_intent(
    *,
    invocation_id: str,
    turn_id: str,
    step_id: str,
    operation_id: str,
    tool: ToolDefinition,
    arguments: Mapping[str, object],
    requires_approval: bool = False,
    authorization_facts_ref: str | None = None,
    authorization_facts_revision: str | None = None,
    approval_fact_ref: str | None = None,
) -> ToolInvocationIntent:
    facts_ref, facts_revision, approval_ref = _authorization_fact_fields(
        authorization_facts_ref=authorization_facts_ref,
        authorization_facts_revision=authorization_facts_revision,
        approval_fact_ref=approval_fact_ref,
    )
    intent = ToolInvocationIntent(
        invocation_id=_required_text(invocation_id, "invocation_id"),
        turn_id=_required_text(turn_id, "turn_id"),
        step_id=_required_text(step_id, "step_id"),
        capability_id=tool.tool_id,
        capability_version=tool.version,
        operation_id=_required_text(operation_id, "operation_id"),
        idempotency_key=f"{operation_id}:{invocation_id}",
        execution_mode=tool.execution_mode,
        resource_locks=tool.resource_locks,
        idempotency=tool.idempotency,
        max_attempts=tool.retry_policy.max_attempts,
        retry_backoff_ms=tool.retry_policy.backoff_ms,
        retryable_error_codes=tool.retry_policy.retryable_error_codes,
        timeout_ms=tool.timeout_ms,
        tool_contract=tool_contract_identity(tool),
        requires_approval=requires_approval,
        arguments=dict(arguments),
        authorization_facts_ref=facts_ref,
        authorization_facts_revision=facts_revision,
        approval_fact_ref=approval_ref,
    )
    validate_governed_payload(intent_to_payload(intent))
    return intent


def intent_to_payload(intent: ToolInvocationIntent) -> dict[str, object]:
    facts_ref, facts_revision, approval_ref = _authorization_fact_fields(
        authorization_facts_ref=intent.authorization_facts_ref,
        authorization_facts_revision=intent.authorization_facts_revision,
        approval_fact_ref=intent.approval_fact_ref,
    )
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "invocation_id": intent.invocation_id,
        "turn_id": intent.turn_id,
        "step_id": intent.step_id,
        "capability_id": intent.capability_id,
        "capability_version": intent.capability_version,
        "operation_id": intent.operation_id,
        "idempotency_key": intent.idempotency_key,
        "execution_mode": intent.execution_mode,
        "resource_locks": list(intent.resource_locks),
        "idempotency": intent.idempotency,
        "max_attempts": intent.max_attempts,
        "retry_backoff_ms": intent.retry_backoff_ms,
        "retryable_error_codes": list(intent.retryable_error_codes),
        "timeout_ms": intent.timeout_ms,
        "tool_contract": dict(intent.tool_contract) if intent.tool_contract is not None else None,
        "requires_approval": intent.requires_approval,
        "arguments": dict(intent.arguments),
    }
    # Keep the v1 wire shape byte-for-byte compatible when no fact was frozen.
    # New readers may consume these optional fields without requiring a schema
    # version split, while historical v1 readers still receive their old shape.
    if facts_ref is not None:
        payload["authorization_facts_ref"] = facts_ref
        payload["authorization_facts_revision"] = facts_revision
    if approval_ref is not None:
        payload["approval_fact_ref"] = approval_ref
    return payload


def intent_from_payload(payload: object) -> ToolInvocationIntent:
    value = _shape(
        payload,
        "tool invocation intent",
        {
            "schema_version",
            "invocation_id",
            "turn_id",
            "step_id",
            "capability_id",
            "capability_version",
            "operation_id",
            "idempotency_key",
            "execution_mode",
            "resource_locks",
            "idempotency",
            "max_attempts",
            "retry_backoff_ms",
            "retryable_error_codes",
            "timeout_ms",
            "tool_contract",
            "requires_approval",
            "arguments",
            "authorization_facts_ref",
            "authorization_facts_revision",
            "approval_fact_ref",
        },
        optional={
            "tool_contract",
            "requires_approval",
            "authorization_facts_ref",
            "authorization_facts_revision",
            "approval_fact_ref",
        },
    )
    if value["schema_version"] != "1.0.0":
        raise AIKernelContractError("tool invocation intent version is unsupported")
    arguments = value["arguments"]
    if not isinstance(arguments, Mapping):
        raise AIKernelContractError("tool invocation arguments must be an object")
    contract = value.get("tool_contract")
    try:
        tool_contract = None if contract is None else validate_tool_contract_identity(contract)
    except ToolContractError as error:
        raise AIKernelContractError(str(error)) from error
    requires_approval = value.get("requires_approval")
    if requires_approval is not None and not isinstance(requires_approval, bool):
        raise AIKernelContractError("requires_approval must be boolean")
    facts_ref, facts_revision, approval_ref = _authorization_fact_fields(
        authorization_facts_ref=value.get("authorization_facts_ref"),
        authorization_facts_revision=value.get("authorization_facts_revision"),
        approval_fact_ref=value.get("approval_fact_ref"),
    )
    intent = ToolInvocationIntent(
        invocation_id=_required_text(value["invocation_id"], "invocation_id"),
        turn_id=_required_text(value["turn_id"], "turn_id"),
        step_id=_required_text(value["step_id"], "step_id"),
        capability_id=_required_text(value["capability_id"], "capability_id"),
        capability_version=_positive_int(value["capability_version"], "capability_version"),
        operation_id=_required_text(value["operation_id"], "operation_id"),
        idempotency_key=_required_text(value["idempotency_key"], "idempotency_key"),
        execution_mode=_enum(value["execution_mode"], {"parallel", "exclusive"}, "execution_mode"),
        resource_locks=_texts(value["resource_locks"], "resource_locks"),
        idempotency=_enum(
            value["idempotency"],
            {"idempotent", "verify_before_retry", "never_retry"},
            "idempotency",
        ),
        max_attempts=_positive_int(value["max_attempts"], "max_attempts"),
        retry_backoff_ms=_non_negative_int(value["retry_backoff_ms"], "retry_backoff_ms"),
        retryable_error_codes=_texts(value["retryable_error_codes"], "retryable_error_codes"),
        timeout_ms=_positive_int(value["timeout_ms"], "timeout_ms"),
        tool_contract=tool_contract,
        requires_approval=requires_approval,
        arguments=dict(arguments),
        authorization_facts_ref=facts_ref,
        authorization_facts_revision=facts_revision,
        approval_fact_ref=approval_ref,
    )
    if intent.idempotency == "never_retry" and intent.max_attempts != 1:
        raise AIKernelContractError("never-retry invocation must use one attempt")
    if intent.retry_backoff_ms > 300_000:
        raise AIKernelContractError("retry_backoff_ms is outside the supported range")
    if intent.max_attempts == 1 and intent.retryable_error_codes:
        raise AIKernelContractError("single-attempt invocation cannot declare retryable errors")
    validate_governed_payload(intent_to_payload(intent))
    return intent


def outcome_to_payload(outcome: ToolInvocationOutcome) -> dict[str, object]:
    payload = {
        "schema_version": "1.0.0",
        "invocation_id": outcome.invocation_id,
        "turn_id": outcome.turn_id,
        "capability_id": outcome.capability_id,
        "attempt": outcome.attempt,
        "status": outcome.status,
        "effect_certainty": outcome.effect_certainty,
        "payload_ref": outcome.payload_ref,
        "receipt_ref": outcome.receipt_ref,
        "evidence_refs": list(outcome.evidence_refs),
        "error_code": outcome.error_code,
        "retryable": outcome.retryable,
    }
    validate_governed_payload(payload)
    return payload


def outcome_from_payload(payload: object) -> ToolInvocationOutcome:
    value = _shape(
        payload,
        "tool invocation outcome",
        {
            "schema_version", "invocation_id", "turn_id", "capability_id",
            "attempt", "status", "effect_certainty", "payload_ref",
            "receipt_ref", "evidence_refs", "error_code", "retryable",
        },
    )
    if value["schema_version"] != "1.0.0":
        raise AIKernelContractError("tool invocation outcome version is unsupported")
    status = _enum(
        value["status"],
        {"completed", "failed", "cancelled", "timed_out", "unknown_effect"},
        "tool invocation outcome status",
    )
    certainty = _enum(
        value["effect_certainty"],
        {"confirmed_none", "confirmed_applied", "unknown"},
        "tool invocation outcome effect certainty",
    )
    if status == "unknown_effect" and certainty != "unknown":
        raise AIKernelContractError("unknown tool outcome must have unknown effect certainty")
    if certainty == "unknown" and status != "unknown_effect":
        raise AIKernelContractError("unknown effect certainty requires unknown tool outcome")
    retryable = value["retryable"]
    if not isinstance(retryable, bool):
        raise AIKernelContractError("tool invocation outcome retryable is invalid")
    refs = _texts(value["evidence_refs"], "tool invocation outcome evidence refs")
    payload_ref = _optional_text(value["payload_ref"], "tool invocation outcome payload ref")
    receipt_ref = _optional_text(value["receipt_ref"], "tool invocation outcome receipt ref")
    error_code = _optional_text(value["error_code"], "tool invocation outcome error code")
    if status == "completed" and error_code is not None:
        raise AIKernelContractError("completed tool outcome cannot carry an error code")
    if status != "completed" and error_code is None:
        raise AIKernelContractError("non-completed tool outcome requires an error code")
    outcome = ToolInvocationOutcome(
        invocation_id=_required_text(value["invocation_id"], "invocation_id"),
        turn_id=_required_text(value["turn_id"], "turn_id"),
        capability_id=_required_text(value["capability_id"], "capability_id"),
        attempt=_positive_int(value["attempt"], "attempt"),
        status=status,  # type: ignore[arg-type]
        effect_certainty=certainty,  # type: ignore[arg-type]
        payload_ref=payload_ref,
        receipt_ref=receipt_ref,
        evidence_refs=refs,
        error_code=error_code,
        retryable=retryable,
    )
    validate_governed_payload(outcome_to_payload(outcome))
    return outcome


def attempt_failure_to_payload(failure: ToolAttemptFailure) -> dict[str, object]:
    payload = {
        "schema_version": "1.0.0",
        "invocation_id": failure.invocation_id,
        "turn_id": failure.turn_id,
        "capability_id": failure.capability_id,
        "attempt": failure.attempt,
        "error_code": failure.error_code,
        "effect_certainty": failure.effect_certainty,
        "backoff_ms": failure.backoff_ms,
    }
    validate_governed_payload(payload)
    return payload


def attempt_failure_from_payload(payload: object) -> ToolAttemptFailure:
    value = _shape(
        payload,
        "tool attempt failure",
        {
            "schema_version",
            "invocation_id",
            "turn_id",
            "capability_id",
            "attempt",
            "error_code",
            "effect_certainty",
            "backoff_ms",
        },
    )
    if value["schema_version"] != "1.0.0":
        raise AIKernelContractError("tool attempt failure version is unsupported")
    if value["effect_certainty"] != "confirmed_none":
        raise AIKernelContractError("retryable attempt must confirm no effect")
    backoff_ms = _non_negative_int(value["backoff_ms"], "backoff_ms")
    if backoff_ms > 300_000:
        raise AIKernelContractError("backoff_ms is outside the supported range")
    failure = ToolAttemptFailure(
        invocation_id=_required_text(value["invocation_id"], "invocation_id"),
        turn_id=_required_text(value["turn_id"], "turn_id"),
        capability_id=_required_text(value["capability_id"], "capability_id"),
        attempt=_positive_int(value["attempt"], "attempt"),
        error_code=_required_text(value["error_code"], "error_code"),
        effect_certainty="confirmed_none",
        backoff_ms=backoff_ms,
    )
    validate_governed_payload(attempt_failure_to_payload(failure))
    return failure


def _shape(
    value: object,
    label: str,
    fields: set[str],
    *,
    optional: set[str] = frozenset(),
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise AIKernelContractError(f"{label} must be an object")
    result = dict(value)
    actual = {str(key) for key in result}
    if not fields - optional <= actual <= fields:
        raise AIKernelContractError(f"{label} fields are invalid")
    return result


def _required_text(value: object, label: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise AIKernelContractError(f"{label} must be non-empty")
    return text


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, label)


def _authorization_fact_fields(
    *,
    authorization_facts_ref: object,
    authorization_facts_revision: object,
    approval_fact_ref: object,
) -> tuple[str | None, str | None, str | None]:
    """Validate optional frozen authorization evidence without widening v1 IDs.

    An authorization reference is meaningful only together with its immutable
    revision.  Approval evidence is independent because read-only actions may
    use authorization facts without a user approval fact.
    """

    facts_ref = _optional_session_ref(authorization_facts_ref, "authorization_facts_ref")
    facts_revision = _optional_safe_identifier(
        authorization_facts_revision, "authorization_facts_revision"
    )
    if (facts_ref is None) != (facts_revision is None):
        raise AIKernelContractError(
            "authorization_facts_ref and authorization_facts_revision must be provided together"
        )
    return (
        facts_ref,
        facts_revision,
        _optional_session_ref(approval_fact_ref, "approval_fact_ref"),
    )


def _optional_session_ref(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SESSION_REF_RE.fullmatch(value):
        raise AIKernelContractError(f"{label} must be a safe session crp reference")
    return value


def _optional_safe_identifier(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SAFE_IDENTIFIER_RE.fullmatch(value):
        raise AIKernelContractError(f"{label} must be a safe identifier")
    return value


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise AIKernelContractError(f"{label} must be positive")
    return value


def _non_negative_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AIKernelContractError(f"{label} must be non-negative")
    return value


def _enum(value: object, allowed: set[str], label: str) -> str:
    text = _required_text(value, label)
    if text not in allowed:
        raise AIKernelContractError(f"{label} is unsupported")
    return text


def _texts(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise AIKernelContractError(f"{label} must be an array")
    result = tuple(_required_text(item, label) for item in value)
    if len(result) != len(set(result)):
        raise AIKernelContractError(f"{label} must be unique")
    return result
