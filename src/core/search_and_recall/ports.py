from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RecallQuery:
    text: str
    project_id: str | None
    layers: tuple[str, ...]
    allowed_trust_statuses: tuple[str, ...]
    limit: int = 12


@dataclass(frozen=True, slots=True)
class RecallHit:
    object_id: str
    layer: str
    content: str
    source_refs: tuple[str, ...]
    trust_status: str
    score: float


@dataclass(frozen=True, slots=True)
class RecallIndexEntry:
    object_id: str
    project_id: str
    layer: str
    content: str
    source_refs: tuple[str, ...]
    trust_status: str
    base_score: float = 0.5


class RecallPort(Protocol):
    """Returns evidence packages and never persists answers."""

    def recall(self, query: RecallQuery) -> tuple[RecallHit, ...]:
        """Return ordered, traceable evidence."""
