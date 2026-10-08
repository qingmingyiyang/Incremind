"""Draft review, confirmation and revision-bound recognition handoff."""

from __future__ import annotations

from core.storage_provider.source_retrieval_index import project_original

from pathlib import Path
from fastapi import HTTPException
from fastapi.responses import FileResponse
from .document_recognition import DocumentRecognitionError, extract_document_candidate
from .legacy_intake_review import LegacyReviewConflict, _PROJECTION_ERRORS
from core.document_engine import SQLiteDocumentRepository
from core.document_engine.ports import DocumentDraft
from . import workspace_generation as generation
from .workspace_contracts import _COLLECTION, _project, _now


class WorkspaceReview:
    def __init__(self, items, documents, service, confirmations, legacy_reviews, root):
        self.items = items
        self.documents = documents
        self.service = service
        self.confirmations = confirmations
        self.legacy_reviews = legacy_reviews
        self.root = root

    def document_candidate(self, project_id, document_id, *, previous=None):
        try:
            return extract_document_candidate(self.documents, self.service, project_id, document_id, previous=previous)
        except DocumentRecognitionError as exc:
            raise HTTPException(409, str(exc)) from None

    def list_legacy_reviews(self, project_id: str = "default"):
        if self.legacy_reviews is None:
            return {"items": []}
        return {"items": self.legacy_reviews.list(_project(project_id))}

    def get_legacy_review(self, source_id: str, project_id: str = "default"):
        if self.legacy_reviews is None:
            raise HTTPException(404, "review_not_found")
        try:
            review = self.legacy_reviews.get(source_id, _project(project_id))
        except ValueError as exc:
            if type(exc) is not ValueError or str(exc) not in _PROJECTION_ERRORS:
                raise
            raise HTTPException(409, str(exc)) from exc
        if review is None:
            raise HTTPException(404, "review_not_found")
        return review

    async def save_legacy_review_draft(self, source_id: str, body: dict):
        if self.legacy_reviews is None:
            raise HTTPException(404, "review_not_found")
        try:
            return self.legacy_reviews.save_draft(
                source_id, _project(body.get("project_id", "default")), body.get("markdown"),
                expected_revision=self.expected_revision(body),
                expected_document_basis=self.expected_document_basis(body),
            )
        except LegacyReviewConflict as exc:
            raise self.legacy_draft_conflict(source_id, body, exc) from exc
        except ValueError as exc:
            raise HTTPException(404 if str(exc) == "review_not_found" else 409, str(exc)) from exc

    async def confirm_legacy_review(self, source_id: str, body: dict):
        if self.legacy_reviews is None:
            raise HTTPException(404, "review_not_found")
        if not isinstance(body.get("expected_markdown"), str):
            raise HTTPException(422, "expected_markdown_required")
        try:
            return self.legacy_reviews.confirm(
                source_id, _project(body.get("project_id", "default")),
                expected_revision=self.expected_revision(body),
                expected_document_basis=self.expected_document_basis(body),
                expected_markdown=body["expected_markdown"],
            )
        except LegacyReviewConflict as exc:
            raise self.legacy_draft_conflict(source_id, body, exc) from exc
        except ValueError as exc:
            raise HTTPException(404 if str(exc) == "review_not_found" else 409, str(exc)) from exc

    async def legacy_review_recognition(self, source_id: str, body: dict):
        project_id = _project(body.get("project_id", "default"))
        with self.items.lock:
            review = self.get_legacy_review(source_id, project_id)
            if review["status"] != "confirmed":
                raise HTTPException(409, "review_not_confirmed")
            return self.document_candidate(project_id, review.get("document_id"))

    def legacy_draft_conflict(self, source_id, body, error):
        current = error.current
        if current is None:
            # The write transaction has rolled back before this fresh read.
            try:
                current = self.legacy_reviews.get(source_id, _project(body.get("project_id", "default")))
            except ValueError as exc:
                if type(exc) is not ValueError or str(exc) not in _PROJECTION_ERRORS:
                    raise
                current = None
        return HTTPException(409, {"code": "draft_revision_conflict", "current": current})

    def expected_revision(self, body: dict) -> int:
        revision = body.get("expected_revision")
        if type(revision) is not int or revision < 1:
            raise HTTPException(422, "expected_revision_required")
        return revision

    @staticmethod
    def expected_document_basis(body: dict):
        basis = body.get("expected_document_basis")
        if "expected_document_basis" not in body or (basis is not None and (
                not isinstance(basis, dict) or set(basis) != {"id", "revision"}
                or not isinstance(basis["id"], str) or not basis["id"]
                or type(basis["revision"]) is not int or basis["revision"] < 1)):
            raise HTTPException(422, "expected_document_basis_required")
        return basis

    def draft_conflict(self, row) -> HTTPException:
        return HTTPException(409, {"code": "draft_revision_conflict", "current": self.items.public_row(row)})

    async def save_draft(self, item_id: str, body: dict):
        project_id = _project(body.pop("project_id", "default"))
        revision = self.expected_revision(body)
        body.pop("expected_revision")
        with self.items.lock, self.items.records.begin() as tx:
            row = tx.read(_COLLECTION, item_id)
            if row is None or row.payload.get("project_id") != project_id:
                raise HTTPException(404, "item_not_found")
            if row.revision != revision:
                raise self.draft_conflict(row)
            if row.payload["status"] != "ready":
                raise HTTPException(409, "draft_not_ready")
            try:
                draft = generation._draft(body, row.payload["source_text"])
            except ValueError:
                raise HTTPException(422, "invalid_draft_evidence") from None
            updated = tx.put(_COLLECTION, item_id,
                             {**row.payload, "draft": draft, "title": draft["title"]},
                             expected_revision=row.revision)
            project_original(tx, item_id, project_id, updated.revision, str(updated.payload.get('source_text') or ''),
                             document_id=updated.payload.get('document_id'), previous_document_id=row.payload.get('document_id'))
            tx.commit()
            return self.items.public_row(updated)

    async def confirm(self, item_id: str, body: dict):
        project_id = _project(body.get("project_id", "default"))
        revision = self.expected_revision(body)
        with self.items.lock:
            if self.confirmations is None:
                with self.items.records.begin() as tx:
                    row = tx.read(_COLLECTION, item_id)
                    if row is None or row.payload.get("project_id") != project_id:
                        raise HTTPException(404, "item_not_found")
                    item = row.payload
                    if item["status"] == "confirmed":
                        if revision not in {row.revision, item.get("reviewed_revision")}:
                            raise self.draft_conflict(row)
                        return self.items.public_row(row)
                    if item["status"] != "ready" or not item["draft"]:
                        raise HTTPException(409, "draft_not_ready")
                    if row.revision != revision:
                        raise self.draft_conflict(row)
                    refs = [{"source_id": item_id, "locator": "workspace://" + item_id}]
                    for field in ("facts", "todos"):
                        for entry in item["draft"][field]:
                            evidence = entry["evidence"]
                            refs.append({"source_id": item_id,
                                         "locator": f"text:{evidence['start']}:{evidence['end']}",
                                         "quote": evidence["quote"]})
                    repository = SQLiteDocumentRepository(self.items.records, namespace_id=self.documents.namespace_id, now=_now())
                    document = repository.create_or_replay_generated_in_uow(DocumentDraft(
                        title=item["draft"]["title"], document_type=item_id,
                        markdown=self.image_markdown(item_id, project_id, item["draft"]), source_refs=tuple(refs),
                        project_id=project_id), tx)
                    updated = tx.put(_COLLECTION, item_id, {**item, "document_id": document["id"],
                                                           "status": "confirmed", "reviewed_revision": row.revision},
                                     expected_revision=row.revision)
                    project_original(tx, item_id, project_id, updated.revision, str(updated.payload.get('source_text') or ''),
                                     document_id=updated.payload.get('document_id'), previous_document_id=row.payload.get('document_id'))
                    tx.commit()
                    return self.items.public_row(updated)
            try:
                row = self.items.item_for(item_id, project_id)
                if row.payload.get('status') == 'confirming':
                    self._validate_pending_comment_markdown(row)
                self.confirmations.confirm(item_id, project_id, revision,
                    lambda draft: self.image_markdown(item_id, project_id, draft))
                return self.items.public_row(self.items.item_for(item_id, project_id))
            except ValueError as exc:
                if str(exc) == "draft_revision_conflict":
                    raise self.draft_conflict(self.items.item_for(item_id, project_id)) from exc
                raise HTTPException(404 if str(exc) == "item_not_found" else 409, str(exc)) from exc

    def _validate_pending_comment_markdown(self, row):
        from .v2.image_read import COMMENT_SECTION_PREFIX, draft_markdown
        from .v2.source_sections import SECTIONS, comment_section_for_item
        records = self.items.records
        operation = records.read('workspace_confirmation_operations', 'confirm-' + row.object_id)
        if (operation is None or operation.payload.get('state') != 'pending'
                or operation.payload.get('project_id') != row.payload.get('project_id')
                or operation.payload.get('draft') != row.payload.get('draft')):
            return  # The confirmation owner retains its existing error handling.
        frozen = operation.payload
        base = generation._markdown(frozen['draft'])
        body = frozen.get('markdown')
        generated_comments = (isinstance(body, str) and body.startswith(base)
            and body[len(base):].startswith(COMMENT_SECTION_PREFIX))
        proof = records.read(SECTIONS, row.object_id)
        # Validate malformed/revoked records even when they project no entries.
        comment_section_for_item(records, row)
        has_proof = proof is not None and proof.payload.get('state') != 'unbound'
        if has_proof or generated_comments:
            if (type(frozen.get('reviewed_revision')) is not int
                    or frozen['reviewed_revision'] != row.payload.get('reviewed_revision')
                    or draft_markdown(records, row, frozen['draft']) != body):
                raise ValueError('confirmation_operation_conflict')

    def image_markdown(self, item_id, project_id, draft):
        from .v2.image_read import draft_markdown
        row = self.items.item_for(item_id, project_id)
        return draft_markdown(self.items.records, row, draft)

    def source(self, item_id: str, project_id: str = "default"):
        item = self.items.item_for(item_id, _project(project_id)).payload
        result = {"id": item_id, "title": item["title"], "text": item["source_text"], "source_text": item["source_text"],
                  "input_kind": item["input_kind"]}
        if item["input_kind"] == "link":
            result["original_url"] = item["url"]
        if item["input_kind"] in {"file", "audio", "image", "video"}:
            result["original_download_url"] = f"/api/workspace/v1/items/{item_id}/original?project_id={project_id}"
        return result

    def original(self, item_id: str, project_id: str = "default"):
        item = self.items.item_for(item_id, _project(project_id)).payload
        if item["input_kind"] not in {"file", "audio", "image", "video"}:
            raise HTTPException(404, "original_not_found")
        path = Path(item["original_path"]).resolve()
        if not path.is_relative_to(self.root.resolve()) or not path.is_file():
            raise HTTPException(404, "original_not_found")
        suffix = path.suffix
        filename = item.get("original_name") or item["title"]
        if not Path(filename).suffix:
            filename += suffix
        return FileResponse(path, filename=filename)

    async def recognition(self, item_id: str, body: dict):
        project_id = _project(body.get("project_id", "default"))
        with self.items.lock:
            item = self.items.item_for(item_id, project_id).payload
            if item["status"] != "confirmed":
                raise HTTPException(409, "item_not_confirmed")
            result = self.document_candidate(project_id, item["document_id"], previous=item)
            if any(item.get(key) != result[key] for key in ("experience_id", "candidate_id")):
                self.items.update(item_id, project_id, {"confirmed"},
                       experience_id=result["experience_id"], candidate_id=result["candidate_id"])
            return result
