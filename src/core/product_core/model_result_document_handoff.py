from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from core.document_engine import DocumentDraft, DocumentRepositoryPort


class ModelRequestReaderPort(Protocol):
    """Reads persisted Model Requests without invoking a provider."""

    def get_request(self, request_id: str) -> Mapping[str, object] | None:
        """Return a Model Request by id."""


class ModelResultReaderPort(Protocol):
    """Reads persisted Model Results without invoking a provider."""

    def get_result(self, result_id: str) -> Mapping[str, object] | None:
        """Return a Model Result by id."""


class ModelResultDocumentHandoffError(ValueError):
    """Raised when a Model Result cannot safely create a Document draft."""


@dataclass(frozen=True, slots=True)
class ModelResultDocumentHandoffResult:
    project_id: str
    model_request_id: str
    model_result_id: str
    document_id: str
    document_revision: int


class CreateDocumentFromModelResult:
    """Creates a source-backed Document draft from a completed local Model Result."""

    def __init__(
        self,
        *,
        model_requests: ModelRequestReaderPort,
        model_results: ModelResultReaderPort,
        documents: DocumentRepositoryPort,
    ) -> None:
        self._model_requests = model_requests
        self._model_results = model_results
        self._documents = documents

    def execute(
        self,
        model_result_id: str,
        *,
        title: str | None = None,
    ) -> ModelResultDocumentHandoffResult:
        result = self._completed_result(model_result_id)
        request_id = _required_str(result, "request_id")
        request = self._model_requests.get_request(request_id)
        if request is None:
            raise ModelResultDocumentHandoffError(f"Model Request not found: {request_id}")
        project_id = _required_str(request, "project_id")
        source_refs = _request_source_refs(request)
        if not source_refs:
            raise ModelResultDocumentHandoffError("Model Request must carry source refs for document handoff")
        document = self._documents.create(
            DocumentDraft(
                title=title or "模型回答文档草稿",
                document_type="qa_answer",
                markdown=_markdown_from_result(result),
                source_refs=tuple(source_refs),
                project_id=project_id,
            )
        )
        return ModelResultDocumentHandoffResult(
            project_id=project_id,
            model_request_id=request_id,
            model_result_id=_required_str(result, "id"),
            document_id=_required_str(document, "id"),
            document_revision=_required_int(document, "revision"),
        )

    def _completed_result(self, model_result_id: str) -> Mapping[str, object]:
        result = self._model_results.get_result(model_result_id)
        if result is None:
            raise ModelResultDocumentHandoffError(f"Model Result not found: {model_result_id}")
        if result.get("status") != "completed":
            raise ModelResultDocumentHandoffError("Model Result must be completed for document handoff")
        output = result.get("output")
        if not isinstance(output, Mapping) or output.get("kind") != "text":
            raise ModelResultDocumentHandoffError("Model Result document handoff requires text output")
        content = output.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ModelResultDocumentHandoffError("Model Result document handoff requires output content")
        if result.get("error") is not None:
            raise ModelResultDocumentHandoffError("Completed Model Result must not include error")
        safety = result.get("safety")
        if isinstance(safety, Mapping) and safety.get("blocked") is True:
            raise ModelResultDocumentHandoffError("Blocked Model Result cannot create document")
        return result


def _markdown_from_result(result: Mapping[str, object]) -> str:
    output = result.get("output")
    if not isinstance(output, Mapping):
        raise ModelResultDocumentHandoffError("Model Result requires output")
    content = output.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ModelResultDocumentHandoffError("Model Result requires output content")
    return "\n".join(
        [
            "# 模型回答草稿",
            "",
            content.strip(),
            "",
            f"来源模型结果：{_required_str(result, 'id')}",
        ]
    )


def _request_source_refs(request: Mapping[str, object]) -> list[dict[str, object]]:
    payload = request.get("payload")
    if not isinstance(payload, Mapping):
        raise ModelResultDocumentHandoffError("Model Request requires payload")
    source_refs = payload.get("source_refs")
    if not isinstance(source_refs, list):
        raise ModelResultDocumentHandoffError("Model Request payload requires source refs")
    refs: list[dict[str, object]] = []
    seen: set[tuple[str, str, str | None]] = set()
    for candidate in source_refs:
        if not isinstance(candidate, Mapping):
            continue
        source_id = candidate.get("source_id")
        locator = candidate.get("locator")
        quote = candidate.get("quote")
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


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ModelResultDocumentHandoffError(f"{key} is required")
    return value


def _required_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ModelResultDocumentHandoffError(f"{key} must be an integer")
    return value
