"""Immutable, replay-verifiable receipt for one governed expert execution.

An expert receipt is deliberately metadata-only.  It records frozen identities,
stage outcomes and opaque references, never prompt bodies, output bodies,
credentials or local filesystem locations.  Runtime and persistence adapters
may store this payload, but must use this codec to construct and verify it.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence

from .expert_binding_snapshot import ExpertBindingSnapshotError, validate_snapshot


EXPERT_EXECUTION_RECEIPT_SCHEMA_VERSION = "1.0.0"

_FIELDS = frozenset({
    "schema_version", "receipt_id", "snapshot_id", "project_id", "expert_id",
    "expert_revision", "context_manifest_revision", "status", "stages",
    "tool_invocation_refs", "input_evidence_refs", "output_refs", "summary",
})
_STAGE_FIELDS = frozenset({"stage", "status"})
_RECEIPT_STATUSES = frozenset({"completed", "failed"})
_STAGE_STATUSES = frozenset({"completed", "failed", "skipped"})
_REF_PREFIXES = (
    "crp://", "source:", "evidence:", "tool-invocation:", "document:",
    "output:", "artifact:", "job:",
)
_ABSOLUTE_PATH = re.compile(r"(?:^[A-Za-z]:[\\/]|^/|^\\\\|(?:^|\s)file:)", re.IGNORECASE)
_SENSITIVE = re.compile(r"(?:secret|cookie|password|authorization|bearer|api[_-]?key|access[_-]?token)", re.IGNORECASE)


class ExpertExecutionReceiptError(ExpertBindingSnapshotError):
    """The receipt is malformed, unsafe, or cannot represent the stated outcome."""


def build_expert_execution_receipt(
    *,
    snapshot: Mapping[str, object],
    status: str,
    stages: Sequence[Mapping[str, object]],
    tool_invocation_refs: Sequence[str] = (),
    input_evidence_refs: Sequence[str] = (),
    output_refs: Sequence[str] = (),
    summary: str,
) -> dict[str, object]:
    """Build the only canonical payload for an expert execution receipt.

    Snapshot fields are copied from the validated frozen snapshot instead of
    trusting a caller-provided identity.  The returned identifier is a digest
    of every semantic receipt field and consequently stable across retries.
    """
    frozen = validate_snapshot(snapshot)
    payload: dict[str, object] = {
        "schema_version": EXPERT_EXECUTION_RECEIPT_SCHEMA_VERSION,
        "snapshot_id": frozen["snapshot_id"],
        "project_id": frozen["project_id"],
        "expert_id": frozen["expert_id"],
        "expert_revision": frozen["expert_revision"],
        "context_manifest_revision": frozen["context_manifest_revision"],
        "status": status,
        "stages": [dict(item) for item in stages],
        "tool_invocation_refs": list(tool_invocation_refs),
        "input_evidence_refs": list(input_evidence_refs),
        "output_refs": list(output_refs),
        "summary": summary,
    }
    validated = validate_expert_execution_receipt({
        **payload,
        "receipt_id": _canonical_receipt_id(payload),
    })
    return deepcopy(validated)


def validate_expert_execution_receipt(receipt: Mapping[str, object]) -> dict[str, object]:
    """Strictly validate a receipt without reading any runtime or storage state."""
    if not isinstance(receipt, Mapping):
        raise ExpertExecutionReceiptError("expert execution receipt must be a mapping")
    keys = {str(key) for key in receipt}
    if keys != _FIELDS:
        unknown = sorted(keys - _FIELDS)
        missing = sorted(_FIELDS - keys)
        detail = f"unknown fields: {unknown}" if unknown else f"missing fields: {missing}"
        raise ExpertExecutionReceiptError(f"expert execution receipt shape is invalid ({detail})")
    if receipt.get("schema_version") != EXPERT_EXECUTION_RECEIPT_SCHEMA_VERSION:
        raise ExpertExecutionReceiptError(
            f"receipt schema_version must be {EXPERT_EXECUTION_RECEIPT_SCHEMA_VERSION}"
        )

    payload = dict(receipt)
    for field in ("receipt_id", "snapshot_id", "project_id", "expert_id", "context_manifest_revision"):
        _safe_text(payload.get(field), field)
    _positive_int(payload.get("expert_revision"), "expert_revision")
    if not str(payload["receipt_id"]).startswith("eer-"):
        raise ExpertExecutionReceiptError("receipt_id must be an eer- prefixed digest")
    _validate_status(payload.get("status"))
    payload["stages"] = _stages(payload.get("stages"))
    payload["tool_invocation_refs"] = _refs(payload.get("tool_invocation_refs"), "tool_invocation_refs")
    payload["input_evidence_refs"] = _refs(payload.get("input_evidence_refs"), "input_evidence_refs")
    payload["output_refs"] = _refs(payload.get("output_refs"), "output_refs")
    _safe_text(payload.get("summary"), "summary", maximum=512)

    if payload["status"] == "completed":
        if not payload["output_refs"]:
            raise ExpertExecutionReceiptError("completed receipt requires output_refs")
        if not payload["input_evidence_refs"]:
            raise ExpertExecutionReceiptError("completed receipt requires input_evidence_refs")
    elif payload["output_refs"]:
        raise ExpertExecutionReceiptError("failed receipt must not contain output_refs")

    expected_id = _canonical_receipt_id(payload)
    if payload["receipt_id"] != expected_id:
        raise ExpertExecutionReceiptError("receipt_id does not match canonical receipt identity")
    return payload


def verify_expert_execution_receipt_replay(
    receipt: Mapping[str, object], *, snapshot: Mapping[str, object]
) -> dict[str, object]:
    """Verify a stored execution against its original binding snapshot.

    This operation verifies only.  It does not query a catalog or select a
    replacement expert, so a drifted receipt always remains fail-closed.
    """
    validated = validate_expert_execution_receipt(receipt)
    frozen = validate_snapshot(snapshot)
    reasons = [
        f"{field}_drift"
        for field in (
            "snapshot_id", "project_id", "expert_id", "expert_revision",
            "context_manifest_revision",
        )
        if validated[field] != frozen[field]
    ]
    if reasons:
        return {"status": "drifted", "reasons": reasons}
    return {"status": "ok", "reasons": []}


def _canonical_receipt_id(receipt: Mapping[str, object]) -> str:
    identity = {
        field: receipt[field]
        for field in sorted(_FIELDS - {"receipt_id"})
    }
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "eer-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _validate_status(value: object) -> None:
    if value not in _RECEIPT_STATUSES:
        raise ExpertExecutionReceiptError(
            f"receipt status must be one of {sorted(_RECEIPT_STATUSES)}"
        )


def _stages(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ExpertExecutionReceiptError("stages must be a non-empty array")
    stages: list[dict[str, str]] = []
    stage_names: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping) or {str(key) for key in item} != _STAGE_FIELDS:
            raise ExpertExecutionReceiptError(f"stages[{index}] shape is invalid")
        stage = _safe_text(item.get("stage"), f"stages[{index}].stage", maximum=96)
        status = item.get("status")
        if status not in _STAGE_STATUSES:
            raise ExpertExecutionReceiptError(
                f"stages[{index}].status must be one of {sorted(_STAGE_STATUSES)}"
            )
        if stage in stage_names:
            raise ExpertExecutionReceiptError("stage names must be unique")
        stage_names.add(stage)
        stages.append({"stage": stage, "status": status})
    return stages


def _refs(value: object, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ExpertExecutionReceiptError(f"{field} must be an array")
    refs = [_opaque_ref(item, f"{field}[{index}]") for index, item in enumerate(value)]
    if len(refs) != len(set(refs)):
        raise ExpertExecutionReceiptError(f"{field} must not contain duplicate refs")
    return refs


def _opaque_ref(value: object, label: str) -> str:
    ref = _safe_text(value, label, maximum=512)
    prefix = next((item for item in _REF_PREFIXES if ref.startswith(item)), None)
    if prefix is None:
        raise ExpertExecutionReceiptError(f"{label} must be an opaque governed ref")
    body = ref[len(prefix):]
    if _ABSOLUTE_PATH.search(body):
        raise ExpertExecutionReceiptError(f"{label} must not contain an absolute path")
    return ref


def _safe_text(value: object, label: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExpertExecutionReceiptError(f"{label} must be a non-empty string")
    text = value.strip()
    if len(text) > maximum or "\n" in text or "\r" in text:
        raise ExpertExecutionReceiptError(f"{label} is too long or contains raw content")
    if _ABSOLUTE_PATH.search(text):
        raise ExpertExecutionReceiptError(f"{label} must not contain an absolute path")
    if _SENSITIVE.search(text):
        raise ExpertExecutionReceiptError(f"{label} must not contain Secret material")
    return text


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ExpertExecutionReceiptError(f"{label} must be a positive int")
    return value
