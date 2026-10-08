"""Document templates ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from core.storage_provider import JsonObjectStore, RebuildStorageSettings

from . import repositories as product_repositories


def _style_prefix_for_source(store: "JsonObjectStore", source_id: str) -> str:
    """Build the unified style prompt prefix for a Source's project (if any).

    Returns an empty string when no Persona / ProjectSkill is published, so
    template generation can unconditionally prepend this without branching.
    """
    from core.product_core.style_profile import StyleProfileService

    project_id = None
    source = store.read("sources", source_id)
    if isinstance(source, Mapping):
        candidate = source.get("project_id")
        if isinstance(candidate, str) and candidate and candidate != "default":
            project_id = candidate
    service = StyleProfileService(object_store=store)
    profile = service.build(project_id=project_id)
    return profile.render_prompt_prefix()


def _style_prefix_for_media_output(store: "JsonObjectStore", output_id: str) -> str:
    """Build the unified style prompt prefix for a media output's source project."""
    from core.product_core.style_profile import StyleProfileService

    project_id = None
    output = store.read("media_processing_outputs", output_id)
    if isinstance(output, Mapping):
        source_id = output.get("source_id")
        if isinstance(source_id, str) and source_id:
            source = store.read("sources", source_id)
            if isinstance(source, Mapping):
                candidate = source.get("project_id")
                if isinstance(candidate, str) and candidate and candidate != "default":
                    project_id = candidate
    service = StyleProfileService(object_store=store)
    profile = service.build(project_id=project_id)
    return profile.render_prompt_prefix()


def _outline_override_for_source(
    runtime_root: Path,
    store: "JsonObjectStore",
    settings: RebuildStorageSettings,
    source_id: str,
) -> object:
    """读取 Source 所属项目的 ProjectSkill.outline（若存在），用于覆盖模板默认 outline。"""
    project_id = None
    source = store.read("sources", source_id)
    if isinstance(source, Mapping):
        candidate = source.get("project_id")
        if isinstance(candidate, str) and candidate and candidate != "default":
            project_id = candidate
    return _outline_override_for_project(runtime_root, store, settings, project_id)


def _outline_override_for_media_output(
    runtime_root: Path,
    store: "JsonObjectStore",
    settings: RebuildStorageSettings,
    output_id: str,
) -> object:
    """读取媒体输出所属项目的 ProjectSkill.outline（若存在）。"""
    project_id = None
    output = store.read("media_processing_outputs", output_id)
    if isinstance(output, Mapping):
        source_id = output.get("source_id")
        if isinstance(source_id, str) and source_id:
            source = store.read("sources", source_id)
            if isinstance(source, Mapping):
                candidate = source.get("project_id")
                if isinstance(candidate, str) and candidate and candidate != "default":
                    project_id = candidate
    return _outline_override_for_project(runtime_root, store, settings, project_id)


def _outline_override_for_project(
    runtime_root: Path,
    store: "JsonObjectStore",
    settings: RebuildStorageSettings,
    project_id: str | None,
) -> object:
    """从 ProjectSkill 读取 outline 字段；不存在则返回 None（用模板默认）。"""
    if not project_id:
        return None
    repo = product_repositories._project_skill_repository(runtime_root, store, settings)
    skill = repo.load(project_id)
    if not isinstance(skill, Mapping):
        return None
    outline = skill.get("outline")
    if isinstance(outline, list) and outline:
        return outline
    return None
