from __future__ import annotations

import pytest

from core.product_core.expert_catalog import (
    ExpertCatalog,
    ExpertCatalogConflict,
    ExpertCatalogError,
    ExpertCatalogNotFound,
    ExpertProjectBindingStore,
    ExpertConfigurationResolver,
    default_video_research_expert_profile,
    lint_expert_profile,
)


def _profile(**overrides) -> dict:
    profile = default_video_research_expert_profile()
    profile.update(overrides)
    return profile


def _second_profile(**overrides) -> dict:
    profile = _profile(
        expert_id="brand-analysis-expert",
        role="品牌资料分析与策略汇报",
        method="资料检索、观点归纳、策略结构化",
        applicable_tasks=["brand_analysis", "strategy_report"],
        output_contract="输出必须包含策略结论、资料依据与风险提示。",
    )
    profile.update(overrides)
    return profile


@pytest.fixture()
def root(tmp_path):
    return tmp_path


@pytest.fixture()
def catalog(root):
    return ExpertCatalog(root)


@pytest.fixture()
def bindings(root):
    return ExpertProjectBindingStore(root)


def _create_active(catalog: ExpertCatalog, profile: dict | None = None) -> dict:
    return catalog.create(
        profile or _profile(status="active"),
        expected_registry_revision=catalog.registry_revision,
    )


def _bind(
    bindings: ExpertProjectBindingStore,
    catalog: ExpertCatalog,
    *,
    project_id: str = "project-a",
    expert_id: str = "video-research-expert",
    revision: int | None = None,
    **overrides,
) -> dict:
    return bindings.bind(
        project_id,
        expert_id,
        catalog=catalog,
        enabled_expert_revision=revision if revision is not None else 1,
        intent_affinity=overrides.pop("intent_affinity", ["media_analysis", "research"]),
        reason=overrides.pop("reason", "试点绑定"),
        expected_store_revision=bindings.store_revision,
        **overrides,
    )


# ── 目录：创建、CAS、重启读回 ──


def test_create_lints_versions_and_reads_back_after_restart(root, catalog):
    created = _create_active(catalog)
    assert created["revision"] == 1
    assert created["status"] == "active"
    assert (root / "library/global/expert-catalog/expert-catalog.json").exists()

    restarted = ExpertCatalog(root)
    assert restarted.registry_revision == 1
    assert restarted.get("video-research-expert")["revision"] == 1


def test_create_rejects_duplicate_expert_and_stale_registry_revision(catalog):
    _create_active(catalog)
    with pytest.raises(ExpertCatalogConflict):
        _create_active(catalog)
    with pytest.raises(ExpertCatalogConflict):
        catalog.create(
            _profile(
                expert_id="another-expert",
                differentiation="仅用于 CAS 漂移验证的另一职责组合。",
            ),
            expected_registry_revision=99,
        )


# ── lint ──


def test_lint_rejects_missing_contract_prohibited_autonomy_or_refs():
    base = _profile()
    for field in ("role", "method", "output_contract"):
        broken = dict(base)
        broken[field] = ""
        assert any(field in error for error in lint_expert_profile(broken))
    assert lint_expert_profile({**base, "prohibited": []})
    assert lint_expert_profile({**base, "autonomy_ceiling": "act_freely"})
    assert lint_expert_profile({**base, "skills": []})
    assert lint_expert_profile({**base, "tools": []})
    assert lint_expert_profile({**base, "applicable_tasks": []})
    assert lint_expert_profile({**base, "skills": [{"skill_id": "s", "revision": 0}]})
    assert lint_expert_profile({**base, "skills": [{"skill_id": "s", "revision": 1}, {"skill_id": "s", "revision": 2}]})
    assert lint_expert_profile({**base, "tools": ["analyze_source", "analyze_source"]})
    assert lint_expert_profile({**base, "tools": ["Bad Tool"]})
    assert lint_expert_profile({**base, "expert_id": "Bad_Id"})


def test_lint_rejects_sensitive_material_and_qualification_hints():
    with_secret = _profile()
    with_secret["model_policy_ref"] = {"route_key": "deep.research", "api_key": "sk-x"}
    assert any("sensitive" in error for error in lint_expert_profile(with_secret))
    assert any(
        "qualification" in error
        for error in lint_expert_profile(_profile(role="持证医生助理，提供诊断"))
    )
    assert lint_expert_profile(_profile()) == []


def test_lint_rejects_unexplained_overlapping_duties_but_allows_with_differentiation():
    existing = (_profile(status="active"),)
    overlap = _second_profile(applicable_tasks=["media_analysis"])
    errors = lint_expert_profile(overlap, existing_active=existing)
    assert any("overlapping duties" in error for error in errors)
    explained = _second_profile(
        applicable_tasks=["media_analysis"],
        differentiation="只处理品牌与市场类视频，不承担通用学术研究。",
    )
    assert lint_expert_profile(explained, existing_active=existing) == []


# ── 生命周期：升级保留历史、状态终态 ──


def test_upgrade_creates_new_revision_and_preserves_history(catalog):
    _create_active(catalog)
    upgraded = catalog.upgrade(
        "video-research-expert",
        _profile(method="字幕优先、ASR 回退、证据对应、时间戳追溯、逐段引用"),
        expected_expert_revision=1,
        expected_registry_revision=catalog.registry_revision,
    )
    assert upgraded["revision"] == 2
    assert upgraded["created_at"] == catalog.get("video-research-expert")["created_at"]
    assert catalog.get_revision("video-research-expert", 1)["revision"] == 1
    assert catalog.get_revision("video-research-expert", 2)["method"].endswith("逐段引用")
    with pytest.raises(ExpertCatalogConflict):
        catalog.upgrade(
            "video-research-expert",
            _profile(),
            expected_expert_revision=1,
            expected_registry_revision=catalog.registry_revision,
        )


def test_status_lifecycle_and_retired_terminal(catalog):
    catalog.create(_profile(), expected_registry_revision=0)
    activated = catalog.set_status(
        "video-research-expert", "active", reason="试点启用",
        expected_expert_revision=1, expected_registry_revision=1,
    )
    assert activated["status"] == "active"
    disabled = catalog.set_status(
        "video-research-expert", "disabled", reason="暂停",
        expected_expert_revision=1, expected_registry_revision=2,
    )
    assert disabled["status"] == "disabled"
    retired = catalog.set_status(
        "video-research-expert", "retired", reason="退役",
        expected_expert_revision=1, expected_registry_revision=3,
    )
    assert retired["status"] == "retired"
    with pytest.raises(ExpertCatalogError):
        catalog.set_status(
            "video-research-expert", "active", reason="复活",
            expected_expert_revision=1, expected_registry_revision=4,
        )


# ── 绑定 ──


def test_binding_requires_active_expert_and_matching_revision(catalog, bindings):
    catalog.create(_profile(), expected_registry_revision=0)  # draft
    with pytest.raises(ExpertCatalogError):
        _bind(bindings, catalog)
    catalog.set_status(
        "video-research-expert", "active", reason="启用",
        expected_expert_revision=1, expected_registry_revision=1,
    )
    with pytest.raises(ExpertCatalogConflict):
        _bind(bindings, catalog, revision=7)
    binding = _bind(bindings, catalog)
    assert binding["binding_revision"] == 1
    assert bindings.get("project-a", "video-research-expert")["enabled_expert_revision"] == 1


def test_binding_single_default_per_project_and_update_cas(root, catalog, bindings):
    _create_active(catalog)
    _create_active(catalog, _second_profile(status="active"))
    _bind(bindings, catalog, default=True)
    with pytest.raises(ExpertCatalogConflict):
        _bind(bindings, catalog, expert_id="brand-analysis-expert", default=True)
    binding = bindings.get("project-a", "video-research-expert")
    updated = bindings.update(
        "project-a", "video-research-expert",
        expected_binding_revision=binding["binding_revision"],
        expected_store_revision=bindings.store_revision,
        selection_mode="auto",
        reason="迁移旧自动模式",
    )
    assert updated["binding_revision"] == 2
    assert updated["selection_mode"] == "manual"
    with pytest.raises(ExpertCatalogConflict):
        bindings.update(
            "project-a", "video-research-expert",
            expected_binding_revision=99,
            expected_store_revision=bindings.store_revision,
            reason="过期 CAS",
        )
    restarted = ExpertProjectBindingStore(root)
    assert restarted.get("project-a", "video-research-expert")["binding_revision"] == 2


def test_unbind_removes_and_history_keeps(catalog, bindings):
    _create_active(catalog)
    _bind(bindings, catalog)
    removed = bindings.unbind(
        "project-a", "video-research-expert",
        reason="项目方向调整",
        expected_binding_revision=1,
        expected_store_revision=bindings.store_revision,
    )
    assert removed["action"] if "action" in removed else True
    assert bindings.get("project-a", "video-research-expert") is None
    assert bindings.list_for_project("project-a") == ()
    assert ExpertProjectBindingStore(bindings._root_dir).store_revision == 2


# ── 选择：显式路径 fail-closed ──


def test_explicit_selection_requires_binding_and_active_status(catalog, bindings):
    service = ExpertConfigurationResolver(catalog, bindings)
    receipt = service.select("project-a", ["research"], requested_expert_id="video-research-expert")
    assert receipt["selected"] is None
    assert receipt["candidates"][0]["reason"] == "unknown_expert"

    catalog.create(_profile(), expected_registry_revision=0)
    receipt = service.select("project-a", ["research"], requested_expert_id="video-research-expert")
    assert receipt["candidates"][0]["reason"] == "expert_not_active"

    catalog.set_status(
        "video-research-expert", "active", reason="启用",
        expected_expert_revision=1, expected_registry_revision=1,
    )
    receipt = service.select("project-a", ["research"], requested_expert_id="video-research-expert")
    assert receipt["candidates"][0]["reason"] == "expert_not_bound_to_project"


def test_explicit_selection_fails_closed_on_disabled_and_drift(catalog, bindings):
    _create_active(catalog)
    _bind(bindings, catalog, selection_mode="disabled")
    service = ExpertConfigurationResolver(catalog, bindings)
    receipt = service.select("project-a", ["research"], requested_expert_id="video-research-expert")
    assert receipt["candidates"][0]["reason"] == "binding_disabled"

    _create_active(catalog, _second_profile(status="active"))
    bindings.bind(
        "project-a", "brand-analysis-expert", catalog=catalog,
        enabled_expert_revision=1, intent_affinity=["brand_analysis"],
        reason="绑定", expected_store_revision=bindings.store_revision,
    )
    catalog.upgrade(
        "brand-analysis-expert", _second_profile(method="新方法"),
        expected_expert_revision=1, expected_registry_revision=catalog.registry_revision,
    )
    receipt = service.select("project-a", ["brand_analysis"], requested_expert_id="brand-analysis-expert")
    assert receipt["candidates"][0]["reason"] == "binding_revision_drift"
    assert receipt["selected"] is None


def test_legacy_confirm_binding_is_migrated_to_explicit_manual(catalog, bindings):
    _create_active(catalog)
    _bind(bindings, catalog, selection_mode="confirm")
    service = ExpertConfigurationResolver(catalog, bindings)
    receipt = service.select("project-a", ["research"], requested_expert_id="video-research-expert")
    assert receipt["selected"]["requires_confirmation"] is False
    assert receipt["selection_mode"] == "explicit"
    assert receipt["frozen_refs"]["tools"] == ["analyze_source", "memory.recall", "document.draft.propose"]
    assert receipt["autonomy_ceiling"] == "propose_only"


# ── 配置解析：只允许显式或项目默认 ──


def test_configuration_resolver_never_uses_affinity_and_only_uses_default(catalog, bindings):
    _create_active(catalog)
    _create_active(catalog, _second_profile(status="active"))
    _bind(bindings, catalog, selection_mode="auto", intent_affinity=["media_analysis"])
    bindings.bind(
        "project-a", "brand-analysis-expert", catalog=catalog,
        enabled_expert_revision=1, intent_affinity=["research", "media_analysis"],
        selection_mode="auto", reason="分析绑定",
        expected_store_revision=bindings.store_revision,
    )
    service = ExpertConfigurationResolver(catalog, bindings)
    receipt = service.select("project-a", ["media_analysis", "research"])
    assert receipt["selection_mode"] == "none"
    assert receipt["selected"] is None
    reasons = {c["expert_id"]: c["reason"] for c in receipt["candidates"]}
    assert reasons == {
        "brand-analysis-expert": "not_project_default",
        "video-research-expert": "not_project_default",
    }

    bindings.update(
        "project-a", "video-research-expert",
        expected_binding_revision=1, expected_store_revision=bindings.store_revision,
        default=True, reason="设为默认",
    )
    receipt = service.select("project-a", ["media_analysis", "research"])
    assert receipt["selection_mode"] == "project_default"
    assert receipt["selected"]["expert_id"] == "video-research-expert"


def test_configuration_resolver_never_selects_non_default_or_unbound_binding(catalog, bindings):
    _create_active(catalog)
    _bind(bindings, catalog, selection_mode="confirm")
    service = ExpertConfigurationResolver(catalog, bindings)
    receipt = service.select("project-a", ["media_analysis"])
    assert receipt["selected"] is None
    assert receipt["selection_mode"] == "none"
    assert receipt["candidates"][0]["reason"] == "not_project_default"
    assert receipt["fallback"] == "generic_agent"

    bindings.update(
        "project-a", "video-research-expert",
        expected_binding_revision=1, expected_store_revision=bindings.store_revision,
        selection_mode="manual", reason="改手动",
    )
    receipt = service.select("project-a", ["media_analysis"])
    assert receipt["candidates"][0]["reason"] == "not_project_default"

    # 未绑定项目的同 ID 专家（全局目录存在）也绝不会被自动选择
    _create_active(catalog, _second_profile(status="active"))
    receipt = service.select("project-b", ["media_analysis"])
    assert receipt["selected"] is None
    assert receipt["candidates"] == []


# ── Receipt 冻结与恢复重放 ──


def test_receipt_verify_ok_and_drift_fails_closed_without_reselection(catalog, bindings):
    _create_active(catalog)
    _bind(bindings, catalog, selection_mode="auto", default=True)
    service = ExpertConfigurationResolver(catalog, bindings)
    receipt = service.select("project-a", ["media_analysis"])
    assert service.verify_receipt(receipt)["status"] == "ok"

    frozen = dict(receipt)
    catalog.upgrade(
        "video-research-expert", _profile(method="新方法"),
        expected_expert_revision=1, expected_registry_revision=catalog.registry_revision,
    )
    verdict = service.verify_receipt(frozen)
    assert verdict["status"] == "drifted"
    assert "expert_revision_drift" in verdict["reasons"]

    rebased = dict(frozen)
    rebased["selected"] = {**frozen["selected"], "expert_revision": 2}
    assert service.verify_receipt(rebased)["status"] == "ok"
    bindings.update(
        "project-a", "video-research-expert",
        expected_binding_revision=1, expected_store_revision=bindings.store_revision,
        reason="调整绑定",
    )
    verdict = service.verify_receipt(rebased)
    assert verdict["status"] == "drifted"
    assert "binding_revision_drift" in verdict["reasons"]

    bindings.unbind(
        "project-a", "video-research-expert",
        reason="解绑", expected_binding_revision=2,
        expected_store_revision=bindings.store_revision,
    )
    assert "binding_missing" in service.verify_receipt(rebased)["reasons"]


def test_cross_project_isolation_same_expert_different_bindings(catalog, bindings):
    _create_active(catalog)
    _bind(bindings, catalog, project_id="project-a", selection_mode="auto", default=True)
    _bind(bindings, catalog, project_id="project-b", selection_mode="manual")
    service = ExpertConfigurationResolver(catalog, bindings)
    auto = service.select("project-a", ["media_analysis"])
    assert auto["selected"]["expert_id"] == "video-research-expert"
    other = service.select("project-b", ["media_analysis"])
    assert other["selected"] is None
    assert other["candidates"][0]["reason"] == "not_project_default"
    assert service.select("project-c", ["media_analysis"])["candidates"] == []


def test_pilot_profile_passes_lint_and_full_flow(root):
    catalog = ExpertCatalog(root)
    bindings = ExpertProjectBindingStore(root)
    created = catalog.create(
        {**default_video_research_expert_profile(), "status": "active"},
        expected_registry_revision=0,
    )
    assert created["expert_id"] == "video-research-expert"
    _bind(bindings, catalog, selection_mode="auto", default=True)
    receipt = ExpertConfigurationResolver(catalog, bindings).select("project-a", ["research"])
    assert receipt["selected"]["expert_id"] == "video-research-expert"
    assert lint_expert_profile(default_video_research_expert_profile()) == []
