"""Small projections of organized Markdown, without reconstructing L0 evidence."""
from __future__ import annotations

from core.document_engine.markdown_sections import (
    summary_of, _lines, _section, _HEADING, _BULLET,
)


def facts_of(markdown: str) -> list[str]:
    """Read top-level bullets in 关键事实 as text only, preserving repetitions.

    The generated Markdown does not retain facts' L0 evidence. Callers needing
    original-source highlights must use the existing frozen draft/source refs.
    Lazy continuation lines stay within their bullet; fenced examples are ignored.
    """
    return _bullets_of(markdown, '关键事实')


def todos_of(markdown: str) -> list[str]:
    """Read current to-do text using the same fenced Markdown section rules."""
    return _bullets_of(markdown, '待办')


def _bullets_of(markdown, title):
    lines = list(_lines(markdown))
    section = _section(markdown, lines, title)
    if section is None:
        return []
    start, end = section
    facts = []
    current = []
    for offset, _, line, visible in lines:
        if not start <= offset < end:
            continue
        bullet = _BULLET.match(line) if visible else None
        if bullet or not visible or not line.strip() or _HEADING.match(line):
            if current:
                facts.append("\n".join(current).strip())
                current = []
            if bullet:
                current.append(bullet[1].strip())
        elif current:
            current.append(line)
    if current:
        facts.append("\n".join(current).strip())
    return facts


def _positive_revision(value):
    return type(value) is int and value > 0


def is_verified(records, documents, document_id) -> bool:
    """Read manual verification or any historical user edit without mutation."""
    document = documents.read(document_id)
    if document is None or not _positive_revision(document.get("revision")):
        return False
    marker = records.read("v2_verifications", document_id)
    if marker is not None:
        revision = marker.payload.get("document_revision")
        if _positive_revision(revision) and revision <= document["revision"]:
            return True
    return any(row.get("operation") == "user_edit"
               for row in documents.revisions(document_id))


def mark_verified(records, document_id, document_revision, *, expected_current_revision=None):
    """Monotonically mark an existing document revision in its own CAS sidecar."""
    if not _positive_revision(document_revision):
        raise ValueError("invalid_document_revision")
    with records.begin() as tx:
        document = tx.read("documents", document_id)
        if document is None:
            raise ValueError("document_not_found")
        current_revision = document.payload.get("revision")
        if expected_current_revision is not None and current_revision != expected_current_revision:
            raise ValueError('document_revision_conflict')
        if not _positive_revision(current_revision) or document_revision > current_revision:
            raise ValueError("document_revision_conflict")
        current = tx.read("v2_verifications", document_id)
        if current is not None:
            marked_revision = current.payload.get("document_revision")
            if _positive_revision(marked_revision) and marked_revision >= document_revision:
                return current
        saved = tx.put("v2_verifications", document_id,
                       {"document_revision": document_revision},
                       expected_revision=current.revision if current else 0)
        tx.commit()
    return saved
