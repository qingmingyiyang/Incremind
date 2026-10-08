from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote
from typing import Protocol

from .answer_model_request import (
    AnswerModelRequestError,
    CreateAnswerModelRequestFromRecallResult,
)


class SourceContentQaStorePort(Protocol):
    """Reads Source and source_content_read objects from core storage."""

    def read(self, collection: str, object_id: str) -> Mapping[str, object] | None:
        """Read one stored object."""


class SourceContentQaRecallPort(Protocol):
    """Persists Recall Request and Result contracts."""

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


class SourceContentQaRecallError(ValueError):
    """Raised when Source content QA recall cannot run safely."""


@dataclass(frozen=True, slots=True)
class SourceContentQaRecallResult:
    status: str
    project_id: str
    source_id: str
    source_title: str
    question: str
    content_read_id: str
    recall_request_id: str
    recall_result_id: str
    recall_status: str
    model_request_id: str | None
    source_refs_display: tuple[str, ...]
    source_links: tuple[Mapping[str, object], ...]
    qa_answer_state: str
    memory_publication_state: str
    blocked_operations: tuple[str, ...]


class CreateSourceContentQaRecall:
    """Creates local, evidence-backed QA recall from a completed Source content read.

    This use case prepares the answer request and evidence package only. It
    does not execute an LLM provider, fabricate an answer, mutate the Source,
    or publish Memory.
    """

    _LAYERS = ("l3_project_skill", "l0_source")

    def __init__(
        self,
        *,
        object_store: SourceContentQaStorePort,
        recalls: SourceContentQaRecallPort,
        answer_requests: CreateAnswerModelRequestFromRecallResult,
        namespace_id: str = "default",
    ) -> None:
        self._object_store = object_store
        self._recalls = recalls
        self._answer_requests = answer_requests
        self._namespace_id = namespace_id

    def execute(
        self,
        source_id: str,
        *,
        question: str,
        project_id: str = "default",
        project_skill_id: str = "skill-default",
        content_read_id: str | None = None,
        created_at: str | None = None,
    ) -> SourceContentQaRecallResult:
        source = self._source(source_id)
        normalized_question = question.strip()
        if not normalized_question:
            raise SourceContentQaRecallError("Source content QA requires a question")
        normalized_project_id = project_id.strip()
        if not normalized_project_id:
            raise SourceContentQaRecallError("Source content QA requires project_id")
        normalized_project_skill_id = project_skill_id.strip()
        if not normalized_project_skill_id:
            raise SourceContentQaRecallError("Source content QA requires project_skill_id")
        read_id = content_read_id.strip() if isinstance(content_read_id, str) and content_read_id.strip() else f"content-read-{source_id}"
        content_read = self._content_read(source_id, read_id)
        timestamp = created_at or _utc_now()
        request = self._recall_request(
            source_id=source_id,
            source_title=_source_title(source),
            project_id=normalized_project_id,
            project_skill_id=normalized_project_skill_id,
            question=normalized_question,
            content_read_id=read_id,
            created_at=timestamp,
        )
        saved_request = self._recalls.save_request(request)
        request_id = _required_str(saved_request, "id")
        snippet = _best_snippet(_required_str(content_read, "text"), normalized_question)
        if not snippet:
            result = self._recalls.create_insufficient_evidence_result(
                request_id=request_id,
                message="Source content QA did not find a matching readable snippet.",
                created_at=timestamp,
            )
            return self._result(
                status="insufficient_evidence",
                source=source,
                project_id=normalized_project_id,
                question=normalized_question,
                content_read_id=read_id,
                request_id=request_id,
                result=result,
                model_request_id=None,
                qa_answer_state="blocked_insufficient_evidence",
            )
        evidence_result = self._recalls.save_result(
            self._recall_result(
                request=saved_request,
                source_id=source_id,
                content_read_id=read_id,
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
            raise SourceContentQaRecallError(str(exc)) from exc
        return self._result(
            status="model_request_created",
            source=source,
            project_id=normalized_project_id,
            question=normalized_question,
            content_read_id=read_id,
            request_id=request_id,
            result=evidence_result,
            model_request_id=answer_request.model_request_id,
            qa_answer_state="model_request_ready_no_answer_generated",
        )

    def _source(self, source_id: str) -> Mapping[str, object]:
        if not source_id.strip():
            raise SourceContentQaRecallError("Source content QA requires source_id")
        source = self._object_store.read("sources", source_id)
        if source is None:
            raise SourceContentQaRecallError(f"Source not found: {source_id}")
        return source

    def _content_read(self, source_id: str, content_read_id: str) -> Mapping[str, object]:
        content_read = self._object_store.read("source_content_reads", content_read_id)
        if content_read is None:
            raise SourceContentQaRecallError(f"Source content read not found: {content_read_id}")
        if content_read.get("source_id") != source_id:
            raise SourceContentQaRecallError("Source content read does not match source")
        if content_read.get("status") != "completed":
            raise SourceContentQaRecallError("Source content QA requires completed source_content_read")
        if not _required_str(content_read, "text").strip():
            raise SourceContentQaRecallError("Source content QA requires readable text")
        return content_read

    def _recall_request(
        self,
        *,
        source_id: str,
        source_title: str,
        project_id: str,
        project_skill_id: str,
        question: str,
        content_read_id: str,
        created_at: str,
    ) -> dict[str, object]:
        return {
            "schema_version": "1.0.0",
            "id": _stable_id("recall-request-source-content-qa", project_id, source_id, content_read_id, question),
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
                    "reason": "Source QA 仍以当前 Project Skill 为问答边界。",
                },
                {
                    "context_id": _stable_id("ctx-source", source_id, content_read_id),
                    "kind": "source",
                    "object_id": source_id,
                    "uri": f"crp://{self._namespace_id}/sources/{source_id}.json",
                    "reason": f"用户正在围绕 Source 正文问答：{source_title}",
                },
            ],
            "created_at": created_at,
        }

    def _recall_result(
        self,
        *,
        request: Mapping[str, object],
        source_id: str,
        content_read_id: str,
        snippet: str,
        created_at: str,
    ) -> dict[str, object]:
        request_id = _required_str(request, "id")
        project_id = _required_str(request, "project_id")
        layers = _required_string_list(request, "layers")
        hit_id = _stable_id("hit-source-content-read", request_id, source_id, content_read_id, snippet)
        token_estimate = max(1, min(12000, len(snippet.split()) or len(snippet) // 4 or 1))
        hit = {
            "hit_id": hit_id,
            "layer": "l0_source",
            "object_id": source_id,
            "project_id": project_id,
            "source_project_label": None,
            "trust_status": "system_generated",
            "score": 0.91,
            "token_estimate": token_estimate,
            "source_refs": [
                {
                    "source_id": source_id,
                    "locator": f"source_content_read:{content_read_id}",
                    "quote": snippet[:500],
                }
            ],
            "snippet": snippet,
            "explanation": "Source content read 命中的正文片段可作为本次 QA 的可追溯证据。",
        }
        missing_layers = [layer for layer in layers if layer != "l0_source"]
        return {
            "schema_version": "1.0.0",
            "id": _stable_id("recall-result-source-content-qa", request_id, hit_id),
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
                "source_ref_count": 1,
            },
            "truncation": {
                "applied": False,
                "reason": "none",
                "dropped_hit_ids": [],
                "final_hit_count": 1,
                "final_token_estimate": token_estimate,
            },
            "explanation": {
                "summary": "已从 Source 正文读取结果形成可追溯 QA 证据。",
                "layer_order": layers,
                "warnings": ["source_content_context_without_answer_generation"],
            },
            "cross_project": {
                "used": False,
                "grant_id": None,
                "project_ids": [],
            },
            "errors": [],
            "created_at": created_at,
        }

    def _result(
        self,
        *,
        status: str,
        source: Mapping[str, object],
        project_id: str,
        question: str,
        content_read_id: str,
        request_id: str,
        result: Mapping[str, object],
        model_request_id: str | None,
        qa_answer_state: str,
    ) -> SourceContentQaRecallResult:
        source_id = _required_str(source, "id")
        source_ref_display = f"{source_id}#source_content_read:{content_read_id}"
        return SourceContentQaRecallResult(
            status=status,
            project_id=project_id,
            source_id=source_id,
            source_title=_source_title(source),
            question=question,
            content_read_id=content_read_id,
            recall_request_id=request_id,
            recall_result_id=_required_str(result, "id"),
            recall_status=_required_str(result, "status"),
            model_request_id=model_request_id,
            source_refs_display=(source_ref_display,),
            source_links=(
                {
                    "source_id": source_id,
                    "title": _source_title(source),
                    "locator": f"source_content_read:{content_read_id}",
                    "label": source_ref_display,
                    "href": f"/?view=rebuild-library-overview&source_id={quote(source_id, safe='')}",
                },
            ),
            qa_answer_state=qa_answer_state,
            memory_publication_state="not_published",
            blocked_operations=(
                "model_provider_execution",
                "qa_answer_fabrication",
                "long_term_memory_publication",
                "memory_candidate_auto_creation",
                "source_content_overwrite",
            ),
        )


def serialize_source_content_qa_recall(result: SourceContentQaRecallResult) -> dict[str, object]:
    return {
        "status": result.status,
        "project_id": result.project_id,
        "source_id": result.source_id,
        "source_title": result.source_title,
        "question": result.question,
        "content_read_id": result.content_read_id,
        "recall_request_id": result.recall_request_id,
        "recall_result_id": result.recall_result_id,
        "recall_status": result.recall_status,
        "model_request_id": result.model_request_id,
        "source_refs_display": list(result.source_refs_display),
        "source_links": [dict(link) for link in result.source_links],
        "qa_answer_state": result.qa_answer_state,
        "memory_publication_state": result.memory_publication_state,
        "blocked_operations": list(result.blocked_operations),
    }


def _best_snippet(text: str, question: str) -> str:
    normalized = " ".join(line.strip() for line in text.splitlines() if line.strip())
    if not normalized:
        return ""
    terms = _question_terms(question)
    if not terms:
        return normalized[:1200].strip()
    matched_indexes = [index for index in (_find_term_index(normalized, term) for term in terms) if index >= 0]
    best_index = min(matched_indexes, default=-1)
    if best_index < 0:
        return ""
    start = max(0, best_index - 240)
    end = min(len(normalized), best_index + 960)
    return normalized[start:end].strip()


def _question_terms(question: str) -> tuple[str, ...]:
    terms = [term for term in re.split(r"[\s，。！？；：、,.!?;:（）()]+", question) if len(term) >= 2]
    cjk_text = "".join(ch for ch in question if "\u4e00" <= ch <= "\u9fff")
    terms.extend(cjk_text[index : index + 2] for index in range(max(0, len(cjk_text) - 1)))
    return tuple(dict.fromkeys(terms))


def _find_term_index(text: str, term: str) -> int:
    return text.lower().find(term.lower())


def _source_title(source: Mapping[str, object]) -> str:
    title = source.get("title") or source.get("display_name") or source.get("id")
    return str(title) if title is not None else "Untitled Source"


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise SourceContentQaRecallError(f"{key} is required")
    return value


def _required_string_list(mapping: Mapping[str, object], key: str) -> list[str]:
    value = mapping.get(key)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SourceContentQaRecallError(f"{key} must be a list")
    strings = [item for item in value if isinstance(item, str) and item]
    if len(strings) != len(value):
        raise SourceContentQaRecallError(f"{key} must contain only strings")
    return strings


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
