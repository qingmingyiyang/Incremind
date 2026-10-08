from __future__ import annotations

import pytest

from core.product_core.expert_binding_snapshot import (
    ExpertBindingSnapshotError,
    SNAPSHOT_SCHEMA_VERSION,
    freeze_expert_binding_snapshot,
    validate_snapshot,
    verify_snapshot_replay,
)
from core.product_core.expert_catalog import (
    ExpertCatalog,
    ExpertProjectBindingStore,
    ExpertConfigurationResolver,
    default_video_research_expert_profile,
)


TOOL_REVISIONS = {
    "analyze_source": 3,
    "memory.recall": 1,
    "document.draft.propose": 2,
}
CONTEXT_MANIFEST_ID = "context-manifest-turn-00000001"
MODEL_ROUTE_REVISION = "a" * 64


@pytest.fixture()
def root(tmp_path):
    return tmp_path


def _prepared(root):
    catalog = ExpertCatalog(root)
    bindings = ExpertProjectBindingStore(root)
    catalog.create(
        {**default_video_research_expert_profile(), "status": "active"},
        expected_registry_revision=0,
    )
    bindings.bind(
        "project-a", "video-research-expert", catalog=catalog,
        enabled_expert_revision=1, intent_affinity=["media_analysis", "research"],
        selection_mode="auto", default=True, reason="试点绑定",
        expected_store_revision=0,
    )
    service = ExpertConfigurationResolver(catalog, bindings)
    receipt = service.select("project-a", ["media_analysis"], budget="research-2k")
    return catalog, bindings, service, receipt


def _freeze(catalog, bindings, receipt, **overrides):
    return freeze_expert_binding_snapshot(
        receipt,
        catalog=catalog,
        bindings=bindings,
        context_manifest_revision=overrides.get("context_manifest_revision", CONTEXT_MANIFEST_ID),
        boundary_revision=overrides.get("boundary_revision", 4),
        model_route_revision=overrides.get("model_route_revision", MODEL_ROUTE_REVISION),
        tool_capability_revisions=overrides.get("tool_capability_revisions", dict(TOOL_REVISIONS)),
        budget=overrides.get("budget"),
    )


def _replay(snapshot, catalog, bindings, **overrides):
    return verify_snapshot_replay(
        snapshot,
        catalog=catalog,
        bindings=bindings,
        context_manifest_revision=overrides.get("context_manifest_revision", snapshot["context_manifest_revision"]),
        boundary_revision=overrides.get("boundary_revision", snapshot["boundary_revision"]),
        model_route_revision=overrides.get("model_route_revision", snapshot["model_route_revision"]),
        tool_capability_revisions=overrides.get(
            "tool_capability_revisions", snapshot["tool_capability_revisions"]
        ),
    )


def test_freeze_produces_immutable_snapshot_with_all_revision_domains(root):
    catalog, bindings, _service, receipt = _prepared(root)
    snapshot = _freeze(catalog, bindings, receipt)
    assert snapshot["schema_version"] == SNAPSHOT_SCHEMA_VERSION
    assert snapshot["expert_id"] == "video-research-expert"
    assert snapshot["expert_revision"] == 1
    assert snapshot["project_id"] == "project-a"
    assert snapshot["binding_revision"] == 1
    assert snapshot["selection_mode"] == "manual"
    assert snapshot["scorecard_ref"] == {
        "scorecard_id": "evidence-grounded-research", "revision": 1,
    }
    assert snapshot["skill_refs"] == [
        {"skill_id": "media-comprehension", "revision": 1},
        {"skill_id": "knowledge-intake", "revision": 1},
    ]
    assert snapshot["tools"] == ["analyze_source", "memory.recall", "document.draft.propose"]
    assert snapshot["tool_capability_revisions"] == TOOL_REVISIONS
    assert snapshot["context_manifest_revision"] == CONTEXT_MANIFEST_ID
    assert snapshot["boundary_revision"] == 4
    assert snapshot["model_route_revision"] == MODEL_ROUTE_REVISION
    assert snapshot["autonomy_ceiling"] == "propose_only"
    assert snapshot["budget"] == "research-2k"
    assert snapshot["fallback"] == "generic_agent"
    assert snapshot["snapshot_id"].startswith("ebs-")
    assert snapshot["frozen_at"]


def test_freeze_is_deterministic_and_rejects_receipt_drift(root):
    catalog, bindings, _service, receipt = _prepared(root)
    first = _freeze(catalog, bindings, receipt)
    second = _freeze(catalog, bindings, receipt)
    assert first["snapshot_id"] == second["snapshot_id"]

    catalog.upgrade(
        "video-research-expert",
        default_video_research_expert_profile() | {"method": "新方法"},
        expected_expert_revision=1,
        expected_registry_revision=catalog.registry_revision,
    )
    with pytest.raises(ExpertBindingSnapshotError, match="drifted selection receipt|drifted between selection"):
        _freeze(catalog, bindings, receipt)


def test_freeze_rejects_tool_revision_mismatch(root):
    catalog, bindings, _service, receipt = _prepared(root)
    with pytest.raises(ExpertBindingSnapshotError, match="cover exactly"):
        _freeze(catalog, bindings, receipt, tool_capability_revisions={"analyze_source": 3})
    bad = dict(TOOL_REVISIONS)
    bad["extra_tool"] = 1
    with pytest.raises(ExpertBindingSnapshotError, match="cover exactly"):
        _freeze(catalog, bindings, receipt, tool_capability_revisions=bad)
    zero = dict(TOOL_REVISIONS)
    zero["analyze_source"] = 0
    with pytest.raises(ExpertBindingSnapshotError, match="positive int"):
        _freeze(catalog, bindings, receipt, tool_capability_revisions=zero)


def test_freeze_refuses_unbound_project_receipt(root):
    catalog, bindings, service, _receipt = _prepared(root)
    receipt = service.select("project-b", ["media_analysis"], requested_expert_id="video-research-expert")
    assert receipt["selected"] is None
    with pytest.raises(ExpertBindingSnapshotError, match="no selected expert"):
        _freeze(catalog, bindings, receipt)


def test_replay_ok_and_every_domain_drift_fails_closed(root):
    catalog, bindings, _service, receipt = _prepared(root)
    snapshot = _freeze(catalog, bindings, receipt)
    assert _replay(snapshot, catalog, bindings)["status"] == "ok"

    catalog.upgrade(
        "video-research-expert",
        default_video_research_expert_profile() | {"method": "升级方法"},
        expected_expert_revision=1,
        expected_registry_revision=catalog.registry_revision,
    )
    verdict = _replay(snapshot, catalog, bindings)
    assert verdict["status"] == "drifted"
    assert "expert_revision_drift" in verdict["reasons"]
    assert "expert_frozen_refs_drift" in verdict["reasons"]

    # 回到原 revision 语义不可逆（历史版本不复活 current），改用第二套绑定校验其余域
    catalog2 = ExpertCatalog(root)
    snapshot2 = dict(snapshot)
    snapshot2["expert_revision"] = 2
    snapshot2["binding_revision"] = 2
    snapshot2["method"] = catalog2.get("video-research-expert")["method"]
    from core.product_core.expert_binding_snapshot import _canonical_snapshot_id
    snapshot2.pop("frozen_at")
    snapshot2["snapshot_id"] = _canonical_snapshot_id(snapshot2)
    bindings.update(
        "project-a", "video-research-expert",
        expected_binding_revision=1, expected_store_revision=bindings.store_revision,
        enabled_expert_revision=2, reason="跟随升级",
    )
    assert _replay(snapshot2, catalog, bindings)["status"] == "ok"

    verdict = _replay(snapshot2, catalog, bindings, context_manifest_revision="context-manifest-turn-00000002")
    assert "context_manifest_revision_drift" in verdict["reasons"]
    verdict = _replay(snapshot2, catalog, bindings, boundary_revision=5)
    assert "boundary_revision_drift" in verdict["reasons"]
    verdict = _replay(snapshot2, catalog, bindings, model_route_revision="b" * 64)
    assert "model_route_revision_drift" in verdict["reasons"]
    drifted_tools = dict(snapshot2["tool_capability_revisions"])
    drifted_tools["analyze_source"] = 4
    verdict = _replay(snapshot2, catalog, bindings, tool_capability_revisions=drifted_tools)
    assert "tool_capability_revision_drift" in verdict["reasons"]

    bindings.update(
        "project-a", "video-research-expert",
        expected_binding_revision=2, expected_store_revision=bindings.store_revision,
        selection_mode="disabled", reason="停用",
    )
    assert "binding_disabled" in _replay(snapshot2, catalog, bindings)["reasons"]
    bindings.unbind(
        "project-a", "video-research-expert",
        reason="解绑", expected_binding_revision=3,
        expected_store_revision=bindings.store_revision,
    )
    assert "binding_missing" in _replay(snapshot2, catalog, bindings)["reasons"]


def test_replay_detects_tampered_snapshot_id(root):
    catalog, bindings, _service, receipt = _prepared(root)
    snapshot = _freeze(catalog, bindings, receipt)
    tampered = dict(snapshot)
    tampered["boundary_revision"] = 99
    verdict = _replay(tampered, catalog, bindings)
    assert "snapshot_id_mismatch" in verdict["reasons"]


def test_validate_snapshot_rejects_unknown_fields_and_bad_schema(root):
    catalog, bindings, _service, receipt = _prepared(root)
    snapshot = _freeze(catalog, bindings, receipt)
    assert validate_snapshot(snapshot)["expert_id"] == "video-research-expert"

    with pytest.raises(ExpertBindingSnapshotError, match="unknown fields"):
        validate_snapshot({**snapshot, "prompt": "隐藏提示词"})
    with pytest.raises(ExpertBindingSnapshotError, match="schema_version"):
        validate_snapshot({**snapshot, "schema_version": "0.9.0"})
    missing = {k: v for k, v in snapshot.items() if k != "binding_revision"}
    with pytest.raises(ExpertBindingSnapshotError, match="binding_revision"):
        validate_snapshot(missing)
    bad_tools = {**snapshot, "tool_capability_revisions": {"analyze_source": 3}}
    with pytest.raises(ExpertBindingSnapshotError, match="cover exactly"):
        validate_snapshot(bad_tools)


@pytest.mark.parametrize("field,bad_value", [
    ("context_manifest_revision", 7),
    ("context_manifest_revision", ""),
    ("model_route_revision", 9),
    ("model_route_revision", ""),
])
def test_snapshot_rejects_non_identity_context_and_model_revisions(root, field, bad_value):
    catalog, bindings, _service, receipt = _prepared(root)
    snapshot = _freeze(catalog, bindings, receipt)
    malformed = {**snapshot, field: bad_value}
    with pytest.raises(ExpertBindingSnapshotError, match="non-empty revision identity string"):
        validate_snapshot(malformed)
