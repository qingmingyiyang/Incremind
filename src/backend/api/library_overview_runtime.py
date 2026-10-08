from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from backend.api.job_runtime import build_rebuild_job_repository
from backend.shared.document_visibility import LegacyDocumentVisibility
from core.aggregate_repository_factory import AggregateRepositoryFactory
from core.composition import ObjectStoreLibraryOverviewReader
from core.memory_core import ObjectStoreMemoryStore, SQLiteMemoryReader
from core.storage_provider import RebuildStorageSettings
from core.storage_provider import SQLiteStructuredRecordStore


class _VisibleDocumentRepository:
    def __init__(self, repository: object) -> None:
        self._repository = repository

    def list(self, *, include_archived: bool = False):
        visibility = LegacyDocumentVisibility.from_repository(self._repository)
        return tuple(
            document for document in self._repository.list(include_archived=include_archived)
            if visibility.allows(document)
        )


class _VisibleLibraryReader:
    def __init__(self, reader: ObjectStoreLibraryOverviewReader, runtime_root: Path) -> None:
        self._reader = reader
        self._records = SQLiteStructuredRecordStore(runtime_root / ".rebuild-data" / "structured-records.sqlite3")

    def __getattr__(self, name: str):
        return getattr(self._reader, name)

    def sources(self):
        items = self._reader.sources()
        return tuple(self._with_workspace_media(item) for item in items if self._source_visible(item))

    def _with_workspace_media(self, source: Mapping[str, object]) -> Mapping[str, object]:
        if source.get("identity_method") != "workspace_confirmation":
            return source
        item_id = source.get("workspace_item_id")
        if not isinstance(item_id, str) or not item_id:
            return source
        row = self._records.read("workspace_items", item_id)
        if (row is None or row.payload.get("status") != "confirmed"
                or row.payload.get("project_id") != source.get("project_id")):
            return source
        metadata = source.get("metadata")
        updated = dict(metadata) if isinstance(metadata, Mapping) else {}
        for field in ("content_kind", "platform"):
            if not updated.get(field) and row.payload.get(field):
                updated[field] = row.payload[field]
        return {**source, "metadata": updated}

    def _source_visible(self, source: Mapping[str, object]) -> bool:
        source_id = source.get("id")
        if isinstance(source_id, str):
            intent = self._records.read("workspace_review_intents", "review-" + source_id)
            if intent is not None and intent.payload.get("state") != "confirmed":
                return False
        if source.get("identity_method") != "workspace_confirmation":
            return True
        operation_id = source.get("confirmation_operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            return False
        operation = self._records.read("workspace_confirmation_operations", operation_id)
        return operation is not None and operation.payload.get("state") == "committed"


def build_library_overview_reader(
    runtime_root: Path,
    store: object,
    settings: RebuildStorageSettings,
) -> _VisibleLibraryReader:
    """Compose Library projections from the currently resolved authorities."""

    runtime_root = Path(runtime_root)
    factory = AggregateRepositoryFactory(
        runtime_root=runtime_root,
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
    reader = ObjectStoreLibraryOverviewReader(
        store,
        document_repository=_VisibleDocumentRepository(factory.document_repository()),
        job_repository=build_rebuild_job_repository(runtime_root, store),
        memory_repository=memory,
        project_skill_repository=skill_resolution.repository,
    )
    return _VisibleLibraryReader(reader, runtime_root)
