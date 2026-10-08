"""Shared application composition for proposal-only Skill learning."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from backend.api.ai_runtime import get_or_build_ai_runtime, resolve_runtime_asset_root
from backend.api.application_skill_learning_runtime import ApplicationSkillLearningRuntime
from backend.api.plugin_runtime import build_plugin_skill_activation
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.application_skill import (
    ApplicationSkillCatalog,
    ApplicationSkillCatalogSnapshot,
    ApplicationSkillSource,
    SkillLearningWorkshop,
    SkillLearningWorkshopError,
)


class TurnProjectAuthority:
    def __init__(self, turn_store: object) -> None:
        self._turn_store = turn_store

    def project_id_for(self, turn_id: str) -> str:
        get_request = getattr(self._turn_store, "get_request", None)
        request = get_request(turn_id) if callable(get_request) else None
        scope = request.get("scope") if isinstance(request, Mapping) else None
        project_id = scope.get("project_id") if isinstance(scope, Mapping) else None
        if not isinstance(project_id, str) or not project_id:
            raise SkillLearningWorkshopError("Turn project authority is unavailable")
        return project_id


@dataclass(frozen=True, slots=True)
class ApplicationSkillLearningComposition:
    runtime: ApplicationSkillLearningRuntime
    project_id: str


def compose_application_skill_learning(
    request: object,
    container: object,
    *,
    turn_id: str,
    skill_id: str,
) -> ApplicationSkillLearningComposition:
    application = getattr(request, "app")
    turn_store = getattr(application.state, "ai_turn_effect_store", None)
    projects = TurnProjectAuthority(turn_store)
    project_id = projects.project_id_for(turn_id)
    snapshot = current_application_skill_snapshot(
        application,
        container,
        project_id=project_id,
        skill_id=skill_id,
    )
    store, _settings = build_rebuild_object_store(getattr(container, "root_dir"))
    return ApplicationSkillLearningComposition(
        ApplicationSkillLearningRuntime(
            workshop=SkillLearningWorkshop(store, snapshot),
            receipts=get_or_build_ai_runtime(request, container),
            projects=projects,
        ),
        project_id,
    )


def current_application_skill_snapshot(
    application: object,
    container: object,
    *,
    project_id: str,
    skill_id: str,
) -> ApplicationSkillCatalogSnapshot:
    root_dir = getattr(container, "root_dir")
    asset_root = resolve_runtime_asset_root()
    sources = (
        ApplicationSkillSource(
            "bundled", asset_root.root_dir / "config" / "application-skills", "bundled",
        ),
        ApplicationSkillSource("user", root_dir / "skills", "user"),
        *build_plugin_skill_activation(root_dir).all_active_sources(),
    )
    catalog = ApplicationSkillCatalog()
    local = catalog.discover_selected(sources, (skill_id,))
    external_runtime = getattr(application.state, "external_extension_runtime", None)
    active_packages = getattr(external_runtime, "active_packages", None)
    external = tuple(
        package
        for package in (active_packages(project_id) if callable(active_packages) else ())
        if package.skill_id == skill_id
    )
    combined = catalog.snapshot_from_packages(
        (*local.packages, *external),
        scanned_source_count=local.scanned_source_count + len(external),
    )
    if combined.get(skill_id) is None:
        raise SkillLearningWorkshopError("Application Skill package is unavailable")
    return combined
