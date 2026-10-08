from __future__ import annotations

import hashlib
from dataclasses import dataclass

from core.memory_core.ports import MemoryReaderPort
from core.product_core.memory_projection_authority_contract import (
    MemoryProjectionAuthoritySnapshot,
)
from core.project_skill_core.ports import ProjectSkillRepositoryPort


@dataclass(frozen=True, slots=True)
class CurrentMemoryProjectionAuthority:
    """Load one project snapshot from the already-resolved current authorities."""

    memory: MemoryReaderPort
    project_skills: ProjectSkillRepositoryPort
    memory_authority_identity: str
    project_skill_authority_identity: str

    @property
    def authority_identity(self) -> str:
        return (
            "progressive-memory-authority-v1:"
            f"{_required_text(self.memory_authority_identity, 'memory_authority_identity')}:"
            f"{_required_text(self.project_skill_authority_identity, 'project_skill_authority_identity')}"
        )

    def generation_token(self, project_id: str) -> str | None:
        _required_text(project_id, "project_id")
        memory_token = getattr(self.memory, "generation_token", None)
        skill_token = getattr(self.project_skills, "generation_token", None)
        if not callable(memory_token) or not callable(skill_token):
            return None
        resolved_memory_token = memory_token()
        resolved_skill_token = skill_token()
        if not _is_generation_token(resolved_memory_token) or not _is_generation_token(
            resolved_skill_token
        ):
            return None
        digest = hashlib.sha256()
        for value in (
            self.authority_identity,
            resolved_memory_token,
            resolved_skill_token,
        ):
            digest.update(value.encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()

    def load(self, project_id: str) -> MemoryProjectionAuthoritySnapshot:
        clean_project_id = _required_text(project_id, "project_id")
        return MemoryProjectionAuthoritySnapshot(
            project_id=clean_project_id,
            authority_identity=self.authority_identity,
            series_memories=tuple(
                dict(item) for item in self.memory.list("series_memory")
            ),
            scenarios=tuple(dict(item) for item in self.memory.list("scenario")),
            atoms=tuple(dict(item) for item in self.memory.list("atom")),
            project_skills=tuple(
                dict(item)
                for item in self.project_skills.list_by_project(clean_project_id)
            ),
            authority_generation_token=self.generation_token(clean_project_id),
        )


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _is_generation_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
