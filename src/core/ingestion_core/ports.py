from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol


SourceKind = Literal["text", "link", "collection", "file", "image", "audio", "video", "question", "other"]


@dataclass(frozen=True, slots=True)
class SourceSubmission:
    kind: SourceKind
    title: str
    original_path: str | None = None
    original_url: str | None = None
    content: str | None = None
    collection_urls: tuple[str, ...] | None = None
    display_name: str | None = None
    media_type: str | None = None
    size_bytes: int | None = None
    file_reference: str | None = None
    image_reference: str | None = None
    audio_reference: str | None = None
    video_reference: str | None = None
    width_px: int | None = None
    height_px: int | None = None
    duration_ms: int | None = None


class SourceRegistrarPort(Protocol):
    """Creates L0 before any derived processing starts."""

    def register(self, submission: SourceSubmission) -> Mapping[str, object]:
        """Return a Source-shaped mapping with an identifier and hash."""


class ExtractionSchedulerPort(Protocol):
    """Schedules extraction through JobRunner rather than inline UI state."""

    def schedule(self, source_id: str, job_type: str) -> str:
        """Return the persistent Job identifier."""
