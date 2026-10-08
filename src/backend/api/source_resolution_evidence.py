from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re

from core.storage_provider import ObjectStorePort, ObjectStoreRevisionError


_PORTABLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,63}$")


class SourceResolutionEvidenceError(ValueError):
    """Stable create-only source resolution evidence failure."""


@dataclass(frozen=True, slots=True)
class SourceResolutionEvidence:
    public_ref: str
    revision: str


class SourceResolutionEvidenceRepository:
    """Platform-neutral evidence authority shared by governed source providers."""

    collection = "source_resolution_evidence"

    def __init__(self, object_store: ObjectStorePort, *, namespace_id: str) -> None:
        if (
            getattr(object_store, "namespace_id", None) != namespace_id
            or not _PORTABLE.fullmatch(namespace_id)
        ):
            raise ValueError("source resolution evidence namespace is invalid")
        self._store = object_store
        self.namespace_id = namespace_id

    def put(
        self,
        *,
        project_id: str,
        evidence_id: str,
        kind: str,
        payload: Mapping[str, object],
    ) -> SourceResolutionEvidence:
        if (
            not _PORTABLE.fullmatch(project_id)
            or not _PORTABLE.fullmatch(evidence_id)
            or not _PORTABLE.fullmatch(kind)
        ):
            raise ValueError("source resolution evidence identity is invalid")
        public_ref = (
            f"crp://{self.namespace_id}/source-resolution-evidence/"
            f"projects/{project_id}/{evidence_id}"
        )
        record = {
            "schema_version": "1.0.0",
            "namespace_id": self.namespace_id,
            "project_id": project_id,
            "evidence_id": evidence_id,
            "public_ref": public_ref,
            "revision": "r1",
            "kind": kind,
            "payload": dict(payload),
        }
        object_id = f"{project_id}~{evidence_id}"
        existing = self._store.read(self.collection, object_id)
        if existing is not None:
            if dict(existing) != record:
                raise SourceResolutionEvidenceError("metadata_identity_conflict")
            return SourceResolutionEvidence(public_ref, "r1")
        try:
            revision = self._store.write(
                self.collection, object_id, record, expected_revision=0
            )
        except ObjectStoreRevisionError:
            existing = self._store.read(self.collection, object_id)
            if existing is None or dict(existing) != record:
                raise SourceResolutionEvidenceError("metadata_identity_conflict")
            return SourceResolutionEvidence(public_ref, "r1")
        if revision != 1:
            raise SourceResolutionEvidenceError("metadata_evidence_revision_invalid")
        return SourceResolutionEvidence(public_ref, "r1")
