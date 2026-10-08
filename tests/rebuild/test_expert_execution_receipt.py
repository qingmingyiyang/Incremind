from __future__ import annotations

import pytest

from core.product_core.expert_binding_snapshot import freeze_expert_binding_snapshot
from core.product_core.expert_catalog import (
    ExpertCatalog,
    ExpertProjectBindingStore,
    ExpertConfigurationResolver,
    default_video_research_expert_profile,
)
from core.product_core.expert_execution_receipt import (
    ExpertExecutionReceiptError,
    build_expert_execution_receipt,
    validate_expert_execution_receipt,
    verify_expert_execution_receipt_replay,
)


TOOL_REVISIONS = {"analyze_source": 3, "memory.recall": 1, "document.draft.propose": 2}
CONTEXT_MANIFEST_ID = "context-manifest-turn-00000001"
MODEL_ROUTE_REVISION = "a" * 64


def _snapshot(tmp_path):
    catalog = ExpertCatalog(tmp_path)
    bindings = ExpertProjectBindingStore(tmp_path)
    catalog.create(
        default_video_research_expert_profile() | {"status": "active"},
        expected_registry_revision=0,
    )
    bindings.bind(
        "project-a", "video-research-expert", catalog=catalog,
        enabled_expert_revision=1, intent_affinity=["media_analysis"],
        selection_mode="auto", default=True, reason="test", expected_store_revision=0,
    )
    selection = ExpertConfigurationResolver(catalog, bindings).select("project-a", ["media_analysis"])
    return freeze_expert_binding_snapshot(
        selection, catalog=catalog, bindings=bindings,
        context_manifest_revision=CONTEXT_MANIFEST_ID, boundary_revision=4,
        model_route_revision=MODEL_ROUTE_REVISION,
        tool_capability_revisions=TOOL_REVISIONS,
    )


def _receipt(snapshot, **overrides):
    values = {
        "status": "completed",
        "stages": [
            {"stage": "context_synthesis", "status": "completed"},
            {"stage": "evidence_analysis", "status": "completed"},
        ],
        "tool_invocation_refs": ["tool-invocation:analyze-source-001"],
        "input_evidence_refs": ["source:source-001#t=42"],
        "output_refs": ["document:research-note-001"],
        "summary": "已完成观点与时间戳证据整理。",
    }
    values.update(overrides)
    return build_expert_execution_receipt(snapshot=snapshot, **values)


def test_build_freezes_snapshot_identity_and_is_deterministic(tmp_path):
    snapshot = _snapshot(tmp_path)
    first = _receipt(snapshot)
    second = _receipt(snapshot)
    assert first == second
    assert first["receipt_id"].startswith("eer-")
    assert first["snapshot_id"] == snapshot["snapshot_id"]
    assert first["project_id"] == "project-a"
    assert first["expert_id"] == "video-research-expert"
    assert first["expert_revision"] == 1
    assert first["context_manifest_revision"] == CONTEXT_MANIFEST_ID
    assert validate_expert_execution_receipt(first) == first


def test_completed_requires_outputs_and_input_evidence_while_failed_has_no_outputs(tmp_path):
    snapshot = _snapshot(tmp_path)
    with pytest.raises(ExpertExecutionReceiptError, match="output_refs"):
        _receipt(snapshot, output_refs=[])
    with pytest.raises(ExpertExecutionReceiptError, match="input_evidence_refs"):
        _receipt(snapshot, input_evidence_refs=[])

    failed = _receipt(
        snapshot, status="failed", output_refs=[],
        stages=[{"stage": "context_synthesis", "status": "failed"}],
        summary="受治理工具执行失败，未产生输出。",
    )
    assert failed["status"] == "failed"
    with pytest.raises(ExpertExecutionReceiptError, match="must not contain output_refs"):
        _receipt(snapshot, status="failed")


def test_unknown_fields_nested_shapes_and_identifier_tampering_fail_closed(tmp_path):
    receipt = _receipt(_snapshot(tmp_path))
    with pytest.raises(ExpertExecutionReceiptError, match="unknown fields"):
        validate_expert_execution_receipt({**receipt, "prompt": "hidden"})
    with pytest.raises(ExpertExecutionReceiptError, match="shape is invalid"):
        validate_expert_execution_receipt({**receipt, "stages": [{"stage": "x", "status": "completed", "extra": 1}]})
    with pytest.raises(ExpertExecutionReceiptError, match="canonical receipt identity"):
        validate_expert_execution_receipt({**receipt, "summary": "篡改摘要"})


@pytest.mark.parametrize("field,value", [
    ("input_evidence_refs", ["source:C:/Users/me/private.txt"]),
    ("output_refs", ["document:/private/output.md"]),
    ("tool_invocation_refs", ["tool-invocation:Bearer abc"]),
    ("input_evidence_refs", ["source:cookie=secret"]),
    ("output_refs", ["这里是一整段原始输出内容而不是引用"]),
])
def test_refs_reject_paths_secrets_and_raw_content(tmp_path, field, value):
    with pytest.raises(ExpertExecutionReceiptError):
        _receipt(_snapshot(tmp_path), **{field: value})


def test_replay_verifies_original_snapshot_without_reselection(tmp_path):
    snapshot = _snapshot(tmp_path)
    receipt = _receipt(snapshot)
    assert verify_expert_execution_receipt_replay(receipt, snapshot=snapshot) == {
        "status": "ok", "reasons": []
    }
    drifted = {**snapshot, "project_id": "project-b"}
    verdict = verify_expert_execution_receipt_replay(receipt, snapshot=drifted)
    assert verdict == {"status": "drifted", "reasons": ["project_id_drift"]}
