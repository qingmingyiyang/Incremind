from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from core.document_engine import DocumentDraft, DocumentRepositoryPort


class WorkbenchDocumentDraftError(ValueError):
    """Raised when a Workbench selection cannot create a safe Document draft."""


@dataclass(frozen=True, slots=True)
class WorkbenchDocumentDraftSelection:
    selection_id: str
    source_id: str
    source_title: str
    source_uri: str
    capture_job_id: str
    media_type: str
    selected_evidence_refs: tuple[str, ...]
    project_id: str | None = None


@dataclass(frozen=True, slots=True)
class WorkbenchDocumentDraftResult:
    status: str
    phase: str
    selection_id: str
    source_id: str
    capture_job_id: str
    document_id: str
    document_revision: int
    document_type: str
    source_refs: tuple[Mapping[str, object], ...]
    source_refs_display: tuple[str, ...]
    next_step_boundary: str
    memory_publication_state: str
    qa_answer_state: str
    user_edit_protection: str
    no_silent_overwrite_boundary: str
    blocked_operations: tuple[str, ...]


class CreateDocumentDraftFromWorkbenchSelection:
    """Creates a source-backed editable Document draft from selected Workbench evidence."""

    _PHASE = "Phase 11 Workbench Memory / Document / QA Vertical Slice"

    def __init__(self, *, documents: DocumentRepositoryPort) -> None:
        self._documents = documents

    def execute(
        self,
        selection: WorkbenchDocumentDraftSelection,
        *,
        title: str | None = None,
        summary: str | None = None,
    ) -> WorkbenchDocumentDraftResult:
        _validate_selection(selection)
        source_refs = _source_refs_from_selection(selection)
        document = self._documents.create(
            DocumentDraft(
                title=title or f"Workbench 文档草稿：{selection.source_title}",
                document_type="summary",
                markdown=_markdown_from_selection(selection, summary=summary),
                source_refs=source_refs,
                project_id=selection.project_id,
            )
        )
        return WorkbenchDocumentDraftResult(
            status="draft_created",
            phase=self._PHASE,
            selection_id=selection.selection_id,
            source_id=selection.source_id,
            capture_job_id=selection.capture_job_id,
            document_id=_required_str(document, "id"),
            document_revision=_required_int(document, "revision"),
            document_type=_required_str(document, "type"),
            source_refs=tuple(source_refs),
            source_refs_display=tuple(_display_source_ref(ref) for ref in source_refs),
            next_step_boundary="document_draft_ready_for_user_edit_and_later_qa_or_memory_candidate_review",
            memory_publication_state="not_published",
            qa_answer_state="not_started",
            user_edit_protection="document_revision_protects_user_edited_blocks",
            no_silent_overwrite_boundary="creates_new_draft_revision_1_only_no_existing_document_overwrite",
            blocked_operations=(
                "memory_candidate_auto_creation",
                "long_term_memory_publication",
                "qa_answer_generation",
                "parser_execution",
                "source_content_read",
                "user_edit_overwrite",
            ),
        )


def serialize_workbench_document_draft(result: WorkbenchDocumentDraftResult) -> dict[str, object]:
    return {
        "status": result.status,
        "phase": result.phase,
        "selection_id": result.selection_id,
        "source_id": result.source_id,
        "capture_job_id": result.capture_job_id,
        "document_id": result.document_id,
        "document_revision": result.document_revision,
        "document_type": result.document_type,
        "source_refs": [dict(ref) for ref in result.source_refs],
        "source_refs_display": list(result.source_refs_display),
        "next_step_boundary": result.next_step_boundary,
        "memory_publication_state": result.memory_publication_state,
        "qa_answer_state": result.qa_answer_state,
        "user_edit_protection": result.user_edit_protection,
        "no_silent_overwrite_boundary": result.no_silent_overwrite_boundary,
        "blocked_operations": list(result.blocked_operations),
    }


def _validate_selection(selection: WorkbenchDocumentDraftSelection) -> None:
    if not selection.selection_id.strip():
        raise WorkbenchDocumentDraftError("Workbench selection requires selection_id")
    if not selection.source_id.strip():
        raise WorkbenchDocumentDraftError("Workbench selection requires source_id")
    if not selection.source_title.strip():
        raise WorkbenchDocumentDraftError("Workbench selection requires source_title")
    if not selection.source_uri.startswith("crp://"):
        raise WorkbenchDocumentDraftError("Workbench selection requires controlled source_uri")
    if not selection.capture_job_id.strip():
        raise WorkbenchDocumentDraftError("Workbench selection requires capture_job_id")
    if not selection.selected_evidence_refs:
        raise WorkbenchDocumentDraftError("Workbench selection requires selected evidence refs")
    if selection.source_uri not in selection.selected_evidence_refs:
        raise WorkbenchDocumentDraftError("Workbench selection evidence must include Source URI")
    if not any(
        ref.startswith("crp://") and f"/jobs/{selection.capture_job_id}/" in ref
        for ref in selection.selected_evidence_refs
    ):
        raise WorkbenchDocumentDraftError("Workbench selection evidence must include capture Job trace")


def _source_refs_from_selection(
    selection: WorkbenchDocumentDraftSelection,
) -> tuple[Mapping[str, object], ...]:
    source_refs = [
        {
            "source_id": selection.source_id,
            "locator": "source:metadata",
            "quote": selection.source_title,
        },
        {
            "source_id": selection.source_id,
            "locator": f"job:{selection.capture_job_id}",
            "quote": "Workbench capture Job trace",
        },
    ]
    return tuple(source_refs)


def _markdown_from_selection(
    selection: WorkbenchDocumentDraftSelection,
    *,
    summary: str | None,
) -> str:
    summary_text = (summary or "该草稿由 Workbench 选中的 Source / Job 证据生成，等待用户编辑和后续问答或记忆候选审核。").strip()
    if not summary_text:
        raise WorkbenchDocumentDraftError("Document draft summary cannot be empty")
    return "\n".join(
        [
            f"# {selection.source_title}",
            "",
            "## 来源",
            "",
            f"- Source: {selection.source_id}",
            f"- Capture Job: {selection.capture_job_id}",
            f"- Media Type: {selection.media_type}",
            "",
            "## 草稿摘要",
            "",
            summary_text,
            "",
            "## 安全边界",
            "",
            "- 未生成 QA 回答。",
            "- 未创建 Memory Candidate。",
            "- 未发布长期 Memory。",
            "- 未读取 Source 内容或运行 parser。",
            "- 用户编辑受 Document revision 保护。",
        ]
    )


def _display_source_ref(ref: Mapping[str, object]) -> str:
    source_id = ref.get("source_id")
    locator = ref.get("locator")
    if not isinstance(source_id, str) or not isinstance(locator, str):
        raise WorkbenchDocumentDraftError("source ref display requires source_id and locator")
    return f"{source_id}#{locator}"


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise WorkbenchDocumentDraftError(f"{key} is required")
    return value


def _required_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise WorkbenchDocumentDraftError(f"{key} must be an integer")
    return value
