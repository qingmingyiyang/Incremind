"""Route-independent rebuild aggregate authority composition."""
from __future__ import annotations

from pathlib import Path

from core.aggregate_repository_factory import AggregateRepositoryFactory
from core.storage_provider import JsonObjectStore, RebuildStorageSettings


def build_rebuild_document_repository_resolution(
    runtime_root: Path,
    store: JsonObjectStore,
    settings: RebuildStorageSettings,
):
    """Resolve the active document authority for routes and startup recovery."""

    return AggregateRepositoryFactory(
        runtime_root=runtime_root,
        namespace_id=settings.namespace_id,
        json_store=store,
    ).document_repository_resolution()
