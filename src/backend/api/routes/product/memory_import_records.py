"""Memory import records ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping
import uuid

from fastapi.responses import JSONResponse

from core.storage_provider import JsonObjectStore

from . import http as product_http


_MEMORY_IMPORT_BATCHES_COLLECTION = "memory_import_batches"


_MEMORY_CANDIDATES_COLLECTION = "memory_candidates"


_MEMORY_CONFLICTS_COLLECTION = "memory_candidate_conflicts"


_MEMORY_IMPORT_RUNTIME_SESSION_ID = uuid.uuid4().hex


def _save_stage16_candidate(store: JsonObjectStore, payload: Mapping[str, object]) -> bool:
    """写入 Stage 1.6 候选；若已存在则覆盖。返回是否成功。"""
    candidate_id = str(payload.get("id", ""))
    if not candidate_id:
        return False
    try:
        store.write(_MEMORY_CANDIDATES_COLLECTION, candidate_id, dict(payload), expected_revision=0)
        return True
    except Exception:  # noqa: BLE001  已存在 → 覆盖
        try:
            store.write(_MEMORY_CANDIDATES_COLLECTION, candidate_id, dict(payload), expected_revision=None)
            return True
        except Exception:  # noqa: BLE001
            return False


def _get_stage16_candidate(store: JsonObjectStore, candidate_id: str) -> Mapping[str, object] | None:
    return store.read(_MEMORY_CANDIDATES_COLLECTION, candidate_id)


def _update_stage16_candidate(store: JsonObjectStore, payload: Mapping[str, object]) -> bool:
    candidate_id = str(payload.get("id", ""))
    if not candidate_id:
        return False
    try:
        store.write(_MEMORY_CANDIDATES_COLLECTION, candidate_id, dict(payload), expected_revision=None)
        return True
    except Exception:  # noqa: BLE001
        return False


def _stage16_error_response(
    detail: str,
    reason: str,
    *,
    status_code: int = 400,
    extra: Mapping[str, object] | None = None,
) -> JSONResponse:
    body: dict[str, object] = {
        "detail": detail,
        "reason": reason,
        "actionable": True,
        "memory_publication_state": "not_published",
    }
    if extra:
        body.update(extra)
    return product_http._json_response(status_code, body, product_http._no_store_headers())


def _stage16_ok_response(
    payload: Mapping[str, object],
    *,
    status_code: int = 200,
) -> JSONResponse:
    return product_http._json_response(status_code, payload, product_http._no_store_headers())


def _serialize_import_batch(
    record: Mapping[str, object],
    *,
    cas_revision: int | None = None,
) -> dict[str, object]:
    stored_status = str(record.get("status", "completed"))
    interrupted = (
        stored_status == "processing"
        and record.get("runtime_session_id") != _MEMORY_IMPORT_RUNTIME_SESSION_ID
    )
    effective_status = "interrupted" if interrupted else stored_status
    occurred_at, recorded_at = _import_dual_time(record)
    return {
        "batch_id": record.get("batch_id", ""),
        "source_type": record.get("source_type", "file"),
        "status": effective_status,
        "stored_status": stored_status,
        "recovery_required": interrupted,
        "recovery_action": "confirm_interrupted" if interrupted else "",
        "cas_revision": cas_revision,
        "total": record.get("total", 0),
        "succeeded": record.get("succeeded", 0),
        "failed": record.get("failed", 0),
        "needs_review": record.get("needs_review", 0),
        "candidate_count": record.get("candidate_count", 0),
        "delta_summary": record.get("delta_summary", ""),
        "created_at": record.get("created_at", ""),
        "occurred_at": occurred_at,
        "recorded_at": recorded_at,
        "completed_at": record.get("completed_at", ""),
        "failures": list(record.get("failures", []) or []),
        "series": list(record.get("series", []) or []),
    }


def _serialize_memory_candidate(record: Mapping[str, object]) -> dict[str, object]:
    occurred_at, recorded_at = _import_dual_time(record)
    return {
        "memory_id": record.get("memory_id") or record.get("id", ""),
        "layer": record.get("layer", "L1"),
        "type": record.get("type", "other"),
        "target_layer": record.get("target_layer"),
        "candidate_type": record.get("candidate_type") or record.get("type", "other"),
        "content": record.get("content", ""),
        "proposed_content": record.get("proposed_content") or record.get("content", ""),
        "summary": record.get("summary", ""),
        "confidence": record.get("confidence", 0.0),
        "trust_level": record.get("trust_level", "unverified"),
        "source_platform": record.get("source_platform", ""),
        "source_type": record.get("source_type", ""),
        "source_role": record.get("source_role", "unknown"),
        "source_ref": record.get("source_ref", ""),
        "source_id": record.get("source_id", ""),
        "source_refs": list(record.get("source_refs", []) or []),
        "evidence_refs": list(record.get("evidence_refs", []) or []),
        "created_at": record.get("created_at", ""),
        "observed_at": record.get("observed_at", ""),
        "occurred_at": occurred_at,
        "recorded_at": recorded_at,
        "conflict_refs": list(record.get("conflict_refs", []) or []),
        "status": record.get("status", "candidate"),
        "import_batch_id": record.get("import_batch_id", ""),
        "privacy_level": record.get("privacy_level", "private"),
        "provider_boundary": record.get("provider_boundary", "local"),
        "group": record.get("group", "needs_review"),
        "promoted_object_id": record.get("promoted_object_id"),
        "project_id": record.get("project_id", ""),
        "series_id": record.get("series_id", ""),
        "atom_ids": list(record.get("atom_ids", []) or []),
        "scenario_ids": list(record.get("scenario_ids", []) or []),
    }


def _import_dual_time(record: Mapping[str, object]) -> tuple[str | None, str]:
    """Read canonical import time while retaining old persisted records."""

    if "occurred_at" in record:
        raw_occurred = record.get("occurred_at")
        occurred_at = (
            str(raw_occurred).strip()
            if raw_occurred is not None and str(raw_occurred).strip()
            else None
        )
    else:
        legacy_occurred = record.get("created_at") or record.get("observed_at")
        occurred_at = str(legacy_occurred).strip() if legacy_occurred else None
    raw_recorded = (
        record.get("recorded_at")
        or record.get("observed_at")
        or record.get("created_at")
        or ""
    )
    return occurred_at, str(raw_recorded).strip()


def _serialize_conflict(record: Mapping[str, object]) -> dict[str, object]:
    result = {
        "conflict_id": record.get("conflict_id") or record.get("id", ""),
        "candidate_id": record.get("candidate_id", ""),
        "existing": record.get("existing", {}),
        "incoming": record.get("incoming", {}),
        "status": record.get("status", "needs_review"),
        "resolution": record.get("resolution", ""),
        "created_at": record.get("created_at", ""),
    }
    if "merged" in record:
        result["merged"] = record.get("merged", {})
    return result
