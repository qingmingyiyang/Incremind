from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from core.storage_provider import ObjectStorePort, ObjectStoreRevisionError

from .codec import SourceManifestCodec, SourceManifestCodecError
from .models import SourceManifest


class SourceManifestArtifactError(ValueError):
    pass


class SourceManifestIdentityConflict(SourceManifestArtifactError):
    pass


@dataclass(frozen=True)
class SourceManifestArtifact:
    namespace_id: str
    project_id: str
    manifest_id: str
    public_ref: str
    revision: str
    manifest: SourceManifest


class SourceManifestArtifactRepository:
    """Immutable manifest authority, deliberately independent of API/runtime layers."""

    collection = "source_manifests"
    _SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")

    def __init__(self, object_store: ObjectStorePort, *, namespace_id: str) -> None:
        self._object_store = object_store
        self._scope(namespace_id, "namespace_id")
        if getattr(object_store, "namespace_id", None) != namespace_id:
            raise SourceManifestArtifactError("source manifest namespace does not match its object store")
        self.namespace_id = namespace_id

    def public_ref(self, *, project_id: str, manifest_id: str) -> str:
        self._scope(project_id, "project_id")
        self._scope(manifest_id, "manifest_id")
        return f"crp://{self.namespace_id}/source-manifests/projects/{project_id}/{manifest_id}"

    def put(
        self,
        *,
        project_id: str,
        manifest_id: str,
        manifest: SourceManifest,
    ) -> SourceManifestArtifact:
        public_ref = self.public_ref(project_id=project_id, manifest_id=manifest_id)
        canonical = SourceManifestCodec.encode(manifest)
        artifact = {
            "namespace_id": self.namespace_id,
            "project_id": project_id,
            "manifest_id": manifest_id,
            "public_ref": public_ref,
            "revision": "r1",
            "manifest": canonical,
        }
        object_id = self._object_id(project_id, manifest_id)
        existing = self._object_store.read(self.collection, object_id)
        if existing is not None:
            return self._replay_or_conflict(existing, manifest)
        try:
            revision = self._object_store.write(self.collection, object_id, artifact, expected_revision=0)
            if revision != 1:
                raise SourceManifestArtifactError("immutable source manifest must start at revision 1")
        except ObjectStoreRevisionError:
            existing = self._object_store.read(self.collection, object_id)
            if existing is None:
                raise
            return self._replay_or_conflict(existing, manifest)
        return SourceManifestArtifact(self.namespace_id, project_id, manifest_id, public_ref, "r1", manifest)

    def resolve_source_ref(self, *, source_ref: str, project_id: str) -> SourceManifestArtifact:
        prefix = f"crp://{self.namespace_id}/source-manifests/projects/{project_id}/"
        if not source_ref.startswith(prefix):
            raise SourceManifestArtifactError("source_ref is outside namespace/project source-manifests scope")
        manifest_id = source_ref.removeprefix(prefix)
        if "/" in manifest_id or not self._SCOPE.fullmatch(manifest_id):
            raise SourceManifestArtifactError("source_ref does not identify one source manifest")
        record = self._object_store.read(self.collection, self._object_id(project_id, manifest_id))
        if record is None:
            raise SourceManifestArtifactError("source manifest was not found")
        artifact = self._record(record)
        if artifact.namespace_id != self.namespace_id or artifact.project_id != project_id or artifact.public_ref != source_ref:
            raise SourceManifestArtifactError("source manifest scope verification failed")
        return artifact

    def _replay_or_conflict(
        self,
        existing: Mapping[str, object],
        manifest: SourceManifest,
    ) -> SourceManifestArtifact:
        existing_artifact = self._record(existing)
        # A v1.0.0 artifact remains immutable and readable.  Replaying its
        # decoded DTO must not turn a read into an implicit write-upgrade.
        if existing_artifact.manifest != manifest:
            raise SourceManifestIdentityConflict("source manifest identity conflicts with existing immutable artifact")
        return existing_artifact

    def _record(self, value: Mapping[str, object]) -> SourceManifestArtifact:
        if set(value) != {"namespace_id", "project_id", "manifest_id", "public_ref", "revision", "manifest"}:
            raise SourceManifestArtifactError("stored source manifest artifact fields are invalid")
        namespace_id, project_id, manifest_id, public_ref, revision, raw_manifest = (
            value[name]
            for name in ("namespace_id", "project_id", "manifest_id", "public_ref", "revision", "manifest")
        )
        if not all(isinstance(item, str) for item in (namespace_id, project_id, manifest_id, public_ref, revision)) or not isinstance(raw_manifest, Mapping):
            raise SourceManifestArtifactError("stored source manifest artifact is invalid")
        if revision != "r1":
            raise SourceManifestArtifactError("stored source manifest revision is invalid")
        try:
            manifest = SourceManifestCodec.decode(raw_manifest)
        except SourceManifestCodecError as error:
            raise SourceManifestArtifactError("stored source manifest codec validation failed") from error
        if namespace_id != self.namespace_id:
            raise SourceManifestArtifactError("stored source manifest namespace is invalid")
        expected_ref = self.public_ref(project_id=project_id, manifest_id=manifest_id)
        if public_ref != expected_ref:
            raise SourceManifestArtifactError("stored source manifest public ref is invalid")
        return SourceManifestArtifact(namespace_id, project_id, manifest_id, public_ref, revision, manifest)

    def _object_id(self, project_id: str, manifest_id: str) -> str:
        self._scope(project_id, "project_id")
        self._scope(manifest_id, "manifest_id")
        object_id = f"{project_id}~{manifest_id}"
        if len(object_id) > 128:
            raise SourceManifestArtifactError("source manifest storage identity is too long")
        return object_id

    def _scope(self, value: str, label: str) -> None:
        if not self._SCOPE.fullmatch(value):
            raise SourceManifestArtifactError(f"{label} is invalid")
