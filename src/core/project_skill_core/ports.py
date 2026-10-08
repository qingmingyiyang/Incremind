from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ProjectSkillUpdate:
    project_id: str
    markdown: str
    structured: Mapping[str, object]
    expected_revision: int
    reason: str
    transition_kind: str = "revision_saved"
    actor: str = "system"
    confirmation_kind: str = "unspecified"
    source_revision: int | None = None


class ProjectSkillRepositoryPort(Protocol):
    """Reads and versions user-controlled Project Skills."""

    def load(self, project_id: str) -> Mapping[str, object] | None:
        """Load Markdown and structured index together."""

    def get(self, skill_id: str) -> Mapping[str, object] | None:
        """Load one current Project Skill by its stable identity."""

    def list_by_project(self, project_id: str) -> tuple[Mapping[str, object], ...]:
        """List current Project Skills for one project in stable identity order."""

    def list_all(self) -> tuple[Mapping[str, object], ...]:
        """List all current Project Skills in stable identity order."""

    def save(self, update: ProjectSkillUpdate) -> Mapping[str, object]:
        """Save an auditable revision without silently overwriting user edits."""

    def markdown(self, project_id: str, *, revision: int | None = None) -> str | None:
        """Read current or historical Markdown."""

    def structured(
        self,
        project_id: str,
        *,
        revision: int | None = None,
    ) -> Mapping[str, object] | None:
        """Read current or historical structured content."""

    def revisions(self, project_id: str) -> tuple[Mapping[str, object], ...]:
        """List immutable revision evidence."""

    def rollback(
        self,
        project_id: str,
        *,
        target_revision: int,
        expected_revision: int,
        reason: str,
    ) -> Mapping[str, object]:
        """Create a new user-confirmed revision from a prior active snapshot."""
