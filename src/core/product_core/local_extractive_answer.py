from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


class LocalExtractiveAnswerRequestPort(Protocol):
    """Reads persisted answer Model Requests."""

    def get_request(self, request_id: str) -> Mapping[str, object] | None:
        """Return a Model Request by id."""


class LocalExtractiveAnswerResultPort(Protocol):
    """Persists local answer Model Results."""

    def create_completed_local_result(
        self,
        *,
        request_id: str,
        output_text: str,
        input_tokens: int,
        output_tokens: int,
        provider_id: str = "local-completion-smoke",
        model_name: str = "local-text-generation-smoke",
        model_version: str = "2026-06-30",
        provider_config_version: int = 1,
        started_at: str | None = None,
        completed_at: str | None = None,
        elapsed_ms: int = 0,
    ) -> Mapping[str, object]:
        """Persist one completed local Model Result."""


class LocalExtractiveAnswerError(ValueError):
    """Raised when a local extractive answer cannot be created safely."""


@dataclass(frozen=True, slots=True)
class LocalExtractiveAnswerResult:
    status: str
    project_id: str
    model_request_id: str
    model_result_id: str
    output_preview: str
    source_refs_display: tuple[str, ...]
    qa_answer_state: str
    memory_publication_state: str
    blocked_operations: tuple[str, ...]


class CreateLocalExtractiveAnswerFromModelRequest:
    """Creates a completed local answer result from an evidence-backed Model Request.

    The answer is extractive: it only summarizes the evidence already embedded
    in the Model Request and does not call any external provider.
    """

    def __init__(
        self,
        *,
        model_requests: LocalExtractiveAnswerRequestPort,
        model_results: LocalExtractiveAnswerResultPort,
    ) -> None:
        self._model_requests = model_requests
        self._model_results = model_results

    def execute(
        self,
        model_request_id: str,
        *,
        created_at: str | None = None,
    ) -> LocalExtractiveAnswerResult:
        request = self._request(model_request_id)
        payload = _required_mapping(request, "payload")
        content = _required_str(payload, "content")
        source_refs = _source_refs(payload.get("source_refs"))
        if not source_refs:
            raise LocalExtractiveAnswerError("Local answer requires source refs")
        output_text = _extractive_answer(content, source_refs)
        result = self._model_results.create_completed_local_result(
            request_id=model_request_id,
            output_text=output_text,
            input_tokens=_token_estimate(content),
            output_tokens=_token_estimate(output_text),
            provider_id="local-extractive-answer",
            model_name="evidence-extractive-answer",
            model_version="2026-07-02",
            provider_config_version=1,
            started_at=created_at,
            completed_at=created_at,
            elapsed_ms=0,
        )
        return LocalExtractiveAnswerResult(
            status="completed",
            project_id=_required_str(request, "project_id"),
            model_request_id=model_request_id,
            model_result_id=_required_str(result, "id"),
            output_preview=output_text[:500],
            source_refs_display=tuple(_display_source_ref(ref) for ref in source_refs),
            qa_answer_state="local_answer_completed",
            memory_publication_state="not_published",
            blocked_operations=(
                "remote_model_provider_execution",
                "api_key_use",
                "long_term_memory_publication",
                "memory_candidate_auto_creation",
            ),
        )

    def _request(self, model_request_id: str) -> Mapping[str, object]:
        if not model_request_id.strip():
            raise LocalExtractiveAnswerError("Local answer requires model_request_id")
        request = self._model_requests.get_request(model_request_id)
        if request is None:
            raise LocalExtractiveAnswerError(f"Model Request not found: {model_request_id}")
        if request.get("capability") != "text_generation":
            raise LocalExtractiveAnswerError("Local answer requires text_generation request")
        provider = _required_mapping(request, "provider_preference")
        if provider.get("mode") != "local_only" or provider.get("allow_remote") is not False:
            raise LocalExtractiveAnswerError("Local answer requires local_only provider preference")
        privacy = _required_mapping(request, "privacy")
        if privacy.get("allow_remote") is not False:
            raise LocalExtractiveAnswerError("Local answer requires local-only privacy")
        payload = _required_mapping(request, "payload")
        if payload.get("kind") != "answer":
            raise LocalExtractiveAnswerError("Local answer requires answer payload")
        return request


def serialize_local_extractive_answer_result(result: LocalExtractiveAnswerResult) -> dict[str, object]:
    return {
        "status": result.status,
        "project_id": result.project_id,
        "model_request_id": result.model_request_id,
        "model_result_id": result.model_result_id,
        "output_preview": result.output_preview,
        "source_refs_display": list(result.source_refs_display),
        "qa_answer_state": result.qa_answer_state,
        "memory_publication_state": result.memory_publication_state,
        "blocked_operations": list(result.blocked_operations),
    }


def _extractive_answer(content: str, source_refs: tuple[Mapping[str, object], ...]) -> str:
    question = _extract_section(content, r"用户问题：(.+?)(?:\n\n|$)") or "用户问题"
    evidence = _evidence_snippets(content)
    if not evidence:
        raise LocalExtractiveAnswerError("Local answer requires evidence snippets")
    source_line = "；".join(_display_source_ref(ref) for ref in source_refs)
    joined = "\n".join(f"- {snippet}" for snippet in evidence[:3])
    return (
        "基于已召回证据的本地回答：\n\n"
        f"问题：{question.strip()}\n\n"
        "证据要点：\n"
        f"{joined}\n\n"
        f"引用来源：{source_line}\n\n"
        "边界：该回答只使用本地 Recall Result 中的证据，没有调用远程模型。"
    )


def _evidence_snippets(content: str) -> list[str]:
    blocks = re.findall(r"\[\d+\]\s+layer=.*?\n(.+?)(?=\n\n\[\d+\]\s+layer=|\Z)", content, flags=re.S)
    snippets: list[str] = []
    for block in blocks:
        normalized = " ".join(line.strip() for line in block.splitlines() if line.strip())
        if normalized:
            snippets.append(normalized[:600])
    return snippets


def _extract_section(content: str, pattern: str) -> str:
    match = re.search(pattern, content, flags=re.S)
    return match.group(1).strip() if match else ""


def _source_refs(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list):
        return ()
    refs: list[Mapping[str, object]] = []
    for item in value:
        if isinstance(item, Mapping) and isinstance(item.get("source_id"), str) and isinstance(item.get("locator"), str):
            refs.append(dict(item))
    return tuple(refs)


def _display_source_ref(ref: Mapping[str, object]) -> str:
    return f"{_required_str(ref, 'source_id')}#{_required_str(ref, 'locator')}"


def _token_estimate(text: str) -> int:
    return max(1, len(text.split()) or len(text) // 4 or 1)


def _required_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise LocalExtractiveAnswerError(f"{key} is required")
    return value


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise LocalExtractiveAnswerError(f"{key} is required")
    return value
