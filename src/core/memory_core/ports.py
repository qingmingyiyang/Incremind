from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol


class MemoryReaderPort(Protocol):
    """Reads traceable memory objects without choosing a storage engine."""

    def get(self, layer: str, object_id: str) -> Mapping[str, object] | None:
        """Read one L0, L1, L2, or L3 object."""

    def list(self, layer: str) -> Sequence[Mapping[str, object]]:
        """Read current projections for one memory layer."""

    def list_by_source(self, source_id: str) -> Sequence[Mapping[str, object]]:
        """Return derived memory objects for a Source."""

    def list_by_project(self, project_id: str) -> Sequence[Mapping[str, object]]:
        """Return published same-project memory objects in L3 → L2 → L1 order."""

    def staged(self, layer: str, object_id: str) -> Mapping[str, object] | None:
        """Read one staged candidate."""


class MemoryWriterPort(Protocol):
    """Publishes candidates and explicit trust transitions."""

    def save_candidate(self, layer: str, payload: Mapping[str, object]) -> str:
        """Persist a candidate and return its stable identifier."""

    def publish(self, layer: str, payload: Mapping[str, object]) -> str:
        """Publish a formal memory object after the owning Job reaches the publish step."""

    def set_trust_status(self, object_id: str, trust_status: str, reason: str) -> None:
        """Record an auditable trust transition."""
