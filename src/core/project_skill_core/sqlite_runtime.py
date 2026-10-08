from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from core.storage_provider import (
    ObjectStoreRevisionError,
    SQLiteStructuredRecordStore,
    SQLiteStructuredRecordUnitOfWork,
    SQLiteUnitOfWorkConflict,
)

from .ports import ProjectSkillUpdate
from .runtime import (
    ObjectStoreProjectSkillRepository,
    ProjectSkillRepositoryError,
)


_PROJECT_SKILL_COLLECTIONS = (
    "project_skill_index",
    "project_skill_json",
    "project_skill_markdown",
    "project_skill_revisions",
    "project_skills",
)


@dataclass(slots=True)
class SQLiteProjectSkillRepository:
    """Atomically persists one Project Skill revision in a SQLite UoW."""

    records: SQLiteStructuredRecordStore
    namespace_id: str = "default"
    now: str = "2026-06-29T19:00:00+08:00"

    def generation_token(self) -> str:
        return self.records.generation_token(_PROJECT_SKILL_COLLECTIONS)

    def load(self, project_id: str) -> Mapping[str, object] | None:
        return self._reader().load(project_id)

    def get(self, skill_id: str) -> Mapping[str, object] | None:
        return self._reader().get(skill_id)

    def list_by_project(self, project_id: str) -> tuple[Mapping[str, object], ...]:
        return self._reader().list_by_project(project_id)

    def list_all(self) -> tuple[Mapping[str, object], ...]:
        return self._reader().list_all()

    def save(self, update: ProjectSkillUpdate) -> Mapping[str, object]:
        snapshot = _project_skill_snapshot(self.records)
        with self.records.begin() as uow:
            repository = ObjectStoreProjectSkillRepository(
                _SQLiteTransactionObjectStore(uow, snapshot),
                namespace_id=self.namespace_id,
                now=self.now,
            )
            try:
                result = repository.save(update)
                uow.commit()
            except ObjectStoreRevisionError as exc:
                raise ProjectSkillRepositoryError(
                    f"Project Skill persistence conflict: {exc}"
                ) from exc
        return result

    def rollback(
        self,
        project_id: str,
        *,
        target_revision: int,
        expected_revision: int,
        reason: str,
    ) -> Mapping[str, object]:
        snapshot = _project_skill_snapshot(self.records)
        with self.records.begin() as uow:
            repository = ObjectStoreProjectSkillRepository(
                _SQLiteTransactionObjectStore(uow, snapshot),
                namespace_id=self.namespace_id,
                now=self.now,
            )
            try:
                result = repository.rollback(
                    project_id,
                    target_revision=target_revision,
                    expected_revision=expected_revision,
                    reason=reason,
                )
                uow.commit()
            except ObjectStoreRevisionError as exc:
                raise ProjectSkillRepositoryError(
                    f"Project Skill persistence conflict: {exc}"
                ) from exc
        return result

    def markdown(self, project_id: str, *, revision: int | None = None) -> str | None:
        return self._reader().markdown(project_id, revision=revision)

    def structured(
        self,
        project_id: str,
        *,
        revision: int | None = None,
    ) -> Mapping[str, object] | None:
        return self._reader().structured(project_id, revision=revision)

    def revisions(self, project_id: str) -> tuple[Mapping[str, object], ...]:
        return self._reader().revisions(project_id)

    def _reader(self) -> ObjectStoreProjectSkillRepository:
        return ObjectStoreProjectSkillRepository(
            _SQLiteRecordObjectStore(self.records),
            namespace_id=self.namespace_id,
            now=self.now,
        )


@dataclass(slots=True)
class _SQLiteRecordObjectStore:
    records: SQLiteStructuredRecordStore

    def read(self, collection: str, object_id: str) -> Mapping[str, object] | None:
        record = self.records.read(collection, object_id)
        return dict(record.payload) if record is not None else None

    def list(self, collection: str) -> Sequence[Mapping[str, object]]:
        return tuple(dict(record.payload) for record in self.records.list(collection))

    def delete(self, collection: str, object_id: str) -> bool:
        raise ProjectSkillRepositoryError(
            "SQLite Project Skill adapter does not support delete"
        )

    def write(
        self,
        collection: str,
        object_id: str,
        payload: Mapping[str, object],
        expected_revision: int | None,
    ) -> int:
        raise ProjectSkillRepositoryError(
            "read-only SQLite Project Skill view cannot write"
        )


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
        raise ProjectSkillRepositoryError(
            "SQLite Project Skill adapter does not support delete"
        )

    def write(
        self,
        collection: str,
        object_id: str,
        payload: Mapping[str, object],
        expected_revision: int | None,
    ) -> int:
        if expected_revision is None:
            raise ProjectSkillRepositoryError(
                "SQLite Project Skill writes require expected_revision"
            )
        current = self.uow.read(collection, object_id)
        if expected_revision == 0 and current is not None:
            raise ObjectStoreRevisionError(
                f"expected revision 0, found existing {collection}/{object_id}"
            )
        if expected_revision > 0 and current is None:
            raise ObjectStoreRevisionError(
                f"expected revision {expected_revision}, found 0"
            )
        storage_revision = 0 if current is None else current.revision
        try:
            record = self.uow.put(
                collection,
                object_id,
                payload,
                expected_revision=storage_revision,
            )
        except SQLiteUnitOfWorkConflict as exc:
            raise ObjectStoreRevisionError(str(exc)) from exc
        self._staged[(collection, object_id)] = dict(record.payload)
        return record.revision


def _project_skill_snapshot(
    records: SQLiteStructuredRecordStore,
) -> dict[str, dict[str, Mapping[str, object]]]:
    return {
        collection: {
            record.object_id: dict(record.payload)
            for record in records.list(collection)
        }
        for collection in _PROJECT_SKILL_COLLECTIONS
    }
