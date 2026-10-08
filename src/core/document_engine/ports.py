from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DocumentDraft:
    title: str
    document_type: str
    markdown: str
    source_refs: tuple[Mapping[str, object], ...]
    project_id: str | None = None


class DocumentRepositoryPort(Protocol):
    """Stores editable documents using optimistic revisions."""

    def create(self, draft: DocumentDraft) -> Mapping[str, object]:
        """Create revision one."""

    def create_or_replay_generated(self, draft: DocumentDraft) -> Mapping[str, object]:
        """Create a generated revision one or fail-closed replay its exact authority."""

    def read(self, document_id: str) -> Mapping[str, object] | None:
        """Read the current revision."""

    def list(self, *, include_archived: bool = False) -> Sequence[Mapping[str, object]]:
        """List current documents, hiding archived documents by default."""

    def save(
        self,
        document_id: str,
        markdown: str,
        source_refs: Sequence[Mapping[str, object]],
        expected_revision: int,
    ) -> Mapping[str, object]:
        """Save only when expected_revision matches."""

    def archive(self, document_id: str, *, expected_revision: int) -> Mapping[str, object]:
        """Append an archived lifecycle revision."""

    def restore(self, document_id: str, *, expected_revision: int) -> Mapping[str, object]:
        """Append a restored lifecycle revision."""
