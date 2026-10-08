"""Pure, non-sensitive receipt contract for one completed Media Hands recipe step.

This module deliberately has no persistence, hashing, network, filesystem or
provider dependency.  It only validates the immutable facts that a durable
store may later associate with :mod:`media_recipe_step_evidence`: an input
state already bound to the step, a receipt reference, one safe output
reference, its resulting state hash, and resource consumption.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re


SCHEMA_VERSION = "1.0.0"
RECEIPT_KIND = "media_recipe_step_receipt"

_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_STEP_NAME = re.compile(r"^[a-z][a-z0-9_.-]{2,79}$")
_STATE_HASH = re.compile(r"^sha256:[a-f0-9]{64}$")
_CRP_REF = re.compile(r"^crp://[A-Za-z0-9][A-Za-z0-9._/-]{0,319}$")
_PAYLOAD_FIELDS = frozenset({
    "schema_version", "kind", "job_id", "execution_id", "provider_id",
    "provider_revision", "step_name", "input_state_hash", "receipt_ref",
    "output_ref", "output_state_hash", "consumed",
})
_SENSITIVE_KEY_PARTS = frozenset({
    "api_key", "apikey", "authorization", "bytes", "content", "cookie",
    "endpoint", "password", "path", "prompt", "secret", "token", "url",
})


@dataclass(frozen=True, slots=True)
class MediaRecipeStepReceipt:
    """Safe completion receipt for exactly one frozen recipe-step identity."""

    job_id: str
    execution_id: str
    provider_id: str
    provider_revision: str
    step_name: str
    input_state_hash: str
    receipt_ref: str
    output_ref: str
    output_state_hash: str
    consumed: Mapping[str, int]


def validate_media_recipe_step_receipt(
    receipt: MediaRecipeStepReceipt,
) -> MediaRecipeStepReceipt:
    """Validate and normalize a typed receipt without following references."""

    if not isinstance(receipt, MediaRecipeStepReceipt):
        raise TypeError("media recipe step receipt must be typed")
    _reject_sensitive(receipt.consumed)
    return MediaRecipeStepReceipt(
        job_id=_identity(receipt.job_id, "job id"),
        execution_id=_identity(receipt.execution_id, "execution id"),
        provider_id=_identity(receipt.provider_id, "provider id"),
        provider_revision=_identity(receipt.provider_revision, "provider revision"),
        step_name=_step_name(receipt.step_name),
        input_state_hash=_state_hash(receipt.input_state_hash, "input state hash"),
        receipt_ref=_crp_ref(receipt.receipt_ref, "receipt reference"),
        output_ref=_crp_ref(receipt.output_ref, "output reference"),
        output_state_hash=_state_hash(receipt.output_state_hash, "output state hash"),
        consumed=_consumed(receipt.consumed),
    )


def media_recipe_step_receipt_to_payload(
    receipt: MediaRecipeStepReceipt,
) -> dict[str, object]:
    """Encode the exact safe schema suitable for durable storage."""

    clean = validate_media_recipe_step_receipt(receipt)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": RECEIPT_KIND,
        "job_id": clean.job_id,
        "execution_id": clean.execution_id,
        "provider_id": clean.provider_id,
        "provider_revision": clean.provider_revision,
        "step_name": clean.step_name,
        "input_state_hash": clean.input_state_hash,
        "receipt_ref": clean.receipt_ref,
        "output_ref": clean.output_ref,
        "output_state_hash": clean.output_state_hash,
        "consumed": dict(clean.consumed),
    }


def media_recipe_step_receipt_from_payload(
    payload: Mapping[str, object],
) -> MediaRecipeStepReceipt:
    """Decode only an exact, non-sensitive receipt payload."""

    if not isinstance(payload, Mapping):
        raise TypeError("media recipe step receipt payload must be a mapping")
    _reject_sensitive(payload)
    if set(payload) != _PAYLOAD_FIELDS:
        raise ValueError("media recipe step receipt fields are not exact")
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("kind") != RECEIPT_KIND:
        raise ValueError("media recipe step receipt schema is invalid")
    return validate_media_recipe_step_receipt(MediaRecipeStepReceipt(
        job_id=_required_string(payload, "job_id"),
        execution_id=_required_string(payload, "execution_id"),
        provider_id=_required_string(payload, "provider_id"),
        provider_revision=_required_string(payload, "provider_revision"),
        step_name=_required_string(payload, "step_name"),
        input_state_hash=_required_string(payload, "input_state_hash"),
        receipt_ref=_required_string(payload, "receipt_ref"),
        output_ref=_required_string(payload, "output_ref"),
        output_state_hash=_required_string(payload, "output_state_hash"),
        consumed=_mapping(payload.get("consumed"), "consumed"),
    ))


def _identity(value: object, name: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ValueError(f"media recipe step receipt {name} is invalid")
    return value


def _step_name(value: object) -> str:
    if not isinstance(value, str) or _STEP_NAME.fullmatch(value) is None:
        raise ValueError("media recipe step receipt step name is invalid")
    return value


def _state_hash(value: object, name: str) -> str:
    if not isinstance(value, str) or _STATE_HASH.fullmatch(value) is None:
        raise ValueError(f"media recipe step receipt {name} is invalid")
    return value


def _crp_ref(value: object, name: str) -> str:
    if not isinstance(value, str) or _CRP_REF.fullmatch(value) is None:
        raise ValueError(f"media recipe step receipt {name} is invalid")
    return value


def _consumed(value: Mapping[str, int]) -> dict[str, int]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str)
        or not isinstance(amount, int)
        or isinstance(amount, bool)
        or amount < 0
        for key, amount in value.items()
    ):
        raise ValueError("media recipe step receipt consumption is invalid")
    return dict(value)


def _mapping(value: object, name: str) -> Mapping[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError(f"media recipe step receipt {name} is invalid")
    return value  # type: ignore[return-value]


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"media recipe step receipt {key} is invalid")
    return value


def _reject_sensitive(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                raise ValueError("media recipe step receipt contains sensitive data")
            _reject_sensitive(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_sensitive(nested)
