from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .workbench_document_memory_candidate import (
    CreateMemoryCandidateFromWorkbenchDocument,
    WorkbenchDocumentMemoryCandidateError,
)


class SourceTemplateMemoryCandidateError(ValueError):
    """Raised when a template Document cannot safely propose Memory Candidates."""


@dataclass(frozen=True, slots=True)
class SourceTemplateMemoryCandidateResult:
    status: str
    project_id: str
    document_id: str
    document_revision: int
    document_type: str
    candidate_id: str
    candidate_status: str
    target_layer: str
    candidate_type: str
    source_refs_display: tuple[str, ...]
    memory_publication_state: str
    review_state: str
    template_prompt: str
    blocked_operations: tuple[str, ...]


class CreateMemoryCandidateFromSourceTemplateDocument:
    """Create pending Memory Candidates from fixed-template editable Documents."""

    _SUPPORTED_DOCUMENT_TYPES = {"answer_manual", "review", "project_summary"}
    _DEFAULT_TARGET_LAYER = {
        "answer_manual": "series_memory",
        "review": "series_memory",
        "project_summary": "project_skill",
    }
    _DEFAULT_CANDIDATE_TYPE = {
        "answer_manual": "answer_summary",
        "review": "document_takeaway",
        "project_summary": "document_takeaway",
    }

    def __init__(
        self,
        *,
        documents,
        candidates,
        namespace_id: str = "default",
    ) -> None:
        self._documents = documents
        self._creator = CreateMemoryCandidateFromWorkbenchDocument(
            documents=documents,
            candidates=candidates,
            namespace_id=namespace_id,
        )

    def execute(
        self,
        document_id: str,
        *,
        expected_revision: int,
        target_layer: str | None = None,
        candidate_type: str | None = None,
        created_at: str | None = None,
    ) -> SourceTemplateMemoryCandidateResult:
        document = self._documents.read(document_id)
        if document is None:
            raise SourceTemplateMemoryCandidateError(f"template Document not found: {document_id}")
        document_type = _required_str(document, "type")
        if document_type not in self._SUPPORTED_DOCUMENT_TYPES:
            raise SourceTemplateMemoryCandidateError("Document is not a supported unified template output")
        selected_target_layer = _clean_optional(target_layer) or self._DEFAULT_TARGET_LAYER[document_type]
        selected_candidate_type = _clean_optional(candidate_type) or self._DEFAULT_CANDIDATE_TYPE[document_type]
        proposed_content = _template_candidate_content(
            markdown=self._documents.markdown(document_id, revision=expected_revision),
            document_type=document_type,
            target_layer=selected_target_layer,
        )
        try:
            result = self._creator.execute(
                document_id,
                expected_revision=expected_revision,
                proposed_content=proposed_content,
                target_layer=selected_target_layer,
                candidate_type=selected_candidate_type,
                created_at=created_at,
            )
        except WorkbenchDocumentMemoryCandidateError as error:
            raise SourceTemplateMemoryCandidateError(str(error)) from error
        return SourceTemplateMemoryCandidateResult(
            status=result.status,
            project_id=result.project_id,
            document_id=result.document_id,
            document_revision=result.document_revision,
            document_type=document_type,
            candidate_id=result.candidate_id,
            candidate_status=result.candidate_status,
            target_layer=result.target_layer,
            candidate_type=selected_candidate_type,
            source_refs_display=result.source_refs_display,
            memory_publication_state="candidate_created_not_published",
            review_state=result.review_state,
            template_prompt=_memory_template_prompt(document_type=document_type, target_layer=result.target_layer),
            blocked_operations=(
                "long_term_memory_publication",
                "auto_promote_memory",
                "provider_execution",
                "document_overwrite",
                "source_content_read",
            ),
        )


def serialize_source_template_memory_candidate_result(
    result: SourceTemplateMemoryCandidateResult,
) -> dict[str, object]:
    return {
        "status": result.status,
        "project_id": result.project_id,
        "document_id": result.document_id,
        "document_revision": result.document_revision,
        "document_type": result.document_type,
        "candidate_id": result.candidate_id,
        "candidate_status": result.candidate_status,
        "target_layer": result.target_layer,
        "candidate_type": result.candidate_type,
        "source_refs_display": list(result.source_refs_display),
        "memory_publication_state": result.memory_publication_state,
        "review_state": result.review_state,
        "template_prompt": result.template_prompt,
        "blocked_operations": list(result.blocked_operations),
    }


def _template_candidate_content(*, markdown: str | None, document_type: str, target_layer: str) -> str:
    if not isinstance(markdown, str) or not markdown.strip():
        raise SourceTemplateMemoryCandidateError("template Memory Candidate requires persisted markdown")
    return "\n".join(
        [
            f"目标记忆层：{target_layer}",
            f"模板类型：{document_type}",
            "",
            markdown.strip(),
        ]
    )


def _memory_template_prompt(*, document_type: str, target_layer: str) -> str:
    return "\n".join(
        [
            "你正在把统一输出模板草稿转为待审四层记忆候选。",
            f"文档类型：{document_type}",
            f"目标层级：{target_layer}",
            "只能使用文档正文和 source_refs 中已有证据。",
            "候选必须等待用户审核，禁止自动写入长期 Memory 或项目 Skill。",
        ]
    )


def _clean_optional(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip()
    return clean or None


def _required_str(value: Mapping[str, object], field_name: str) -> str:
    candidate = value.get(field_name)
    if not isinstance(candidate, str) or not candidate.strip():
        raise SourceTemplateMemoryCandidateError(f"template Document requires {field_name}")
    return candidate
