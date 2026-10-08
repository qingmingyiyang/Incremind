"""External review formats ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from core.document_engine import DocumentRepositoryPort
from core.memory_core import MemoryReaderPort

from . import http as product_http


def _external_agent_draft_matches(
    draft: Mapping[str, object],
    *,
    status_filter: str | None,
    project_filter: str | None,
) -> bool:
    if status_filter and draft.get("status") != status_filter:
        return False
    if project_filter and draft.get("project_id") != project_filter:
        return False
    return True


def _serialize_external_agent_review_draft(draft: Mapping[str, object]) -> dict[str, object]:
    review = draft.get("review") if isinstance(draft.get("review"), Mapping) else {}
    application = draft.get("application") if isinstance(draft.get("application"), Mapping) else {}
    return {
        "schema_version": draft.get("schema_version"),
        "id": draft.get("id"),
        "proposal_id": draft.get("proposal_id"),
        "proposal_type": draft.get("proposal_type"),
        "draft_type": draft.get("draft_type"),
        "status": draft.get("status"),
        "project_id": draft.get("project_id"),
        "target_id": draft.get("target_id"),
        "summary": draft.get("summary"),
        "proposed_content": draft.get("proposed_content"),
        "suggested_changes": draft.get("suggested_changes"),
        "source_refs": draft.get("source_refs") if isinstance(draft.get("source_refs"), list) else [],
        "evidence_refs": draft.get("evidence_refs") if isinstance(draft.get("evidence_refs"), list) else [],
        "review": {
            "state": review.get("state"),
            "requires_user_confirmation": review.get("requires_user_confirmation"),
            "auto_apply_allowed": review.get("auto_apply_allowed"),
            "reviewed_by": review.get("reviewed_by"),
            "reviewed_at": review.get("reviewed_at"),
        },
        "application": {
            "state": application.get("state"),
            "blocked_operations": application.get("blocked_operations")
            if isinstance(application.get("blocked_operations"), list)
            else [],
            "writes_long_term_memory": application.get("writes_long_term_memory"),
            "writes_staging_memory": application.get("writes_staging_memory"),
        },
        "created_at": draft.get("created_at"),
        "updated_at": draft.get("updated_at"),
        "ref": draft.get("ref"),
        "read_only": True,
        "allowed_operations": ["review_later"],
        "forbidden_operations": [
            "apply_without_user_confirmation",
            "automatic_memory_publication",
            "staging_memory_write",
            "direct_long_term_memory_write",
        ],
        "memory_publication_state": "not_published",
    }


def _serialize_external_agent_review_draft_preview(
    draft: Mapping[str, object],
    *,
    documents: DocumentRepositoryPort,
    skills: Any,
    memory: MemoryReaderPort,
) -> dict[str, object]:
    draft_payload = _serialize_external_agent_review_draft(draft)
    draft_type = draft.get("draft_type")
    match draft_type:
        case "document_revision":
            preview = _external_agent_document_preview(documents, draft)
        case "project_skill_update":
            preview = _external_agent_project_skill_preview(skills, draft)
        case "series_update":
            preview = _external_agent_series_preview(memory, draft)
        case _:
            preview = {
                "target_exists": False,
                "current_revision": None,
                "proposed_revision": None,
                "changed_fields": [],
                "warnings": ["unsupported draft type"],
            }
    return {
        "status": "ready",
        "draft": draft_payload,
        "preview": preview,
        "read_only": True,
        "requires_user_confirmation": True,
        "allowed_operations": ["review_later"],
        "forbidden_operations": [
            "apply_without_user_confirmation",
            "automatic_memory_publication",
            "staging_memory_write",
            "provider_secret_request",
            "cookie_request",
            "remote_upload",
        ],
        "memory_publication_state": "not_published",
    }


def _external_agent_document_preview(
    documents: DocumentRepositoryPort,
    draft: Mapping[str, object],
) -> dict[str, object]:
    document_id = product_http._clean_str(draft.get("target_id"))
    proposed_markdown = product_http._clean_str(draft.get("proposed_content"))
    current = documents.read(document_id) if document_id is not None else None
    current_markdown = documents.markdown(document_id) if document_id is not None else None
    current_revision = current.get("revision") if current is not None else None
    return {
        "target_kind": "document",
        "target_id": document_id,
        "target_exists": current is not None,
        "current_revision": current_revision,
        "proposed_revision": current_revision + 1 if isinstance(current_revision, int) else None,
        "changed_fields": ["markdown"] if current_markdown != proposed_markdown else [],
        "current_summary": _text_preview(current_markdown),
        "proposed_summary": _text_preview(proposed_markdown),
        "warnings": _preview_warnings(current is not None, proposed_markdown is not None),
    }


def _external_agent_project_skill_preview(skills: Any, draft: Mapping[str, object]) -> dict[str, object]:
    suggested_changes = draft.get("suggested_changes")
    structured = (
        _project_skill_structured_from_suggested_changes(suggested_changes)
        if isinstance(suggested_changes, Mapping)
        else None
    )
    project_id = product_http._clean_str(draft.get("project_id")) or (
        product_http._clean_str(structured.get("project_id")) if structured is not None else None
    )
    current = skills.load(project_id) if project_id is not None else None
    current_markdown = skills.markdown(project_id) if project_id is not None and current is not None else None
    proposed_markdown = (
        _project_skill_markdown_from_suggested_changes(suggested_changes, draft)
        if isinstance(suggested_changes, Mapping)
        else None
    )
    current_revision = current.get("revision") if current is not None else None
    proposed_revision = structured.get("revision") if structured is not None else None
    changed_fields = _changed_top_level_fields(current, structured)
    if current_markdown != proposed_markdown:
        changed_fields.append("markdown")
    return {
        "target_kind": "project_skill",
        "target_id": product_http._clean_str(draft.get("target_id")) or (structured.get("id") if structured is not None else None),
        "project_id": project_id,
        "target_exists": current is not None,
        "current_revision": current_revision,
        "proposed_revision": proposed_revision,
        "changed_fields": sorted(set(changed_fields)),
        "current_summary": _text_preview(current_markdown),
        "proposed_summary": _text_preview(proposed_markdown or (structured.get("purpose") if structured else None)),
        "warnings": _preview_warnings(current is not None, structured is not None),
    }


def _external_agent_series_preview(memory: MemoryReaderPort, draft: Mapping[str, object]) -> dict[str, object]:
    suggested_changes = draft.get("suggested_changes")
    series_memory = (
        _series_memory_from_suggested_changes(suggested_changes) if isinstance(suggested_changes, Mapping) else None
    )
    target_id = product_http._clean_str(draft.get("target_id"))
    series_memory_id = product_http._clean_str(series_memory.get("id")) if series_memory is not None else target_id
    current = memory.get("series_memory", series_memory_id) if series_memory_id is not None else None
    current_revision = current.get("revision") if current is not None else None
    proposed_revision = series_memory.get("revision") if series_memory is not None else None
    return {
        "target_kind": "series_memory",
        "target_id": target_id,
        "series_id": product_http._clean_str(series_memory.get("series_id")) if series_memory is not None else target_id,
        "series_memory_id": series_memory_id,
        "target_exists": current is not None,
        "current_revision": current_revision,
        "proposed_revision": proposed_revision,
        "changed_fields": sorted(set(_changed_top_level_fields(current, series_memory))),
        "current_summary": _text_preview(current.get("overview") if current is not None else None),
        "proposed_summary": _text_preview(series_memory.get("overview") if series_memory is not None else None),
        "warnings": _preview_warnings(current is not None, series_memory is not None),
    }


def _changed_top_level_fields(
    current: Mapping[str, object] | None,
    proposed: Mapping[str, object] | None,
) -> list[str]:
    if proposed is None:
        return []
    if current is None:
        return sorted(str(key) for key in proposed.keys())
    fields: list[str] = []
    for key, value in proposed.items():
        if current.get(key) != value:
            fields.append(str(key))
    return fields


def _preview_warnings(target_exists: bool, proposed_exists: bool) -> list[str]:
    warnings: list[str] = []
    if not target_exists:
        warnings.append("target_not_found")
    if not proposed_exists:
        warnings.append("proposed_content_missing")
    return warnings


def _text_preview(value: object, *, limit: int = 180) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _mark_external_agent_review_draft_applied(
    draft: Mapping[str, object],
    *,
    document_id: str,
    document_revision: object,
) -> dict[str, object]:
    applied = dict(draft)
    review = dict(draft.get("review")) if isinstance(draft.get("review"), Mapping) else {}
    application = dict(draft.get("application")) if isinstance(draft.get("application"), Mapping) else {}
    timestamp = datetime.now(timezone.utc).isoformat()
    applied["status"] = "applied"
    applied["updated_at"] = timestamp
    review.update(
        {
            "state": "applied",
            "reviewed_by": "user",
            "reviewed_at": timestamp,
        }
    )
    application.update(
        {
            "state": "applied",
            "applied_by": "user",
            "applied_at": timestamp,
            "applied_document_id": document_id,
            "applied_document_revision": document_revision,
            "writes_long_term_memory": False,
            "writes_staging_memory": False,
        }
    )
    applied["review"] = review
    applied["application"] = application
    return applied


def _mark_external_agent_project_skill_draft_applied(
    draft: Mapping[str, object],
    *,
    project_id: str,
    project_skill_id: object,
    project_skill_revision: object,
) -> dict[str, object]:
    applied = dict(draft)
    review = dict(draft.get("review")) if isinstance(draft.get("review"), Mapping) else {}
    application = dict(draft.get("application")) if isinstance(draft.get("application"), Mapping) else {}
    timestamp = datetime.now(timezone.utc).isoformat()
    applied["status"] = "applied"
    applied["updated_at"] = timestamp
    review.update(
        {
            "state": "applied",
            "reviewed_by": "user",
            "reviewed_at": timestamp,
        }
    )
    application.update(
        {
            "state": "applied",
            "applied_by": "user",
            "applied_at": timestamp,
            "applied_project_id": project_id,
            "applied_project_skill_id": project_skill_id,
            "applied_project_skill_revision": project_skill_revision,
            "writes_long_term_memory": False,
            "writes_staging_memory": False,
        }
    )
    applied["review"] = review
    applied["application"] = application
    return applied


def _mark_external_agent_series_draft_applied(
    draft: Mapping[str, object],
    *,
    series_id: str,
    series_memory_id: str,
    series_memory_revision: object,
) -> dict[str, object]:
    applied = dict(draft)
    review = dict(draft.get("review")) if isinstance(draft.get("review"), Mapping) else {}
    application = dict(draft.get("application")) if isinstance(draft.get("application"), Mapping) else {}
    timestamp = datetime.now(timezone.utc).isoformat()
    applied["status"] = "applied"
    applied["updated_at"] = timestamp
    review.update(
        {
            "state": "applied",
            "reviewed_by": "user",
            "reviewed_at": timestamp,
        }
    )
    application.update(
        {
            "state": "applied",
            "applied_by": "user",
            "applied_at": timestamp,
            "applied_series_id": series_id,
            "applied_series_memory_id": series_memory_id,
            "applied_series_memory_revision": series_memory_revision,
            "writes_long_term_memory": True,
            "writes_long_term_memory_reason": "user_confirmed_series_update_apply",
            "writes_staging_memory": False,
        }
    )
    applied["review"] = review
    applied["application"] = application
    return applied


def _series_memory_from_suggested_changes(
    suggested_changes: Mapping[str, object],
) -> dict[str, object] | None:
    for key in ("structured", "series_memory", "structured_series_memory"):
        value = suggested_changes.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return None


def _project_skill_structured_from_suggested_changes(
    suggested_changes: Mapping[str, object],
) -> dict[str, object] | None:
    for key in ("structured", "project_skill", "structured_project_skill"):
        value = suggested_changes.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return None


def _project_skill_markdown_from_suggested_changes(
    suggested_changes: Mapping[str, object],
    draft: Mapping[str, object],
) -> str | None:
    for key in ("markdown", "project_skill_markdown"):
        value = product_http._clean_str(suggested_changes.get(key))
        if value is not None:
            return value
    return product_http._clean_str(draft.get("proposed_content"))
