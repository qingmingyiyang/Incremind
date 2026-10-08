"""Scoped hard erasure for the recognition SQLite authority.

The service deliberately deletes every authoritative payload that can replay a
selected recognition.  It leaves only content-free tombstones and receipts.
SQLite's ``secure_delete`` applies to pages freed by this transaction; WAL
checkpointing, SQLite backups, and user-created Markdown exports are outside
this transaction and therefore remain explicit operational boundaries.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from contextlib import closing

from backend.recognition import WorkScope, RecognitionConflict
from backend.recognition_retrieval import SQLiteEmbeddingCache
from .source_egress import POLICY_COLLECTIONS
from core.storage_provider import (
    SQLiteStructuredRecord,
    SQLiteStructuredRecordStore,
    SQLiteStructuredRecordUnitOfWork,
)


_RECOGNITIONS = "recognitions"
_VERSIONS = "recognition_versions"
_RELATIONS = "recognition_relations"
_RELATION_PROPOSALS = "recognition_relation_proposals"
_RESTRUCTURE_PROPOSALS = "recognition_restructure_proposals"
_EXPERIENCES = "recognition_experiences"
_CANDIDATES = "recognition_candidates"
_QUESTIONS = "recognition_questions"
_PACKETS = "recognition_context_packets"
_TASKS = "recognition_tasks"
_FEEDBACK = "recognition_task_feedback"
_DOCUMENTS = "documents"
_DOCUMENT_REVISIONS = "document_revisions"
_DOCUMENT_MARKDOWN = "document_markdown"
_MIGRATION_IMPORTS = "recognition_migration_imports"
_MIGRATION_VERSIONS = "recognition_migration_versions"
_TOMBSTONES = "recognition_tombstones"
_RECEIPTS = "recognition_erasure_receipts"
_BOUNDARY = "secure_delete_transaction; backups_and_exports_require_separate_cleanup; "


class ErasureError(RecognitionConflict):
    """The requested erasure is unavailable in this work scope."""


class ErasureConflict(ErasureError):
    """The requested erasure was based on a stale recognition revision."""


@dataclass(frozen=True, slots=True)
class ErasurePreview:
    recognition_id: str
    counts: Mapping[str, int]
    affected: Mapping[str, tuple[str, ...]]
    revisions: Mapping[str, Mapping[str, int]]


@dataclass(frozen=True, slots=True)
class ErasureReceipt:
    recognition_id: str
    erased: bool
    idempotent: bool
    counts: Mapping[str, int]
    cache_state: str
    storage_boundary: str


class ErasureService:
    """Delete a recognition and its in-scope replay closure in one UoW."""

    def __init__(self, records: SQLiteStructuredRecordStore, runtime_root: Path) -> None:
        self._records = records
        self._runtime_root = runtime_root.resolve(strict=False)

    def preview(self, *, scope: WorkScope, recognition_id: str, expected_revision: int) -> ErasurePreview:
        with self._records.begin() as uow:
            root = self._current(uow, scope, recognition_id, expected_revision)
            plan = self._plan(uow, scope, root)
            uow.rollback()
        return ErasurePreview(recognition_id, _counts(plan), _affected(plan), _revisions(plan))

    def recover_pending(self):
        """Retry only the derived-cache cleanup recorded by prior erasures."""
        recovered = 0
        for receipt in self._records.list(_RECEIPTS):
            needs_wal = "wal_checkpoint_pending" in receipt.payload.get("storage_boundary", "")
            if receipt.payload.get("cache_state") != "pending" and not needs_wal:
                continue
            value = receipt.payload["scope"]
            scope = WorkScope(value["user_id"], value.get("project_id"))
            state = self._clear_cache(scope, tuple(receipt.payload.get("recognition_ids", (receipt.object_id,))))
            self._record_cache_state(receipt.object_id, state)
            if needs_wal:
                self._record_storage_boundary(receipt.object_id, _BOUNDARY + self._checkpoint())
            recovered += state == "cleared"
        return recovered

    def erase(self, *, scope: WorkScope, recognition_id: str, expected_revision: int, expected_plan=None) -> ErasureReceipt:
        with self._records.begin() as uow:
            tombstone = uow.read(_TOMBSTONES, recognition_id)
            if tombstone is not None:
                if not _same_scope(tombstone.payload, scope):
                    raise ErasureError("recognition is unavailable in this work scope")
                receipt = uow.read(_RECEIPTS, recognition_id)
                uow.rollback()
                if receipt is not None and receipt.payload.get("cache_state") == "pending":
                    cache_state = self._clear_cache(scope, tuple(receipt.payload.get("recognition_ids", (recognition_id,))))
                    self._record_cache_state(recognition_id, cache_state)
                    receipt = self._records.read(_RECEIPTS, recognition_id)
                if receipt is not None and "wal_checkpoint_pending" in receipt.payload.get("storage_boundary", ""):
                    self._record_storage_boundary(recognition_id, _BOUNDARY + self._checkpoint())
                    receipt = self._records.read(_RECEIPTS, recognition_id)
                return _replay_receipt(recognition_id, receipt)
            root = self._current(uow, scope, recognition_id, expected_revision)
            plan = self._plan(uow, scope, root)
            if expected_plan is not None and expected_plan != _revisions(plan):
                raise ErasureConflict("erasure scope changed; preview again")
            # This setting is connection-local and covers the pages freed below.
            uow.connection.execute("PRAGMA secure_delete=ON")
            for collection, records in plan.items():
                for record in records:
                    uow.delete(collection, record.object_id, expected_revision=record.revision)
            counts = _counts(plan)
            for object_type in (_RECOGNITIONS, _EXPERIENCES):
                for object_id in _ids_from_plan(plan, object_type):
                    uow.put(_TOMBSTONES, object_id, {
                        "id": object_id, "scope": _scope_payload(scope), "object_type": object_type, "deleted_at": _now(),
                    }, expected_revision=0)
            uow.put(_RECEIPTS, recognition_id, {
                "id": recognition_id, "scope": _scope_payload(scope), "deleted_at": _now(),
                "counts": dict(counts), "cache_state": "pending",
                "recognition_ids": list(_ids_from_plan(plan, _RECOGNITIONS)),
                "storage_boundary": _BOUNDARY + "wal_checkpoint_pending",
            }, expected_revision=0)
            uow.commit()
        cache_state = self._clear_cache(scope, _ids_from_plan(plan, _RECOGNITIONS))
        self._record_cache_state(recognition_id, cache_state)
        boundary = _BOUNDARY + self._checkpoint()
        self._record_storage_boundary(recognition_id, boundary)
        return ErasureReceipt(recognition_id, True, False, counts, cache_state, boundary)

    def _checkpoint(self):
        try:
            with closing(sqlite3.connect(self._records.database_path, timeout=1)) as connection:
                status = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            return "wal_truncated" if status[0] == 0 else "wal_checkpoint_pending"
        except (OSError, sqlite3.Error):
            return "wal_checkpoint_pending"

    def _record_storage_boundary(self, recognition_id, boundary):
        with self._records.begin() as uow:
            receipt = uow.read(_RECEIPTS, recognition_id)
            if receipt is not None:
                uow.put(_RECEIPTS, recognition_id, {**receipt.payload, "storage_boundary": boundary}, expected_revision=receipt.revision)
            uow.commit()

    def _current(self, uow: SQLiteStructuredRecordUnitOfWork, scope: WorkScope, recognition_id: str, expected_revision: int) -> SQLiteStructuredRecord:
        record = uow.read(_RECOGNITIONS, recognition_id)
        if record is None or not _same_scope(record.payload, scope):
            raise ErasureError("recognition is unavailable in this work scope")
        if record.revision != expected_revision:
            raise ErasureConflict("recognition revision conflicted")
        return record

    def _plan(self, uow: SQLiteStructuredRecordUnitOfWork, scope: WorkScope, root: SQLiteStructuredRecord) -> dict[str, tuple[SQLiteStructuredRecord, ...]]:
        recognitions = tuple(record for record in uow.list(_RECOGNITIONS) if _same_scope(record.payload, scope))
        all_experiences = tuple(record for record in uow.list(_EXPERIENCES) if _same_scope(record.payload, scope))
        migration_imports = tuple(record for record in uow.list(_MIGRATION_IMPORTS) if _same_scope(record.payload, scope))
        ids = _recognition_closure(recognitions, {root.object_id}, set())
        packets, tasks, task_experiences = (), (), ()
        import_ids: set[str] = set()
        imported_experience_ids: set[str] = set()
        # A user-retained task result can become a source for another recognition.
        # Follow that edge until no additional recognition can expose more tasks.
        while True:
            packets = _scoped_packets(uow.list(_PACKETS), scope, ids)
            packet_ids = {record.object_id for record in packets}
            tasks = tuple(record for record in uow.list(_TASKS) if _same_project(record.payload, scope) and record.payload.get("context_packet_id") in packet_ids)
            task_ids = {record.object_id for record in tasks}
            task_experiences = _task_experiences(all_experiences, scope, task_ids)
            selected_imports = _migration_import_closure(
                migration_imports,
                ids,
                {record.object_id for record in task_experiences} | imported_experience_ids,
            )
            next_import_ids = {record.object_id for record in selected_imports}
            next_imported_experience_ids = {
                experience_id
                for record in selected_imports
                for experience_id in _id_set(record.payload.get("experience_ids"))
            }
            imported_recognition_ids = {
                recognition_id
                for record in selected_imports
                for recognition_id in _id_set(record.payload.get("recognition_ids"))
            }
            all_experience_ids = {record.object_id for record in task_experiences} | next_imported_experience_ids
            expanded = _recognition_closure(recognitions, ids | imported_recognition_ids, all_experience_ids)
            if (expanded == ids and next_import_ids == import_ids
                    and next_imported_experience_ids == imported_experience_ids):
                break
            ids = expanded
            import_ids = next_import_ids
            imported_experience_ids = next_imported_experience_ids
        selected = tuple(record for record in recognitions if record.object_id in ids)
        experiences = tuple(record for record in all_experiences if record.object_id in (
            {record.object_id for record in task_experiences} | imported_experience_ids
        ))
        experience_ids = {record.object_id for record in experiences}
        candidates = _scoped_matches(uow.list(_CANDIDATES), scope, ids, experience_ids, ("source_recognition_ids", "recognition_id"), ("source_experience_ids",))
        questions = _scoped_matches(uow.list(_QUESTIONS), scope, ids, recognition_fields=("recognition_ids",))
        relations = _scoped_matches(uow.list(_RELATIONS), scope, ids, recognition_fields=("from_id", "to_id"))
        proposals = _scoped_matches(uow.list(_RELATION_PROPOSALS), scope, ids, recognition_fields=("from_id", "to_id"))
        restructure_proposals = _scoped_matches(
            uow.list(_RESTRUCTURE_PROPOSALS), scope, ids, experience_ids,
            recognition_fields=("input_recognition_ids", "output_recognition_ids"),
            experience_fields=("input_experience_ids",),
        )
        graph_views = _scoped_matches(uow.list("recognition_graph_views"), scope,
                                      ids | {record.object_id for record in questions}, experience_ids,
                                      recognition_fields=("node_ids",), experience_fields=("node_ids",))
        versions = tuple(record for record in uow.list(_VERSIONS) if record.payload.get("recognition_id") in ids)
        selected_import_records = tuple(record for record in migration_imports if record.object_id in import_ids)
        migration_versions = tuple(
            record for record in uow.list(_MIGRATION_VERSIONS)
            if _same_scope(record.payload, scope) and (
                record.payload.get("import_id") in import_ids
                or record.payload.get("recognition_id") in ids
            )
        )
        task_ids = {record.object_id for record in tasks}
        feedback = tuple(record for record in uow.list(_FEEDBACK) if _same_scope(record.payload, scope) and record.payload.get("task_id") in task_ids)
        documents = _scoped_documents(uow.list(_DOCUMENTS), scope, ids, task_ids)
        document_ids = {record.object_id for record in documents}
        revisions = tuple(record for record in uow.list(_DOCUMENT_REVISIONS) if record.payload.get("document_id") in document_ids)
        markdown = tuple(record for record in uow.list(_DOCUMENT_MARKDOWN) if record.payload.get("document_id") in document_ids)
        preferences = tuple(record for record in uow.list("recognition_recall_preferences") if record.object_id in ids and _same_project(record.payload, scope))
        egress_policies = {
            collection: tuple(record for record in uow.list(collection)
                if _same_scope(record.payload, scope) and record.payload.get("source_type") == kind
                and record.payload.get("source_id") in (ids if kind == "recognition" else experience_ids))
            for kind, collection in POLICY_COLLECTIONS.items()
        }
        return {
            _RECOGNITIONS: selected, _VERSIONS: versions, _RELATIONS: relations, _RELATION_PROPOSALS: proposals,
            _EXPERIENCES: experiences, _RESTRUCTURE_PROPOSALS: restructure_proposals,
            _MIGRATION_IMPORTS: selected_import_records, _MIGRATION_VERSIONS: migration_versions,
            _CANDIDATES: candidates, _QUESTIONS: questions, _PACKETS: packets,
            _TASKS: tasks, _FEEDBACK: feedback, _DOCUMENTS: documents, _DOCUMENT_REVISIONS: revisions,
            _DOCUMENT_MARKDOWN: markdown,
            "recognition_recall_preferences": preferences,
            **egress_policies,
            "recognition_graph_views": graph_views,
        }

    def _clear_cache(self, scope: WorkScope, recognition_ids: tuple[str, ...]) -> str:
        cache_path = self._runtime_root / "recognition-vectors.sqlite3"
        if not cache_path.exists():
            return "cleared"
        try:
            cache = SQLiteEmbeddingCache(str(cache_path))
            try:
                for recognition_id in recognition_ids:
                    cache.delete_recognition(project_id=scope.project_id or "default", recognition_id=recognition_id)
            finally:
                cache.close()
            return "cleared"
        except (OSError, ValueError, sqlite3.Error):
            return "pending"

    def _record_cache_state(self, recognition_id: str, state: str) -> None:
        with self._records.begin() as uow:
            receipt = uow.read(_RECEIPTS, recognition_id)
            if receipt is not None and receipt.payload.get("cache_state") != state:
                uow.put(_RECEIPTS, recognition_id, {**dict(receipt.payload), "cache_state": state}, expected_revision=receipt.revision)
            uow.commit()


def _migration_import_closure(
    records: Iterable[SQLiteStructuredRecord],
    recognition_ids: set[str],
    experience_ids: set[str],
) -> tuple[SQLiteStructuredRecord, ...]:
    """Return whole migration batches touched by the normal erasure closure.

    A migration archive deliberately contains the original bundle and history
    for every item imported together.  Retaining it after any target item is
    erased would retain that item's source text, so the batch is the minimum
    safe deletion unit once one of its imported targets is selected.
    """
    return tuple(record for record in records if (
        _id_set(record.payload.get("recognition_ids")) & recognition_ids
        or _id_set(record.payload.get("experience_ids")) & experience_ids
    ))


def _scoped_matches(records: Iterable[SQLiteStructuredRecord], scope: WorkScope, recognition_ids: set[str], experience_ids: set[str] | tuple[str, ...] = (), recognition_fields: tuple[str, ...] = (), experience_fields: tuple[str, ...] = ()) -> tuple[SQLiteStructuredRecord, ...]:
    experience_ids = set(experience_ids)
    return tuple(record for record in records if _same_scope(record.payload, scope) and (
        any(_id_set(record.payload.get(field)) & recognition_ids for field in recognition_fields)
        or any(_id_set(record.payload.get(field)) & experience_ids for field in experience_fields)
    ))


def _recognition_closure(records: Iterable[SQLiteStructuredRecord], recognition_ids: set[str], experience_ids: set[str]) -> set[str]:
    result = set(recognition_ids)
    changed = True
    while changed:
        changed = False
        for record in records:
            payload = record.payload
            parents = _id_set(payload.get("source_recognition_ids")) | _id_set(payload.get("parent_ids"))
            sources = _id_set(payload.get("source_experience_ids"))
            if record.object_id not in result and (parents & result or sources & experience_ids):
                result.add(record.object_id)
                changed = True
    return result


def _task_experiences(records: Iterable[SQLiteStructuredRecord], scope: WorkScope, task_ids: set[str]) -> tuple[SQLiteStructuredRecord, ...]:
    return tuple(record for record in records if _same_scope(record.payload, scope) and any(
        record.object_id.startswith(f"experience-{task_id}-r") or f"task://{task_id}" in str(record.payload.get("content", ""))
        for task_id in task_ids
    ))


def _scoped_packets(records: Iterable[SQLiteStructuredRecord], scope: WorkScope, ids: set[str]) -> tuple[SQLiteStructuredRecord, ...]:
    result = []
    for record in records:
        if not _same_project(record.payload, scope):
            continue
        items = record.payload.get("items")
        if record.payload.get("kind") == "restructure":
            snapshot = record.payload.get("snapshot")
            rows = snapshot.get("recognitions", []) if isinstance(snapshot, Mapping) else []
            if isinstance(rows, list) and any(isinstance(row, Mapping) and row.get("id") in ids for row in rows):
                result.append(record)
                continue
        if isinstance(items, list) and any(isinstance(item, Mapping) and item.get("id") in ids for item in items):
            result.append(record)
    return tuple(result)


def _scoped_documents(records: Iterable[SQLiteStructuredRecord], scope: WorkScope, ids: set[str], task_ids: set[str]) -> tuple[SQLiteStructuredRecord, ...]:
    result = []
    for record in records:
        if not _same_project(record.payload, scope):
            continue
        refs = record.payload.get("source_refs")
        has_source = isinstance(refs, list) and any(isinstance(ref, Mapping) and (ref.get("source_id") in ids or ref.get("source_id") in task_ids) for ref in refs)
        if has_source:
            result.append(record)
    return tuple(result)


def _id_set(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    return {item for item in value if isinstance(item, str)} if isinstance(value, (list, tuple)) else set()


def _same_scope(payload: Mapping[str, object], scope: WorkScope) -> bool:
    value = payload.get("scope")
    return isinstance(value, Mapping) and value.get("user_id") == scope.user_id and value.get("project_id") == scope.project_id


def _same_project(payload: Mapping[str, object], scope: WorkScope) -> bool:
    return payload.get("project_id") == scope.project_id


def _scope_payload(scope: WorkScope) -> dict[str, str | None]:
    return {"user_id": scope.user_id, "project_id": scope.project_id}


def _counts(plan: Mapping[str, Iterable[SQLiteStructuredRecord]]) -> dict[str, int]:
    return {collection: len(tuple(records)) for collection, records in plan.items() if tuple(records)}


def _affected(plan: Mapping[str, Iterable[SQLiteStructuredRecord]]) -> dict[str, tuple[str, ...]]:
    return {collection: tuple(record.object_id for record in records) for collection, records in plan.items() if tuple(records)}


def _revisions(plan):
    return {collection: {record.object_id: record.revision for record in records}
            for collection, records in plan.items() if records}


def _ids_from_plan(plan: Mapping[str, Iterable[SQLiteStructuredRecord]], collection: str) -> tuple[str, ...]:
    return tuple(record.object_id for record in plan.get(collection, ()))


def _replay_receipt(recognition_id: str, receipt: SQLiteStructuredRecord | None) -> ErasureReceipt:
    counts = dict(receipt.payload.get("counts", {})) if receipt is not None else {}
    state = str(receipt.payload.get("cache_state", "pending")) if receipt is not None else "pending"
    boundary = str(receipt.payload.get("storage_boundary", _BOUNDARY + "wal_checkpoint_pending")) if receipt else _BOUNDARY + "wal_checkpoint_pending"
    return ErasureReceipt(recognition_id, False, True, counts, state, boundary)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
