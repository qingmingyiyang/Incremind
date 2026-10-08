from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from core.document_engine import DocumentExpectedRevisionError, DocumentRepositoryError
from core.storage_provider import (
    ExternalApplyEvidence,
    ExternalApplyOperation,
    ExternalApplySagaConflict,
    ObjectStorePort,
)


class ExternalDocumentApplyError(ValueError):
    """Raised when an external Document apply cannot safely converge."""


class ExternalDocumentApplyConflict(ExternalDocumentApplyError):
    """Raised when durable evidence conflicts with the requested apply."""


class ExternalDocumentRepository(Protocol):
    def read(self, document_id: str) -> Mapping[str, object] | None: ...

    def save_user_edit(
        self,
        document_id: str,
        *,
        markdown: str,
        expected_revision: int,
        reason: str,
        source_refs: Sequence[Mapping[str, object]] | None = None,
    ) -> Mapping[str, object]: ...

    def revisions(self, document_id: str) -> tuple[Mapping[str, object], ...]: ...


class ExternalApplyOperationStore(Protocol):
    def prepare(
        self,
        *,
        operation_id: str,
        evidence: ExternalApplyEvidence,
        now: str | None = None,
    ) -> ExternalApplyOperation: ...

    def mark_document_applied(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        applied_document_revision: int,
        now: str | None = None,
    ) -> ExternalApplyOperation: ...

    def finalize(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        now: str | None = None,
    ) -> ExternalApplyOperation: ...


@dataclass(frozen=True, slots=True)
class ExternalDocumentApplyResult:
    operation_id: str
    operation_revision: int
    document_id: str
    document_revision: int
    state: str


class ExternalDocumentApplySagaService:
    def __init__(
        self,
        *,
        documents: ExternalDocumentRepository,
        drafts: ObjectStorePort,
        operations: ExternalApplyOperationStore,
        namespace_id: str = "default",
        document_authority_identity: str = "json:object-store-v1",
        now=None,
    ) -> None:
        self._documents = documents
        self._drafts = drafts
        self._operations = operations
        self._namespace_id = namespace_id
        self._document_authority_identity = document_authority_identity
        self._now = now or _utc_now

    def prepare(self, draft_id: str, *, expected_revision: int) -> ExternalApplyOperation:
        draft = self._drafts.read("external_agent_review_drafts", draft_id)
        if draft is None:
            raise ExternalDocumentApplyError("external agent review draft not found")
        target, markdown, source_refs = _draft_contract(draft, expected_revision)
        payload_hash = _payload_hash(draft_id, target, expected_revision, markdown, source_refs)
        evidence = ExternalApplyEvidence(
            namespace_id=self._namespace_id,
            document_id=target,
            base_revision=expected_revision,
            payload_sha256=payload_hash,
            document_authority_identity=self._document_authority_identity,
        )
        try:
            return self._operations.prepare(operation_id=draft_id, evidence=evidence)
        except ExternalApplySagaConflict as exc:
            raise ExternalDocumentApplyConflict(str(exc)) from exc

    def apply(self, draft_id: str, *, expected_revision: int) -> ExternalDocumentApplyResult:
        operation = self.prepare(draft_id, expected_revision=expected_revision)
        evidence = operation.evidence
        target = evidence.document_id
        payload_hash = evidence.payload_sha256
        draft = self._drafts.read("external_agent_review_drafts", draft_id)
        if draft is None:
            raise ExternalDocumentApplyError("external agent review draft not found")
        _target, markdown, source_refs = _draft_contract(draft, expected_revision)
        token = _operation_token(draft_id, payload_hash)

        if operation.state == "prepared":
            applied_revision = _matching_revision(
                self._documents.revisions(target),
                token=token,
                base_revision=expected_revision,
            )
            if applied_revision is None:
                current = self._documents.read(target)
                if current is None:
                    raise ExternalDocumentApplyError("document not found")
                if current.get("revision") != expected_revision:
                    raise ExternalDocumentApplyConflict("document revision advanced without operation evidence")
                try:
                    updated = self._documents.save_user_edit(
                        target,
                        markdown=markdown,
                        expected_revision=expected_revision,
                        reason=token,
                        source_refs=source_refs,
                    )
                except DocumentExpectedRevisionError as exc:
                    raise ExternalDocumentApplyConflict(str(exc)) from exc
                except DocumentRepositoryError as exc:
                    raise ExternalDocumentApplyError(str(exc)) from exc
                applied_revision = _positive_int(updated.get("revision"), "applied document revision")
            operation = self._operations.mark_document_applied(
                draft_id,
                expected_revision=operation.revision,
                applied_document_revision=applied_revision,
            )

        if operation.state == "document_applied":
            applied_revision = _positive_int(
                operation.applied_document_revision,
                "applied document revision",
            )
            current_draft = self._drafts.read("external_agent_review_drafts", draft_id)
            if current_draft is None:
                raise ExternalDocumentApplyError("external agent review draft disappeared")
            if not _draft_finalized_for_operation(current_draft, draft_id, target, applied_revision):
                if current_draft.get("status") != "pending_review":
                    raise ExternalDocumentApplyError("review draft state drifted before finalization")
                finalized_draft = _finalized_draft(
                    current_draft,
                    operation_id=draft_id,
                    document_id=target,
                    document_revision=applied_revision,
                    timestamp=self._now(),
                )
                self._drafts.write(
                    "external_agent_review_drafts",
                    draft_id,
                    finalized_draft,
                    expected_revision=None,
                )
            operation = self._operations.finalize(
                draft_id,
                expected_revision=operation.revision,
            )

        if operation.state != "finalized" or operation.applied_document_revision is None:
            raise ExternalDocumentApplyError("external apply operation did not finalize")
        return ExternalDocumentApplyResult(
            operation_id=draft_id,
            operation_revision=operation.revision,
            document_id=target,
            document_revision=operation.applied_document_revision,
            state=operation.state,
        )


def _draft_contract(draft, expected_revision):
    if draft.get("draft_type") != "document_revision":
        raise ExternalDocumentApplyError("review draft is not a document revision")
    status = draft.get("status")
    application = draft.get("application") if isinstance(draft.get("application"), Mapping) else {}
    operation_id = application.get("operation_id")
    if status != "pending_review" and not (status == "applied" and isinstance(operation_id, str)):
        raise ExternalDocumentApplyError("review draft is not pending or recoverable")
    target = draft.get("target_id")
    markdown = draft.get("proposed_content")
    if not isinstance(target, str) or not target or not isinstance(markdown, str) or not markdown:
        raise ExternalDocumentApplyError("review draft target or proposed content is invalid")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
        raise ExternalDocumentApplyError("expected_revision must be positive")
    refs = draft.get("source_refs")
    source_refs = [dict(item) for item in refs if isinstance(item, Mapping)] if isinstance(refs, list) else []
    return target, markdown, source_refs


def _payload_hash(draft_id, target, base_revision, markdown, source_refs) -> str:
    payload = json.dumps(
        {
            "draft_id": draft_id,
            "document_id": target,
            "base_revision": base_revision,
            "markdown": markdown,
            "source_refs": source_refs,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _operation_token(operation_id: str, payload_hash: str) -> str:
    return f"external_apply:{operation_id}:{payload_hash}"


def _matching_revision(revisions, *, token: str, base_revision: int) -> int | None:
    matches = [
        item
        for item in revisions
        if item.get("reason") == token and item.get("parent_revision") == base_revision
    ]
    if len(matches) > 1:
        raise ExternalDocumentApplyConflict("multiple document revisions match one external apply operation")
    if not matches:
        return None
    return _positive_int(matches[0].get("revision"), "matched document revision")


def _draft_finalized_for_operation(draft, operation_id, document_id, document_revision) -> bool:
    application = draft.get("application") if isinstance(draft.get("application"), Mapping) else {}
    return (
        draft.get("status") == "applied"
        and application.get("operation_id") == operation_id
        and application.get("applied_document_id") == document_id
        and application.get("applied_document_revision") == document_revision
    )


def _finalized_draft(draft, *, operation_id, document_id, document_revision, timestamp):
    result = dict(draft)
    review = dict(draft.get("review")) if isinstance(draft.get("review"), Mapping) else {}
    application = dict(draft.get("application")) if isinstance(draft.get("application"), Mapping) else {}
    result.update({"status": "applied", "updated_at": timestamp})
    review.update({"state": "applied", "reviewed_by": "user", "reviewed_at": timestamp})
    application.update(
        {
            "state": "applied",
            "operation_id": operation_id,
            "applied_by": "user",
            "applied_at": timestamp,
            "applied_document_id": document_id,
            "applied_document_revision": document_revision,
            "writes_long_term_memory": False,
            "writes_staging_memory": False,
        }
    )
    result["review"] = review
    result["application"] = application
    return result


def _positive_int(value, label):
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ExternalDocumentApplyError(f"{label} is invalid")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
