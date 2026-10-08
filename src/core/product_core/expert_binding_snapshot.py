"""Expert Binding Snapshot — 目标流程图「冻结 Expert Binding Snapshot」的领域层。

任务启动时以已验证的 Selection Receipt 为输入生成不可变快照，冻结目标要求的全部
revision 域：expert_id/revision、project/binding revision、Skill revisions、
Tool capability revisions、Context Manifest revision、Boundary revision、
Model route revision，以及自主上限、预算与 fallback。

恢复路径只重放校验（``verify_snapshot_replay``）：catalog/binding 现值与调用方提供的
runtime revision 逐域与快照比对，任一漂移返回 drifted 与具体理由，绝不重新选择。

本模块是纯领域切片：不接 ai_kernel、不触网络；Turn 接线与 run lease/checkpoint 属
后续 ai_kernel 组合切片。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
from typing import Mapping

from .expert_catalog import (
    ExpertCatalog,
    ExpertCatalogError,
    ExpertProjectBindingStore,
    _clean_str,
)


def _str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item.strip())

SNAPSHOT_SCHEMA_VERSION = "1.0.0"

_SNAPSHOT_FIELDS = frozenset({
    "schema_version", "snapshot_id", "project_id", "expert_id",
    "expert_revision", "binding_revision", "selection_mode",
    "role", "method", "output_contract", "prohibited",
    "skill_refs", "tools", "tool_capability_revisions",
    "context_manifest_revision", "boundary_revision", "model_route_revision",
    "autonomy_ceiling", "scorecard_ref", "budget", "fallback",
    "receipt_decided_at", "frozen_at",
})


class ExpertBindingSnapshotError(ExpertCatalogError):
    pass


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ExpertBindingSnapshotError(f"{field} must be a positive int")
    return value


def _revision_identity(value: object, field: str) -> str:
    """Validate an opaque, immutable revision identity without inventing a counter.

    Context manifests are identified by their immutable ``manifest_id`` and model
    routing snapshots by their opaque revision identity (the production model
    router currently emits a SHA-256 digest).  Neither authority is an ordinal
    revision, so coercing either into an integer would manufacture a second,
    non-authoritative version scheme.
    """
    if not isinstance(value, str) or not value.strip():
        raise ExpertBindingSnapshotError(f"{field} must be a non-empty revision identity string")
    return value


def _canonical_snapshot_id(payload: Mapping[str, object]) -> str:
    identity = {key: payload[key] for key in sorted(_SNAPSHOT_FIELDS - {"snapshot_id", "frozen_at"})}
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "ebs-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def freeze_expert_binding_snapshot(
    selection_receipt: Mapping[str, object],
    *,
    catalog: ExpertCatalog,
    bindings: ExpertProjectBindingStore,
    context_manifest_revision: str,
    boundary_revision: int,
    model_route_revision: str,
    tool_capability_revisions: Mapping[str, int],
    budget: str | None = None,
) -> dict[str, object]:
    """从已验证的 Selection Receipt 冻结不可变绑定快照。

    冻结前置：receipt 必须含 selected 且 ``verify_receipt`` 为 ok；冻结瞬间再次核对
    catalog/binding 现值；工具 revision 键集必须与 profile tools 精确一致。
    """
    service = _selection_service(catalog, bindings)
    verdict = service.verify_receipt(selection_receipt)
    if verdict["status"] != "ok":
        raise ExpertBindingSnapshotError(
            "cannot freeze from a drifted selection receipt: " + "; ".join(verdict["reasons"])
        )
    selected = selection_receipt.get("selected")
    if not isinstance(selected, Mapping):
        raise ExpertBindingSnapshotError("selection receipt has no selected expert")
    expert_id = _clean_str(selected.get("expert_id"))
    expert = catalog.get(expert_id)
    if expert is None:
        raise ExpertBindingSnapshotError(f"expert {expert_id} disappeared before freeze")
    if expert.get("revision") != selected.get("expert_revision"):
        raise ExpertBindingSnapshotError(
            f"expert {expert_id} revision drifted between selection and freeze"
        )
    binding = bindings.get(_clean_str(selection_receipt.get("project_id")), expert_id)
    if binding is None:
        raise ExpertBindingSnapshotError(f"binding for {expert_id} disappeared before freeze")
    if binding.get("binding_revision") != selected.get("binding_revision"):
        raise ExpertBindingSnapshotError(
            f"binding for {expert_id} drifted between selection and freeze"
        )
    if binding.get("selection_mode") == "disabled":
        raise ExpertBindingSnapshotError(f"binding for {expert_id} was disabled before freeze")

    skills = expert.get("skills")
    if not isinstance(skills, list) or not skills:
        raise ExpertBindingSnapshotError("expert profile has no skill references to freeze")
    tools = _str_tuple(expert.get("tools"))
    if not tools:
        raise ExpertBindingSnapshotError("expert profile has no tools to freeze")
    capability_revisions = dict(tool_capability_revisions)
    if set(capability_revisions) != set(tools):
        raise ExpertBindingSnapshotError(
            "tool_capability_revisions must cover exactly the profile tools: "
            f"expected {sorted(tools)}, got {sorted(capability_revisions)}"
        )
    for tool_id, revision in capability_revisions.items():
        _positive_int(revision, f"tool_capability_revisions[{tool_id}]")

    frozen_refs = selection_receipt.get("frozen_refs")
    if isinstance(frozen_refs, Mapping) and frozen_refs.get("skills") is not None:
        if frozen_refs["skills"] != skills:
            raise ExpertBindingSnapshotError("receipt frozen skill refs drifted from catalog")

    payload: dict[str, object] = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "project_id": _clean_str(selection_receipt.get("project_id")),
        "expert_id": expert_id,
        "expert_revision": _positive_int(expert.get("revision"), "expert_revision"),
        "binding_revision": _positive_int(binding.get("binding_revision"), "binding_revision"),
        "selection_mode": _clean_str(binding.get("selection_mode")),
        "role": _clean_str(expert.get("role")),
        "method": _clean_str(expert.get("method")),
        "output_contract": _clean_str(expert.get("output_contract")),
        "prohibited": list(_str_tuple(expert.get("prohibited"))),
        "skill_refs": deepcopy(skills),
        "tools": list(tools),
        "tool_capability_revisions": capability_revisions,
        "context_manifest_revision": _revision_identity(
            context_manifest_revision, "context_manifest_revision"
        ),
        "boundary_revision": _positive_int(boundary_revision, "boundary_revision"),
        "model_route_revision": _revision_identity(model_route_revision, "model_route_revision"),
        "autonomy_ceiling": _clean_str(expert.get("autonomy_ceiling")) or None,
        "scorecard_ref": deepcopy(expert.get("quality_gate_ref")),
        "budget": _clean_str(budget) or _clean_str(selection_receipt.get("budget")) or "default",
        "fallback": _clean_str(selection_receipt.get("fallback")) or "generic_agent",
        "receipt_decided_at": _clean_str(selection_receipt.get("decided_at")) or None,
    }
    payload["snapshot_id"] = _canonical_snapshot_id(payload)
    payload["frozen_at"] = datetime.now(UTC).isoformat()
    return payload


def verify_snapshot_replay(
    snapshot: Mapping[str, object],
    *,
    catalog: ExpertCatalog,
    bindings: ExpertProjectBindingStore,
    context_manifest_revision: str,
    boundary_revision: int,
    model_route_revision: str,
    tool_capability_revisions: Mapping[str, int],
) -> dict[str, object]:
    """恢复路径专用：只校验冻结快照，不重新选择。任一域漂移返回 drifted。"""
    validated = validate_snapshot(snapshot)
    reasons: list[str] = []
    expert_id = validated["expert_id"]
    project_id = validated["project_id"]

    expert = catalog.get(expert_id)
    if expert is None:
        reasons.append("expert_missing_from_catalog")
    else:
        if expert.get("revision") != validated["expert_revision"]:
            reasons.append("expert_revision_drift")
        if expert.get("status") != "active":
            reasons.append("expert_not_active")
        elif (
            expert.get("skills") != validated["skill_refs"]
            or _str_tuple(expert.get("tools")) != tuple(validated["tools"])
            or _clean_str(expert.get("role")) != validated["role"]
            or _clean_str(expert.get("method")) != validated["method"]
            or _clean_str(expert.get("output_contract")) != validated["output_contract"]
            or _str_tuple(expert.get("prohibited")) != tuple(validated["prohibited"])
            or expert.get("quality_gate_ref") != validated["scorecard_ref"]
        ):
            reasons.append("expert_frozen_refs_drift")

    binding = bindings.get(project_id, expert_id)
    if binding is None:
        reasons.append("binding_missing")
    else:
        if binding.get("binding_revision") != validated["binding_revision"]:
            reasons.append("binding_revision_drift")
        if binding.get("selection_mode") == "disabled":
            reasons.append("binding_disabled")

    if context_manifest_revision != validated["context_manifest_revision"]:
        reasons.append("context_manifest_revision_drift")
    if boundary_revision != validated["boundary_revision"]:
        reasons.append("boundary_revision_drift")
    if model_route_revision != validated["model_route_revision"]:
        reasons.append("model_route_revision_drift")
    if dict(tool_capability_revisions) != validated["tool_capability_revisions"]:
        reasons.append("tool_capability_revision_drift")

    expected_id = _canonical_snapshot_id(validated)
    if validated["snapshot_id"] != expected_id:
        reasons.append("snapshot_id_mismatch")
    if reasons:
        return {"status": "drifted", "reasons": reasons}
    return {"status": "ok", "reasons": []}


def validate_snapshot(snapshot: Mapping[str, object]) -> dict[str, object]:
    """严格 codec：schema_version、必填字段、类型与未知顶层字段校验。"""
    if not isinstance(snapshot, Mapping):
        raise ExpertBindingSnapshotError("snapshot must be a mapping")
    unknown = set(snapshot) - _SNAPSHOT_FIELDS
    if unknown:
        raise ExpertBindingSnapshotError(f"snapshot has unknown fields: {sorted(unknown)}")
    if snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ExpertBindingSnapshotError(
            f"snapshot schema_version must be {SNAPSHOT_SCHEMA_VERSION}"
        )
    payload = dict(snapshot)
    for field in (
        "snapshot_id", "project_id", "expert_id", "expert_revision", "binding_revision",
        "role", "method", "output_contract", "prohibited", "scorecard_ref",
        "context_manifest_revision", "boundary_revision", "model_route_revision",
    ):
        if field not in payload:
            raise ExpertBindingSnapshotError(f"snapshot missing required field {field}")
    _positive_int(payload.get("expert_revision"), "expert_revision")
    _positive_int(payload.get("binding_revision"), "binding_revision")
    _revision_identity(payload.get("context_manifest_revision"), "context_manifest_revision")
    _positive_int(payload.get("boundary_revision"), "boundary_revision")
    _revision_identity(payload.get("model_route_revision"), "model_route_revision")
    if not _clean_str(payload.get("expert_id")) or not _clean_str(payload.get("project_id")):
        raise ExpertBindingSnapshotError("snapshot must name expert_id and project_id")
    for field in ("role", "method", "output_contract"):
        if not _clean_str(payload.get(field)):
            raise ExpertBindingSnapshotError(f"snapshot {field} must be non-empty")
    prohibited = payload.get("prohibited")
    if not isinstance(prohibited, list) or not prohibited or any(
        not isinstance(item, str) or not item.strip() for item in prohibited
    ):
        raise ExpertBindingSnapshotError("snapshot prohibited rules must be non-empty strings")
    scorecard = payload.get("scorecard_ref")
    if not isinstance(scorecard, Mapping) or not _clean_str(scorecard.get("scorecard_id")):
        raise ExpertBindingSnapshotError("snapshot must freeze a governed scorecard ref")
    _positive_int(scorecard.get("revision"), "scorecard_ref.revision")
    if not isinstance(payload.get("snapshot_id"), str) or not payload["snapshot_id"].startswith("ebs-"):
        raise ExpertBindingSnapshotError("snapshot_id must be an ebs- prefixed digest")
    skills = payload.get("skill_refs")
    if not isinstance(skills, list) or not skills:
        raise ExpertBindingSnapshotError("snapshot must freeze at least one skill ref")
    tools = payload.get("tools")
    if not isinstance(tools, list) or not tools:
        raise ExpertBindingSnapshotError("snapshot must freeze at least one tool")
    capability = payload.get("tool_capability_revisions")
    if not isinstance(capability, Mapping) or set(capability) != set(tools):
        raise ExpertBindingSnapshotError(
            "snapshot tool_capability_revisions must cover exactly the frozen tools"
        )
    for tool_id, revision in capability.items():
        _positive_int(revision, f"tool_capability_revisions[{tool_id}]")
    return payload


def _selection_service(catalog: ExpertCatalog, bindings: ExpertProjectBindingStore):
    from .expert_catalog import ExpertConfigurationResolver

    return ExpertConfigurationResolver(catalog, bindings)
