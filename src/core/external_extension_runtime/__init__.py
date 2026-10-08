"""Core-owned runtime boundary for external extension intake Effects."""

from .artifact_evidence import (
    ArtifactEvidence,
    ArtifactEvidenceError,
    ImmutableQuarantineArtifactStore,
    artifact_receipt_ref,
)
from .fact_store import (
    ExternalExtensionFactConflict,
    ExternalExtensionFactError,
    ExternalExtensionFactStore,
)
from .lifecycle import (
    ACQUIRE_EFFECT_KIND,
    RESOLVE_EFFECT_KIND,
    ArtifactAcquirer,
    ExternalExtensionAcquireHandler,
    ExternalExtensionResolveHandler,
    SourceResolver,
    build_acquire_effect_intent,
    build_resolve_effect_intent,
)

__all__ = [
    "ACQUIRE_EFFECT_KIND",
    "RESOLVE_EFFECT_KIND",
    "ArtifactAcquirer",
    "ArtifactEvidence",
    "ArtifactEvidenceError",
    "ExternalExtensionAcquireHandler",
    "ExternalExtensionFactConflict",
    "ExternalExtensionFactError",
    "ExternalExtensionFactStore",
    "ExternalExtensionResolveHandler",
    "ImmutableQuarantineArtifactStore",
    "SourceResolver",
    "build_acquire_effect_intent",
    "build_resolve_effect_intent",
    "artifact_receipt_ref",
]
