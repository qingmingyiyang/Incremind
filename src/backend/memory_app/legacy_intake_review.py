"""Human review of old-OS auto-intake, anchored to its original Source and Job.

An intent is admitted before the capture Job. Generated Documents remain hidden by
the shared visibility policy until the intent reaches ``confirmed``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from backend.api.job_runtime import build_rebuild_job_repository
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.api.task_reference_projection import task_ref_for_workbench_content_transform
from core.document_engine import (
    DocumentExpectedRevisionError, DocumentRepositoryError, SQLiteDocumentRepository,
)
from core.document_engine.ports import DocumentDraft
from core.storage_provider import (
    ObjectStoreRevisionError, SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict,
)
from .legacy_review_index import LegacyReviewReadIndex


COLLECTION = "workspace_review_intents"
_CONFIRM_REASON = "workspace legacy review confirmed"
_PROJECTION_ERRORS = frozenset({
    "review_source_binding_changed", "review_multiple_documents",
    "review_document_project_mismatch", "review_document_archived",
})
_RECOVERABLE_REASONS = frozenset({
    "review_not_found", "review_not_confirming", "review_source_binding_changed",
    "review_source_changed_during_confirmation", "review_draft_empty",
    "review_document_binding_changed", "review_document_revision_invalid",
    "review_intent_changed", "review_document_binding_invalid",
    "review_document_concurrent_edit", "review_multiple_documents",
    "review_document_project_mismatch", "review_document_archived",
})


def _recovery_reason(error: Exception) -> str | None:
    if isinstance(error, (DocumentExpectedRevisionError, SQLiteUnitOfWorkConflict,
                          ObjectStoreRevisionError)):
        return "review_revision_conflict"
    if isinstance(error, DocumentRepositoryError) and isinstance(error.__cause__, ObjectStoreRevisionError):
        return "review_revision_conflict"
    if type(error) is ValueError and str(error) in _RECOVERABLE_REASONS:
        return str(error)
    return None


class LegacyReviewConflict(ValueError):
    def __init__(self, current: dict[str, object] | None = None) -> None:
        super().__init__("review_revision_conflict")
        self.current = current


class LegacyIntakeReview:
    def __init__(
        self, runtime_root: Path, records: SQLiteStructuredRecordStore,
        documents: object, *, object_store: object | None = None,
        jobs: object | None = None,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.records = records
        self.documents = documents
        self.object_store = object_store or build_rebuild_object_store(self.runtime_root)[0]
        self.jobs = jobs or build_rebuild_job_repository(self.runtime_root, self.object_store)

    def list(self, project_id: str) -> tuple[dict[str, object], ...]:
        rows = self.records.list_matching(COLLECTION, project_id=project_id)
        index = LegacyReviewReadIndex(self.documents, self.jobs, self.object_store,
                                      (str(row.payload.get("source_id")) for row in rows), project_id)
        items = []
        for row in rows:
            try:
                items.append({**self._project(row.payload, index=index), "revision": row.revision})
            except ValueError as error:
                if type(error) is not ValueError or str(error) not in _PROJECTION_ERRORS:
                    raise
                # A broken binding must not hide healthy rows or expose content
                # from its new owner. Keep writes and individual reads strict.
                source_id = row.payload.get("source_id")
                if (not isinstance(source_id, str) or not source_id
                        or row.object_id != "review-" + source_id
                        or row.payload.get("id") != row.object_id):
                    source_id = None
                items.append({
                    "review_type": "legacy", "id": row.object_id, "source_id": source_id,
                    "project_id": project_id, "status": "failed", "projection_error": True,
                    "error": str(error), "title": "旧资料审核暂不可用", "input_kind": "text",
                    "source_text": "", "draft_markdown": "", "job_id": None, "task_ref": None,
                    "document_id": None, "document_revision": None, "document_status": None,
                    "created_at": None,
                })
        return tuple(items)

    def get(self, source_id: str, project_id: str) -> dict[str, object] | None:
        row = self.records.read(COLLECTION, "review-" + source_id)
        if row is None or row.payload.get("project_id") != project_id:
            return None
        return {**self._project(row.payload), "revision": row.revision}

    def _review_in_transaction(self, tx, source_id: str, project_id: str, projection):
        row = tx.read(COLLECTION, "review-" + source_id)
        if row is None or row.payload.get("project_id") != project_id:
            raise ValueError("review_not_found")
        if row.revision != projection["revision"]:
            raise LegacyReviewConflict()
        # Enlist same-database reads. Do not open another repository connection
        # or build a display projection while holding the writer transaction.
        documents = (
            (record.payload for record in tx.list("documents"))
            if isinstance(self.documents, SQLiteDocumentRepository)
            and self.documents.records.database_path == self.records.database_path
            else self.documents.list(include_archived=True)
        )
        document = self._single_document(source_id, project_id, documents=documents,
                                         state=row.payload.get("state"))
        basis = {"id": document["id"], "revision": document["revision"]} if document else None
        if basis != projection["document_basis"]:
            raise LegacyReviewConflict()
        return row

    def _required_review(self, source_id, project_id):
        projection = self.get(source_id, project_id)
        if projection is None:
            raise ValueError("review_not_found")
        return projection

    @staticmethod
    def _check_version(projection, expected_revision, expected_document_basis):
        if (type(expected_revision) is not int or expected_revision < 1
                or projection["revision"] != expected_revision
                or projection["document_basis"] != expected_document_basis):
            raise LegacyReviewConflict(projection)

    def save_draft(
        self, source_id: str, project_id: str, markdown: str, *,
        expected_revision: int, expected_document_basis: Mapping[str, object] | None,
    ) -> dict[str, object]:
        if not isinstance(markdown, str):
            raise ValueError("review_markdown_invalid")
        intent_id = "review-" + source_id
        projection = self._required_review(source_id, project_id)
        self._check_version(projection, expected_revision, expected_document_basis)
        with self.records.begin() as tx:
            row = self._review_in_transaction(tx, source_id, project_id, projection)
            if projection["status"] != "ready":
                raise ValueError("review_not_ready")
            if row.payload.get("state") != "pending":
                raise ValueError("review_already_confirming_or_confirmed")
            updated = tx.put(COLLECTION, intent_id, {**row.payload, "draft_markdown": markdown},
                             expected_revision=row.revision)
            tx.commit()
        # Return the acknowledged write, not a later window's update.
        return {**projection, "draft_markdown": markdown, "revision": updated.revision}

    def confirm(
        self, source_id: str, project_id: str, *, expected_revision: int,
        expected_document_basis: Mapping[str, object] | None, expected_markdown: str,
    ) -> dict[str, object]:
        intent_id = "review-" + source_id
        projection = self._required_review(source_id, project_id)
        with self.records.begin() as tx:
            row = self._review_in_transaction(tx, source_id, project_id, projection)
            intent = dict(row.payload)
            if intent.get("state") in {"confirming", "confirmed"}:
                if (type(expected_revision) is not int
                        or expected_revision not in {row.revision, intent.get("reviewed_revision")}
                        or expected_markdown != intent.get("confirmed_markdown")):
                    raise LegacyReviewConflict(projection)
                if intent["state"] == "confirmed":
                    return projection
            if intent.get("state") == "pending":
                self._check_version(projection, expected_revision, expected_document_basis)
                if projection["status"] != "ready":
                    raise ValueError("review_not_ready")
                markdown = projection["draft_markdown"]
                if markdown != expected_markdown:
                    raise LegacyReviewConflict(projection)
                if not isinstance(markdown, str) or not markdown.strip():
                    raise ValueError("review_draft_empty")
                source, source_revision = self._source_snapshot(intent)
                if (source_revision != projection["source_revision"]
                        or (not isinstance(intent.get("draft_markdown"), str)
                            and projection["document_basis"] is None
                            and self._source_text(source_id, source) != markdown)):
                    raise LegacyReviewConflict()
                basis = projection["document_basis"]
                frozen = {
                    **intent, "state": "confirming", "confirmed_markdown": markdown,
                    "reviewed_revision": row.revision,
                    "confirmed_source_revision": source_revision,
                    "expected_document_id": basis["id"] if basis else None,
                    "expected_document_revision": basis["revision"] if basis else None,
                }
                tx.put(COLLECTION, intent_id, frozen, expected_revision=row.revision)
                tx.commit()
            elif intent.get("state") not in {"confirming", "confirmed"}:
                raise ValueError("review_not_ready")
        return self._finish(source_id, project_id)

    def recover_confirming(self) -> tuple[dict[str, object], ...]:
        recovered, _report = self._recover_confirming(strict=True)
        return recovered

    def recover_confirming_report(self) -> dict[str, tuple[dict[str, str], ...]]:
        """Recover each frozen intent at startup while reporting known conflicts only."""
        _recovered, report = self._recover_confirming(strict=False)
        return report

    def _recover_confirming(
        self, *, strict: bool,
    ) -> tuple[tuple[dict[str, object], ...], dict[str, tuple[dict[str, str], ...]]]:
        completed: list[dict[str, object]] = []
        recovered: list[dict[str, str]] = []
        failures: list[dict[str, str]] = []
        for row in self.records.list(COLLECTION):
            intent = row.payload
            if intent.get("state") != "confirming":
                continue
            source_id, project_id = intent.get("source_id"), intent.get("project_id")
            identity = {
                "intent_id": row.object_id,
                "source_id": source_id if isinstance(source_id, str) else "",
                "project_id": project_id if isinstance(project_id, str) else "",
            }
            if (not isinstance(source_id, str) or not source_id
                    or not isinstance(project_id, str) or not project_id
                    or row.object_id != "review-" + source_id
                    or intent.get("id") != row.object_id):
                if strict:
                    raise ValueError("review_intent_invalid")
                failures.append({**identity, "reason": "review_intent_invalid"})
                continue
            try:
                result = self._finish(source_id, project_id)
            except Exception as error:
                if strict:
                    raise
                reason = _recovery_reason(error)
                if reason is None:
                    raise
                failures.append({**identity, "reason": reason})
            else:
                completed.append(result)
                recovered.append(identity)
        return tuple(completed), {"recovered": tuple(recovered), "failures": tuple(failures)}

    def _finish(self, source_id: str, project_id: str) -> dict[str, object]:
        intent_id = "review-" + source_id
        row = self.records.read(COLLECTION, intent_id)
        if row is None or row.payload.get("project_id") != project_id:
            raise ValueError("review_not_found")
        intent = dict(row.payload)
        if intent.get("state") == "confirmed":
            result = self.get(source_id, project_id)
            assert result is not None
            return result
        if intent.get("state") != "confirming":
            raise ValueError("review_not_confirming")
        try:
            return self._finish_pending(intent, source_id, project_id)
        except Exception as error:
            if _recovery_reason(error) not in {
                "review_revision_conflict", "review_intent_changed", "review_document_binding_changed",
            }:
                raise
            current = self.records.read(COLLECTION, intent_id)
            if current is None:
                raise
            completed = current.payload
            document_id, revision = completed.get("document_id"), completed.get("document_revision")
            if (not isinstance(document_id, str) or not document_id
                    or type(revision) is not int or revision < 1
                    or completed != {**intent, "state": "confirmed", "document_id": document_id,
                                     "document_revision": revision}):
                raise
            expected_id = intent.get("expected_document_id")
            if expected_id is not None and (document_id != expected_id
                    or revision != intent.get("expected_document_revision", 0) + 1):
                raise
            if expected_id is None and revision != 1:
                raise
            # A competing request committed this exact frozen confirmation.
            # Read only after the losing transaction rolled back; never replay
            # a different draft or turn an arbitrary storage error into success.
            result = self._required_review(source_id, project_id)
            if result["status"] != "confirmed":
                raise
            return result

    def _finish_pending(self, intent, source_id: str, project_id: str) -> dict[str, object]:
        intent_id = "review-" + source_id
        self._assert_source(intent)
        if (isinstance(intent.get("confirmed_source_revision"), int)
                and self.object_store.revision("sources", source_id) != intent["confirmed_source_revision"]):
            raise ValueError("review_source_changed_during_confirmation")
        markdown = intent.get("confirmed_markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            raise ValueError("review_draft_empty")
        expected_id = intent.get("expected_document_id")
        if isinstance(expected_id, str):
            document = self._single_document(source_id, project_id)
            if document is None or document["id"] != expected_id:
                raise ValueError("review_document_binding_changed")
            expected_revision = intent.get("expected_document_revision")
            if not isinstance(expected_revision, int):
                raise ValueError("review_document_revision_invalid")
            if document["revision"] == expected_revision:
                self.documents.save_user_edit(
                    expected_id, markdown=markdown, expected_revision=expected_revision,
                    reason=_CONFIRM_REASON,
                )
            else:
                self._assert_confirmed_edit(expected_id, expected_revision + 1, markdown, document)
            with self.records.begin() as tx:
                current = tx.read(COLLECTION, intent_id)
                if current is None or current.payload != intent:
                    raise ValueError("review_intent_changed")
                tx.put(COLLECTION, intent_id,
                       {**intent, "state": "confirmed", "document_id": expected_id,
                        "document_revision": expected_revision + 1},
                       expected_revision=current.revision)
                tx.commit()
        elif expected_id is None:
            if not isinstance(self.documents, SQLiteDocumentRepository):
                return self._finish_missing_object_store(intent, source_id, project_id)
            # A missing generated Document is published with the intent in one UoW.
            with self.records.begin() as tx:
                current = tx.read(COLLECTION, intent_id)
                if current is None or current.payload != intent:
                    raise ValueError("review_intent_changed")
                if any(
                    any(isinstance(ref, Mapping) and ref.get("source_id") == source_id
                        for ref in document.get("source_refs", ()))
                    for document in (row.payload for row in tx.list("documents"))
                ):
                    raise ValueError("review_document_binding_changed")
                source = self._assert_source(intent)
                repository = SQLiteDocumentRepository(
                    self.records, namespace_id=self.documents.namespace_id,
                    now=datetime.now(timezone.utc).isoformat(),
                )
                document = repository.create_or_replay_generated_in_uow(
                    DocumentDraft(
                        title=str(source.get("title") or "导入资料"),
                        document_type="legacy_review", markdown=markdown,
                        source_refs=({"source_id": source_id, "locator": "source://" + source_id},),
                        project_id=project_id,
                    ), tx,
                )
                tx.put(COLLECTION, intent_id,
                       {**intent, "state": "confirmed", "document_id": document["id"],
                        "document_revision": document["revision"]},
                       expected_revision=current.revision)
                tx.commit()
        else:
            raise ValueError("review_document_binding_invalid")
        result = self.get(source_id, project_id)
        assert result is not None
        return result

    def _finish_missing_object_store(
        self, intent: Mapping[str, object], source_id: str, project_id: str,
    ) -> dict[str, object]:
        """Replay a JSON Document write after an interrupted confirmation."""
        markdown = str(intent["confirmed_markdown"])
        source = self._assert_source(intent)
        document = self._single_document(source_id, project_id)
        if document is None:
            document = self.documents.create_or_replay_generated(DocumentDraft(
                title=str(source.get("title") or "导入资料"), document_type="legacy_review",
                markdown=markdown,
                source_refs=({"source_id": source_id, "locator": "source://" + source_id},),
                project_id=project_id,
            ))
        if (document.get("type") != "legacy_review" or document.get("revision") != 1
                or self.documents.markdown(str(document["id"])) != markdown
                or document.get("source_refs") != [{"source_id": source_id, "locator": "source://" + source_id}]):
            raise ValueError("review_document_binding_changed")
        intent_id = str(intent["id"])
        with self.records.begin() as tx:
            current = tx.read(COLLECTION, intent_id)
            if current is None or current.payload != intent:
                raise ValueError("review_intent_changed")
            tx.put(COLLECTION, intent_id,
                   {**intent, "state": "confirmed", "document_id": document["id"],
                    "document_revision": 1}, expected_revision=current.revision)
            tx.commit()
        result = self.get(source_id, project_id)
        assert result is not None
        return result

    def _assert_confirmed_edit(
        self, document_id: str, revision: int, markdown: str,
        document: Mapping[str, object],
    ) -> None:
        if document.get("revision") != revision or self.documents.markdown(document_id) != markdown:
            raise ValueError("review_document_concurrent_edit")
        record = self.documents.revision(document_id, revision)
        if not isinstance(record, Mapping) or record.get("reason") != _CONFIRM_REASON:
            raise ValueError("review_document_concurrent_edit")

    def _single_document(self, source_id: str, project_id: str, *, documents=None, state=None) -> Mapping[str, object] | None:
        if documents is None:
            documents = self.documents.list(include_archived=True)
        matches = [
            document for document in documents
            if any(isinstance(ref, Mapping) and ref.get("source_id") == source_id
                   for ref in document.get("source_refs", ()))
        ]
        if len(matches) > 1:
            raise ValueError("review_multiple_documents")
        if not matches:
            return None
        document = matches[0]
        if document.get("project_id") != project_id:
            raise ValueError("review_document_project_mismatch")
        if document.get("status") == "archived" and (state if state is not None else self._intent_state(source_id)) != "confirmed":
            raise ValueError("review_document_archived")
        return document

    def _intent_state(self, source_id: str) -> object:
        row = self.records.read(COLLECTION, "review-" + source_id)
        return row.payload.get("state") if row is not None else None

    def _assert_source(self, intent: Mapping[str, object]) -> Mapping[str, object]:
        return self._source_snapshot(intent)[0]

    def _source_snapshot(self, intent):
        source_id = str(intent["source_id"])
        before_revision = self.object_store.revision("sources", source_id)
        source = self.object_store.read("sources", source_id)
        admitted_revision = intent.get("source_revision")
        current_revision = self.object_store.revision("sources", source_id)
        if (not isinstance(source, Mapping) or source.get("id") != source_id
                or source.get("project_id", "default") != intent.get("project_id")
                or not isinstance(admitted_revision, int) or not isinstance(current_revision, int)
                or current_revision < admitted_revision or before_revision != current_revision):
            raise ValueError("review_source_binding_changed")
        return source, current_revision

    def _source_text(self, source_id, source, *, index=None):
        metadata = source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {}
        text = metadata.get("content_snapshot") or metadata.get("content")
        if not isinstance(text, str) or not text:
            if index is None:
                index = LegacyReviewReadIndex(self.documents, self.jobs, self.object_store,
                                               (source_id,), source.get("project_id", "default"))
            read = index.reads_by_source.get(source_id)
            text = str(read.get("text") or "") if read is not None else ""
        return text

    def _project(self, intent: Mapping[str, object], *, index=None) -> dict[str, object]:
        source, source_revision = self._source_snapshot(intent)
        source_id, project_id = str(intent["source_id"]), str(intent["project_id"])
        if index is None:
            index = LegacyReviewReadIndex(self.documents, self.jobs, self.object_store, (source_id,), project_id)
        document = self._single_document(source_id, project_id,
                                         documents=index.documents_by_source.get(source_id, ()))
        capture = self.jobs.get(str(intent["job_id"]))
        transform = index.transforms_by_source.get(source_id)
        job = transform or capture
        state = intent.get("state")
        job_status = job.get("status") if isinstance(job, Mapping) else None
        if state == "confirmed":
            status = "confirmed"
        elif state == "confirming":
            status = "processing"
        elif job_status in {None, "failed", "cancelled"}:
            status = "failed"
        elif job_status == "completed":
            status = "ready"
        else:
            status = "processing"
        text = self._source_text(source_id, source, index=index)
        markdown = intent.get("confirmed_markdown") if state in {"confirming", "confirmed"} else None
        if not isinstance(markdown, str):
            markdown = intent.get("draft_markdown")
        if not isinstance(markdown, str):
            markdown = self.documents.markdown(str(document["id"]), revision=document["revision"]) if document else text
        transform_id = transform.get("id") if isinstance(transform, Mapping) else None
        original_url = source.get("original_url")
        raw_error = job.get("error") if isinstance(job, Mapping) else "capture_job_not_persisted"
        if isinstance(raw_error, Mapping):
            raw_error = raw_error.get("code")
        error = raw_error if isinstance(raw_error, str) and raw_error else None
        if status == "failed" and error is None:
            error = "processing_failed"
        result: dict[str, object] = {
            "review_type": "legacy", "id": str(intent["id"]), "source_id": source_id,
            "project_id": project_id, "job_id": str(intent["job_id"]),
            "task_ref": task_ref_for_workbench_content_transform(project_id=project_id, job_id=transform_id)
            if isinstance(transform_id, str) else None,
            "title": str(source.get("title") or "导入资料"),
            "input_kind": str(source.get("type") or source.get("source_type") or "text"),
            "source_text": text, "source_revision": source_revision, "draft_markdown": markdown,
            "document_basis": {"id": document["id"], "revision": document["revision"]} if document else None,
            "document_id": str(document["id"]) if document else None,
            "document_revision": intent.get("document_revision") if state == "confirmed"
            else (document.get("revision") if document else None),
            "status": status, "created_at": source.get("created_at"),
            "error": error,
            "document_status": document.get("status") if document else None,
        }
        if isinstance(original_url, str) and original_url:
            result["original_url"] = original_url
        return result
