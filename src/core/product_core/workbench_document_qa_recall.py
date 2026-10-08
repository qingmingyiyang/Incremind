from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .answer_model_request import (
    AnswerModelRequestError,
    CreateAnswerModelRequestFromRecallResult,
)


class WorkbenchDocumentQaReaderPort(Protocol):
    """Reads selected Workbench Documents without mutating user edits."""

    def read(self, document_id: str) -> Mapping[str, object] | None:
        """Read the current Document revision."""

    def markdown(self, document_id: str, *, revision: int | None = None) -> str | None:
        """Read persisted Document markdown."""


class WorkbenchDocumentRecallPort(Protocol):
    """Persists Recall Request and Result contracts for Document QA."""

    def save_request(self, request: Mapping[str, object]) -> Mapping[str, object]:
        """Persist a Recall Request."""

    def save_result(self, result: Mapping[str, object]) -> Mapping[str, object]:
        """Persist a Recall Result."""

    def create_insufficient_evidence_result(
        self,
        *,
        request_id: str,
        message: str = "没有找到足够证据。",
        created_at: str | None = None,
    ) -> Mapping[str, object]:
        """Persist an insufficient-evidence Recall Result."""

    def create_index_unavailable_result(
        self,
        *,
        request_id: str,
        message: str = "召回索引暂不可用，无法完成证据检索。",
        created_at: str | None = None,
    ) -> Mapping[str, object]:
        """Persist an index-unavailable Recall Result."""


class WorkbenchDocumentQaRecallError(ValueError):
    """Raised when selected Document QA cannot run safely."""


@dataclass(frozen=True, slots=True)
class WorkbenchDocumentQaRecallResult:
    status: str
    phase: str
    project_id: str
    document_id: str
    document_revision: int
    question: str
    recall_request_id: str
    recall_result_id: str
    recall_status: str
    model_request_id: str | None
    source_refs_display: tuple[str, ...]
    qa_answer_state: str
    memory_publication_state: str
    blocked_operations: tuple[str, ...]


class CreateWorkbenchDocumentQaRecall:
    """Creates source-backed QA recall for a selected Workbench Document.

    The use case persists traceable Recall Request/Result records and creates a
    local-only Model Request only when the recall result contains source-backed
    evidence. It never executes a provider, never fabricates an answer, and
    never publishes Memory.
    """

    _PHASE = "Phase 11 Workbench Memory / Document / QA Vertical Slice"
    _LAYERS = ("l3_project_skill", "l0_source")

    def __init__(
        self,
        *,
        documents: WorkbenchDocumentQaReaderPort,
        recalls: WorkbenchDocumentRecallPort,
        answer_requests: CreateAnswerModelRequestFromRecallResult,
        namespace_id: str = "default",
    ) -> None:
        self._documents = documents
        self._recalls = recalls
        self._answer_requests = answer_requests
        self._namespace_id = namespace_id

    def execute(
        self,
        document_id: str,
        *,
        expected_revision: int,
        question: str,
        project_skill_id: str,
        index_available: bool = True,
        created_at: str | None = None,
    ) -> WorkbenchDocumentQaRecallResult:
        document = self._document(document_id, expected_revision=expected_revision)
        project_id = _required_str(document, "project_id")
        source_refs = _source_refs(document.get("source_refs"))
        normalized_question = question.strip()
        if not normalized_question:
            raise WorkbenchDocumentQaRecallError("Workbench Document QA requires a question")
        if not project_skill_id.strip():
            raise WorkbenchDocumentQaRecallError("Workbench Document QA requires project_skill_id")
        timestamp = created_at or _utc_now()
        request = self._recall_request(
            project_id=project_id,
            document_id=document_id,
            document_revision=expected_revision,
            project_skill_id=project_skill_id,
            question=normalized_question,
            source_refs=source_refs,
            created_at=timestamp,
        )
        saved_request = self._recalls.save_request(request)
        request_id = _required_str(saved_request, "id")
        if not index_available:
            result = self._recalls.create_index_unavailable_result(
                request_id=request_id,
                message="Workbench Document QA recall index is unavailable.",
                created_at=timestamp,
            )
            return self._blocked_result(
                document=document,
                question=normalized_question,
                request_id=request_id,
                result=result,
                source_refs=source_refs,
                status="index_unavailable",
                qa_answer_state="blocked_index_unavailable",
            )
        markdown = self._documents.markdown(document_id, revision=expected_revision)
        snippet = _snippet(markdown)
        if not snippet:
            result = self._recalls.create_insufficient_evidence_result(
                request_id=request_id,
                message="Workbench Document QA has no readable document snippet.",
                created_at=timestamp,
            )
            return self._blocked_result(
                document=document,
                question=normalized_question,
                request_id=request_id,
                result=result,
                source_refs=source_refs,
                status="insufficient_evidence",
                qa_answer_state="blocked_insufficient_evidence",
            )
        evidence_result = self._recalls.save_result(
            _evidence_recall_result(
                request=saved_request,
                document_id=document_id,
                source_refs=source_refs,
                snippet=snippet,
                created_at=timestamp,
            )
        )
        try:
            answer_request = self._answer_requests.execute(
                _required_str(evidence_result, "id"),
                created_at=timestamp,
            )
        except AnswerModelRequestError as exc:
            raise WorkbenchDocumentQaRecallError(str(exc)) from exc
        return WorkbenchDocumentQaRecallResult(
            status="model_request_created",
            phase=self._PHASE,
            project_id=project_id,
            document_id=document_id,
            document_revision=expected_revision,
            question=normalized_question,
            recall_request_id=request_id,
            recall_result_id=answer_request.recall_result_id,
            recall_status=_required_str(evidence_result, "status"),
            model_request_id=answer_request.model_request_id,
            source_refs_display=tuple(_display_source_ref(ref) for ref in source_refs),
            qa_answer_state="model_request_ready_no_answer_generated",
            memory_publication_state="not_published",
            blocked_operations=(
                "model_provider_execution",
                "qa_answer_fabrication",
                "long_term_memory_publication",
                "memory_candidate_auto_creation",
                "document_overwrite",
                "source_content_read",
                "user_edit_overwrite",
            ),
        )

    def _document(self, document_id: str, *, expected_revision: int) -> Mapping[str, object]:
        if not document_id.strip():
            raise WorkbenchDocumentQaRecallError("Workbench Document QA requires document_id")
        document = self._documents.read(document_id)
        if document is None:
            raise WorkbenchDocumentQaRecallError(f"Workbench Document not found: {document_id}")
        current_revision = _required_int(document, "revision")
        if current_revision != expected_revision:
            raise WorkbenchDocumentQaRecallError(
                f"Workbench Document expected revision {expected_revision}, found {current_revision}"
            )
        if document.get("status") != "draft":
            raise WorkbenchDocumentQaRecallError("Workbench Document QA requires an editable draft")
        conflict = document.get("conflict")
        if isinstance(conflict, Mapping) and conflict.get("status") not in {None, "none"}:
            raise WorkbenchDocumentQaRecallError("Workbench Document QA is blocked by unresolved conflict")
        if not _required_str(document, "project_id"):
            raise WorkbenchDocumentQaRecallError("Workbench Document QA requires project_id")
        if not _source_refs(document.get("source_refs")):
            raise WorkbenchDocumentQaRecallError("Workbench Document QA requires source refs")
        return document

    def _recall_request(
        self,
        *,
        project_id: str,
        document_id: str,
        document_revision: int,
        project_skill_id: str,
        question: str,
        source_refs: Sequence[Mapping[str, object]],
        created_at: str,
    ) -> dict[str, object]:
        return {
            "schema_version": "1.0.0",
            "id": _stable_id(
                "recall-request-document-qa",
                project_id,
                document_id,
                str(document_revision),
                question,
                project_skill_id,
            ),
            "query": question,
            "project_id": project_id,
            "scope": "project",
            "layers": list(self._LAYERS),
            "trust_filter": {
                "include": ["trusted", "user_confirmed", "system_generated"],
                "minimum_confidence": 0.6,
                "allow_imported_unverified": False,
            },
            "budget": {
                "max_hits": 4,
                "max_tokens": 4000,
                "per_layer_limits": {
                    "l3_project_skill": 1,
                    "l4_persona": 0,
                    "l3_series_memory": 0,
                    "l2_scenario": 0,
                    "l1_atom": 0,
                    "l0_source": 3,
                },
            },
            "cross_project": {
                "allowed": False,
                "grant_id": None,
                "project_ids": [],
            },
            "required_context_refs": [
                {
                    "context_id": _stable_id("ctx-project-skill", project_id, project_skill_id),
                    "kind": "project_skill",
                    "object_id": project_skill_id,
                    "uri": f"crp://{self._namespace_id}/projects/{project_id}/project-skill.json",
                    "reason": "Document QA 仍以当前 Project Skill 为边界锚点。",
                },
                {
                    "context_id": _stable_id("ctx-document", document_id, str(document_revision)),
                    "kind": "document",
                    "object_id": document_id,
                    "uri": f"crp://{self._namespace_id}/documents/{document_id}.json",
                    "reason": "Workbench 选中的 Document 是本次问答上下文。",
                },
                *[
                    {
                        "context_id": _stable_id(
                            "ctx-source",
                            _required_str(ref, "source_id"),
                            _required_str(ref, "locator"),
                        ),
                        "kind": "source",
                        "object_id": _required_str(ref, "source_id"),
                        "uri": f"crp://{self._namespace_id}/sources/{_required_str(ref, 'source_id')}.json",
                        "reason": "Document QA 必须保留 Source 证据引用。",
                    }
                    for ref in source_refs
                ],
            ],
            "created_at": created_at,
        }

    def _blocked_result(
        self,
        *,
        document: Mapping[str, object],
        question: str,
        request_id: str,
        result: Mapping[str, object],
        source_refs: Sequence[Mapping[str, object]],
        status: str,
        qa_answer_state: str,
    ) -> WorkbenchDocumentQaRecallResult:
        return WorkbenchDocumentQaRecallResult(
            status=status,
            phase=self._PHASE,
            project_id=_required_str(document, "project_id"),
            document_id=_required_str(document, "id"),
            document_revision=_required_int(document, "revision"),
            question=question,
            recall_request_id=request_id,
            recall_result_id=_required_str(result, "id"),
            recall_status=_required_str(result, "status"),
            model_request_id=None,
            source_refs_display=tuple(_display_source_ref(ref) for ref in source_refs),
            qa_answer_state=qa_answer_state,
            memory_publication_state="not_published",
            blocked_operations=(
                "model_request_creation",
                "model_provider_execution",
                "qa_answer_fabrication",
                "long_term_memory_publication",
                "memory_candidate_auto_creation",
                "document_overwrite",
                "source_content_read",
                "user_edit_overwrite",
            ),
        )


def serialize_workbench_document_qa_recall(result: WorkbenchDocumentQaRecallResult) -> dict[str, object]:
    return {
        "status": result.status,
        "phase": result.phase,
        "project_id": result.project_id,
        "document_id": result.document_id,
        "document_revision": result.document_revision,
        "question": result.question,
        "recall_request_id": result.recall_request_id,
        "recall_result_id": result.recall_result_id,
        "recall_status": result.recall_status,
        "model_request_id": result.model_request_id,
        "source_refs_display": list(result.source_refs_display),
        "qa_answer_state": result.qa_answer_state,
        "memory_publication_state": result.memory_publication_state,
        "blocked_operations": list(result.blocked_operations),
    }


def _evidence_recall_result(
    *,
    request: Mapping[str, object],
    document_id: str,
    source_refs: Sequence[Mapping[str, object]],
    snippet: str,
    created_at: str,
) -> dict[str, object]:
    request_id = _required_str(request, "id")
    project_id = _required_str(request, "project_id")
    layers = _required_string_list(request, "layers")
    source_id = _required_str(source_refs[0], "source_id")
    hit_id = _stable_id("hit-document-source", request_id, document_id, source_id, snippet)
    token_estimate = max(1, min(12000, len(snippet.split()) or len(snippet) // 4 or 1))
    hit = {
        "hit_id": hit_id,
        "layer": "l0_source",
        "object_id": source_id,
        "project_id": project_id,
        "source_project_label": None,
        "trust_status": "system_generated",
        "score": 0.92,
        "token_estimate": token_estimate,
        "source_refs": [dict(ref) for ref in source_refs],
        "snippet": snippet,
        "explanation": "Workbench Document 保留的 Source refs 可作为本次 QA 的可追溯证据。",
    }
    missing_layers = [layer for layer in layers if layer != "l0_source"]
    return {
        "schema_version": "1.0.0",
        "id": _stable_id("recall-result-document-qa", request_id, hit_id),
        "request_id": request_id,
        "project_id": project_id,
        "status": "partial" if missing_layers else "evidence_found",
        "hits": [hit],
        "coverage": {
            "status": "partial" if missing_layers else "sufficient",
            "requested_layers": layers,
            "covered_layers": ["l0_source"],
            "missing_layers": missing_layers,
            "low_trust": False,
            "source_ref_count": len(source_refs),
        },
        "truncation": {
            "applied": False,
            "reason": "none",
            "dropped_hit_ids": [],
            "final_hit_count": 1,
            "final_token_estimate": token_estimate,
        },
        "explanation": {
            "summary": "已从选中 Workbench Document 的 Source refs 形成可追溯 QA 证据。",
            "layer_order": layers,
            "warnings": ["document_context_without_answer_generation"],
        },
        "cross_project": {
            "used": False,
            "grant_id": None,
            "project_ids": [],
        },
        "errors": [],
        "created_at": created_at,
    }


def _source_refs(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
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
    return tuple(refs)


def _snippet(markdown: str | None) -> str:
    if not isinstance(markdown, str):
        return ""
    normalized = " ".join(line.strip() for line in markdown.splitlines() if line.strip())
    return normalized[:1200].strip()


def _display_source_ref(ref: Mapping[str, object]) -> str:
    source_id = ref.get("source_id")
    locator = ref.get("locator")
    if not isinstance(source_id, str) or not isinstance(locator, str):
        raise WorkbenchDocumentQaRecallError("source ref display requires source_id and locator")
    return f"{source_id}#{locator}"


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise WorkbenchDocumentQaRecallError(f"{key} is required")
    return value


def _required_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise WorkbenchDocumentQaRecallError(f"{key} must be an integer")
    return value


def _required_string_list(mapping: Mapping[str, object], key: str) -> list[str]:
    value = mapping.get(key)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise WorkbenchDocumentQaRecallError(f"{key} must be a list")
    strings = [item for item in value if isinstance(item, str) and item]
    if len(strings) != len(value):
        raise WorkbenchDocumentQaRecallError(f"{key} must contain only strings")
    return strings


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
