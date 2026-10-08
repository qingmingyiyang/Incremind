from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from core.memory_core.runtime import memory_candidate_id


class WorkbenchDocumentReaderPort(Protocol):
    """Reads editable Workbench Documents without mutating them."""

    def read(self, document_id: str) -> Mapping[str, object] | None:
        """Read the current Document revision."""

    def markdown(self, document_id: str, *, revision: int | None = None) -> str | None:
        """Read persisted Document markdown."""


class WorkbenchDocumentMemoryCandidateWriterPort(Protocol):
    """Persists reviewable Memory Candidates without publishing memory."""

    def save(self, candidate: Mapping[str, object]) -> Mapping[str, object]:
        """Persist one Memory Candidate."""


class WorkbenchDocumentMemoryCandidateError(ValueError):
    """Raised when a Workbench Document cannot safely propose Memory Candidates."""


@dataclass(frozen=True, slots=True)
class WorkbenchDocumentMemoryCandidateResult:
    status: str
    phase: str
    project_id: str
    document_id: str
    document_revision: int
    candidate_id: str
    candidate_status: str
    target_layer: str
    source_refs_display: tuple[str, ...]
    memory_publication_state: str
    review_state: str
    blocked_operations: tuple[str, ...]


class CreateMemoryCandidateFromWorkbenchDocument:
    """Creates a reviewable Memory Candidate from a selected editable Workbench Document."""

    _PHASE = "Phase 11 Workbench Memory / Document / QA Vertical Slice"

    def __init__(
        self,
        *,
        documents: WorkbenchDocumentReaderPort,
        candidates: WorkbenchDocumentMemoryCandidateWriterPort,
        namespace_id: str = "default",
    ) -> None:
        self._documents = documents
        self._candidates = candidates
        self._namespace_id = namespace_id

    def execute(
        self,
        document_id: str,
        *,
        expected_revision: int,
        proposed_content: str | None = None,
        target_layer: str = "atom",
        candidate_type: str = "document_takeaway",
        created_at: str | None = None,
    ) -> WorkbenchDocumentMemoryCandidateResult:
        if not document_id.strip():
            raise WorkbenchDocumentMemoryCandidateError("Workbench Document Memory Candidate requires document_id")
        document = self._documents.read(document_id)
        if document is None:
            raise WorkbenchDocumentMemoryCandidateError(f"Workbench Document not found: {document_id}")
        current_revision = _required_int(document, "revision")
        if current_revision != expected_revision:
            raise WorkbenchDocumentMemoryCandidateError(
                f"Workbench Document expected revision {expected_revision}, found {current_revision}"
            )
        if document.get("status") != "draft":
            raise WorkbenchDocumentMemoryCandidateError("Workbench Document must be a draft without unresolved conflict")
        project_id = _required_str(document, "project_id")
        source_refs = _source_refs(document.get("source_refs"))
        if not source_refs:
            raise WorkbenchDocumentMemoryCandidateError("Workbench Document Memory Candidate requires source refs")
        markdown = self._documents.markdown(document_id, revision=current_revision)
        content = _candidate_content(proposed_content=proposed_content, markdown=markdown)
        timestamp = created_at or _utc_now()
        candidate = {
            "schema_version": "1.0.0",
            "id": memory_candidate_id(document_id, str(current_revision), target_layer, candidate_type, content),
            "project_id": project_id,
            "target_layer": target_layer,
            "candidate_type": candidate_type,
            "status": "pending_review",
            "proposed_content": content,
            "source_refs": source_refs,
            "provenance": {
                "model_result_id": None,
                "model_request_id": None,
                "recall_result_id": None,
                "document_id": document_id,
                "document_revision": current_revision,
                "input_refs": [
                    {
                        "kind": "document",
                        "object_id": document_id,
                        "uri": f"crp://{self._namespace_id}/documents/{document_id}.json",
                    }
                ],
            },
            "review": {
                "requires_user_confirmation": True,
                "auto_promote_allowed": False,
                "reason": "Workbench Document 只能进入待审记忆候选，不能自动写入长期记忆。",
                "reviewed_by": None,
                "reviewed_at": None,
            },
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        try:
            saved = self._candidates.save(candidate)
        except ValueError as error:
            raise WorkbenchDocumentMemoryCandidateError(str(error)) from error
        return WorkbenchDocumentMemoryCandidateResult(
            status="candidate_created",
            phase=self._PHASE,
            project_id=project_id,
            document_id=document_id,
            document_revision=current_revision,
            candidate_id=_required_str(saved, "id"),
            candidate_status=_required_str(saved, "status"),
            target_layer=_required_str(saved, "target_layer"),
            source_refs_display=tuple(_display_source_ref(ref) for ref in source_refs),
            memory_publication_state="not_published",
            review_state="pending_user_review",
            blocked_operations=(
                "long_term_memory_publication",
                "auto_promote_memory",
                "qa_answer_generation",
                "document_overwrite",
                "source_content_read",
                "user_edit_overwrite",
            ),
        )


def serialize_workbench_document_memory_candidate(
    result: WorkbenchDocumentMemoryCandidateResult,
) -> dict[str, object]:
    return {
        "status": result.status,
        "phase": result.phase,
        "project_id": result.project_id,
        "document_id": result.document_id,
        "document_revision": result.document_revision,
        "candidate_id": result.candidate_id,
        "candidate_status": result.candidate_status,
        "target_layer": result.target_layer,
        "source_refs_display": list(result.source_refs_display),
        "memory_publication_state": result.memory_publication_state,
        "review_state": result.review_state,
        "blocked_operations": list(result.blocked_operations),
    }


def _candidate_content(*, proposed_content: str | None, markdown: str | None) -> str:
    content = proposed_content.strip() if isinstance(proposed_content, str) else ""
    if not content and isinstance(markdown, str):
        content = markdown.strip()
    if not content:
        raise WorkbenchDocumentMemoryCandidateError("Workbench Document Memory Candidate requires proposed content")
    return content


def _source_refs(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    refs: list[dict[str, object]] = []
    seen: set[tuple[str, str, str | None]] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        source_id = item.get("source_id")
        locator = item.get("locator")
        quote = item.get("quote")
        if not isinstance(source_id, str) or not source_id:
            continue
        if not isinstance(locator, str) or not locator:
            continue
        key = (source_id, locator, quote if isinstance(quote, str) else None)
        if key in seen:
            continue
        seen.add(key)
        ref: dict[str, object] = {"source_id": source_id, "locator": locator}
        if isinstance(quote, str):
            ref["quote"] = quote
        refs.append(ref)
    return refs


def _display_source_ref(ref: Mapping[str, object]) -> str:
    source_id = ref.get("source_id")
    locator = ref.get("locator")
    if not isinstance(source_id, str) or not isinstance(locator, str):
        raise WorkbenchDocumentMemoryCandidateError("source ref display requires source_id and locator")
    return f"{source_id}#{locator}"


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise WorkbenchDocumentMemoryCandidateError(f"{key} is required")
    return value


def _required_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise WorkbenchDocumentMemoryCandidateError(f"{key} must be an integer")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
