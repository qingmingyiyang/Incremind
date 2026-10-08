from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from core.memory_core.runtime import memory_candidate_id


class ModelRequestReaderPort(Protocol):
    """Reads persisted Model Requests without invoking a provider."""

    def get_request(self, request_id: str) -> Mapping[str, object] | None:
        """Return a Model Request by id."""


class ModelResultReaderPort(Protocol):
    """Reads persisted Model Results without invoking a provider."""

    def get_result(self, result_id: str) -> Mapping[str, object] | None:
        """Return a Model Result by id."""


class MemoryCandidateWriterPort(Protocol):
    """Persists reviewable Memory Candidates without publishing memory."""

    def save(self, candidate: Mapping[str, object]) -> Mapping[str, object]:
        """Persist one Memory Candidate."""


class ModelResultMemoryCandidateError(ValueError):
    """Raised when a Model Result cannot safely propose Memory Candidates."""


@dataclass(frozen=True, slots=True)
class ModelResultMemoryCandidateResult:
    project_id: str
    model_request_id: str
    model_result_id: str
    candidate_id: str
    status: str
    target_layer: str


class CreateMemoryCandidateFromModelResult:
    """Creates a reviewable Memory Candidate from a completed local Model Result."""

    def __init__(
        self,
        *,
        model_requests: ModelRequestReaderPort,
        model_results: ModelResultReaderPort,
        candidates: MemoryCandidateWriterPort,
        namespace_id: str = "default",
    ) -> None:
        self._model_requests = model_requests
        self._model_results = model_results
        self._candidates = candidates
        self._namespace_id = namespace_id

    def execute(
        self,
        model_result_id: str,
        *,
        target_layer: str = "atom",
        candidate_type: str = "answer_fact",
        document_id: str | None = None,
        document_revision: int | None = None,
        created_at: str | None = None,
    ) -> ModelResultMemoryCandidateResult:
        result = self._completed_local_result(model_result_id)
        request_id = _required_str(result, "request_id")
        request = self._model_requests.get_request(request_id)
        if request is None:
            raise ModelResultMemoryCandidateError(f"Model Request not found: {request_id}")
        project_id = _required_str(request, "project_id")
        payload = _payload(request)
        recall_result_id = _required_str(payload, "recall_result_id")
        source_refs = _source_refs(payload.get("source_refs"))
        if not source_refs:
            raise ModelResultMemoryCandidateError("Model Request must carry source refs for Memory Candidate")
        if document_id is None and document_revision is not None:
            raise ModelResultMemoryCandidateError("document_revision requires document_id")
        if document_id is not None and (not isinstance(document_revision, int) or document_revision < 1):
            raise ModelResultMemoryCandidateError("document_id requires positive document_revision")
        content = _output_content(result)
        timestamp = created_at or _utc_now()
        candidate = {
            "schema_version": "1.0.0",
            "id": memory_candidate_id(model_result_id, target_layer, candidate_type, content),
            "project_id": project_id,
            "target_layer": target_layer,
            "candidate_type": candidate_type,
            "status": "pending_review",
            "proposed_content": content,
            "source_refs": source_refs,
            "provenance": {
                "model_result_id": _required_str(result, "id"),
                "model_request_id": request_id,
                "recall_result_id": recall_result_id,
                "document_id": document_id,
                "document_revision": document_revision,
                "input_refs": _input_refs(
                    namespace_id=self._namespace_id,
                    payload=payload,
                    model_request_id=request_id,
                    model_result_id=model_result_id,
                    document_id=document_id,
                ),
            },
            "review": {
                "requires_user_confirmation": True,
                "auto_promote_allowed": False,
                "reason": "模型回答只能进入待审候选，不能自动写入长期记忆。",
                "reviewed_by": None,
                "reviewed_at": None,
            },
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        saved = self._candidates.save(candidate)
        return ModelResultMemoryCandidateResult(
            project_id=project_id,
            model_request_id=request_id,
            model_result_id=_required_str(result, "id"),
            candidate_id=_required_str(saved, "id"),
            status=_required_str(saved, "status"),
            target_layer=_required_str(saved, "target_layer"),
        )

    def _completed_local_result(self, model_result_id: str) -> Mapping[str, object]:
        result = self._model_results.get_result(model_result_id)
        if result is None:
            raise ModelResultMemoryCandidateError(f"Model Result not found: {model_result_id}")
        if result.get("status") != "completed":
            raise ModelResultMemoryCandidateError("Model Result must be completed for Memory Candidate")
        provider = result.get("provider")
        if not isinstance(provider, Mapping) or provider.get("mode") != "local" or provider.get("remote") is not False:
            raise ModelResultMemoryCandidateError("Memory Candidate requires local completed Model Result")
        model = result.get("model")
        if not isinstance(model, Mapping) or model.get("capability") != "text_generation":
            raise ModelResultMemoryCandidateError("Memory Candidate requires text generation result")
        if result.get("error") is not None:
            raise ModelResultMemoryCandidateError("Completed Model Result must not include error")
        safety = result.get("safety")
        if isinstance(safety, Mapping) and safety.get("blocked") is True:
            raise ModelResultMemoryCandidateError("Blocked Model Result cannot create Memory Candidate")
        _output_content(result)
        return result


def _payload(request: Mapping[str, object]) -> Mapping[str, object]:
    payload = request.get("payload")
    if not isinstance(payload, Mapping):
        raise ModelResultMemoryCandidateError("Model Request requires payload")
    if payload.get("kind") != "answer":
        raise ModelResultMemoryCandidateError("Memory Candidate requires answer Model Request")
    return payload


def _output_content(result: Mapping[str, object]) -> str:
    output = result.get("output")
    if not isinstance(output, Mapping) or output.get("kind") != "text":
        raise ModelResultMemoryCandidateError("Memory Candidate requires text output")
    content = output.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ModelResultMemoryCandidateError("Memory Candidate requires output content")
    return content.strip()


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


def _input_refs(
    *,
    namespace_id: str,
    payload: Mapping[str, object],
    model_request_id: str,
    model_result_id: str,
    document_id: str | None,
) -> list[dict[str, object]]:
    refs: list[dict[str, object]] = []
    for ref in payload.get("input_refs", []):
        if not isinstance(ref, Mapping):
            continue
        kind = ref.get("kind")
        object_id = ref.get("object_id")
        uri = ref.get("uri")
        if isinstance(kind, str) and isinstance(object_id, str) and isinstance(uri, str):
            refs.append({"kind": kind, "object_id": object_id, "uri": uri})
    refs.append(
        {
            "kind": "model_request",
            "object_id": model_request_id,
            "uri": f"crp://{namespace_id}/model-requests/{model_request_id}.json",
        }
    )
    refs.append(
        {
            "kind": "model_result",
            "object_id": model_result_id,
            "uri": f"crp://{namespace_id}/model-results/{model_result_id}.json",
        }
    )
    if document_id is not None:
        refs.append(
            {
                "kind": "document",
                "object_id": document_id,
                "uri": f"crp://{namespace_id}/documents/{document_id}.json",
            }
        )
    deduped: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        key = (str(ref["kind"]), str(ref["object_id"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ref)
    return deduped


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ModelResultMemoryCandidateError(f"{key} is required")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
