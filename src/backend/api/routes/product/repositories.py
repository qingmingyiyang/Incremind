"""Repositories ownership for the product API."""
from __future__ import annotations

from pathlib import Path

from backend.api.job_runtime import build_rebuild_job_repository as _job_repository
from backend.api.rebuild_authority_runtime import build_rebuild_document_repository_resolution
from backend.api.rebuild_storage_runtime import (
    resolve_rebuild_repository_root as _resolve_rebuild_repository_root,
    build_rebuild_object_store,
)

from core.aggregate_repository_factory import AggregateRepositoryFactory
from core.memory_core import ObjectStoreMemoryStore, SQLiteMemoryReader
from core.product_core.memory_projection_authority import CurrentMemoryProjectionAuthority
from core.product_core.memory_projection_repository import ObjectStoreMemoryProjectionRepository
from core.storage_provider import JsonObjectStore, RebuildStorageSettings


def resolve_rebuild_repository_root(route_file: Path) -> Path:
    """Compatibility export for callers that still import the legacy route module."""

    return _resolve_rebuild_repository_root(route_file)


REPOSITORY_ROOT = resolve_rebuild_repository_root(Path(__file__))


def _object_store(
    runtime_root: Path,
):
    """Compatibility export for tests and routes not migrated from this module."""

    return build_rebuild_object_store(runtime_root, repository_root=REPOSITORY_ROOT)


def _document_repository(
    runtime_root: Path,
    store: JsonObjectStore,
    settings: RebuildStorageSettings,
):
    return AggregateRepositoryFactory(
        runtime_root=runtime_root,
        namespace_id=settings.namespace_id,
        json_store=store,
    ).document_repository()


def _document_repository_resolution(
    runtime_root: Path,
    store: JsonObjectStore,
    settings: RebuildStorageSettings,
):
    return build_rebuild_document_repository_resolution(runtime_root, store, settings)


def _project_skill_repository(
    runtime_root: Path,
    store: JsonObjectStore,
    settings: RebuildStorageSettings,
):
    return AggregateRepositoryFactory(
        runtime_root=runtime_root,
        namespace_id=settings.namespace_id,
        json_store=store,
    ).project_skill_repository()


def _memory_projection_runtime(container):
    store, settings = _object_store(container.root_dir)
    factory = AggregateRepositoryFactory(
        runtime_root=container.root_dir,
        namespace_id=settings.namespace_id,
        json_store=store,
    )
    memory_resolution = factory.memory_publication_authority_resolution()
    skill_resolution = factory.project_skill_repository_resolution()
    memory = (
        SQLiteMemoryReader(memory_resolution.records)
        if memory_resolution.records is not None
        else ObjectStoreMemoryStore(store)
    )
    authority = CurrentMemoryProjectionAuthority(
        memory=memory,
        project_skills=skill_resolution.repository,
        memory_authority_identity=memory_resolution.authority_identity,
        project_skill_authority_identity=skill_resolution.authority_identity,
    )
    return (
        store,
        settings,
        authority,
        ObjectStoreMemoryProjectionRepository(store),
        _job_repository(container.root_dir, store),
    )
