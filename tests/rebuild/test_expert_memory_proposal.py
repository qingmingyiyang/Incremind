from __future__ import annotations

import pytest

from backend.api.expert_memory_proposal_runtime import ExpertMemoryProposalRuntime
from core.product_core.expert_binding_snapshot import (
    freeze_expert_binding_snapshot,
)
from core.product_core.expert_catalog import (
    ExpertCatalog,
    ExpertProjectBindingStore,
    ExpertConfigurationResolver,
    default_video_research_expert_profile,
)
from core.product_core.expert_memory_proposal import (
    ExpertMemoryProposalError,
    build_expert_memory_proposal,
    submit_expert_memory_proposal,
)
from core.product_core.expert_execution_receipt import (
    ExpertExecutionReceiptError,
    build_expert_execution_receipt,
)
from core.product_core.external_agent_proposal_import import (
    ExternalAgentProposalImportError,
    ImportExternalAgentProposal,
)
from core.storage_provider import JsonObjectStore

TOOL_REVISIONS = {"analyze_source": 3, "memory.recall": 1, "document.draft.propose": 2}
CONTEXT_MANIFEST_ID = "context-manifest-turn-00000001"
MODEL_ROUTE_REVISION = "a" * 64
SUGGESTED = {
    "target_layer": "atom",
    "candidate_type": "preference",
    "content": "视频研究结论：字幕优先于 ASR，时间戳必须逐条对应。",
}


def _receipt(snapshot, **overrides) -> dict:
    values = {
        "status": "completed",
        "summary": "完成该视频的观点—证据整理。",
        "stages": [{"stage": "resolve", "status": "completed"}],
        "tool_invocation_refs": ["tool-invocation:inv-001"],
        "input_evidence_refs": ["source:src-1#t=42"],
        "output_refs": ["document:doc-1"],
    }
    values.update(overrides)
    return build_expert_execution_receipt(snapshot=snapshot, **values)


def _world(tmp_path):
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    catalog = ExpertCatalog(tmp_path)
    bindings = ExpertProjectBindingStore(tmp_path)
    catalog.create(
        {**default_video_research_expert_profile(), "status": "active"},
        expected_registry_revision=0,
    )
    bindings.bind(
        "project-a", "video-research-expert", catalog=catalog,
        enabled_expert_revision=1, intent_affinity=["media_analysis"],
        selection_mode="auto", default=True, reason="试点",
        expected_store_revision=0,
    )
    service = ExpertConfigurationResolver(catalog, bindings)
    selection = service.select("project-a", ["media_analysis"])
    snapshot = freeze_expert_binding_snapshot(
        selection, catalog=catalog, bindings=bindings,
        context_manifest_revision=CONTEXT_MANIFEST_ID, boundary_revision=4,
        model_route_revision=MODEL_ROUTE_REVISION, tool_capability_revisions=dict(TOOL_REVISIONS),
    )
    receipt = _receipt(snapshot)
    importer = ImportExternalAgentProposal(store)
    kwargs = dict(
        snapshot=snapshot, catalog=catalog, bindings=bindings,
        context_manifest_revision=CONTEXT_MANIFEST_ID, boundary_revision=4,
        model_route_revision=MODEL_ROUTE_REVISION, tool_capability_revisions=dict(TOOL_REVISIONS),
    )
    return snapshot, receipt, importer, kwargs, store


def _submit(importer, receipt, kwargs, **overrides):
    return submit_expert_memory_proposal(
        importer,
        expert_receipt=receipt,
        suggested_changes=overrides.pop("suggested_changes", dict(SUGGESTED)),
        **{**kwargs, **overrides},
    )


def test_submit_creates_pending_review_candidate_with_provenance(tmp_path):
    snapshot, receipt, importer, kwargs, store = _world(tmp_path)
    result = _submit(importer, receipt, kwargs)
    assert result.status == "pending_review"
    assert result.proposal_type == "memory_candidate_proposal"
    assert result.memory_candidate_id
    assert result.proposal_id.startswith("expert-")

    stored = store.read("external_agent_proposals", result.proposal_id)
    assert stored["requires_user_review"] is True
    assert stored["review"]["auto_promote_allowed"] is False
    provenance = stored["suggested_changes"]["expert_provenance"]
    assert provenance["expert_id"] == "video-research-expert"
    assert provenance["snapshot_id"] == snapshot["snapshot_id"]
    assert provenance["receipt_id"] == receipt["receipt_id"]
    assert provenance["receipt_status"] == "completed"
    assert provenance["tool_invocation_refs"] == ["tool-invocation:inv-001"]
    candidate = store.read("memory_candidates", result.memory_candidate_id)
    assert candidate["status"] == "pending_review"
    assert candidate["target_layer"] == "atom"


def _snapshot_id_of(stored) -> str:
    return stored["suggested_changes"]["expert_provenance"]["snapshot_id"]


def test_duplicate_submission_returns_existing_pending_review_proposal(tmp_path):
    _snapshot, receipt, importer, kwargs, store = _world(tmp_path)
    first = _submit(importer, receipt, kwargs)
    # builder 层幂等：同一 receipt+建议 → 同一确定性 proposal_id
    rebuilt = build_expert_memory_proposal(
        snapshot=_snapshot, catalog=kwargs["catalog"], bindings=kwargs["bindings"],
        context_manifest_revision=CONTEXT_MANIFEST_ID, boundary_revision=4, model_route_revision=MODEL_ROUTE_REVISION,
        tool_capability_revisions=dict(TOOL_REVISIONS),
        expert_receipt=receipt, suggested_changes=dict(SUGGESTED),
    )
    assert rebuilt["proposal_id"] == first.proposal_id
    replayed = _submit(importer, receipt, kwargs)
    assert replayed.status == "pending_review"
    assert replayed.proposal_id == first.proposal_id
    assert replayed.memory_candidate_id == first.memory_candidate_id
    proposals = store.list("external_agent_proposals")
    assert len([p for p in proposals if p.get("id") == first.proposal_id]) == 1


def test_snapshot_drift_blocks_proposal(tmp_path):
    snapshot, receipt, importer, kwargs, _store = _world(tmp_path)
    catalog = kwargs["catalog"]
    catalog.upgrade(
        "video-research-expert",
        default_video_research_expert_profile() | {"method": "升级"},
        expected_expert_revision=1,
        expected_registry_revision=catalog.registry_revision,
    )
    with pytest.raises(ExpertMemoryProposalError, match="drifted"):
        _submit(importer, receipt, kwargs)


def test_receipt_identity_mismatch_blocks_proposal(tmp_path):
    snapshot, receipt, importer, kwargs, _store = _world(tmp_path)
    bindings = kwargs["bindings"]
    catalog = kwargs["catalog"]
    bindings.bind(
        "project-b", "video-research-expert", catalog=catalog,
        enabled_expert_revision=1, intent_affinity=["media_analysis"],
        selection_mode="auto", reason="隔离测试",
        expected_store_revision=bindings.store_revision,
    )
    foreign_selection = ExpertConfigurationResolver(catalog, bindings).select(
        "project-b", ["media_analysis"], requested_expert_id="video-research-expert"
    )
    foreign_snapshot = freeze_expert_binding_snapshot(
        foreign_selection, catalog=catalog, bindings=bindings,
        context_manifest_revision=CONTEXT_MANIFEST_ID, boundary_revision=4,
        model_route_revision=MODEL_ROUTE_REVISION,
        tool_capability_revisions=dict(TOOL_REVISIONS),
    )
    foreign_receipt = _receipt(foreign_snapshot)
    with pytest.raises(ExpertMemoryProposalError, match="snapshot_id"):
        _submit(importer, foreign_receipt, kwargs)
    with pytest.raises(ExpertExecutionReceiptError, match="canonical receipt identity"):
        _submit(importer, {**receipt, "expert_revision": 2}, kwargs)


def test_empty_summary_and_evidence_block_proposal(tmp_path):
    snapshot, receipt, importer, kwargs, _store = _world(tmp_path)
    with pytest.raises(ExpertExecutionReceiptError, match="summary"):
        _submit(importer, {**receipt, "summary": ""}, kwargs)
    with pytest.raises(ExpertExecutionReceiptError, match="input_evidence_refs"):
        _submit(importer, {**receipt, "input_evidence_refs": []}, kwargs)


@pytest.mark.parametrize("legacy_field", ["evidence_refs", "decided_at"])
def test_legacy_receipt_fields_fail_closed(tmp_path, legacy_field):
    _snapshot, receipt, importer, kwargs, _store = _world(tmp_path)
    with pytest.raises(ExpertExecutionReceiptError, match="unknown fields"):
        _submit(importer, {**receipt, legacy_field: []}, kwargs)


def test_cross_project_and_provenance_spoofing_blocked(tmp_path):
    snapshot, receipt, importer, kwargs, _store = _world(tmp_path)
    forged = dict(SUGGESTED)
    forged["expert_provenance"] = {"kind": "fake"}
    with pytest.raises(ExpertMemoryProposalError, match="system-stamped"):
        _submit(importer, receipt, kwargs, suggested_changes=forged)
    with pytest.raises(ExpertMemoryProposalError):
        _submit(importer, receipt, kwargs, suggested_changes="纯文本建议")
    # 快照属于 project-a；提交到 project-b 必须被拒
    payload = build_expert_memory_proposal(
        snapshot=snapshot, catalog=kwargs["catalog"], bindings=kwargs["bindings"],
        context_manifest_revision=CONTEXT_MANIFEST_ID, boundary_revision=4, model_route_revision=MODEL_ROUTE_REVISION,
        tool_capability_revisions=dict(TOOL_REVISIONS),
        expert_receipt=receipt, suggested_changes=dict(SUGGESTED),
    )
    assert payload["project_id"] == "project-a"
    assert payload["requires_user_review"] is True


def test_review_required_cannot_be_disabled(tmp_path):
    _snapshot, receipt, importer, kwargs, _store = _world(tmp_path)
    result = _submit(importer, receipt, kwargs)
    stored = importer._object_store.read("external_agent_proposals", result.proposal_id)
    assert stored["requires_user_review"] is True
    assert "automatic_memory_publication" in stored["blocked_operations"]


def test_production_adapter_rejects_sensitive_summary_before_outbox_intent(tmp_path):
    snapshot, receipt, _importer, _kwargs, store = _world(tmp_path)
    runtime = ExpertMemoryProposalRuntime(tmp_path, store, namespace_id="default")

    with pytest.raises(ExternalAgentProposalImportError, match="sensitive material"):
        runtime.prepare(
            snapshot,
            receipt,
            {
                "summary": "研究结论包含 sk-1234567890abcdefghijklmnop",
                "evidence_refs": ["source:src-1#t=42"],
            },
        )

    assert store.list("external_agent_proposals") == ()
    assert store.list("memory_candidates") == ()
