"""Safe receipt projection for a completed Media Hands provider effect.

The projection is intentionally smaller than a Document or media-output
authority.  It contains only references and the frozen consumption/checkpoint
facts necessary to settle a Job after a process crash.  Provider response
bodies, local paths, secrets and arbitrary metadata are rejected.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import re


SCHEMA_VERSION = "1.1.0"
RECEIPT_KIND = "media_operation_execution_receipt"

_CRP_REF = re.compile(r"^crp://[A-Za-z0-9][A-Za-z0-9._:/-]{0,319}$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_STATE_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_OUTPUT_KINDS = frozenset({"asset", "document", "knowledge", "report"})
_FIELDS = frozenset({
    "schema_version", "kind", "job_id", "source_id", "operation", "manifest_ref",
    "manifest_revision", "provider_id", "provider_revision", "execution_id",
    "permission_snapshot", "receipt_ref", "published_outputs", "checkpoint", "consumed", "log_refs",
    "credential_use",
})
_FIELDS_V1 = _FIELDS - {"credential_use"}
_SENSITIVE_KEYS = frozenset({
    "api_key", "apikey", "authorization", "bytes", "content", "cookie", "cookies",
    "endpoint", "local_path", "password", "path", "prompt", "secret", "token", "url",
})


def media_job_uri_segment(job_id: str) -> str:
    """Return the stable URI-safe segment for a formal Job identity."""

    return hashlib.sha256(_identity(job_id, "job id").encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class MediaOperationExecutionReceipt:
    job_id: str
    source_id: str
    operation: str
    manifest_ref: str
    manifest_revision: str
    provider_id: str
    provider_revision: str
    execution_id: str
    permission_snapshot: Mapping[str, object]
    receipt_ref: str
    published_outputs: tuple[Mapping[str, object], ...]
    checkpoint: Mapping[str, object]
    consumed: Mapping[str, int]
    log_refs: tuple[str, ...]
    credential_use: Mapping[str, object] | None = None


def validate_media_operation_execution_receipt(
    receipt: MediaOperationExecutionReceipt,
    *,
    budget: Mapping[str, int],
) -> MediaOperationExecutionReceipt:
    """Validate a receipt without loading an output or following any reference."""

    if not isinstance(receipt, MediaOperationExecutionReceipt):
        raise TypeError("media operation execution receipt must be typed")
    _reject_sensitive(receipt.published_outputs)
    _reject_sensitive(receipt.checkpoint)
    _reject_sensitive(receipt.consumed)
    outputs = tuple(_output(output) for output in receipt.published_outputs)
    if not outputs:
        raise ValueError("media execution receipt requires a published output")
    checkpoint = _checkpoint(receipt.checkpoint, job_id=receipt.job_id)
    consumed = _consumed(receipt.consumed, budget=budget)
    permission_snapshot = _permission_snapshot(
        receipt.permission_snapshot,
        manifest_ref=receipt.manifest_ref,
        manifest_revision=receipt.manifest_revision,
    )
    credential_use = _credential_use(receipt.credential_use)
    return MediaOperationExecutionReceipt(
        job_id=_identity(receipt.job_id, "job id"),
        source_id=_identity(receipt.source_id, "source id"),
        operation=_identity(receipt.operation, "operation"),
        manifest_ref=_manifest_ref(receipt.manifest_ref),
        manifest_revision=_identity(receipt.manifest_revision, "manifest revision"),
        provider_id=_identity(receipt.provider_id, "provider id"),
        provider_revision=_identity(receipt.provider_revision, "provider revision"),
        execution_id=_identity(receipt.execution_id, "execution id"),
        permission_snapshot=permission_snapshot,
        receipt_ref=_crp_ref(receipt.receipt_ref, "receipt reference"),
        published_outputs=outputs,
        checkpoint=checkpoint,
        consumed=consumed,
        log_refs=tuple(_crp_ref(value, "log reference") for value in receipt.log_refs),
        credential_use=credential_use,
    )


def media_operation_execution_receipt_to_payload(
    receipt: MediaOperationExecutionReceipt,
    *,
    budget: Mapping[str, int],
) -> dict[str, object]:
    clean = validate_media_operation_execution_receipt(receipt, budget=budget)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": RECEIPT_KIND,
        "job_id": clean.job_id,
        "source_id": clean.source_id,
        "operation": clean.operation,
        "manifest_ref": clean.manifest_ref,
        "manifest_revision": clean.manifest_revision,
        "provider_id": clean.provider_id,
        "provider_revision": clean.provider_revision,
        "execution_id": clean.execution_id,
        "permission_snapshot": dict(clean.permission_snapshot),
        "receipt_ref": clean.receipt_ref,
        "published_outputs": [dict(value) for value in clean.published_outputs],
        "checkpoint": dict(clean.checkpoint),
        "consumed": dict(clean.consumed),
        "log_refs": list(clean.log_refs),
        "credential_use": None if clean.credential_use is None else dict(clean.credential_use),
    }


def media_operation_execution_receipt_from_payload(
    payload: Mapping[str, object],
    *,
    budget: Mapping[str, int],
) -> MediaOperationExecutionReceipt:
    if not isinstance(payload, Mapping):
        raise TypeError("media execution receipt payload must be a mapping")
    _reject_sensitive(payload)
    schema_version = payload.get("schema_version")
    payload_fields = frozenset(payload)
    if payload_fields not in {_FIELDS, _FIELDS_V1}:
        raise ValueError("media execution receipt fields are not exact")
    if schema_version not in {"1.0.0", SCHEMA_VERSION} or payload.get("kind") != RECEIPT_KIND:
        raise ValueError("media execution receipt schema is invalid")
    if schema_version == "1.0.0" and payload_fields != _FIELDS_V1:
        raise ValueError("media execution receipt schema is invalid")
    if schema_version == SCHEMA_VERSION and payload_fields != _FIELDS:
        raise ValueError("media execution receipt schema is invalid")
    outputs = payload.get("published_outputs")
    logs = payload.get("log_refs")
    if not isinstance(outputs, Sequence) or isinstance(outputs, (str, bytes)):
        raise ValueError("media execution receipt outputs are invalid")
    if not isinstance(logs, Sequence) or isinstance(logs, (str, bytes)):
        raise ValueError("media execution receipt logs are invalid")
    return validate_media_operation_execution_receipt(MediaOperationExecutionReceipt(
        job_id=_string(payload.get("job_id")),
        source_id=_string(payload.get("source_id")),
        operation=_string(payload.get("operation")),
        manifest_ref=_string(payload.get("manifest_ref")),
        manifest_revision=_string(payload.get("manifest_revision")),
        provider_id=_string(payload.get("provider_id")),
        provider_revision=_string(payload.get("provider_revision")),
        execution_id=_string(payload.get("execution_id")),
        permission_snapshot=_mapping(payload.get("permission_snapshot"), "permission snapshot"),
        receipt_ref=_string(payload.get("receipt_ref")),
        published_outputs=tuple(_mapping(value, "published output") for value in outputs),
        checkpoint=_mapping(payload.get("checkpoint"), "checkpoint"),
        consumed=_integer_mapping(payload.get("consumed"), "consumed"),
        log_refs=tuple(_string(value) for value in logs),
        credential_use=_mapping(payload.get("credential_use"), "credential use")
        if payload.get("credential_use") is not None else None,
    ), budget=budget)


def _output(value: Mapping[str, object]) -> dict[str, object]:
    output = dict(_mapping(value, "published output"))
    if set(output) != {"kind", "uri", "object_id", "published"}:
        raise ValueError("media receipt output shape is invalid")
    if output.get("kind") not in _OUTPUT_KINDS or output.get("published") is not True:
        raise ValueError("media receipt output is not published")
    _crp_ref(output.get("uri"), "output reference")
    _identity(output.get("object_id"), "output object id")
    return output


def _checkpoint(value: Mapping[str, object], *, job_id: str) -> dict[str, object]:
    checkpoint = dict(_mapping(value, "checkpoint"))
    if set(checkpoint) != {"resume_step", "checkpoint_uri", "state_hash", "updated_at"}:
        raise ValueError("media receipt checkpoint shape is invalid")
    if checkpoint.get("resume_step") != "execute_operation":
        raise ValueError("media receipt checkpoint step is invalid")
    uri = _crp_ref(checkpoint.get("checkpoint_uri"), "checkpoint reference")
    if f"/jobs/{job_id}/" not in uri and f"/jobs/{media_job_uri_segment(job_id)}/" not in uri:
        raise ValueError("media receipt checkpoint is not bound to the Job")
    state_hash = checkpoint.get("state_hash")
    if not isinstance(state_hash, str) or _STATE_HASH.fullmatch(state_hash) is None:
        raise ValueError("media receipt checkpoint hash is invalid")
    _string(checkpoint.get("updated_at"))
    return checkpoint


def _consumed(value: Mapping[str, int], *, budget: Mapping[str, int]) -> dict[str, int]:
    consumed = dict(value)
    if set(consumed) != set(budget) or any(
        not isinstance(item, int) or isinstance(item, bool) or item < 0
        for item in consumed.values()
    ):
        raise ValueError("media receipt consumption is invalid")
    if any(item > budget[key] for key, item in consumed.items()):
        raise ValueError("media receipt consumption exceeds the frozen budget")
    return consumed


def _permission_snapshot(
    value: Mapping[str, object], *, manifest_ref: str, manifest_revision: str
) -> dict[str, object]:
    snapshot = dict(_mapping(value, "permission snapshot"))
    expected = {
        "project_id", "manifest_ref", "manifest_revision", "grant_ref", "grant_revision",
        "revocation_generation",
    }
    if set(snapshot) != expected:
        raise ValueError("media receipt permission snapshot shape is invalid")
    if snapshot.get("manifest_ref") != manifest_ref or snapshot.get("manifest_revision") != manifest_revision:
        raise ValueError("media receipt permission snapshot does not bind its manifest")
    _identity(snapshot.get("project_id"), "permission project id")
    _crp_ref(snapshot.get("grant_ref"), "permission grant reference")
    _identity(snapshot.get("grant_revision"), "permission grant revision")
    generation = snapshot.get("revocation_generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise ValueError("media receipt permission generation is invalid")
    return snapshot


def _credential_use(value: Mapping[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    item = dict(_mapping(value, "credential use"))
    if set(item) != {"provider", "authorization_revision", "secret_generation", "result"}:
        raise ValueError("media receipt credential use shape is invalid")
    if item.get("provider") != "xiaohongshu" or item.get("result") != "completed":
        raise ValueError("media receipt credential use result is invalid")
    for key in ("authorization_revision", "secret_generation"):
        revision = item.get(key)
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ValueError("media receipt credential use revision is invalid")
    return item


def _identity(value: object, name: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ValueError(f"media receipt {name} is invalid")
    return value


def _manifest_ref(value: object) -> str:
    result = _crp_ref(value, "manifest reference")
    if "/source-manifests/" not in result:
        raise ValueError("media receipt manifest reference is invalid")
    return result


def _crp_ref(value: object, name: str) -> str:
    if not isinstance(value, str) or _CRP_REF.fullmatch(value) is None:
        raise ValueError(f"media receipt {name} is invalid")
    return value


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"media receipt {name} is invalid")
    return value


def _integer_mapping(value: object, name: str) -> Mapping[str, int]:
    mapping = _mapping(value, name)
    if any(not isinstance(item, int) or isinstance(item, bool) for item in mapping.values()):
        raise ValueError(f"media receipt {name} is invalid")
    return mapping  # type: ignore[return-value]


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("media receipt string is invalid")
    return value


def _reject_sensitive(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).strip().lower().replace("-", "_") in _SENSITIVE_KEYS:
                raise ValueError("media execution receipt contains sensitive data")
            _reject_sensitive(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_sensitive(nested)
