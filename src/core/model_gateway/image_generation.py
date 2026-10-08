"""Provider-neutral, non-persistent image-generation contracts.

The image bytes returned here are deliberately *not* an Asset authority.  A
caller must hand them to the existing controlled asset-ingestion flow before
they become durable user data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol


ImageGenerationCapability = Literal["image_generation"]


@dataclass(frozen=True, slots=True)
class ImageGenerationRequest:
    """One ephemeral provider invocation.

    ``prompt`` and ``input_image_b64`` are transient inputs.  Implementations
    must not persist them, log them, or put them in routing receipts.
    """

    capability: ImageGenerationCapability
    prompt: str
    parameters: Mapping[str, object]
    privacy_scope: str
    input_image_b64: str | None = None


@dataclass(frozen=True, slots=True)
class ImageGenerationResult:
    """Validated generated image bytes awaiting controlled asset ingestion."""

    capability: ImageGenerationCapability
    image_bytes: bytes
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    provider: str
    model: str


class ImageGenerationGatewayPort(Protocol):
    """Runs image generation without taking ownership of generated assets."""

    def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        """Return ephemeral bytes; no output becomes durable in this call."""
