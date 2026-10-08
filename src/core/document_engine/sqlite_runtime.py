from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from core.storage_provider import (
    ObjectStoreRevisionError,
    SQLiteStructuredRecordStore,
    SQLiteStructuredRecordUnitOfWork,
    SQLiteUnitOfWorkConflict,
)

from .ports import DocumentDraft
from .retrieval_index import project_document
from .runtime import (
    DocumentRepositoryError,
    ObjectStoreDocumentRepository,
)


@dataclass(slots=True)
class SQLiteDocumentRepository:
    """Atomically persists one Document revision in a SQLite UoW.

    The adapter is explicit and is not part of application composition. It
    reuses the existing Document domain rules while making the current object,
    revision record and Markdown payload visible in one commit.
    """

    records: SQLiteStructuredRecordStore
    namespace_id: str = "default"
    now: str = "2026-06-29T18:00:00+08:00"

    def create(self, draft: DocumentDraft) -> Mapping[str, object]:
        return self._mutate("create", draft)

    def create_or_replay_generated(self, draft: DocumentDraft) -> Mapping[str, object]:
        return self._mutate("create_or_replay_generated", draft)

    def create_or_replay_generated_in_uow(
        self, draft: DocumentDraft, uow: SQLiteStructuredRecordUnitOfWork,
    ) -> Mapping[str, object]:
        """Enlist a generated Document in an existing structured-record UoW.

        The caller commits the UoW together with its own status records. This
        keeps the Document, revision and Markdown atomic with the workflow
        publication point.
        """
        snapshot = {
            collection: {row.object_id: dict(row.payload) for row in uow.list(collection)}
            for collection in ("documents", "document_revisions", "document_markdown")
        }
        repository = ObjectStoreDocumentRepository(
            _SQLiteTransactionObjectStore(uow, snapshot),
            namespace_id=self.namespace_id,
            now=self.now,
        )
        try:
            return repository.create_or_replay_generated(draft)
        except ObjectStoreRevisionError as exc:
            raise DocumentRepositoryError(f"document persistence conflict: {exc}") from exc

    def read(self, document_id: str) -> Mapping[str, object] | None:
        return self._reader().read(document_id)

    def list(self, *, include_archived: bool = False) -> tuple[Mapping[str, object], ...]:
        return self._reader().list(include_archived=include_archived)

    def save(
        self,
        document_id: str,
        markdown: str,
        source_refs: Sequence[Mapping[str, object]],
        expected_revision: int,
    ) -> Mapping[str, object]:
        return self.save_user_edit(
            document_id,
            markdown=markdown,
            source_refs=source_refs,
            expected_revision=expected_revision,
        )

    def save_user_edit(
        self,
        document_id: str,
        *,
        markdown: str,
        expected_revision: int,
        title: str | None = None,
        reason: str = "user edited document markdown",
        source_refs: Sequence[Mapping[str, object]] | None = None,
    ) -> Mapping[str, object]:
        return self._mutate(
            "save_user_edit",
            document_id,
            markdown=markdown,
            expected_revision=expected_revision,
            title=title,
            reason=reason,
            source_refs=source_refs,
        )

    def apply_ai_patch(
        self,
        document_id: str,
        *,
        blocks: Sequence[Mapping[str, object]],
        expected_revision: int,
        reason: str = "AI patch document blocks",
        source_refs: Sequence[Mapping[str, object]] | None = None,
    ) -> Mapping[str, object]:
        return self._mutate(
            "apply_ai_patch",
            document_id,
            blocks=blocks,
            expected_revision=expected_revision,
            reason=reason,
            source_refs=source_refs,
        )

    def archive(self, document_id: str, *, expected_revision: int) -> Mapping[str, object]:
        return self._mutate("archive", document_id, expected_revision=expected_revision)

    def restore(self, document_id: str, *, expected_revision: int) -> Mapping[str, object]:
        return self._mutate("restore", document_id, expected_revision=expected_revision)

    def revision(self, document_id: str, revision: int) -> Mapping[str, object] | None:
        return self._reader().revision(document_id, revision)

    def revisions(self, document_id: str) -> tuple[Mapping[str, object], ...]:
        return self._reader().revisions(document_id)

    def markdown(self, document_id: str, *, revision: int | None = None) -> str | None:
        return self._reader().markdown(document_id, revision=revision)

    def _reader(self) -> ObjectStoreDocumentRepository:
        return ObjectStoreDocumentRepository(
            _SQLiteRecordObjectStore(self.records),
            namespace_id=self.namespace_id,
            now=self.now,
        )

    def _mutate(self, method_name: str, *args, **kwargs) -> Mapping[str, object]:
        snapshot = _document_snapshot(self.records)
        with self.records.begin() as uow:
            repository = ObjectStoreDocumentRepository(
                _SQLiteTransactionObjectStore(uow, snapshot),
                namespace_id=self.namespace_id,
                now=self.now,
            )
            try:
                result = getattr(repository, method_name)(*args, **kwargs)
                uow.commit()
            except ObjectStoreRevisionError as exc:
                raise DocumentRepositoryError(f"document persistence conflict: {exc}") from exc
        return result


@dataclass(slots=True)
class _SQLiteRecordObjectStore:
    records: SQLiteStructuredRecordStore

    def read(self, collection: str, object_id: str) -> Mapping[str, object] | None:
        record = self.records.read(collection, object_id)
        return dict(record.payload) if record is not None else None

    def list(self, collection: str) -> Sequence[Mapping[str, object]]:
        return tuple(dict(record.payload) for record in self.records.list(collection))

    def delete(self, collection: str, object_id: str) -> bool:
        raise DocumentRepositoryError("SQLite Document adapter does not support delete")

    def write(
        self,
        collection: str,
        object_id: str,
        payload: Mapping[str, object],
        expected_revision: int | None,
    ) -> int:
        raise DocumentRepositoryError("read-only SQLite Document view cannot write")


@dataclass(slots=True)
class _SQLiteTransactionObjectStore:
    uow: SQLiteStructuredRecordUnitOfWork
    snapshot: Mapping[str, Mapping[str, Mapping[str, object]]]
    _staged: dict[tuple[str, str], Mapping[str, object]] = field(
        init=False,
        default_factory=dict,
    )

    def read(self, collection: str, object_id: str) -> Mapping[str, object] | None:
        staged = self._staged.get((collection, object_id))
        if staged is not None:
            return dict(staged)
        payload = self.snapshot.get(collection, {}).get(object_id)
        return dict(payload) if payload is not None else None

    def list(self, collection: str) -> Sequence[Mapping[str, object]]:
        values = {
            object_id: dict(payload)
            for object_id, payload in self.snapshot.get(collection, {}).items()
        }
        for (staged_collection, object_id), payload in self._staged.items():
            if staged_collection == collection:
                values[object_id] = dict(payload)
        return tuple(values[object_id] for object_id in sorted(values))

    def delete(self, collection: str, object_id: str) -> bool:
        raise DocumentRepositoryError("SQLite Document adapter does not support delete")

    def write(
        self,
        collection: str,
        object_id: str,
        payload: Mapping[str, object],
        expected_revision: int | None,
    ) -> int:
        if expected_revision is None:
            raise DocumentRepositoryError("SQLite Document writes require expected_revision")
        try:
            record = self.uow.put(
                collection,
                object_id,
                payload,
                expected_revision=expected_revision,
            )
        except SQLiteUnitOfWorkConflict as exc:
            raise ObjectStoreRevisionError(str(exc)) from exc
        self._staged[(collection, object_id)] = dict(record.payload)
        if collection == 'document_markdown':
            document = self.read('documents', str(payload['document_id']))
            if document is not None and document['revision'] == payload['revision']:
                project_document(self.uow, document, str(payload['markdown']))
        return record.revision


def _document_snapshot(
    records: SQLiteStructuredRecordStore,
) -> dict[str, dict[str, Mapping[str, object]]]:
    snapshot: dict[str, dict[str, Mapping[str, object]]] = {}
    for collection in ("documents", "document_revisions", "document_markdown"):
        snapshot[collection] = {
            record.object_id: dict(record.payload)
            for record in records.list(collection)
        }
    return snapshot
