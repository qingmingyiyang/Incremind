from __future__ import annotations

from pathlib import Path

import pytest

from core.application_skill import (
    ApplicationSkillBindingRegistry,
    ApplicationSkillCatalog,
    ApplicationSkillConsumerRuntime,
    ApplicationSkillImportService,
    ApplicationSkillManagementConflict,
    ApplicationSkillManagementService,
    ApplicationSkillResolutionError,
    ApplicationSkillResolver,
    ApplicationSkillSource,
    ObjectStoreApplicationSkillTraceRepository,
)
from core.storage_provider import JsonObjectStore


def _write_skill(root: Path, skill_id: str, marker: str = "METHOD-BODY") -> Path:
    package = root / skill_id
    package.mkdir(parents=True, exist_ok=True)
    (package / "SKILL.md").write_text(
        "---\n"
        f"name: {skill_id}\n"
        f"description: Use {skill_id} for project document review.\n"
        "---\n\n"
        f"# {marker}\n",
        encoding="utf-8",
    )
    return package


def _write_governed_skill(
    root: Path, skill_id: str, *, trigger_boundary: str, validation: str,
) -> Path:
    package = root / skill_id
    package.mkdir(parents=True, exist_ok=True)
    (package / "SKILL.md").write_text(
        "---\n"
        f"name: {skill_id}\n"
        f"description: Use {skill_id} for governed project work.\n"
        f"trigger_boundary: {trigger_boundary}\n"
        f"validation: {validation}\n"
        "maturity: verified\n"
        "---\n\n# Governed method\n",
        encoding="utf-8",
    )
    return package


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / "runtime" / ".rebuild-data", legacy_root=tmp_path / "runtime" / "library")


def _management(tmp_path: Path, store: JsonObjectStore) -> ApplicationSkillManagementService:
    catalog = ApplicationSkillCatalog()
    sources = (ApplicationSkillSource("user", tmp_path / "runtime" / "skills", "user"),)
    bindings = ApplicationSkillBindingRegistry(store, now="2026-07-18T17:00:00+00:00")
    return ApplicationSkillManagementService(
        catalog=catalog,
        sources=sources,
        bindings=bindings,
        resolver=ApplicationSkillResolver(bindings, now="2026-07-18T17:00:00+00:00"),
        store=store,
    )


def test_import_preview_is_zero_write_and_confirm_copies_only_selected_package(
    tmp_path: Path,
) -> None:
    selected = _write_skill(tmp_path / "selected-parent", "document-review")
    _write_skill(tmp_path / "selected-parent", "unselected-sibling", "SIBLING-BODY")
    target = tmp_path / "runtime" / "skills"
    service = ApplicationSkillImportService(ApplicationSkillCatalog(), target)

    preview = service.preview(str(selected))

    assert preview["status"] == "validated"
    assert preview["action"] == "import"
    assert preview["write_effect"] == "none"
    assert preview["package"]["skill_id"] == "document-review"
    assert not target.exists()

    imported = service.confirm(
        str(selected),
        expected_fingerprint=str(preview["package"]["fingerprint"]),
        preview_token=str(preview["preview_token"]),
        confirm=True,
        reason="用户确认导入本地方法包。",
    )

    assert imported["status"] == "imported"
    assert (target / "document-review" / "SKILL.md").is_file()
    assert not (target / "unselected-sibling").exists()
    assert "METHOD-BODY" in (target / "document-review" / "SKILL.md").read_text(encoding="utf-8")

    replay = service.preview(str(selected))
    assert replay["action"] == "already_imported"
    confirmed_replay = service.confirm(
        str(selected),
        expected_fingerprint=str(replay["package"]["fingerprint"]),
        preview_token=str(replay["preview_token"]),
        confirm=True,
        reason="确认同一包已导入。",
    )
    assert confirmed_replay["status"] == "already_imported"
    assert confirmed_replay["replayed"] is True


def test_skill_governance_metadata_is_frozen_and_deprecated_package_is_not_effective(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "runtime" / "skills" / "document-review"
    package_root.mkdir(parents=True)
    (package_root / "SKILL.md").write_text(
        "---\n"
        "name: document-review\n"
        "description: Review project documents.\n"
        "trigger_boundary: Only project document review; never personnel decisions.\n"
        "validation: Every conclusion must cite a supplied project source.\n"
        "maturity: deprecated\n"
        "---\n\n# Review method\n",
        encoding="utf-8",
    )
    catalog = ApplicationSkillCatalog().discover(
        (ApplicationSkillSource("user", package_root.parent, "user"),)
    )
    package = catalog.get("document-review")

    assert package is not None
    assert package.trigger_boundary == "Only project document review; never personnel decisions."
    assert package.validation == "Every conclusion must cite a supplied project source."
    assert package.maturity == "deprecated"

    store = _store(tmp_path)
    registry = ApplicationSkillBindingRegistry(store, now="2026-08-28T12:00:00+00:00")
    preview = registry.preview_bind(
        package,
        project_id="project-alpha",
        allowed_consumers=("document.generate",),
    )
    registry.activate(
        package,
        project_id="project-alpha",
        allowed_consumers=("document.generate",),
        priority=500,
        trigger_terms=(),
        expected_registry_revision=0,
        preview_token=str(preview["preview_token"]),
        confirm=True,
        reason="Verify deprecated Skill exclusion.",
    )

    assert registry.effective_bindings(
        catalog, project_id="project-alpha", consumer="document.generate"
    ) == ()
    assert registry.status(catalog)["bindings"][0]["effective_status"] == "deprecated"


def test_legacy_skill_metadata_defaults_are_safe_and_invalid_maturity_is_rejected(
    tmp_path: Path,
) -> None:
    legacy = _write_skill(tmp_path / "legacy", "document-review")
    package = ApplicationSkillCatalog().inspect_package(legacy)
    assert package.trigger_boundary == package.description
    assert package.validation == "manual-review-required"
    assert package.maturity == "draft"

    (legacy / "SKILL.md").write_text(
        "---\nname: document-review\ndescription: Review.\nmaturity: immortal\n---\n\n# Body\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="maturity must be"):
        ApplicationSkillCatalog().inspect_package(legacy)


def test_import_rejects_preview_drift_and_existing_different_target(tmp_path: Path) -> None:
    selected = _write_skill(tmp_path / "source", "document-review")
    target = tmp_path / "runtime" / "skills"
    service = ApplicationSkillImportService(ApplicationSkillCatalog(), target)
    preview = service.preview(str(selected))
    (selected / "SKILL.md").write_text(
        "---\nname: document-review\ndescription: Changed package.\n---\n\n# CHANGED\n",
        encoding="utf-8",
    )

    with pytest.raises(ApplicationSkillManagementConflict, match="fingerprint drifted"):
        service.confirm(
            str(selected),
            expected_fingerprint=str(preview["package"]["fingerprint"]),
            preview_token=str(preview["preview_token"]),
            confirm=True,
            reason="确认导入。",
        )

    _write_skill(target, "document-review", "DIFFERENT-TARGET")
    with pytest.raises(ApplicationSkillManagementConflict, match="different package"):
        service.preview(str(selected))


def test_management_status_binding_and_preview_never_return_instruction_or_path(
    tmp_path: Path,
) -> None:
    package = _write_skill(tmp_path / "runtime" / "skills", "document-review", "SECRET-METHOD-BODY")
    store = _store(tmp_path)
    service = _management(tmp_path, store)
    status = service.status()
    serialized = str(status)

    assert status["catalog"]["packages"][0]["skill_id"] == "document-review"
    assert str(package.resolve()) not in serialized
    assert "SECRET-METHOD-BODY" not in serialized

    preview = service.preview_binding(
        skill_id="document-review",
        project_id="project-alpha",
        allowed_consumers=("document.generate",),
        priority=700,
        trigger_terms=("项目文档",),
    )
    activated = service.activate_binding(
        skill_id="document-review",
        project_id="project-alpha",
        allowed_consumers=("document.generate",),
        priority=700,
        trigger_terms=("项目文档",),
        expected_registry_revision=preview["registry_revision"],
        preview_token=preview["preview_token"],
        proposal_id=preview["proposal_id"],
        confirm=True,
        reason="用户确认项目使用该方法。",
    )
    resolution = service.preview_resolution(
        project_id="project-alpha",
        consumer="document.generate",
        task_kind="project-document",
        task_text="生成项目文档。",
    )

    assert activated["status"] == "activated"
    assert resolution["selected"][0]["skill_id"] == "document-review"
    assert resolution["write_effect"] == "none"
    assert "SECRET-METHOD-BODY" not in str(resolution)
    assert tuple(store.list("application_skill_resolution_traces")) == ()

    deactivate_preview = service.preview_deactivation(
        project_id="project-alpha",
        skill_id="document-review",
        expected_registry_revision=activated["registry_revision"],
    )
    deactivated = service.deactivate_binding(
        project_id="project-alpha",
        skill_id="document-review",
        expected_registry_revision=activated["registry_revision"],
        proposal_id=deactivate_preview["proposal_id"],
        confirm=True,
        reason="用户确认停用该方法。",
    )
    assert deactivated["status"] == "deactivated"


def test_invocation_and_project_summary_are_redacted_and_project_isolated(tmp_path: Path) -> None:
    _write_skill(tmp_path / "runtime" / "skills", "document-review", "PRIVATE-METHOD-BODY")
    store = _store(tmp_path)
    service = _management(tmp_path, store)
    preview = service.preview_binding(
        skill_id="document-review",
        project_id="project-alpha",
        allowed_consumers=("document.generate",),
        priority=700,
        trigger_terms=("项目文档",),
    )
    service.activate_binding(
        skill_id="document-review",
        project_id="project-alpha",
        allowed_consumers=("document.generate",),
        priority=700,
        trigger_terms=("项目文档",),
        expected_registry_revision=preview["registry_revision"],
        preview_token=preview["preview_token"],
        proposal_id=preview["proposal_id"],
        confirm=True,
        reason="用户确认项目使用该方法。",
    )
    catalog = ApplicationSkillCatalog()
    source = ApplicationSkillSource("user", tmp_path / "runtime" / "skills", "user")
    bindings = ApplicationSkillBindingRegistry(store, now="2026-07-18T17:01:00+00:00")
    runtime = ApplicationSkillConsumerRuntime(
        catalog=catalog,
        sources=(source,),
        resolver=ApplicationSkillResolver(
            bindings,
            trace_store=ObjectStoreApplicationSkillTraceRepository(store),
            now="2026-07-18T17:01:00+00:00",
        ),
    )
    runtime.resolve_context(
        project_id="project-alpha",
        consumer="document.generate",
        task_kind="project-document",
        task_text="PRIVATE-TASK-TEXT 项目文档",
        invocation_id="model-request-document-1234abcd",
    )

    invocations = service.invocations("project-alpha")
    summary = service.project_summary("project-alpha")

    assert len(invocations["invocations"]) == 1
    assert "PRIVATE-TASK-TEXT" not in str(invocations)
    assert "PRIVATE-METHOD-BODY" not in str(invocations)
    assert summary["methods"][0]["name"] == "document-review"
    assert summary["methods"][0]["last_used_at"] == "2026-07-18T17:01:00+00:00"
    assert service.invocations("project-beta")["invocations"] == []
    assert service.project_summary("project-beta")["methods"] == []


def test_workbench_consumer_respects_explicit_skill_whitelist_and_smaller_budgets(
    tmp_path: Path,
) -> None:
    source = tmp_path / "runtime" / "skills"
    _write_skill(source, "alpha-review", "ALPHA-ONLY-METHOD")
    _write_skill(source, "beta-review", "BETA-ONLY-METHOD")
    store = _store(tmp_path)
    catalog = ApplicationSkillCatalog().discover((ApplicationSkillSource("user", source, "user"),))
    bindings = ApplicationSkillBindingRegistry(store, now="2026-08-24T10:00:00+00:00")
    for priority, skill_id in ((800, "alpha-review"), (700, "beta-review")):
        package = catalog.get(skill_id)
        assert package is not None
        preview = bindings.preview_bind(
            package,
            project_id="project-alpha",
            allowed_consumers=("turn.workbench-question",),
            priority=priority,
            trigger_terms=("架构审计",),
        )
        bindings.activate(
            package,
            project_id="project-alpha",
            allowed_consumers=("turn.workbench-question",),
            priority=priority,
            trigger_terms=("架构审计",),
            expected_registry_revision=preview["registry_revision"],
            preview_token=preview["preview_token"],
            confirm=True,
            reason="Bind the reviewed workbench method.",
        )
    traces = ObjectStoreApplicationSkillTraceRepository(store)
    resolver = ApplicationSkillResolver(bindings, trace_store=traces, now="2026-08-24T10:00:00+00:00")

    resolution = resolver.resolve(
        catalog,
        project_id="project-alpha",
        consumer="turn.workbench-question",
        task_kind="workbench-question",
        task_text="请进行架构审计。",
        invocation_id="workbench-question-whitelist-001",
        enabled_skill_ids=("missing-skill", "beta-review"),
    )

    assert [item.match.skill_id for item in resolution.selected] == ["beta-review"]
    assert [item.skill_id for item in resolution.matched] == ["beta-review"]
    assert resolution.budget_excluded_skill_ids == ()
    assert resolution.trace["consumer"] == "turn.workbench-question"

    budgeted = resolver.resolve(
        catalog,
        project_id="project-alpha",
        consumer="turn.workbench-question",
        task_kind="workbench-question",
        task_text="请进行架构审计。",
        invocation_id="workbench-question-budget-001",
        enabled_skill_ids=("beta-review",),
        max_instruction_bytes=1,
    )

    assert budgeted.selected == ()
    assert budgeted.budget_excluded_skill_ids == ("beta-review",)
    assert budgeted.trace["selected"] == []
    assert budgeted.trace["budget_excluded_skill_ids"] == ["beta-review"]

    with pytest.raises(ApplicationSkillResolutionError, match="context exceeds its hard budget"):
        resolver.resolve(
            catalog,
            project_id="project-alpha",
            consumer="turn.workbench-question",
            task_kind="workbench-question",
            task_text="请进行架构审计。",
            invocation_id="workbench-question-context-001",
            enabled_skill_ids=("beta-review",),
            max_context_bytes=1,
        )


def test_trigger_boundary_and_validation_are_closed_runtime_gates(tmp_path: Path) -> None:
    source = tmp_path / "runtime" / "skills"
    _write_governed_skill(
        source, "evidence-review",
        trigger_boundary="task-kinds:project-document",
        validation="requires-source-context",
    )
    store = _store(tmp_path)
    catalog = ApplicationSkillCatalog().discover((ApplicationSkillSource("user", source, "user"),))
    package = catalog.get("evidence-review")
    assert package is not None
    bindings = ApplicationSkillBindingRegistry(store, now="2026-08-30T18:00:00+00:00")
    preview = bindings.preview_bind(
        package, project_id="project-alpha", allowed_consumers=("document.generate",),
        priority=700, trigger_terms=("审计",),
    )
    bindings.activate(
        package, project_id="project-alpha", allowed_consumers=("document.generate",),
        priority=700, trigger_terms=("审计",),
        expected_registry_revision=preview["registry_revision"],
        preview_token=preview["preview_token"], confirm=True, reason="Reviewed binding.",
    )
    resolver = ApplicationSkillResolver(bindings)

    wrong_kind = resolver.preview(
        catalog, project_id="project-alpha", consumer="document.generate",
        task_kind="summary", task_text="审计 crp://default/source/a",
    )
    missing_source = resolver.preview(
        catalog, project_id="project-alpha", consumer="document.generate",
        task_kind="project-document", task_text="审计项目材料",
    )
    allowed = resolver.preview(
        catalog, project_id="project-alpha", consumer="document.generate",
        task_kind="project-document", task_text="审计 crp://default/source/a",
    )

    assert wrong_kind.selected_matches == ()
    assert missing_source.selected_matches == ()
    assert [item.skill_id for item in allowed.selected_matches] == ["evidence-review"]
    assert allowed.selected_matches[0].reasons[:3] == (
        "maturity:verified",
        "boundary:task-kind:project-document",
        "validation:requires-source-context",
    )


def test_package_upgrade_drift_fails_closed_and_exact_rollback_reactivates(tmp_path: Path) -> None:
    source = tmp_path / "runtime" / "skills"
    _write_skill(source, "document-review", "VERSION-ONE")
    store = _store(tmp_path)
    catalog_service = ApplicationSkillCatalog()
    source_ref = ApplicationSkillSource("user", source, "user")
    catalog_v1 = catalog_service.discover((source_ref,))
    package_v1 = catalog_v1.get("document-review")
    assert package_v1 is not None
    bindings = ApplicationSkillBindingRegistry(store, now="2026-08-30T19:00:00+00:00")
    preview = bindings.preview_bind(
        package_v1, project_id="project-alpha", allowed_consumers=("document.generate",),
        priority=700, trigger_terms=("项目文档",),
    )
    bindings.activate(
        package_v1, project_id="project-alpha", allowed_consumers=("document.generate",),
        priority=700, trigger_terms=("项目文档",),
        expected_registry_revision=preview["registry_revision"],
        preview_token=preview["preview_token"], confirm=True, reason="Activate v1.",
    )

    _write_skill(source, "document-review", "VERSION-TWO")
    catalog_v2 = catalog_service.discover((source_ref,))
    assert bindings.status(catalog_v2)["bindings"][0]["effective_status"] == "drifted"
    assert ApplicationSkillResolver(bindings).preview(
        catalog_v2, project_id="project-alpha", consumer="document.generate",
        task_kind="project-document", task_text="生成项目文档",
    ).selected_matches == ()

    _write_skill(source, "document-review", "VERSION-ONE")
    rolled_back = catalog_service.discover((source_ref,))
    assert rolled_back.get("document-review").fingerprint == package_v1.fingerprint
    assert bindings.status(rolled_back)["bindings"][0]["effective_status"] == "active"
    assert [item.skill_id for item in ApplicationSkillResolver(bindings).preview(
        rolled_back, project_id="project-alpha", consumer="document.generate",
        task_kind="project-document", task_text="生成项目文档",
    ).selected_matches] == ["document-review"]
