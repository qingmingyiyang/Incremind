"""Shared immutable contract for Effect-v2 Memory Candidate execution."""

from __future__ import annotations


EFFECT_KIND = "memory_candidate_from_source_output"
INTENT_SCHEMA = "candidate-memory-job-execution-v2"
RECEIPT_KIND = "candidate-memory-job-execution.receipt"
RECEIPT_SCHEMA = "candidate-memory-job-execution-receipt-v2"
RECEIPT_TABLE = "candidate_effect_domain_receipt"


def candidate_evidence_revision(
    prefix: str, *, source_id: str, content_read_id: str, text_sha256: str,
) -> str:
    """Return the stable identity of the content read, excluding derived markers."""

    for label, value in (
        ("prefix", prefix), ("source_id", source_id), ("content_read_id", content_read_id),
    ):
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or any(character.isspace() or ord(character) < 32 for character in value)
        ):
            raise ValueError(f"{label} must be a non-empty opaque token")
    if (
        not isinstance(text_sha256, str)
        or len(text_sha256) != 64
        or any(character not in "0123456789abcdef" for character in text_sha256)
    ):
        raise ValueError("text_sha256 must be a lowercase SHA-256 evidence token")
    return f"{prefix}:sha256:{text_sha256}"


__all__ = (
    "EFFECT_KIND",
    "INTENT_SCHEMA",
    "RECEIPT_KIND",
    "RECEIPT_SCHEMA",
    "RECEIPT_TABLE",
    "candidate_evidence_revision",
)
