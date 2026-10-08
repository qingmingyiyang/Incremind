"""Safe, immutable execution evidence for one Media Hands provider effect.

This module intentionally owns no store and performs no provider calls.  It
gives a durable adapter a small, replay-safe contract: once an external media
effect may have started, an indeterminate failure is quarantined as
``unknown_effect``.  A later verified receipt may reconcile that record to
``completed``; it must never cause a second provider effect by itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Literal


MediaOperationExecutionState = Literal["started", "unknown_effect", "completed"]

SCHEMA_VERSION = "1.0.0"
EVIDENCE_KIND = "media_operation_execution_evidence"

_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_OPERATION = re.compile(r"^[a-z][a-z0-9_.-]{2,79}$")
_CRP_REF = re.compile(r"^crp://[A-Za-z0-9][A-Za-z0-9._/-]{0,319}$")
_STATES = frozenset({"started", "unknown_effect", "completed"})
_SENSITIVE_KEYS = frozenset({
    "api_key", "apikey", "authorization", "bytes", "content", "cookie",
    "cookies", "endpoint", "local_path", "password", "path", "prompt",
    "secret", "token", "url", "uri",
})
_PAYLOAD_FIELDS = frozenset({
    "schema_version", "kind", "job_id", "source_id", "operation", "manifest_ref",
    "manifest_revision", "provider_id", "provider_revision", "execution_id", "state",
    "receipt_ref",
})


@dataclass(frozen=True, slots=True)
class MediaOperationExecutionEvidence:
    """Non-sensitive lifecycle evidence for one provider execution attempt."""

    job_id: str
    source_id: str
    operation: str
    manifest_ref: str
    manifest_revision: str
    provider_id: str
    provider_revision: str
    execution_id: str
    state: MediaOperationExecutionState
    receipt_ref: str | None = None


def validate_media_operation_execution_evidence(
    evidence: MediaOperationExecutionEvidence,
) -> MediaOperationExecutionEvidence:
    """Normalize and validate one typed evidence value without side effects."""

    if not isinstance(evidence, MediaOperationExecutionEvidence):
        raise TypeError("media operation execution evidence must be typed")
    state = evidence.state
    if state not in _STATES:
        raise ValueError("media operation execution state is invalid")
    clean = MediaOperationExecutionEvidence(
        job_id=_identity(evidence.job_id, "job id"),
        source_id=_identity(evidence.source_id, "source id"),
        operation=_operation(evidence.operation),
        manifest_ref=_manifest_ref(evidence.manifest_ref),
        manifest_revision=_identity(evidence.manifest_revision, "manifest revision"),
        provider_id=_identity(evidence.provider_id, "provider id"),
        provider_revision=_identity(evidence.provider_revision, "provider revision"),
        execution_id=_identity(evidence.execution_id, "execution id"),
        state=state,
        receipt_ref=_receipt_ref(evidence.receipt_ref),
    )
    if clean.state == "completed":
        if clean.receipt_ref is None:
            raise ValueError("completed media operation execution requires a receipt reference")
    elif clean.receipt_ref is not None:
        raise ValueError("incomplete media operation execution contains a receipt reference")
    return clean


def can_transition_media_operation_execution_evidence(
    current: MediaOperationExecutionEvidence,
    candidate: MediaOperationExecutionEvidence,
) -> bool:
    """Return whether a lifecycle change is valid; reconciliation is unknown→completed."""

    current = validate_media_operation_execution_evidence(current)
    candidate = validate_media_operation_execution_evidence(candidate)
    if _identity_tuple(current) != _identity_tuple(candidate):
        return False
    return (
        current.state == "started" and candidate.state in {"unknown_effect", "completed"}
    ) or (
        current.state == "unknown_effect" and candidate.state == "completed"
    )


def transition_media_operation_execution_evidence(
    current: MediaOperationExecutionEvidence,
    candidate: MediaOperationExecutionEvidence,
) -> MediaOperationExecutionEvidence:
    """Validate a transition and return its normalized candidate.

    ``unknown_effect -> completed`` is the explicit reconciliation path.  It
    requires a receipt reference, so it cannot silently authorize replay.
    """

    clean = validate_media_operation_execution_evidence(candidate)
    if not can_transition_media_operation_execution_evidence(current, clean):
        raise ValueError("media operation execution transition is invalid")
    return clean


def media_operation_execution_evidence_to_payload(
    evidence: MediaOperationExecutionEvidence,
) -> dict[str, object]:
    """Return a strictly safe schema payload for a future durable adapter."""

    clean = validate_media_operation_execution_evidence(evidence)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": EVIDENCE_KIND,
        "job_id": clean.job_id,
        "source_id": clean.source_id,
        "operation": clean.operation,
        "manifest_ref": clean.manifest_ref,
        "manifest_revision": clean.manifest_revision,
        "provider_id": clean.provider_id,
        "provider_revision": clean.provider_revision,
        "execution_id": clean.execution_id,
        "state": clean.state,
        "receipt_ref": clean.receipt_ref,
    }


def media_operation_execution_evidence_from_payload(
    payload: Mapping[str, object],
) -> MediaOperationExecutionEvidence:
    """Decode an exact, non-sensitive evidence schema payload."""

    if not isinstance(payload, Mapping):
        raise TypeError("media operation execution evidence payload must be a mapping")
    _reject_sensitive(payload)
    if set(payload) != _PAYLOAD_FIELDS:
        raise ValueError("media operation execution evidence fields are not exact")
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("kind") != EVIDENCE_KIND:
        raise ValueError("media operation execution evidence schema is invalid")
    receipt_ref = payload.get("receipt_ref")
    return validate_media_operation_execution_evidence(MediaOperationExecutionEvidence(
        job_id=payload.get("job_id") if isinstance(payload.get("job_id"), str) else "",
        source_id=payload.get("source_id") if isinstance(payload.get("source_id"), str) else "",
        operation=payload.get("operation") if isinstance(payload.get("operation"), str) else "",
        manifest_ref=payload.get("manifest_ref") if isinstance(payload.get("manifest_ref"), str) else "",
        manifest_revision=payload.get("manifest_revision") if isinstance(payload.get("manifest_revision"), str) else "",
        provider_id=payload.get("provider_id") if isinstance(payload.get("provider_id"), str) else "",
        provider_revision=payload.get("provider_revision") if isinstance(payload.get("provider_revision"), str) else "",
        execution_id=payload.get("execution_id") if isinstance(payload.get("execution_id"), str) else "",
        state=payload.get("state") if isinstance(payload.get("state"), str) else "",  # type: ignore[arg-type]
        receipt_ref=receipt_ref if isinstance(receipt_ref, str) else None,
    ))


def _identity(value: object, name: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ValueError(f"media operation execution {name} is invalid")
    return value


def _operation(value: object) -> str:
    if not isinstance(value, str) or _OPERATION.fullmatch(value) is None:
        raise ValueError("media operation execution operation is invalid")
    return value


def _receipt_ref(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _CRP_REF.fullmatch(value) is None:
        raise ValueError("media operation execution receipt reference is invalid")
    return value


def _manifest_ref(value: object) -> str:
    if not isinstance(value, str) or _CRP_REF.fullmatch(value) is None or "/source-manifests/" not in value:
        raise ValueError("media operation execution manifest reference is invalid")
    return value


def _identity_tuple(evidence: MediaOperationExecutionEvidence) -> tuple[str, str, str, str, str, str, str, str]:
    return (
        evidence.job_id,
        evidence.source_id,
        evidence.operation,
        evidence.manifest_ref,
        evidence.manifest_revision,
        evidence.provider_id,
        evidence.provider_revision,
        evidence.execution_id,
    )


def _reject_sensitive(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).strip().lower().replace("-", "_") in _SENSITIVE_KEYS:
                raise ValueError("media operation execution evidence contains sensitive data")
            _reject_sensitive(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_sensitive(nested)
