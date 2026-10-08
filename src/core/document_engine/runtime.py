from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from core.storage_provider import ObjectStorePort, ObjectStoreRevisionError

from .ports import DocumentDraft


class DocumentRepositoryError(ValueError):
    """Raised when a document operation violates the runtime contract."""


class DocumentExpectedRevisionError(DocumentRepositoryError):
    """Raised when the caller saves against a stale document revision."""


@dataclass(slots=True)
class ObjectStoreDocumentRepository:
    """ObjectStore-backed editable Document repository for Phase 3 runtime smoke."""

    object_store: ObjectStorePort
    namespace_id: str = "default"
    now: str = "2026-06-29T18:00:00+08:00"

    def create(self, draft: DocumentDraft) -> Mapping[str, object]:
        source_refs = _normalize_source_refs(draft.source_refs)
        if not source_refs:
            raise DocumentRepositoryError("document draft requires source refs")
        document_id = _document_id(draft.document_type, draft.title, draft.markdown)
        if self.object_store.read("documents", document_id) is not None:
            raise DocumentRepositoryError(f"document already exists: {document_id}")
        blocks = _blocks_from_markdown(
            draft.markdown,
            source_refs=source_refs,
            origin="ai",
            edited_by_user=False,
        )
        snapshot = _source_snapshot(document_id, 1, source_refs, self.now)
        content_hash = _content_hash(draft.markdown)
        document = {
            "schema_version": "1.0.0",
            "id": document_id,
            "title": draft.title,
            "type": draft.document_type,
            "project_id": draft.project_id,
            "markdown_uri": f"crp://{self.namespace_id}/documents/{document_id}.md",
            "content_hash": content_hash,
            "source_refs": source_refs,
            "source_snapshot": snapshot,
            "blocks": blocks,
            "revision": 1,
            "status": "draft",
            "created_at": self.now,
            "updated_at": self.now,
        }
        revision = self._revision(
            document_id=document_id,
            revision=1,
            parent_revision=None,
            operation="create",
            author="system",
            reason="initial document generated from source snapshot",
            base_content_hash=None,
            new_content_hash=content_hash,
            source_snapshot=snapshot,
            changed_blocks=[
                {"operation": "add", "block_id": block["id"], "block": block}
                for block in blocks
            ],
            conflict={"status": "none", "conflict_blocks": [], "resolution": None},
        )
        self.object_store.write("documents", document_id, document, expected_revision=0)
        self.object_store.write(
            "document_revisions",
            _revision_object_id(document_id, 1),
            revision,
            expected_revision=0,
        )
        self._write_markdown(document_id, 1, draft.markdown)
        return dict(document)

    def create_or_replay_generated(self, draft: DocumentDraft) -> Mapping[str, object]:
        """Create a generated r1 once, then replay only its complete exact authority.

        A deterministic Document ID alone is not an idempotency proof: it omits
        the project and source snapshot. An existing ID is accepted only when
        the current document, create revision and r1 Markdown payload together
        prove that they were created from this draft.
        """

        source_refs = _normalize_source_refs(draft.source_refs)
        if not source_refs:
            raise DocumentRepositoryError("document draft requires source refs")
        document_id = _document_id(draft.document_type, draft.title, draft.markdown)
        current = self.read(document_id)
        if current is None:
            return self.create(draft)
        return self._validate_generated_replay(
            current=current,
            document_id=document_id,
            draft=draft,
            source_refs=source_refs,
        )

    def read(self, document_id: str) -> Mapping[str, object] | None:
        document = self.object_store.read("documents", document_id)
        return dict(document) if document is not None else None

    def list(self, *, include_archived: bool = False) -> tuple[Mapping[str, object], ...]:
        documents = (
            dict(item)
            for item in self.object_store.list("documents")
            if include_archived or item.get("status") != "archived"
        )
        return tuple(sorted(documents, key=lambda item: str(item.get("id", ""))))

    def save(
        self,
        document_id: str,
        markdown: str,
        source_refs: Sequence[Mapping[str, object]],
        expected_revision: int,
    ) -> Mapping[str, object]:
        return self.save_user_edit(
            document_id,
            markdown=markdown,
            expected_revision=expected_revision,
            source_refs=source_refs,
        )

    def save_user_edit(
        self,
        document_id: str,
        *,
        markdown: str,
        expected_revision: int,
        title: str | None = None,
        reason: str = "user edited document markdown",
        source_refs: Sequence[Mapping[str, object]] | None = None,
    ) -> Mapping[str, object]:
        current = self._current_editable_document(document_id, expected_revision)
        refs = (
            _normalize_source_refs(source_refs)
            if source_refs is not None
            else _normalize_source_refs(_required_list(current, "source_refs"))
        )
        blocks = _blocks_from_markdown(
            markdown,
            source_refs=(),
            origin="user",
            edited_by_user=True,
        )
        return self._commit_revision(
            current,
            markdown=markdown,
            blocks=blocks,
            source_refs=refs,
            operation="user_edit",
            author="user",
            reason=reason,
            status="draft",
            title=title,
            conflict={"status": "none", "conflict_blocks": [], "resolution": None},
        )

    def apply_ai_patch(
        self,
        document_id: str,
        *,
        blocks: Sequence[Mapping[str, object]],
        expected_revision: int,
        reason: str = "AI patch document blocks",
        source_refs: Sequence[Mapping[str, object]] | None = None,
    ) -> Mapping[str, object]:
        current = self._current_editable_document(document_id, expected_revision)
        incoming_blocks = [_normalize_block(block) for block in blocks]
        protected = _protected_block_ids(current)
        conflict_blocks = sorted(
            block["id"] for block in incoming_blocks if block["id"] in protected
        )
        refs = (
            _normalize_source_refs(source_refs)
            if source_refs is not None
            else _normalize_source_refs(_required_list(current, "source_refs"))
        )
        if conflict_blocks:
            unchanged_markdown = self.markdown(document_id, revision=expected_revision)
            if unchanged_markdown is None:
                unchanged_markdown = _markdown_from_blocks(_required_list(current, "blocks"))
            unchanged_blocks = [_normalize_block(block) for block in _required_list(current, "blocks")]
            return self._commit_revision(
                current,
                markdown=unchanged_markdown,
                blocks=unchanged_blocks,
                source_refs=refs,
                operation="ai_patch",
                author="system",
                reason=reason,
                status="conflicted",
                changed_blocks=[
                    {"operation": "update", "block_id": block["id"], "block": block}
                    for block in incoming_blocks
                ],
                conflict={
                    "status": "detected",
                    "conflict_blocks": conflict_blocks,
                    "resolution": None,
                },
            )

        merged_blocks = _merge_blocks(_required_list(current, "blocks"), incoming_blocks)
        markdown = _markdown_from_blocks(merged_blocks)
        return self._commit_revision(
            current,
            markdown=markdown,
            blocks=merged_blocks,
            source_refs=refs,
            operation="ai_patch",
            author="system",
            reason=reason,
            status="draft",
            changed_blocks=[
                {"operation": "update", "block_id": block["id"], "block": block}
                for block in incoming_blocks
            ],
            conflict={"status": "none", "conflict_blocks": [], "resolution": None},
        )

    def archive(
        self,
        document_id: str,
        *,
        expected_revision: int,
        reason: str = "user archived document",
    ) -> Mapping[str, object]:
        current = self._current_document(document_id, expected_revision)
        current_status = _required_str(current, "status")
        if current_status == "archived":
            raise DocumentRepositoryError("document is already archived")
        markdown = self.markdown(document_id, revision=expected_revision)
        if markdown is None:
            raise DocumentRepositoryError("document markdown not found")
        return self._commit_revision(
            current,
            markdown=markdown,
            blocks=_required_list(current, "blocks"),
            source_refs=_required_list(current, "source_refs"),
            operation="archive",
            author="user",
            reason=reason,
            status="archived",
            conflict={"status": "none", "conflict_blocks": [], "resolution": None},
            lifecycle={"archived_from_status": current_status},
        )

    def restore(
        self,
        document_id: str,
        *,
        expected_revision: int,
        reason: str = "user restored document",
    ) -> Mapping[str, object]:
        current = self._current_document(document_id, expected_revision)
        if current.get("status") != "archived":
            raise DocumentRepositoryError("document is not archived")
        restore_status = current.get("archived_from_status")
        if not isinstance(restore_status, str) or restore_status == "archived":
            restore_status = "draft"
        markdown = self.markdown(document_id, revision=expected_revision)
        if markdown is None:
            raise DocumentRepositoryError("document markdown not found")
        return self._commit_revision(
            current,
            markdown=markdown,
            blocks=_required_list(current, "blocks"),
            source_refs=_required_list(current, "source_refs"),
            operation="restore",
            author="user",
            reason=reason,
            status=restore_status,
            conflict={"status": "none", "conflict_blocks": [], "resolution": None},
            lifecycle={"restored_from_revision": expected_revision},
        )

    def revision(self, document_id: str, revision: int) -> Mapping[str, object] | None:
        item = self.object_store.read("document_revisions", _revision_object_id(document_id, revision))
        return dict(item) if item is not None else None

    def revisions(self, document_id: str) -> tuple[Mapping[str, object], ...]:
        items = [
            dict(item)
            for item in self.object_store.list("document_revisions")
            if item.get("document_id") == document_id
        ]
        return tuple(sorted(items, key=lambda item: int(item["revision"])))

    def markdown(self, document_id: str, *, revision: int | None = None) -> str | None:
        if revision is None:
            current = self.read(document_id)
            if current is None:
                return None
            revision = _required_int(current, "revision")
        item = self.object_store.read("document_markdown", _revision_object_id(document_id, revision))
        if item is None:
            return None
        markdown = item.get("markdown")
        if not isinstance(markdown, str):
            raise DocumentRepositoryError("stored markdown payload must contain markdown")
        return markdown

    def _current_document(self, document_id: str, expected_revision: int) -> Mapping[str, object]:
        current = self.read(document_id)
        if current is None:
            raise DocumentRepositoryError(f"document not found: {document_id}")
        if _required_int(current, "revision") != expected_revision:
            raise DocumentExpectedRevisionError(
                f"expected revision {expected_revision}, found {current['revision']}"
            )
        return current

    def _validate_generated_replay(
        self,
        *,
        current: Mapping[str, object],
        document_id: str,
        draft: DocumentDraft,
        source_refs: Sequence[Mapping[str, object]],
    ) -> Mapping[str, object]:
        """Return only an untouched r1 document that proves the supplied draft."""

        expected_hash = _content_hash(draft.markdown)
        expected_snapshot = _initial_source_snapshot(
            document_id=document_id,
            source_refs=source_refs,
        )
        expected_blocks = _blocks_from_markdown(
            draft.markdown,
            source_refs=source_refs,
            origin="ai",
            edited_by_user=False,
        )
        _require_replay_value(current, "id", document_id)
        _require_replay_value(current, "title", draft.title)
        _require_replay_value(current, "type", draft.document_type)
        if "project_id" not in current or current["project_id"] != draft.project_id:
            _raise_replay_conflict("project_id does not match the generated draft")
        _require_replay_value(current, "revision", 1)
        _require_replay_value(current, "status", "draft")
        _require_replay_value(current, "markdown_uri", f"crp://{self.namespace_id}/documents/{document_id}.md")
        _require_replay_value(current, "content_hash", expected_hash)
        _require_replay_value(current, "source_refs", list(source_refs))
        _require_initial_source_snapshot(current.get("source_snapshot"), expected_snapshot)
        _require_replay_value(current, "blocks", expected_blocks)

        revision = self.revision(document_id, 1)
        if revision is None:
            _raise_replay_conflict("r1 revision is missing")
        _require_replay_value(revision, "id", f"document-revision-{document_id}-r1")
        _require_replay_value(revision, "document_id", document_id)
        _require_replay_value(revision, "revision", 1)
        _require_replay_value(revision, "parent_revision", None)
        _require_replay_value(revision, "operation", "create")
        _require_replay_value(revision, "base_content_hash", None)
        _require_replay_value(revision, "new_content_hash", expected_hash)
        _require_initial_source_snapshot(revision.get("source_snapshot"), expected_snapshot)
        if revision.get("source_snapshot") != current.get("source_snapshot"):
            _raise_replay_conflict("current and r1 source snapshots differ")
        if self.revisions(document_id) != (revision,):
            _raise_replay_conflict("generated document has unexpected revision history")

        markdown = self.object_store.read("document_markdown", _revision_object_id(document_id, 1))
        if markdown is None:
            _raise_replay_conflict("r1 markdown payload is missing")
        _require_replay_value(markdown, "id", _revision_object_id(document_id, 1))
        _require_replay_value(markdown, "document_id", document_id)
        _require_replay_value(markdown, "revision", 1)
        _require_replay_value(markdown, "markdown", draft.markdown)
        _require_replay_value(markdown, "content_hash", expected_hash)
        return dict(current)

    def _current_editable_document(
        self, document_id: str, expected_revision: int
    ) -> Mapping[str, object]:
        current = self._current_document(document_id, expected_revision)
        if current.get("status") == "archived":
            raise DocumentRepositoryError("archived document must be restored before editing")
        return current

    def _commit_revision(
        self,
        current: Mapping[str, object],
        *,
        markdown: str,
        blocks: Sequence[Mapping[str, object]],
        source_refs: Sequence[Mapping[str, object]],
        operation: str,
        author: str,
        reason: str,
        status: str,
        conflict: Mapping[str, object],
        changed_blocks: Sequence[Mapping[str, object]] | None = None,
        title: str | None = None,
        lifecycle: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        document_id = _required_str(current, "id")
        parent_revision = _required_int(current, "revision")
        next_revision = parent_revision + 1
        refs = _normalize_source_refs(source_refs)
        snapshot = _source_snapshot(document_id, next_revision, refs, self.now)
        new_content_hash = _content_hash(markdown)
        normalized_blocks = [_normalize_block(block) for block in blocks]
        document = dict(current)
        next_title = title.strip() if isinstance(title, str) and title.strip() else _required_str(current, "title")
        document.update(
            {
                "title": next_title,
                "content_hash": new_content_hash,
                "source_refs": refs,
                "source_snapshot": snapshot,
                "blocks": normalized_blocks,
                "revision": next_revision,
                "status": status,
                "updated_at": self.now,
            }
        )
        document.pop("archived_from_status", None)
        if lifecycle is not None:
            document.update(dict(lifecycle))
        block_changes = list(changed_blocks) if changed_blocks is not None else [
            {"operation": "update", "block_id": block["id"], "block": block}
            for block in normalized_blocks
        ]
        revision = self._revision(
            document_id=document_id,
            revision=next_revision,
            parent_revision=parent_revision,
            operation=operation,
            author=author,
            reason=reason,
            base_content_hash=_required_str(current, "content_hash"),
            new_content_hash=new_content_hash,
            source_snapshot=snapshot,
            changed_blocks=block_changes,
            conflict=conflict,
        )
        try:
            self.object_store.write(
                "documents",
                document_id,
                document,
                expected_revision=parent_revision,
            )
        except ObjectStoreRevisionError as exc:
            raise DocumentExpectedRevisionError(str(exc)) from exc
        self.object_store.write(
            "document_revisions",
            _revision_object_id(document_id, next_revision),
            revision,
            expected_revision=0,
        )
        self._write_markdown(document_id, next_revision, markdown)
        return dict(document)

    def _revision(
        self,
        *,
        document_id: str,
        revision: int,
        parent_revision: int | None,
        operation: str,
        author: str,
        reason: str,
        base_content_hash: str | None,
        new_content_hash: str,
        source_snapshot: Mapping[str, object],
        changed_blocks: Sequence[Mapping[str, object]],
        conflict: Mapping[str, object],
    ) -> dict[str, object]:
        return {
            "schema_version": "1.0.0",
            "id": f"document-revision-{document_id}-r{revision}",
            "document_id": document_id,
            "revision": revision,
            "parent_revision": parent_revision,
            "operation": operation,
            "author": author,
            "reason": reason,
            "base_content_hash": base_content_hash,
            "new_content_hash": new_content_hash,
            "source_snapshot": dict(source_snapshot),
            "changed_blocks": [dict(change) for change in changed_blocks],
            "conflict": dict(conflict),
            "created_at": self.now,
        }

    def _write_markdown(self, document_id: str, revision: int, markdown: str) -> None:
        self.object_store.write(
            "document_markdown",
            _revision_object_id(document_id, revision),
            {
                "id": _revision_object_id(document_id, revision),
                "document_id": document_id,
                "revision": revision,
                "markdown": markdown,
                "content_hash": _content_hash(markdown),
                "created_at": self.now,
            },
            expected_revision=0,
        )


def _document_id(document_type: str, title: str, markdown: str) -> str:
    safe_type = _safe_id(document_type)
    digest = hashlib.sha256(f"{document_type}\n{title}\n{markdown}".encode("utf-8")).hexdigest()[:12]
    return f"document-{safe_type}-{digest}"


def _revision_object_id(document_id: str, revision: int) -> str:
    return f"{document_id}~r{revision}"


def _content_hash(markdown: str) -> str:
    return f"sha256:{hashlib.sha256(markdown.encode('utf-8')).hexdigest()}"


def _source_snapshot(
    document_id: str,
    revision: int,
    source_refs: Sequence[Mapping[str, object]],
    captured_at: str,
) -> dict[str, object]:
    refs = _normalize_source_refs(source_refs)
    snapshot_hash = hashlib.sha256(
        json.dumps(refs, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "snapshot_id": f"snapshot-{document_id}-r{revision}",
        "snapshot_hash": f"sha256:{snapshot_hash}",
        "source_refs": refs,
        "captured_at": captured_at,
    }


def _initial_source_snapshot(
    *, document_id: str, source_refs: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    """Build the draft-derived portion of a generated r1 source snapshot."""

    refs = _normalize_source_refs(source_refs)
    snapshot_hash = hashlib.sha256(
        json.dumps(refs, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "snapshot_id": f"snapshot-{document_id}-r1",
        "snapshot_hash": f"sha256:{snapshot_hash}",
        "source_refs": refs,
    }


def _require_initial_source_snapshot(value: object, expected: Mapping[str, object]) -> None:
    if not isinstance(value, Mapping):
        _raise_replay_conflict("source snapshot is missing or malformed")
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            _raise_replay_conflict(f"source snapshot {key} does not match the generated draft")
    captured_at = value.get("captured_at")
    if not isinstance(captured_at, str) or not captured_at:
        _raise_replay_conflict("source snapshot captured_at is missing or malformed")


def _require_replay_value(mapping: Mapping[str, object], key: str, expected: object) -> None:
    if key not in mapping or mapping[key] != expected:
        _raise_replay_conflict(f"{key} does not match the generated draft")


def _raise_replay_conflict(reason: str) -> None:
    raise DocumentRepositoryError(f"generated document replay conflict: {reason}")


def _normalize_source_refs(source_refs: Sequence[Mapping[str, object]] | object) -> list[dict[str, object]]:
    if not isinstance(source_refs, Sequence) or isinstance(source_refs, (str, bytes)):
        raise DocumentRepositoryError("source_refs must be a sequence")
    refs: list[dict[str, object]] = []
    for ref in source_refs:
        if not isinstance(ref, Mapping):
            raise DocumentRepositoryError("source ref must be an object")
        source_id = ref.get("source_id")
        locator = ref.get("locator")
        if not isinstance(source_id, str) or not source_id:
            raise DocumentRepositoryError("source ref requires source_id")
        if not isinstance(locator, str) or not locator:
            raise DocumentRepositoryError("source ref requires locator")
        normalized: dict[str, object] = {"source_id": source_id, "locator": locator}
        quote = ref.get("quote")
        if isinstance(quote, str):
            normalized["quote"] = quote
        refs.append(normalized)
    return refs


def _blocks_from_markdown(
    markdown: str,
    *,
    source_refs: Sequence[Mapping[str, object]],
    origin: str,
    edited_by_user: bool,
) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = []
    lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    if not lines:
        lines = [""]
    normalized_refs = _normalize_source_refs(source_refs) if source_refs else []
    for index, line in enumerate(lines, start=1):
        block_type = "heading" if line.startswith("#") else "paragraph"
        content = line.lstrip("#").strip() if block_type == "heading" else line
        block_refs = [] if edited_by_user else normalized_refs
        blocks.append(
            {
                "id": f"block-{index:03d}",
                "block_type": block_type,
                "origin": origin,
                "content": content,
                "source_refs": block_refs,
                "edited_by_user": edited_by_user,
                "lock_policy": "user_edit_protected" if edited_by_user else "source_required",
            }
        )
    return blocks


def _normalize_block(block: Mapping[str, object]) -> dict[str, object]:
    block_id = _required_str(block, "id")
    block_type = _required_str(block, "block_type")
    origin = _required_str(block, "origin")
    content = block.get("content")
    edited = block.get("edited_by_user")
    lock_policy = _required_str(block, "lock_policy")
    if not isinstance(content, str):
        raise DocumentRepositoryError(f"block {block_id} requires content")
    if not isinstance(edited, bool):
        raise DocumentRepositoryError(f"block {block_id} requires edited_by_user")
    refs = _normalize_source_refs(block.get("source_refs", []))
    return {
        "id": block_id,
        "block_type": block_type,
        "origin": origin,
        "content": content,
        "source_refs": refs,
        "edited_by_user": edited,
        "lock_policy": lock_policy,
    }


def _merge_blocks(
    current_blocks: Sequence[Mapping[str, object]],
    incoming_blocks: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    merged = [_normalize_block(block) for block in current_blocks]
    by_id = {block["id"]: index for index, block in enumerate(merged)}
    for incoming in incoming_blocks:
        if incoming["id"] in by_id:
            merged[by_id[incoming["id"]]] = dict(incoming)
        else:
            merged.append(dict(incoming))
    return merged


def _markdown_from_blocks(blocks: Sequence[Mapping[str, object]]) -> str:
    lines: list[str] = []
    for block in blocks:
        normalized = _normalize_block(block)
        content = str(normalized["content"])
        if normalized["block_type"] == "heading":
            lines.append(f"# {content}")
        else:
            lines.append(content)
    return "\n\n".join(lines)


def _protected_block_ids(document: Mapping[str, object]) -> set[str]:
    protected: set[str] = set()
    for block in _required_list(document, "blocks"):
        if not isinstance(block, Mapping):
            continue
        if block.get("edited_by_user") is True and block.get("lock_policy") == "user_edit_protected":
            block_id = block.get("id")
            if isinstance(block_id, str):
                protected.add(block_id)
    return protected


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise DocumentRepositoryError(f"{key} is required")
    return value


def _required_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise DocumentRepositoryError(f"{key} must be an integer")
    return value


def _required_list(mapping: Mapping[str, object], key: str) -> list[object]:
    value = mapping.get(key)
    if not isinstance(value, list):
        raise DocumentRepositoryError(f"{key} must be a list")
    return list(value)


def _safe_id(value: str) -> str:
    safe = re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-")
    return safe or "document"
