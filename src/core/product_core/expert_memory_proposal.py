"""Expert Memory Proposal — 专家结果进入记忆候选的唯一领域入口。

专家输出不能直接修改正式记忆：本模块把 Expert Receipt + 证据 + 建议变更映射为既有
``ImportExternalAgentProposal`` 的 proposal-only 提案，进入 ``pending_review`` 候选
审核链（验收 10：长期记忆只能通过候选审核进入）。

构建前置全部 fail-closed：
- 绑定快照必须通过严格校验且 ``verify_snapshot_replay`` 为 ok（revision 漂移立即停止）；
- Expert Receipt 必须通过 canonical codec，且 identity 必须与快照一致；
- 证据非空；project_id 必须等于快照 project_id（跨项目隔离）；
- ``requires_user_review`` 恒为 True；proposal_type 限既有四类；
- 专家 provenance 由本模块系统盖章写入 ``suggested_changes["expert_provenance"]``，
  调用方预置同名键一律拒绝；
- proposal_id 由 snapshot_id + receipt 摘要 + 建议内容确定性派生，重复提交落同一
  pending 提案（幂等）。
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Mapping

from .expert_binding_snapshot import (
    ExpertBindingSnapshotError,
    validate_snapshot,
    verify_snapshot_replay,
)
from .expert_catalog import ExpertCatalog, ExpertProjectBindingStore, _clean_str
from .expert_execution_receipt import validate_expert_execution_receipt

ALLOWED_EXPERT_PROPOSAL_TYPES = frozenset({
    "memory_candidate_proposal",
    "series_update_proposal",
    "project_skill_update_proposal",
    "document_revision_proposal",
})


class ExpertMemoryProposalError(ExpertBindingSnapshotError):
    pass


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:16]


def _require_receipt_field(receipt: Mapping[str, object], key: str, expected: object, label: str) -> None:
    actual = receipt.get(key)
    if actual != expected:
        raise ExpertMemoryProposalError(
            f"expert receipt {key} does not match frozen snapshot {label}: "
            f"receipt={actual!r} snapshot={expected!r}"
        )


def _refs_with_locator(refs: object, field: str) -> list[Mapping[str, object]]:
    if not isinstance(refs, list) or not refs:
        raise ExpertMemoryProposalError(f"{field} must be a non-empty list")
    cleaned: list[Mapping[str, object]] = []
    for index, item in enumerate(refs):
        if isinstance(item, str) and item.strip():
            cleaned.append({"locator": item.strip()})
        elif isinstance(item, Mapping):
            locator = item.get("locator") or item.get("ref") or item.get("source_id")
            if not (isinstance(locator, str) and locator.strip()):
                raise ExpertMemoryProposalError(f"{field}[{index}] requires a locator")
            cleaned.append(dict(item))
        else:
            raise ExpertMemoryProposalError(f"{field}[{index}] is invalid")
    return cleaned


def build_expert_memory_proposal(
    *,
    snapshot: Mapping[str, object],
    catalog: ExpertCatalog,
    bindings: ExpertProjectBindingStore,
    context_manifest_revision: int,
    boundary_revision: int,
    model_route_revision: int,
    tool_capability_revisions: Mapping[str, int],
    expert_receipt: Mapping[str, object],
    suggested_changes: Mapping[str, object],
    source_refs: list[object] | None = None,
    evidence_refs: list[object] | None = None,
    proposal_type: str = "memory_candidate_proposal",
) -> dict[str, object]:
    """把专家结果映射为 proposal-only 提案 payload（交 ImportExternalAgentProposal.execute）。"""
    validated = validate_snapshot(snapshot)
    replay = verify_snapshot_replay(
        snapshot,
        catalog=catalog,
        bindings=bindings,
        context_manifest_revision=context_manifest_revision,
        boundary_revision=boundary_revision,
        model_route_revision=model_route_revision,
        tool_capability_revisions=tool_capability_revisions,
    )
    if replay["status"] != "ok":
        raise ExpertMemoryProposalError(
            "frozen binding snapshot drifted; refusing to build proposal: "
            + "; ".join(replay["reasons"])
        )

    canonical_receipt = validate_expert_execution_receipt(expert_receipt)
    _require_receipt_field(canonical_receipt, "snapshot_id", validated["snapshot_id"], "snapshot_id")
    _require_receipt_field(canonical_receipt, "project_id", validated["project_id"], "project_id")
    _require_receipt_field(canonical_receipt, "expert_id", validated["expert_id"], "expert_id")
    _require_receipt_field(canonical_receipt, "expert_revision", validated["expert_revision"], "expert_revision")
    _require_receipt_field(
        canonical_receipt,
        "context_manifest_revision",
        validated["context_manifest_revision"],
        "context_manifest_revision",
    )
    receipt_summary = _clean_str(canonical_receipt.get("summary"))
    if not receipt_summary:
        raise ExpertMemoryProposalError("expert_receipt.summary is required")

    if proposal_type not in ALLOWED_EXPERT_PROPOSAL_TYPES:
        raise ExpertMemoryProposalError(
            f"proposal_type must be one of {sorted(ALLOWED_EXPERT_PROPOSAL_TYPES)}"
        )
    if not isinstance(suggested_changes, Mapping):
        raise ExpertMemoryProposalError(
            "suggested_changes must be a mapping so target_layer and provenance survive review"
        )
    if "expert_provenance" in suggested_changes:
        raise ExpertMemoryProposalError(
            "suggested_changes must not pre-set expert_provenance; it is system-stamped"
        )

    receipt_evidence = canonical_receipt["input_evidence_refs"]
    caller_evidence = evidence_refs or []
    merged_evidence = _refs_with_locator(
        [*receipt_evidence, *caller_evidence], "evidence_refs"
    )
    clean_source_refs = (
        _refs_with_locator(source_refs, "source_refs") if source_refs else [
            {"locator": f"expert-binding-snapshot:{validated['snapshot_id']}"}
        ]
    )

    provenance = {
        "kind": "expert_execution",
        "snapshot_id": validated["snapshot_id"],
        "project_id": validated["project_id"],
        "expert_id": validated["expert_id"],
        "expert_revision": validated["expert_revision"],
        "binding_revision": validated["binding_revision"],
        "context_manifest_revision": validated["context_manifest_revision"],
        "boundary_revision": validated["boundary_revision"],
        "model_route_revision": validated["model_route_revision"],
        "tool_capability_revisions": deepcopy(dict(validated["tool_capability_revisions"])),
        "autonomy_ceiling": validated.get("autonomy_ceiling"),
        "receipt_id": canonical_receipt["receipt_id"],
        "receipt_status": canonical_receipt["status"],
        "receipt_summary": receipt_summary,
        "receipt_stages": deepcopy(canonical_receipt["stages"]),
        "tool_invocation_refs": deepcopy(canonical_receipt["tool_invocation_refs"]),
        "output_refs": deepcopy(canonical_receipt["output_refs"]),
        "snapshot_frozen_at": deepcopy(snapshot.get("frozen_at")),
    }

    stamped_changes = deepcopy(dict(suggested_changes))
    stamped_changes["expert_provenance"] = provenance

    proposal_id = "expert-" + _digest({
        "snapshot_id": validated["snapshot_id"],
        "receipt": _digest(canonical_receipt),
        "suggested_changes": _digest(stamped_changes),
        "proposal_type": proposal_type,
    })

    return {
        "proposal_id": proposal_id,
        "proposal_type": proposal_type,
        "project_id": validated["project_id"],
        "summary": receipt_summary,
        "suggested_changes": stamped_changes,
        "requires_user_review": True,
        "source_refs": clean_source_refs,
        "evidence_refs": merged_evidence,
    }


def submit_expert_memory_proposal(
    importer,
    *,
    snapshot: Mapping[str, object],
    catalog: ExpertCatalog,
    bindings: ExpertProjectBindingStore,
    context_manifest_revision: int,
    boundary_revision: int,
    model_route_revision: int,
    tool_capability_revisions: Mapping[str, int],
    expert_receipt: Mapping[str, object],
    suggested_changes: Mapping[str, object],
    source_refs: list[object] | None = None,
    evidence_refs: list[object] | None = None,
    proposal_type: str = "memory_candidate_proposal",
):
    """构建并经既有 importer 提交；返回 ExternalAgentProposalImportResult。"""
    payload = build_expert_memory_proposal(
        snapshot=snapshot,
        catalog=catalog,
        bindings=bindings,
        context_manifest_revision=context_manifest_revision,
        boundary_revision=boundary_revision,
        model_route_revision=model_route_revision,
        tool_capability_revisions=tool_capability_revisions,
        expert_receipt=expert_receipt,
        suggested_changes=suggested_changes,
        source_refs=source_refs,
        evidence_refs=evidence_refs,
        proposal_type=proposal_type,
    )
    return importer.execute(
        proposal=payload, project_id=_clean_str(payload["project_id"])
    )
