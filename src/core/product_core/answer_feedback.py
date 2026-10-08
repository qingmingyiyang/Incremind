from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol


class RecallFeedbackReaderPort(Protocol):
    def get_request(self, request_id: str) -> Mapping[str, object] | None:
        """Return a Recall Request."""

    def get_result(self, result_id: str) -> Mapping[str, object] | None:
        """Return a Recall Result."""


class ModelFeedbackRequestReaderPort(Protocol):
    def get_request(self, request_id: str) -> Mapping[str, object] | None:
        """Return a Model Request."""


class ModelFeedbackResultReaderPort(Protocol):
    def get_result(self, result_id: str) -> Mapping[str, object] | None:
        """Return a Model Result."""


class DocumentFeedbackReaderPort(Protocol):
    def read(self, document_id: str) -> Mapping[str, object] | None:
        """Return a Document."""


class MemoryCandidateFeedbackReaderPort(Protocol):
    def get(self, candidate_id: str) -> Mapping[str, object] | None:
        """Return a Memory Candidate."""


class AnswerFeedbackWriterPort(Protocol):
    def save(self, feedback: Mapping[str, object]) -> Mapping[str, object]:
        """Persist an Answer Feedback record."""


class AnswerFeedbackError(ValueError):
    """Raised when answer feedback would break provenance or review rules."""


@dataclass(frozen=True, slots=True)
class AnswerFeedbackResult:
    feedback_id: str
    project_id: str
    recall_result_id: str
    memory_candidate_id: str
    candidate_status: str
    feedback_type: str
    source_ref_count: int


class CreateAnswerFeedbackFromReviewedCandidate:
    """Creates feedback linking a reviewed Memory Candidate back to its answer chain."""

    def __init__(
        self,
        *,
        recalls: RecallFeedbackReaderPort,
        model_requests: ModelFeedbackRequestReaderPort,
        model_results: ModelFeedbackResultReaderPort,
        documents: DocumentFeedbackReaderPort,
        candidates: MemoryCandidateFeedbackReaderPort,
        feedback: AnswerFeedbackWriterPort,
        namespace_id: str = "default",
    ) -> None:
        self._recalls = recalls
        self._model_requests = model_requests
        self._model_results = model_results
        self._documents = documents
        self._candidates = candidates
        self._feedback = feedback
        self._namespace_id = namespace_id

    def execute(
        self,
        candidate_id: str,
        *,
        created_at: str | None = None,
    ) -> AnswerFeedbackResult:
        candidate = self._reviewed_candidate(candidate_id)
        provenance = _mapping(candidate, "provenance")
        review = _mapping(candidate, "review")
        project_id = _required_str(candidate, "project_id")
        recall_result_id = _required_str(provenance, "recall_result_id")
        model_request_id = _required_str(provenance, "model_request_id")
        model_result_id = _required_str(provenance, "model_result_id")
        document_id = provenance.get("document_id")
        if document_id is not None and not isinstance(document_id, str):
            raise AnswerFeedbackError("candidate provenance document_id must be string or null")
        recall_result = self._recall_result(recall_result_id, project_id=project_id)
        model_request = self._model_request(model_request_id, recall_result_id=recall_result_id)
        self._model_result(model_result_id, model_request_id=model_request_id)
        if document_id is not None:
            self._document(document_id, project_id=project_id)
        source_refs = _source_refs(candidate.get("source_refs"))
        if not source_refs:
            raise AnswerFeedbackError("answer feedback requires candidate source refs")
        request_source_refs = _source_refs(_mapping(model_request, "payload").get("source_refs"))
        if source_refs != request_source_refs:
            raise AnswerFeedbackError("answer feedback source refs must match Model Request source refs")
        feedback_type = _feedback_type(_required_str(candidate, "status"))
        payload = {
            "schema_version": "1.0.0",
            "id": _feedback_id(candidate_id, feedback_type, _required_str(review, "reviewed_at")),
            "project_id": project_id,
            "recall_result_id": recall_result_id,
            "model_request_id": model_request_id,
            "model_result_id": model_result_id,
            "document_id": document_id,
            "memory_candidate_id": candidate_id,
            "candidate_status": _required_str(candidate, "status"),
            "feedback_type": feedback_type,
            "source_refs": source_refs,
            "review": {
                "reviewed_by": _required_str(review, "reviewed_by"),
                "reviewed_at": _required_str(review, "reviewed_at"),
                "reason": _required_str(review, "reason"),
            },
            "input_refs": _input_refs(
                namespace_id=self._namespace_id,
                recall_result_id=recall_result_id,
                model_request_id=model_request_id,
                model_result_id=model_result_id,
                document_id=document_id,
                candidate_id=candidate_id,
            ),
            "created_at": created_at or _utc_now(),
        }
        saved = self._feedback.save(payload)
        return AnswerFeedbackResult(
            feedback_id=_required_str(saved, "id"),
            project_id=project_id,
            recall_result_id=recall_result_id,
            memory_candidate_id=candidate_id,
            candidate_status=_required_str(saved, "candidate_status"),
            feedback_type=_required_str(saved, "feedback_type"),
            source_ref_count=len(source_refs),
        )

    def _reviewed_candidate(self, candidate_id: str) -> Mapping[str, object]:
        candidate = self._candidates.get(candidate_id)
        if candidate is None:
            raise AnswerFeedbackError(f"Memory Candidate not found: {candidate_id}")
        status = candidate.get("status")
        if status not in {"rejected", "promoted"}:
            raise AnswerFeedbackError("answer feedback requires reviewed Memory Candidate")
        review = _mapping(candidate, "review")
        if not isinstance(review.get("reviewed_at"), str):
            raise AnswerFeedbackError("reviewed Memory Candidate requires reviewed_at")
        if status == "promoted" and review.get("reviewed_by") != "user":
            raise AnswerFeedbackError("promoted answer feedback requires user reviewer")
        return candidate

    def _recall_result(self, recall_result_id: str, *, project_id: str) -> Mapping[str, object]:
        result = self._recalls.get_result(recall_result_id)
        if result is None:
            raise AnswerFeedbackError(f"Recall Result not found: {recall_result_id}")
        if result.get("project_id") != project_id:
            raise AnswerFeedbackError("Recall Result project_id must match candidate")
        return result

    def _model_request(self, model_request_id: str, *, recall_result_id: str) -> Mapping[str, object]:
        request = self._model_requests.get_request(model_request_id)
        if request is None:
            raise AnswerFeedbackError(f"Model Request not found: {model_request_id}")
        payload = _mapping(request, "payload")
        if payload.get("recall_result_id") != recall_result_id:
            raise AnswerFeedbackError("Model Request recall_result_id must match candidate")
        return request

    def _model_result(self, model_result_id: str, *, model_request_id: str) -> Mapping[str, object]:
        result = self._model_results.get_result(model_result_id)
        if result is None:
            raise AnswerFeedbackError(f"Model Result not found: {model_result_id}")
        if result.get("request_id") != model_request_id:
            raise AnswerFeedbackError("Model Result request_id must match candidate")
        return result

    def _document(self, document_id: str, *, project_id: str) -> Mapping[str, object]:
        document = self._documents.read(document_id)
        if document is None:
            raise AnswerFeedbackError(f"Document not found: {document_id}")
        if document.get("project_id") != project_id:
            raise AnswerFeedbackError("Document project_id must match candidate")
        return document


def _feedback_type(candidate_status: str) -> str:
    if candidate_status == "promoted":
        return "candidate_promoted_to_draft"
    if candidate_status == "rejected":
        return "candidate_rejected"
    raise AnswerFeedbackError("answer feedback requires rejected or promoted candidate")


def _input_refs(
    *,
    namespace_id: str,
    recall_result_id: str,
    model_request_id: str,
    model_result_id: str,
    document_id: str | None,
    candidate_id: str,
) -> list[dict[str, object]]:
    refs = [
        {
            "kind": "recall_result",
            "object_id": recall_result_id,
            "uri": f"crp://{namespace_id}/recall-results/{recall_result_id}.json",
        },
        {
            "kind": "model_request",
            "object_id": model_request_id,
            "uri": f"crp://{namespace_id}/model-requests/{model_request_id}.json",
        },
        {
            "kind": "model_result",
            "object_id": model_result_id,
            "uri": f"crp://{namespace_id}/model-results/{model_result_id}.json",
        },
    ]
    if document_id is not None:
        refs.append(
            {
                "kind": "document",
                "object_id": document_id,
                "uri": f"crp://{namespace_id}/documents/{document_id}.json",
            }
        )
    refs.append(
        {
            "kind": "memory_candidate",
            "object_id": candidate_id,
            "uri": f"crp://{namespace_id}/memory-candidates/{candidate_id}.json",
        }
    )
    return refs


def _source_refs(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    refs: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        source_id = item.get("source_id")
        locator = item.get("locator")
        if not isinstance(source_id, str) or not source_id:
            continue
        if not isinstance(locator, str) or not locator:
            continue
        ref: dict[str, object] = {"source_id": source_id, "locator": locator}
        quote = item.get("quote")
        if isinstance(quote, str):
            ref["quote"] = quote
        refs.append(ref)
    return refs


def _mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise AnswerFeedbackError(f"{key} is required")
    return value


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise AnswerFeedbackError(f"{key} is required")
    return value


def _feedback_id(candidate_id: str, feedback_type: str, reviewed_at: str) -> str:
    digest = hashlib.sha256(f"{candidate_id}\n{feedback_type}\n{reviewed_at}".encode("utf-8")).hexdigest()[:16]
    return f"answer-feedback-{digest}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
