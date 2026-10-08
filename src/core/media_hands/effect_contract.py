"""Shared immutable contract for Media Hands Effect-v2 execution."""

from __future__ import annotations


EFFECT_KIND = "media_hands_job_execution"
INTENT_SCHEMA = "media-hands-job-execution/v2"
RECEIPT_KIND = "media-hands-job-execution.receipt"
RECEIPT_SCHEMA = "media-hands-job-execution-receipt/v2"


def provider_revision_identity(provider_id: str, provider_revision: str) -> str:
    """Return the complete provider identity frozen into an Effect revision set."""

    for label, value in (("provider_id", provider_id), ("provider_revision", provider_revision)):
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or any(character.isspace() or ord(character) < 32 for character in value)
            or "/" in value
        ):
            raise ValueError(f"{label} must be a non-empty opaque provider token")
    return f"media-hands-provider/{provider_id}/{provider_revision}"


__all__ = (
    "EFFECT_KIND",
    "INTENT_SCHEMA",
    "RECEIPT_KIND",
    "RECEIPT_SCHEMA",
    "provider_revision_identity",
)
