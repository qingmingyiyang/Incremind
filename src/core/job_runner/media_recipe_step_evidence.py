"""Immutable, non-sensitive evidence for one governed Media Hands recipe step.

The contract is intentionally pure data.  It does not calculate a hash, access
the filesystem, persist state, or invoke a provider.  A durable store can use
the supplied ``input_state_hash`` as an already-computed identity fact to make
each individual recipe step replay-safe.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Literal


MediaRecipeStepState = Literal["started", "unknown_effect", "completed"]

SCHEMA_VERSION = "1.0.0"
EVIDENCE_KIND = "media_recipe_step_evidence"

_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_STEP_NAME = re.compile(r"^[a-z][a-z0-9_.-]{2,79}$")
_STATE_HASH = re.compile(r"^sha256:[a-f0-9]{64}$")
_CRP_REF = re.compile(r"^crp://[A-Za-z0-9][A-Za-z0-9._/-]{0,319}$")
_STATES = frozenset({"started", "unknown_effect", "completed"})
_SENSITIVE_KEYS = frozenset({
    "api_key", "apikey", "authorization", "bytes", "content", "cookie",
    "cookies", "endpoint", "local_path", "password", "path", "prompt",
    "secret", "token", "url", "uri",
})
_PAYLOAD_FIELDS = frozenset({
    "schema_version", "kind", "job_id", "execution_id", "provider_id",
    "provider_revision", "step_name", "input_state_hash", "state", "receipt_ref",
})


@dataclass(frozen=True, slots=True)
class MediaRecipeStepEvidence:
    """Lifecycle evidence for exactly one provider recipe step attempt."""

    job_id: str
    execution_id: str
    provider_id: str
    provider_revision: str
    step_name: str
    input_state_hash: str
    state: MediaRecipeStepState
    receipt_ref: str | None = None


def validate_media_recipe_step_evidence(
    evidence: MediaRecipeStepEvidence,
) -> MediaRecipeStepEvidence:
    """Validate and normalize typed evidence without any side effect."""

    if not isinstance(evidence, MediaRecipeStepEvidence):
        raise TypeError("media recipe step evidence must be typed")
    if evidence.state not in _STATES:
        raise ValueError("media recipe step state is invalid")
    clean = MediaRecipeStepEvidence(
        job_id=_identity(evidence.job_id, "job id"),
        execution_id=_identity(evidence.execution_id, "execution id"),
        provider_id=_identity(evidence.provider_id, "provider id"),
        provider_revision=_identity(evidence.provider_revision, "provider revision"),
        step_name=_step_name(evidence.step_name),
        input_state_hash=_input_state_hash(evidence.input_state_hash),
        state=evidence.state,
        receipt_ref=_receipt_ref(evidence.receipt_ref),
    )
    if clean.state == "completed":
        if clean.receipt_ref is None:
            raise ValueError("completed media recipe step requires a receipt reference")
    elif clean.receipt_ref is not None:
        raise ValueError("incomplete media recipe step contains a receipt reference")
    return clean


def can_transition_media_recipe_step_evidence(
    current: MediaRecipeStepEvidence,
    candidate: MediaRecipeStepEvidence,
) -> bool:
    """Return whether one monotonic step lifecycle transition is permitted."""

    current = validate_media_recipe_step_evidence(current)
    candidate = validate_media_recipe_step_evidence(candidate)
    if _identity_tuple(current) != _identity_tuple(candidate):
        return False
    return (
        current.state == "started" and candidate.state in {"unknown_effect", "completed"}
    ) or (
        current.state == "unknown_effect" and candidate.state == "completed"
    )


def transition_media_recipe_step_evidence(
    current: MediaRecipeStepEvidence,
    candidate: MediaRecipeStepEvidence,
) -> MediaRecipeStepEvidence:
    """Validate and return a legal monotonic step transition."""

    clean = validate_media_recipe_step_evidence(candidate)
    if not can_transition_media_recipe_step_evidence(current, clean):
        raise ValueError("media recipe step transition is invalid")
    return clean


def media_recipe_step_evidence_to_payload(
    evidence: MediaRecipeStepEvidence,
) -> dict[str, object]:
    """Encode the exact safe payload schema for durable storage."""

    clean = validate_media_recipe_step_evidence(evidence)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": EVIDENCE_KIND,
        "job_id": clean.job_id,
        "execution_id": clean.execution_id,
        "provider_id": clean.provider_id,
        "provider_revision": clean.provider_revision,
        "step_name": clean.step_name,
        "input_state_hash": clean.input_state_hash,
        "state": clean.state,
        "receipt_ref": clean.receipt_ref,
    }


def media_recipe_step_evidence_from_payload(
    payload: Mapping[str, object],
) -> MediaRecipeStepEvidence:
    """Decode an exact, non-sensitive step evidence payload."""

    if not isinstance(payload, Mapping):
        raise TypeError("media recipe step evidence payload must be a mapping")
    _reject_sensitive(payload)
    if set(payload) != _PAYLOAD_FIELDS:
        raise ValueError("media recipe step evidence fields are not exact")
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("kind") != EVIDENCE_KIND:
        raise ValueError("media recipe step evidence schema is invalid")
    receipt_ref = payload["receipt_ref"]
    if receipt_ref is not None and not isinstance(receipt_ref, str):
        raise ValueError("media recipe step receipt reference is invalid")
    return validate_media_recipe_step_evidence(MediaRecipeStepEvidence(
        job_id=_required_string(payload, "job_id"),
        execution_id=_required_string(payload, "execution_id"),
        provider_id=_required_string(payload, "provider_id"),
        provider_revision=_required_string(payload, "provider_revision"),
        step_name=_required_string(payload, "step_name"),
        input_state_hash=_required_string(payload, "input_state_hash"),
        state=_required_string(payload, "state"),  # type: ignore[arg-type]
        receipt_ref=receipt_ref,
    ))


def _identity(value: object, name: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ValueError(f"media recipe step {name} is invalid")
    return value


def _step_name(value: object) -> str:
    if not isinstance(value, str) or _STEP_NAME.fullmatch(value) is None:
        raise ValueError("media recipe step name is invalid")
    return value


def _input_state_hash(value: object) -> str:
    if not isinstance(value, str) or _STATE_HASH.fullmatch(value) is None:
        raise ValueError("media recipe step input state hash is invalid")
    return value


def _receipt_ref(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _CRP_REF.fullmatch(value) is None:
        raise ValueError("media recipe step receipt reference is invalid")
    return value


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str):
        raise ValueError(f"media recipe step {key} is invalid")
    return value


def _identity_tuple(evidence: MediaRecipeStepEvidence) -> tuple[str, str, str, str, str, str]:
    return (
        evidence.job_id,
        evidence.execution_id,
        evidence.provider_id,
        evidence.provider_revision,
        evidence.step_name,
        evidence.input_state_hash,
    )


def _reject_sensitive(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).strip().lower().replace("-", "_") in _SENSITIVE_KEYS:
                raise ValueError("media recipe step evidence contains sensitive data")
            _reject_sensitive(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_sensitive(nested)
