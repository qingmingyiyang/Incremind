from __future__ import annotations

from pathlib import Path

from core.aggregate_repository_factory import AggregateRepositoryFactory
from core.memory_core import ObjectStoreMemoryStore, SQLiteMemoryReader
from core.product_core.related_memory import RelatedMemoryService
from core.product_core.library_overview_search import SearchLibraryOverview
from backend.api.library_overview_runtime import build_library_overview_reader
from core.search_and_recall import (
    LibrarySearchService,
    ObjectStoreRecallIndex,
    build_recall_authority_ledger,
    build_recall_entries_from_authorities,
)
from core.storage_provider import RebuildStorageSettings


def load_current_recall_entries(runtime_root: Path, store: object):
    """Load Recall entries from the currently resolved Memory and Skill authorities."""

    factory = AggregateRepositoryFactory(
        runtime_root=Path(runtime_root),
        namespace_id=store.namespace_id,
        json_store=store,
    )
    memory_resolution = factory.memory_publication_authority_resolution()
    memory = (
        SQLiteMemoryReader(memory_resolution.records)
        if memory_resolution.records is not None
        else ObjectStoreMemoryStore(store)
    )
    return build_recall_entries_from_authorities(
        store,
        memory=memory,
        project_skills=factory.project_skill_repository(),
    )


def build_library_search_service(
    runtime_root: Path,
    store: object,
) -> LibrarySearchService:
    """Compose Library search without giving the HTTP adapter storage authority."""

    recall_index = ObjectStoreRecallIndex(store)
    current_entries = load_current_recall_entries(runtime_root, store)
    return LibrarySearchService(
        recall_index=recall_index,
        active_manifest=recall_index.manifest(),
        source_ledger=build_recall_authority_ledger(current_entries),
        current_entries=current_entries,
    )


def build_library_overview_search(runtime_root: Path, store: object, settings: RebuildStorageSettings) -> SearchLibraryOverview:
    """Search the same visible Library items served by the Overview endpoint."""

    return SearchLibraryOverview(
        build_library_overview_reader(runtime_root, store, settings),
        namespace_id=settings.namespace_id,
        read_record=store.read,
    )


def build_related_memory_service(
    runtime_root: Path,
    store: object,
    settings: RebuildStorageSettings,
) -> RelatedMemoryService:
    """Compose related-memory reads from the active Memory and Skill authorities."""

    factory = AggregateRepositoryFactory(
        runtime_root=Path(runtime_root),
        namespace_id=settings.namespace_id,
        json_store=store,
    )
    memory_resolution = factory.memory_publication_authority_resolution()
    memory = (
        SQLiteMemoryReader(memory_resolution.records)
        if memory_resolution.records is not None
        else ObjectStoreMemoryStore(store)
    )
    return RelatedMemoryService(
        store,
        memory=memory,
        skills=factory.project_skill_repository(),
    )
