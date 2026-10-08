from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from core.product_core.memory_projection_contract import (
    GENERATOR_POLICY_ID,
    MemoryRetrievalProjection,
    PROJECTION_VERSION,
    serialize_memory_retrieval_projection,
)
from core.product_core.object_store_port import (
    RevisionedProductObjectStorePort,
    is_revision_conflict,
)


MANIFEST_COLLECTION = "memory_retrieval_projection_manifests"
ARTIFACT_COLLECTION = "memory_retrieval_projection_items"
FAILURE_COLLECTION = "memory_retrieval_projection_failures"
GENERATION_COLLECTION = "memory_retrieval_projection_generations"
REPOSITORY_SCHEMA_VERSION = "1.0.0"
_FINGERPRINT_LENGTH = 64


class MemoryProjectionRepositoryError(ValueError):
    """Base error for the non-authoritative projection repository."""


class MemoryProjectionRepositoryConflict(MemoryProjectionRepositoryError):
    """Raised when a concurrent manifest transition wins CAS."""


class MemoryProjectionRepositoryIntegrityError(MemoryProjectionRepositoryError):
    """Raised when a derived artifact or manifest cannot be trusted."""


@dataclass(frozen=True, slots=True)
class ProjectionReadResult:
    status: str
    fallback_to_authority: bool
    reason_code: str
    projection: Mapping[str, object] | None
    manifest: Mapping[str, object] | None


@dataclass(slots=True)
class ObjectStoreMemoryProjectionRepository:
    object_store: RevisionedProductObjectStorePort

    def begin_rebuild(
        self,
        *,
        project_id: str,
        authority_identity: str,
        authority_fingerprint: str,
        job_id: str,
        updated_at: str,
    ) -> Mapping[str, object]:
        project_id = _required_text(project_id, "project_id")
        authority_identity = _required_text(
            authority_identity,
            "authority_identity",
        )
        _require_fingerprint(authority_fingerprint)
        job_id = _required_text(job_id, "job_id")
        updated_at = _required_text(updated_at, "updated_at")
        current = self._read_manifest(project_id, repair_interrupted=True)
        if (
            current is not None
            and current.get("authority_identity") == authority_identity
            and current.get("requested_authority_fingerprint")
            == authority_fingerprint
            and current.get("projection_version") == PROJECTION_VERSION
            and current.get("generator_policy_id") == GENERATOR_POLICY_ID
            and current.get("status") in {"ready", "rebuilding"}
        ):
            if current.get("status") == "ready":
                return current
            if current.get("job_id") != job_id:
                raise MemoryProjectionRepositoryConflict(
                    "projection fingerprint is already rebuilding under another job"
                )
            return current
        payload = _next_manifest(
            current,
            project_id=project_id,
            authority_identity=authority_identity,
            requested_authority_fingerprint=authority_fingerprint,
            status="rebuilding",
            job_id=job_id,
            failure_code=None,
            updated_at=updated_at,
        )
        return self._write_manifest(current, payload)

    def mark_stale(
        self,
        *,
        project_id: str,
        authority_identity: str,
        authority_fingerprint: str,
        updated_at: str,
    ) -> Mapping[str, object]:
        project_id = _required_text(project_id, "project_id")
        authority_identity = _required_text(
            authority_identity,
            "authority_identity",
        )
        _require_fingerprint(authority_fingerprint)
        current = self._read_manifest(project_id, repair_interrupted=True)
        if (
            current is not None
            and current.get("status") == "ready"
            and current.get("authority_identity") == authority_identity
            and current.get("active_authority_fingerprint")
            == authority_fingerprint
            and current.get("projection_version") == PROJECTION_VERSION
            and current.get("generator_policy_id") == GENERATOR_POLICY_ID
        ):
            return current
        payload = _next_manifest(
            current,
            project_id=project_id,
            authority_identity=authority_identity,
            requested_authority_fingerprint=authority_fingerprint,
            status="stale",
            job_id=None,
            failure_code=None,
            updated_at=_required_text(updated_at, "updated_at"),
        )
        return self._write_manifest(current, payload)

    def stage_projection(
        self,
        projection: MemoryRetrievalProjection | Mapping[str, object],
    ) -> str:
        payload = _projection_payload(projection)
        project_id = _required_mapping_text(payload, "project_id")
        authority_identity = _required_mapping_text(
            payload,
            "authority_identity",
        )
        authority_fingerprint = _required_mapping_text(
            payload,
            "authority_fingerprint",
        )
        _require_fingerprint(authority_fingerprint)
        _validate_projection_payload(
            payload,
            project_id=project_id,
            authority_identity=authority_identity,
            authority_fingerprint=authority_fingerprint,
        )
        artifact_id = projection_artifact_id(
            project_id,
            authority_identity,
            authority_fingerprint,
        )
        artifact = {
            "schema_version": REPOSITORY_SCHEMA_VERSION,
            "artifact_id": artifact_id,
            "project_id": project_id,
            "authority_identity": authority_identity,
            "authority_fingerprint": authority_fingerprint,
            "projection_version": PROJECTION_VERSION,
            "generator_policy_id": GENERATOR_POLICY_ID,
            "projection_digest": _digest_payload(payload),
            "metrics": _projection_metrics(payload),
            "projection": payload,
            "created_at": _required_mapping_text(payload, "generated_at"),
            "derived": True,
            "business_authority": False,
        }
        existing = self.object_store.read(ARTIFACT_COLLECTION, artifact_id)
        storage_revision = self.object_store.revision(
            ARTIFACT_COLLECTION,
            artifact_id,
        )
        if existing is not None:
            if dict(existing) != artifact:
                raise MemoryProjectionRepositoryIntegrityError(
                    "immutable projection artifact conflicts with current fingerprint"
                )
            if storage_revision == 0:
                self._write_immutable_artifact(artifact_id, artifact)
            elif storage_revision != 1:
                raise MemoryProjectionRepositoryIntegrityError(
                    "immutable projection artifact revision drifted"
                )
            return artifact_id
        if storage_revision != 0:
            raise MemoryProjectionRepositoryIntegrityError(
                "projection artifact metadata exists without payload"
            )
        self._write_immutable_artifact(artifact_id, artifact)
        return artifact_id

    def activate_staged(
        self,
        *,
        project_id: str,
        authority_identity: str,
        authority_fingerprint: str,
        job_id: str,
        artifact_id: str,
        updated_at: str,
    ) -> Mapping[str, object]:
        project_id = _required_text(project_id, "project_id")
        authority_identity = _required_text(
            authority_identity,
            "authority_identity",
        )
        _require_fingerprint(authority_fingerprint)
        job_id = _required_text(job_id, "job_id")
        artifact_id = _required_text(artifact_id, "artifact_id")
        expected_artifact_id = projection_artifact_id(
            project_id,
            authority_identity,
            authority_fingerprint,
        )
        if artifact_id != expected_artifact_id:
            raise MemoryProjectionRepositoryIntegrityError(
                "projection artifact identity does not match activation request"
            )
        artifact = self._read_artifact(
            artifact_id,
            project_id=project_id,
            authority_identity=authority_identity,
            authority_fingerprint=authority_fingerprint,
        )
        current = self._read_manifest(project_id, repair_interrupted=True)
        if current is None:
            raise MemoryProjectionRepositoryConflict(
                "projection activation requires rebuilding manifest"
            )
        if (
            current.get("authority_identity") != authority_identity
            or current.get("requested_authority_fingerprint")
            != authority_fingerprint
            or current.get("job_id") != job_id
        ):
            if (
                current.get("status") == "ready"
                and current.get("active_artifact_id") == artifact_id
                and current.get("active_authority_fingerprint")
                == authority_fingerprint
            ):
                return current
            raise MemoryProjectionRepositoryConflict(
                "projection activation lost current manifest ownership"
            )
        payload = _next_manifest(
            current,
            project_id=project_id,
            authority_identity=authority_identity,
            requested_authority_fingerprint=authority_fingerprint,
            status="ready",
            job_id=job_id,
            failure_code=None,
            updated_at=_required_text(updated_at, "updated_at"),
            active_artifact_id=artifact_id,
            active_authority_fingerprint=authority_fingerprint,
            active_metrics=artifact["metrics"],
        )
        return self._write_manifest(current, payload)

    def record_failure(
        self,
        *,
        project_id: str,
        authority_identity: str,
        authority_fingerprint: str,
        job_id: str,
        attempt: int,
        failure_code: str,
        recorded_at: str,
    ) -> Mapping[str, object]:
        project_id = _required_text(project_id, "project_id")
        authority_identity = _required_text(
            authority_identity,
            "authority_identity",
        )
        _require_fingerprint(authority_fingerprint)
        job_id = _required_text(job_id, "job_id")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise MemoryProjectionRepositoryError(
                "failure attempt must be a positive integer"
            )
        failure_code = _required_text(failure_code, "failure_code")
        if len(failure_code) > 80:
            raise MemoryProjectionRepositoryError(
                "failure_code exceeds repository limit"
            )
        recorded_at = _required_text(recorded_at, "recorded_at")
        failure_id = _stable_id(
            "projection-failure",
            project_id,
            authority_identity,
            authority_fingerprint,
            job_id,
            str(attempt),
            failure_code,
        )
        failure = {
            "schema_version": REPOSITORY_SCHEMA_VERSION,
            "failure_id": failure_id,
            "project_id": project_id,
            "authority_identity": authority_identity,
            "authority_fingerprint": authority_fingerprint,
            "job_id": job_id,
            "attempt": attempt,
            "failure_code": failure_code,
            "recorded_at": recorded_at,
            "contains_projection_body": False,
        }
        existing_failure = self.object_store.read(
            FAILURE_COLLECTION,
            failure_id,
        )
        if existing_failure is None:
            try:
                self.object_store.write(
                    FAILURE_COLLECTION,
                    failure_id,
                    failure,
                    expected_revision=0,
                )
            except Exception as error:
                if not is_revision_conflict(self.object_store, error):
                    raise
                raise MemoryProjectionRepositoryConflict(
                    "projection failure record lost create race"
                ) from error
        elif dict(existing_failure) != failure:
            raise MemoryProjectionRepositoryIntegrityError(
                "projection failure identity conflicts"
            )

        current = self._read_manifest(project_id, repair_interrupted=True)
        if current is None:
            return failure
        if (
            current.get("authority_identity") != authority_identity
            or current.get("requested_authority_fingerprint")
            != authority_fingerprint
            or current.get("job_id") != job_id
        ):
            return failure
        payload = _next_manifest(
            current,
            project_id=project_id,
            authority_identity=authority_identity,
            requested_authority_fingerprint=authority_fingerprint,
            status="failed",
            job_id=job_id,
            failure_code=failure_code,
            updated_at=recorded_at,
        )
        self._write_manifest(current, payload)
        return failure

    def load_current(
        self,
        *,
        project_id: str,
        authority_identity: str,
        authority_fingerprint: str,
    ) -> ProjectionReadResult:
        project_id = _required_text(project_id, "project_id")
        authority_identity = _required_text(
            authority_identity,
            "authority_identity",
        )
        _require_fingerprint(authority_fingerprint)
        try:
            manifest = self._read_manifest(
                project_id,
                repair_interrupted=False,
            )
        except MemoryProjectionRepositoryIntegrityError:
            return ProjectionReadResult(
                status="corrupt",
                fallback_to_authority=True,
                reason_code="projection_manifest_corrupt",
                projection=None,
                manifest=None,
            )
        if manifest is None:
            return ProjectionReadResult(
                status="missing",
                fallback_to_authority=True,
                reason_code="projection_manifest_missing",
                projection=None,
                manifest=None,
            )
        if manifest.get("authority_identity") != authority_identity:
            return _stale_result(manifest, "authority_identity_changed")
        if (
            manifest.get("projection_version") != PROJECTION_VERSION
            or manifest.get("generator_policy_id") != GENERATOR_POLICY_ID
        ):
            return _stale_result(manifest, "projection_policy_changed")
        if manifest.get("status") != "ready":
            return _stale_result(
                manifest,
                f"projection_{manifest.get('status')}",
            )
        if (
            manifest.get("requested_authority_fingerprint")
            != authority_fingerprint
            or manifest.get("active_authority_fingerprint")
            != authority_fingerprint
        ):
            return _stale_result(manifest, "authority_fingerprint_changed")
        artifact_id = manifest.get("active_artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            return ProjectionReadResult(
                status="corrupt",
                fallback_to_authority=True,
                reason_code="projection_active_artifact_missing",
                projection=None,
                manifest=manifest,
            )
        try:
            artifact = self._read_artifact(
                artifact_id,
                project_id=project_id,
                authority_identity=authority_identity,
                authority_fingerprint=authority_fingerprint,
            )
        except MemoryProjectionRepositoryIntegrityError:
            return ProjectionReadResult(
                status="corrupt",
                fallback_to_authority=True,
                reason_code="projection_artifact_corrupt",
                projection=None,
                manifest=manifest,
            )
        projection = artifact.get("projection")
        if not isinstance(projection, Mapping):
            return ProjectionReadResult(
                status="corrupt",
                fallback_to_authority=True,
                reason_code="projection_payload_missing",
                projection=None,
                manifest=manifest,
            )
        return ProjectionReadResult(
            status="fresh",
            fallback_to_authority=False,
            reason_code="projection_fingerprint_current",
            projection=dict(projection),
            manifest=manifest,
        )

    def manifests(self) -> tuple[Mapping[str, object], ...]:
        manifests: list[Mapping[str, object]] = []
        for raw in self.object_store.list(MANIFEST_COLLECTION):
            project_id = raw.get("project_id")
            if not isinstance(project_id, str):
                raise MemoryProjectionRepositoryIntegrityError(
                    "projection manifest requires project_id"
                )
            manifest = self._read_manifest(project_id, repair_interrupted=True)
            if manifest is not None:
                manifests.append(manifest)
        return tuple(
            sorted(
                manifests,
                key=lambda item: str(item.get("project_id")),
            )
        )

    def artifact(
        self,
        *,
        project_id: str,
        authority_identity: str,
        authority_fingerprint: str,
    ) -> Mapping[str, object] | None:
        artifact_id = projection_artifact_id(
            project_id,
            authority_identity,
            authority_fingerprint,
        )
        raw = self.object_store.read(ARTIFACT_COLLECTION, artifact_id)
        if raw is None:
            return None
        return self._read_artifact(
            artifact_id,
            project_id=project_id,
            authority_identity=authority_identity,
            authority_fingerprint=authority_fingerprint,
        )

    def bind_generation(
        self,
        *,
        project_id: str,
        authority_identity: str,
        authority_generation_token: str,
        authority_fingerprint: str,
    ) -> Mapping[str, object]:
        project_id = _required_text(project_id, "project_id")
        authority_identity = _required_text(
            authority_identity,
            "authority_identity",
        )
        _require_generation_token(authority_generation_token)
        _require_fingerprint(authority_fingerprint)
        current = self.load_current(
            project_id=project_id,
            authority_identity=authority_identity,
            authority_fingerprint=authority_fingerprint,
        )
        if current.status != "fresh":
            raise MemoryProjectionRepositoryIntegrityError(
                "generation binding requires a fresh projection"
            )
        binding_id = projection_generation_id(project_id)
        existing = self.object_store.read(
            GENERATION_COLLECTION,
            binding_id,
        )
        revision = self.object_store.revision(
            GENERATION_COLLECTION,
            binding_id,
        )
        if existing is None and revision != 0:
            raise MemoryProjectionRepositoryIntegrityError(
                "generation binding metadata exists without payload"
            )
        payload = {
            "schema_version": REPOSITORY_SCHEMA_VERSION,
            "binding_id": binding_id,
            "project_id": project_id,
            "authority_identity": authority_identity,
            "authority_generation_token": authority_generation_token,
            "authority_fingerprint": authority_fingerprint,
            "projection_version": PROJECTION_VERSION,
            "generator_policy_id": GENERATOR_POLICY_ID,
            "derived": True,
            "business_authority": False,
        }
        if existing is not None and dict(existing) == payload:
            return payload
        try:
            self.object_store.write(
                GENERATION_COLLECTION,
                binding_id,
                payload,
                expected_revision=revision,
            )
        except Exception as error:
            if not is_revision_conflict(self.object_store, error):
                raise
            raise MemoryProjectionRepositoryConflict(
                "generation binding CAS conflict"
            ) from error
        return payload

    def load_current_for_generation(
        self,
        *,
        project_id: str,
        authority_identity: str,
        authority_generation_token: str,
    ) -> tuple[str, ProjectionReadResult] | None:
        project_id = _required_text(project_id, "project_id")
        authority_identity = _required_text(
            authority_identity,
            "authority_identity",
        )
        _require_generation_token(authority_generation_token)
        binding = self.object_store.read(
            GENERATION_COLLECTION,
            projection_generation_id(project_id),
        )
        if not isinstance(binding, Mapping):
            return None
        if (
            set(binding)
            != {
                "schema_version",
                "binding_id",
                "project_id",
                "authority_identity",
                "authority_generation_token",
                "authority_fingerprint",
                "projection_version",
                "generator_policy_id",
                "derived",
                "business_authority",
            }
            or binding.get("schema_version") != REPOSITORY_SCHEMA_VERSION
            or binding.get("binding_id")
            != projection_generation_id(project_id)
            or binding.get("project_id") != project_id
            or binding.get("authority_identity") != authority_identity
            or binding.get("authority_generation_token")
            != authority_generation_token
            or binding.get("projection_version") != PROJECTION_VERSION
            or binding.get("generator_policy_id") != GENERATOR_POLICY_ID
            or binding.get("derived") is not True
            or binding.get("business_authority") is not False
        ):
            return None
        fingerprint = binding.get("authority_fingerprint")
        if not isinstance(fingerprint, str):
            return None
        try:
            _require_fingerprint(fingerprint)
        except MemoryProjectionRepositoryError:
            return None
        return (
            fingerprint,
            self.load_current(
                project_id=project_id,
                authority_identity=authority_identity,
                authority_fingerprint=fingerprint,
            ),
        )

    def discard_corrupt_current(
        self,
        *,
        project_id: str,
        authority_identity: str,
        authority_fingerprint: str,
    ) -> dict[str, object]:
        """Discard only the current project's corrupt derived projection state.

        The manifest is removed first, so concurrent readers fail closed before
        the deterministic artifact is removed. Business authority is never
        addressed by this repository.
        """

        project_id = _required_text(project_id, "project_id")
        authority_identity = _required_text(
            authority_identity,
            "authority_identity",
        )
        _require_fingerprint(authority_fingerprint)
        current = self.load_current(
            project_id=project_id,
            authority_identity=authority_identity,
            authority_fingerprint=authority_fingerprint,
        )
        if current.status != "corrupt":
            raise MemoryProjectionRepositoryIntegrityError(
                "projection repair only accepts corrupt derived state"
            )
        manifest_discarded = self.object_store.delete(
            MANIFEST_COLLECTION,
            projection_manifest_id(project_id),
        )
        self.object_store.delete(
            GENERATION_COLLECTION,
            projection_generation_id(project_id),
        )
        artifact_discarded = self.object_store.delete(
            ARTIFACT_COLLECTION,
            projection_artifact_id(
                project_id,
                authority_identity,
                authority_fingerprint,
            ),
        )
        return {
            "status": "discarded",
            "manifest_discarded": manifest_discarded,
            "artifact_discarded": artifact_discarded,
        }

    def _read_artifact(
        self,
        artifact_id: str,
        *,
        project_id: str,
        authority_identity: str,
        authority_fingerprint: str,
    ) -> Mapping[str, object]:
        artifact = self.object_store.read(ARTIFACT_COLLECTION, artifact_id)
        storage_revision = self.object_store.revision(
            ARTIFACT_COLLECTION,
            artifact_id,
        )
        if artifact is None or storage_revision != 1:
            raise MemoryProjectionRepositoryIntegrityError(
                "projection artifact is missing or incomplete"
            )
        if set(artifact) != {
            "schema_version",
            "artifact_id",
            "project_id",
            "authority_identity",
            "authority_fingerprint",
            "projection_version",
            "generator_policy_id",
            "projection_digest",
            "metrics",
            "projection",
            "created_at",
            "derived",
            "business_authority",
        }:
            raise MemoryProjectionRepositoryIntegrityError(
                "projection artifact fields drifted"
            )
        if (
            artifact.get("artifact_id") != artifact_id
            or artifact.get("project_id") != project_id
            or artifact.get("authority_identity") != authority_identity
            or artifact.get("authority_fingerprint")
            != authority_fingerprint
            or artifact.get("projection_version") != PROJECTION_VERSION
            or artifact.get("generator_policy_id") != GENERATOR_POLICY_ID
            or artifact.get("derived") is not True
            or artifact.get("business_authority") is not False
        ):
            raise MemoryProjectionRepositoryIntegrityError(
                "projection artifact identity or safety metadata drifted"
            )
        projection = artifact.get("projection")
        if not isinstance(projection, Mapping):
            raise MemoryProjectionRepositoryIntegrityError(
                "projection artifact requires projection payload"
            )
        if artifact.get("projection_digest") != _digest_payload(projection):
            raise MemoryProjectionRepositoryIntegrityError(
                "projection artifact digest drifted"
            )
        if artifact.get("metrics") != _projection_metrics(projection):
            raise MemoryProjectionRepositoryIntegrityError(
                "projection artifact metrics drifted"
            )
        _validate_projection_payload(
            projection,
            project_id=project_id,
            authority_identity=authority_identity,
            authority_fingerprint=authority_fingerprint,
        )
        return dict(artifact)

    def _read_manifest(
        self,
        project_id: str,
        *,
        repair_interrupted: bool,
    ) -> Mapping[str, object] | None:
        manifest_id = projection_manifest_id(project_id)
        manifest = self.object_store.read(MANIFEST_COLLECTION, manifest_id)
        storage_revision = self.object_store.revision(
            MANIFEST_COLLECTION,
            manifest_id,
        )
        if manifest is None:
            if storage_revision != 0:
                raise MemoryProjectionRepositoryIntegrityError(
                    "projection manifest metadata exists without payload"
                )
            return None
        repository_revision = manifest.get("repository_revision")
        if (
            not isinstance(repository_revision, int)
            or isinstance(repository_revision, bool)
            or repository_revision < 1
        ):
            raise MemoryProjectionRepositoryIntegrityError(
                "projection manifest repository_revision is invalid"
            )
        if repository_revision != storage_revision:
            if (
                repair_interrupted
                and repository_revision == storage_revision + 1
            ):
                try:
                    written = self.object_store.write(
                        MANIFEST_COLLECTION,
                        manifest_id,
                        manifest,
                        expected_revision=storage_revision,
                    )
                except Exception as error:
                    if not is_revision_conflict(self.object_store, error):
                        raise
                    raise MemoryProjectionRepositoryConflict(
                        "projection manifest repair lost CAS"
                    ) from error
                if written != repository_revision:
                    raise MemoryProjectionRepositoryIntegrityError(
                        "projection manifest repair revision drifted"
                    )
                storage_revision = written
            else:
                raise MemoryProjectionRepositoryIntegrityError(
                    "projection manifest payload and metadata revisions differ"
                )
        _validate_manifest(
            manifest,
            manifest_id=manifest_id,
            project_id=project_id,
            storage_revision=storage_revision,
        )
        return dict(manifest)

    def _write_manifest(
        self,
        current: Mapping[str, object] | None,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        project_id = _required_mapping_text(payload, "project_id")
        manifest_id = projection_manifest_id(project_id)
        expected_revision = (
            int(current["repository_revision"])
            if current is not None
            else 0
        )
        if payload.get("repository_revision") != expected_revision + 1:
            raise MemoryProjectionRepositoryIntegrityError(
                "projection manifest next revision is invalid"
            )
        try:
            written = self.object_store.write(
                MANIFEST_COLLECTION,
                manifest_id,
                payload,
                expected_revision=expected_revision,
            )
        except Exception as error:
            if not is_revision_conflict(self.object_store, error):
                raise
            raise MemoryProjectionRepositoryConflict(
                "projection manifest CAS conflict"
            ) from error
        if written != expected_revision + 1:
            raise MemoryProjectionRepositoryIntegrityError(
                "projection manifest storage revision drifted"
            )
        return dict(payload)

    def _write_immutable_artifact(
        self,
        artifact_id: str,
        artifact: Mapping[str, object],
    ) -> None:
        try:
            written = self.object_store.write(
                ARTIFACT_COLLECTION,
                artifact_id,
                artifact,
                expected_revision=0,
            )
        except Exception as error:
            if not is_revision_conflict(self.object_store, error):
                raise
            existing = self.object_store.read(
                ARTIFACT_COLLECTION,
                artifact_id,
            )
            if existing is not None and dict(existing) == dict(artifact):
                storage_revision = self.object_store.revision(
                    ARTIFACT_COLLECTION,
                    artifact_id,
                )
                if storage_revision == 1:
                    return
                if storage_revision == 0:
                    try:
                        repaired = self.object_store.write(
                            ARTIFACT_COLLECTION,
                            artifact_id,
                            artifact,
                            expected_revision=0,
                        )
                    except Exception as repair_error:
                        if not is_revision_conflict(
                            self.object_store,
                            repair_error,
                        ):
                            raise
                        raise MemoryProjectionRepositoryConflict(
                            "projection artifact repair lost CAS"
                        ) from repair_error
                    if repaired == 1:
                        return
            raise MemoryProjectionRepositoryConflict(
                "projection artifact create conflict"
            ) from error
        if written != 1:
            raise MemoryProjectionRepositoryIntegrityError(
                "projection artifact must be immutable revision one"
            )


def projection_manifest_id(project_id: str) -> str:
    project_id = _required_text(project_id, "project_id")
    return _stable_id("projection-manifest", project_id)


def projection_generation_id(project_id: str) -> str:
    project_id = _required_text(project_id, "project_id")
    return _stable_id("projection-generation", project_id)


def projection_artifact_id(
    project_id: str,
    authority_identity: str,
    authority_fingerprint: str,
) -> str:
    project_id = _required_text(project_id, "project_id")
    authority_identity = _required_text(
        authority_identity,
        "authority_identity",
    )
    _require_fingerprint(authority_fingerprint)
    return _stable_id(
        "projection-artifact",
        project_id,
        authority_identity,
        authority_fingerprint,
        PROJECTION_VERSION,
        GENERATOR_POLICY_ID,
    )


def _next_manifest(
    current: Mapping[str, object] | None,
    *,
    project_id: str,
    authority_identity: str,
    requested_authority_fingerprint: str,
    status: str,
    job_id: str | None,
    failure_code: str | None,
    updated_at: str,
    active_artifact_id: str | None = None,
    active_authority_fingerprint: str | None = None,
    active_metrics: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if status not in {"ready", "stale", "rebuilding", "failed"}:
        raise MemoryProjectionRepositoryError(
            "projection manifest status is invalid"
        )
    previous_active_artifact = (
        current.get("active_artifact_id")
        if current is not None
        else None
    )
    previous_active_fingerprint = (
        current.get("active_authority_fingerprint")
        if current is not None
        else None
    )
    previous_active_metrics = (
        current.get("active_metrics")
        if current is not None
        else None
    )
    return {
        "schema_version": REPOSITORY_SCHEMA_VERSION,
        "manifest_id": projection_manifest_id(project_id),
        "project_id": project_id,
        "authority_identity": authority_identity,
        "projection_version": PROJECTION_VERSION,
        "generator_policy_id": GENERATOR_POLICY_ID,
        "requested_authority_fingerprint": requested_authority_fingerprint,
        "active_artifact_id": (
            active_artifact_id
            if active_artifact_id is not None
            else previous_active_artifact
        ),
        "active_authority_fingerprint": (
            active_authority_fingerprint
            if active_authority_fingerprint is not None
            else previous_active_fingerprint
        ),
        "active_metrics": (
            dict(active_metrics)
            if active_metrics is not None
            else previous_active_metrics
        ),
        "status": status,
        "job_id": job_id,
        "failure_code": failure_code,
        "repository_revision": (
            int(current["repository_revision"]) + 1
            if current is not None
            else 1
        ),
        "updated_at": updated_at,
        "derived": True,
        "business_authority": False,
    }


def _validate_manifest(
    manifest: Mapping[str, object],
    *,
    manifest_id: str,
    project_id: str,
    storage_revision: int,
) -> None:
    if set(manifest) != {
        "schema_version",
        "manifest_id",
        "project_id",
        "authority_identity",
        "projection_version",
        "generator_policy_id",
        "requested_authority_fingerprint",
        "active_artifact_id",
        "active_authority_fingerprint",
        "active_metrics",
        "status",
        "job_id",
        "failure_code",
        "repository_revision",
        "updated_at",
        "derived",
        "business_authority",
    }:
        raise MemoryProjectionRepositoryIntegrityError(
            "projection manifest fields drifted"
        )
    if (
        manifest.get("schema_version") != REPOSITORY_SCHEMA_VERSION
        or manifest.get("manifest_id") != manifest_id
        or manifest.get("project_id") != project_id
        or manifest.get("repository_revision") != storage_revision
        or manifest.get("derived") is not True
        or manifest.get("business_authority") is not False
    ):
        raise MemoryProjectionRepositoryIntegrityError(
            "projection manifest identity or safety metadata drifted"
        )
    for policy_field in ("projection_version", "generator_policy_id"):
        value = manifest.get(policy_field)
        if not isinstance(value, str) or not value:
            raise MemoryProjectionRepositoryIntegrityError(
                f"projection manifest {policy_field} is invalid"
            )
    status = manifest.get("status")
    if status not in {"ready", "stale", "rebuilding", "failed"}:
        raise MemoryProjectionRepositoryIntegrityError(
            "projection manifest status is invalid"
        )
    authority_identity = manifest.get("authority_identity")
    if not isinstance(authority_identity, str) or not authority_identity:
        raise MemoryProjectionRepositoryIntegrityError(
            "projection manifest authority_identity is invalid"
        )
    requested = manifest.get("requested_authority_fingerprint")
    if not isinstance(requested, str):
        raise MemoryProjectionRepositoryIntegrityError(
            "projection manifest fingerprint is invalid"
        )
    _require_fingerprint(requested)
    active_artifact = manifest.get("active_artifact_id")
    active_fingerprint = manifest.get("active_authority_fingerprint")
    active_metrics = manifest.get("active_metrics")
    if active_artifact is None:
        if active_fingerprint is not None or active_metrics is not None:
            raise MemoryProjectionRepositoryIntegrityError(
                "projection manifest active pointer is incomplete"
            )
    else:
        if (
            not isinstance(active_artifact, str)
            or not active_artifact
            or not isinstance(active_fingerprint, str)
            or not isinstance(active_metrics, Mapping)
        ):
            raise MemoryProjectionRepositoryIntegrityError(
                "projection manifest active pointer is invalid"
            )
        _require_fingerprint(active_fingerprint)
        _validate_projection_metrics(active_metrics)
    if status == "ready":
        if (
            active_fingerprint != requested
            or not isinstance(active_artifact, str)
        ):
            raise MemoryProjectionRepositoryIntegrityError(
                "ready projection manifest requires matching active artifact"
            )
    if status == "rebuilding" and not isinstance(manifest.get("job_id"), str):
        raise MemoryProjectionRepositoryIntegrityError(
            "rebuilding projection manifest requires job_id"
        )
    if status == "failed" and not isinstance(
        manifest.get("failure_code"),
        str,
    ):
        raise MemoryProjectionRepositoryIntegrityError(
            "failed projection manifest requires failure_code"
        )


def _projection_payload(
    projection: MemoryRetrievalProjection | Mapping[str, object],
) -> dict[str, object]:
    if isinstance(projection, MemoryRetrievalProjection):
        return serialize_memory_retrieval_projection(projection)
    return dict(projection)


def _validate_projection_payload(
    projection: Mapping[str, object],
    *,
    project_id: str,
    authority_identity: str,
    authority_fingerprint: str,
) -> None:
    expected_top_level = {
        "schema_version",
        "projection_version",
        "project_id",
        "authority_identity",
        "authority_fingerprint",
        "generated_at",
        "status",
        "derived_from",
        "project_skill_refs",
        "r0_items",
        "r1_items",
        "safety",
    }
    if set(projection) != expected_top_level:
        raise MemoryProjectionRepositoryIntegrityError(
            "projection payload fields drifted from Phase B contract"
        )
    safety = projection.get("safety")
    if (
        projection.get("schema_version") != "1.0.0"
        or projection.get("projection_version") != PROJECTION_VERSION
        or projection.get("project_id") != project_id
        or projection.get("authority_identity") != authority_identity
        or projection.get("authority_fingerprint")
        != authority_fingerprint
        or projection.get("status") not in {"ready", "empty"}
        or not isinstance(safety, Mapping)
        or safety.get("derived") is not True
        or safety.get("business_authority") is not False
        or safety.get("rebuildable") is not True
        or safety.get("business_writes_allowed") is not False
    ):
        raise MemoryProjectionRepositoryIntegrityError(
            "projection payload identity or safety contract is invalid"
        )
    if set(safety) != {
        "derived",
        "business_authority",
        "rebuildable",
        "source_body_included",
        "project_skill_body_included",
        "business_writes_allowed",
    }:
        raise MemoryProjectionRepositoryIntegrityError(
            "projection payload safety fields drifted"
        )
    if (
        safety.get("source_body_included") is not False
        or safety.get("project_skill_body_included") is not False
    ):
        raise MemoryProjectionRepositoryIntegrityError(
            "projection payload cannot include source or Project Skill body"
        )
    for key in ("derived_from", "project_skill_refs", "r0_items", "r1_items"):
        if not isinstance(projection.get(key), list):
            raise MemoryProjectionRepositoryIntegrityError(
                f"projection payload requires {key}"
            )
    r0_items = projection["r0_items"]
    r1_items = projection["r1_items"]
    if projection.get("status") == "empty":
        if any(
            projection[key]
            for key in (
                "derived_from",
                "project_skill_refs",
                "r0_items",
                "r1_items",
            )
        ):
            raise MemoryProjectionRepositoryIntegrityError(
                "empty projection payload cannot contain derived records"
            )
        return
    if not r0_items or not r1_items:
        raise MemoryProjectionRepositoryIntegrityError(
            "ready projection payload requires R0 and R1 records"
        )
    for item in r0_items:
        _validate_projection_item(
            item,
            projection_type="r0_series_router",
            expected_fields={
                "projection_id",
                "projection_type",
                "project_id",
                "series_id",
                "series_memory_id",
                "authority_identity",
                "authority_fingerprint",
                "generator_policy_id",
                "projection_version",
                "generated_at",
                "status",
                "failure_code",
                "title",
                "description",
                "keywords",
                "source_refs",
                "derived_from",
                "content_length",
            },
            project_id=project_id,
            authority_identity=authority_identity,
            authority_fingerprint=authority_fingerprint,
            generated_at=projection.get("generated_at"),
        )
    for item in r1_items:
        _validate_projection_item(
            item,
            projection_type="r1_series_digest",
            expected_fields={
                "projection_id",
                "projection_type",
                "project_id",
                "series_id",
                "series_memory_id",
                "authority_identity",
                "authority_fingerprint",
                "generator_policy_id",
                "projection_version",
                "generated_at",
                "status",
                "failure_code",
                "summary",
                "scenario_refs",
                "atom_refs",
                "skill_refs",
                "source_refs",
                "derived_from",
                "content_length",
            },
            project_id=project_id,
            authority_identity=authority_identity,
            authority_fingerprint=authority_fingerprint,
            generated_at=projection.get("generated_at"),
        )
    for ref in projection["project_skill_refs"]:
        if not isinstance(ref, Mapping) or set(ref) != {
            "skill_id",
            "revision",
            "name",
            "purpose_preview",
        }:
            raise MemoryProjectionRepositoryIntegrityError(
                "projection Project Skill reference fields drifted"
            )
    _validate_derived_refs(projection["derived_from"])


def _validate_projection_item(
    item: object,
    *,
    projection_type: str,
    expected_fields: set[str],
    project_id: str,
    authority_identity: str,
    authority_fingerprint: str,
    generated_at: object,
) -> None:
    if not isinstance(item, Mapping) or set(item) != expected_fields:
        raise MemoryProjectionRepositoryIntegrityError(
            f"{projection_type} projection item fields drifted"
        )
    if (
        item.get("projection_type") != projection_type
        or item.get("project_id") != project_id
        or item.get("authority_identity") != authority_identity
        or item.get("authority_fingerprint") != authority_fingerprint
        or item.get("generator_policy_id") != GENERATOR_POLICY_ID
        or item.get("projection_version") != PROJECTION_VERSION
        or item.get("generated_at") != generated_at
        or item.get("status") != "ready"
        or item.get("failure_code") is not None
    ):
        raise MemoryProjectionRepositoryIntegrityError(
            f"{projection_type} projection item identity drifted"
        )
    if not isinstance(item.get("source_refs"), list):
        raise MemoryProjectionRepositoryIntegrityError(
            f"{projection_type} source refs are invalid"
        )
    for source_ref in item["source_refs"]:
        if (
            not isinstance(source_ref, Mapping)
            or set(source_ref) != {"source_id", "locator"}
        ):
            raise MemoryProjectionRepositoryIntegrityError(
                f"{projection_type} source ref fields drifted"
            )
    derived_from = item.get("derived_from")
    if not isinstance(derived_from, list) or not derived_from:
        raise MemoryProjectionRepositoryIntegrityError(
            f"{projection_type} requires derived_from"
        )
    _validate_derived_refs(derived_from)
    content_length = item.get("content_length")
    if (
        not isinstance(content_length, int)
        or isinstance(content_length, bool)
        or content_length < 1
    ):
        raise MemoryProjectionRepositoryIntegrityError(
            f"{projection_type} content_length is invalid"
        )


def _validate_derived_refs(refs: object) -> None:
    if not isinstance(refs, list):
        raise MemoryProjectionRepositoryIntegrityError(
            "projection derived refs are invalid"
        )
    for ref in refs:
        if not isinstance(ref, Mapping) or set(ref) != {
            "object_type",
            "object_id",
            "revision",
            "content_hash",
        }:
            raise MemoryProjectionRepositoryIntegrityError(
                "projection derived ref fields drifted"
            )


def _projection_metrics(
    projection: Mapping[str, object],
) -> dict[str, object]:
    r0_items = projection.get("r0_items")
    r1_items = projection.get("r1_items")
    skill_refs = projection.get("project_skill_refs")
    if not isinstance(r0_items, list) or not isinstance(r1_items, list):
        raise MemoryProjectionRepositoryIntegrityError(
            "projection metrics require R0 and R1 items"
        )
    if not isinstance(skill_refs, list):
        raise MemoryProjectionRepositoryIntegrityError(
            "projection metrics require project skill refs"
        )
    scenario_counts: list[int] = []
    atom_counts: list[int] = []
    content_length = 0
    for item in (*r0_items, *r1_items):
        if not isinstance(item, Mapping):
            raise MemoryProjectionRepositoryIntegrityError(
                "projection metrics require object items"
            )
        length = item.get("content_length")
        if not isinstance(length, int) or isinstance(length, bool) or length < 0:
            raise MemoryProjectionRepositoryIntegrityError(
                "projection metrics require content lengths"
            )
        content_length += length
    for item in r1_items:
        if not isinstance(item, Mapping):
            continue
        scenarios = item.get("scenario_refs")
        atoms = item.get("atom_refs")
        if not isinstance(scenarios, list) or not isinstance(atoms, list):
            raise MemoryProjectionRepositoryIntegrityError(
                "projection metrics require R1 reference lists"
            )
        scenario_counts.append(len(scenarios))
        atom_counts.append(len(atoms))
    return {
        "r0_item_count": len(r0_items),
        "r1_item_count": len(r1_items),
        "scenario_ref_count": sum(scenario_counts),
        "atom_ref_count": sum(atom_counts),
        "project_skill_ref_count": len(skill_refs),
        "content_length": content_length,
        "limits_reached": {
            "scenario_per_series": any(count >= 8 for count in scenario_counts),
            "atom_per_series": any(count >= 16 for count in atom_counts),
            "project_skill_per_project": len(skill_refs) >= 8,
        },
    }


def _validate_projection_metrics(metrics: Mapping[str, object]) -> None:
    count_fields = {
        "r0_item_count",
        "r1_item_count",
        "scenario_ref_count",
        "atom_ref_count",
        "project_skill_ref_count",
        "content_length",
    }
    if set(metrics) != {*count_fields, "limits_reached"}:
        raise MemoryProjectionRepositoryIntegrityError(
            "projection manifest metrics fields drifted"
        )
    for field in count_fields:
        value = metrics.get(field)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            raise MemoryProjectionRepositoryIntegrityError(
                f"projection manifest metric {field} is invalid"
            )
    limits = metrics.get("limits_reached")
    expected_limits = {
        "scenario_per_series",
        "atom_per_series",
        "project_skill_per_project",
    }
    if not isinstance(limits, Mapping) or set(limits) != expected_limits:
        raise MemoryProjectionRepositoryIntegrityError(
            "projection manifest limit metrics fields drifted"
        )
    if any(not isinstance(limits.get(field), bool) for field in expected_limits):
        raise MemoryProjectionRepositoryIntegrityError(
            "projection manifest limit metrics are invalid"
        )


def _stale_result(
    manifest: Mapping[str, object],
    reason_code: str,
) -> ProjectionReadResult:
    return ProjectionReadResult(
        status="stale",
        fallback_to_authority=True,
        reason_code=reason_code,
        projection=None,
        manifest=dict(manifest),
    )


def _digest_payload(payload: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise MemoryProjectionRepositoryIntegrityError(
            "projection payload must be canonical JSON"
        ) from error
    return hashlib.sha256(encoded).hexdigest()


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:40]}"


def _require_fingerprint(value: str) -> None:
    if (
        len(value) != _FINGERPRINT_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise MemoryProjectionRepositoryError(
            "authority_fingerprint must be lowercase SHA-256"
        )


def _require_generation_token(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != _FINGERPRINT_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise MemoryProjectionRepositoryError(
            "authority_generation_token must be lowercase SHA-256"
        )


def _required_mapping_text(
    mapping: Mapping[str, object],
    key: str,
) -> str:
    return _required_text(mapping.get(key), key)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MemoryProjectionRepositoryError(f"{field} is required")
    return value.strip()
