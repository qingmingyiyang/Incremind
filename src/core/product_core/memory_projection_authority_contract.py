"""Pure domain contract for coherent Memory Projection authority snapshots.

This module is intentionally independent of the rebuild Job lifecycle.  It is
shared by authority readers and Effect-v2 admission/execution paths.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from core.product_core.memory_projection_builder import (
    build_r0_r1_memory_projection,
)
from core.product_core.memory_projection_contract import (
    MemoryRetrievalProjection,
)


class MemoryProjectionAuthoritySnapshotPort(Protocol):
    def load(self, project_id: str) -> "MemoryProjectionAuthoritySnapshot":
        """Return one coherent current authority snapshot for a project."""


@dataclass(frozen=True, slots=True)
class MemoryProjectionAuthoritySnapshot:
    project_id: str
    authority_identity: str
    series_memories: tuple[Mapping[str, object], ...]
    scenarios: tuple[Mapping[str, object], ...]
    atoms: tuple[Mapping[str, object], ...]
    project_skills: tuple[Mapping[str, object], ...]
    authority_generation_token: str | None = None

    def build(self, *, generated_at: str) -> MemoryRetrievalProjection:
        return build_r0_r1_memory_projection(
            project_id=self.project_id,
            authority_identity=self.authority_identity,
            series_memories=self.series_memories,
            scenarios=self.scenarios,
            atoms=self.atoms,
            project_skills=self.project_skills,
            generated_at=generated_at,
        )


def authority_snapshot_fingerprint(
    snapshot: MemoryProjectionAuthoritySnapshot,
) -> str:
    """Return the stable fingerprint for authority content, not generation time."""

    return snapshot.build(
        generated_at="2000-01-01T00:00:00+00:00",
    ).authority_fingerprint
