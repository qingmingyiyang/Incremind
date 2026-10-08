from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from core.product_core.memory_projection_authority_contract import (
    MemoryProjectionAuthoritySnapshotPort,
    authority_snapshot_fingerprint,
)
from core.product_core.memory_projection_repository import (
    ObjectStoreMemoryProjectionRepository,
)
from core.product_core.project_brain_projection_status import (
    get_project_brain_projection_status,
)


class ProjectionEffectReaderPort(Protocol):
    def get(self, operation_id: str) -> object | None:
        """Read one Core Effect without exposing its internal payload."""


def memory_retrieval_settings_payload(
    *,
    project_id: str,
    authority: MemoryProjectionAuthoritySnapshotPort,
    projections: ObjectStoreMemoryProjectionRepository,
) -> dict[str, object]:
    status = get_project_brain_projection_status(
        project_id=project_id,
        authority=authority,
        projections=projections,
    ).to_payload()
    return {
        "project_id": project_id,
        "status": status,
        "automatic_organization": {
            "enabled": True,
            "trigger": "after_project_question",
            "label": "项目问答后自动准备概况",
            "blocks_first_answer": False,
        },
        "reading_order": [
            {"step": 1, "label": "项目总览"},
            {"step": 2, "label": "系列概况"},
            {"step": 3, "label": "详细资料", "only_when_needed": True},
            {"step": 4, "label": "原始来源", "only_when_needed": True},
        ],
        "privacy": {
            "local_derived_index": True,
            "published_memory_only": True,
            "original_content_in_status": False,
            "external_egress_requires_provider_consent": True,
            "team_memory_included": False,
        },
    }


def memory_projection_diagnostics_payload(
    *,
    project_id: str,
    authority: MemoryProjectionAuthoritySnapshotPort,
    projections: ObjectStoreMemoryProjectionRepository,
    effects: ProjectionEffectReaderPort,
) -> dict[str, object]:
    snapshot = authority.load(project_id)
    fingerprint = authority_snapshot_fingerprint(snapshot)
    result = projections.load_current(
        project_id=project_id,
        authority_identity=snapshot.authority_identity,
        authority_fingerprint=fingerprint,
    )
    public = get_project_brain_projection_status(
        project_id=project_id,
        authority=_FixedAuthority(snapshot),
        projections=projections,
    ).to_payload()
    manifest = result.manifest if isinstance(result.manifest, Mapping) else {}
    metrics = manifest.get("active_metrics")
    operation_id = manifest.get("job_id")
    effect = effects.get(operation_id) if isinstance(operation_id, str) and operation_id else None
    diagnostic_code = str(public.get("diagnostic_code") or "")
    can_refresh = public.get("status") in {"preparing", "needs_refresh"}
    if diagnostic_code in {"projection_artifact_corrupt", "projection_manifest_corrupt"}:
        can_refresh = True
    return {
        "project_id": project_id,
        "public_status": public,
        "projection": {
            "manifest_state": manifest.get("status") or result.status,
            "projection_version": manifest.get("projection_version"),
            "generator_policy_id": manifest.get("generator_policy_id"),
            "updated_at": manifest.get("updated_at"),
            "authority_fingerprint_hint": fingerprint[:12],
            "metrics": _safe_metrics(metrics),
        },
        "latest_rebuild": _safe_effect(effect),
        "actions": {
            "refresh_available": can_refresh,
            "requires_confirmation": True,
            "corrupt_requires_repair": public.get("status") == "unavailable",
        },
        "safety": {
            "read_only": True,
            "content_included": False,
            "source_ids_included": False,
            "locators_included": False,
            "paths_included": False,
            "urls_included": False,
            "provider_payload_included": False,
        },
    }


def _safe_metrics(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {"series_count": 0, "skill_count": 0}
    return {
        "series_count": _non_negative_int(value.get("r1_item_count")),
        "skill_count": _non_negative_int(value.get("project_skill_ref_count")),
    }


def _safe_effect(value: object | None) -> dict[str, object] | None:
    if value is None:
        return None
    state = getattr(value, "state", None)
    state_value = getattr(state, "value", state)
    status = {
        "SETTLED_OK": "completed", "SETTLED_ERR": "failed",
        "UNKNOWN": "unknown", "INFLIGHT": "running", "PLANNED": "pending",
    }.get(state_value, "unknown")
    return {
        "operation_id": getattr(value, "operation_id", None),
        "status": status,
        "receipt_recorded": bool(getattr(value, "result_ref", None)),
        "error_recorded": bool(getattr(value, "error_ref", None)),
    }


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


class _FixedAuthority:
    def __init__(self, snapshot) -> None:
        self._snapshot = snapshot

    def load(self, project_id: str):
        if project_id != self._snapshot.project_id:
            raise ValueError("project_id does not match the fixed authority snapshot")
        return self._snapshot
