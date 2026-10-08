from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from core.storage_provider import ObjectStorePort

from .sqlite_manifest import SqliteFts5Manifest, sqlite_fts5_manifest_payload


class SqliteFts5ActivationError(ValueError):
    """Raised when a SQLite FTS5 candidate is activated without required proof."""


@dataclass(frozen=True, slots=True)
class SqliteFts5ActivationResult:
    active_manifest: Mapping[str, object]
    previous_manifest: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class ObjectStoreSqliteFts5ActivationRepository:
    """Activates a verified SQLite FTS5 candidate manifest under the active manifest id."""

    object_store: ObjectStorePort
    manifest_collection: str = "recall_index_manifests"
    active_manifest_id: str = "active"

    def activate(
        self,
        *,
        candidate_manifest: SqliteFts5Manifest | Mapping[str, object],
        verified_job: Mapping[str, object],
        activated_by: str,
        activated_at: str | None = None,
        database_uri: str | None = None,
    ) -> SqliteFts5ActivationResult:
        candidate = sqlite_fts5_manifest_payload(candidate_manifest)
        _validate_verified_job(candidate, verified_job)
        if database_uri is not None:
            _validate_database_uri(database_uri)
        previous = self.object_store.read(self.manifest_collection, self.active_manifest_id)
        timestamp = activated_at or _utc_now()
        active = _active_manifest_payload(
            candidate=candidate,
            verified_job=verified_job,
            previous=previous,
            activated_by=activated_by,
            activated_at=timestamp,
            database_uri=database_uri,
        )
        self.object_store.write(self.manifest_collection, self.active_manifest_id, active, expected_revision=None)
        return SqliteFts5ActivationResult(
            active_manifest=active,
            previous_manifest=dict(previous) if previous is not None else None,
        )

    def activate_effect_v2(
        self,
        *,
        candidate_manifest: SqliteFts5Manifest | Mapping[str, object],
        operation_id: str,
        verification_ref: str,
        artifact_revision: str,
        activated_by: str,
        database_uri: str,
        activated_at: str | None = None,
    ) -> SqliteFts5ActivationResult:
        """Activate a verified Effect-v2 artifact without fabricating a Job fact."""
        candidate = sqlite_fts5_manifest_payload(candidate_manifest)
        _validate_effect_verification(
            candidate,
            operation_id=operation_id,
            verification_ref=verification_ref,
            artifact_revision=artifact_revision,
        )
        _validate_database_uri(database_uri)
        previous = self.object_store.read(self.manifest_collection, self.active_manifest_id)
        active = _active_effect_manifest_payload(
            candidate=candidate,
            operation_id=operation_id,
            verification_ref=verification_ref,
            artifact_revision=artifact_revision,
            previous=previous,
            activated_by=activated_by,
            activated_at=activated_at or _utc_now(),
            database_uri=database_uri,
        )
        self.object_store.write(
            self.manifest_collection, self.active_manifest_id, active,
            expected_revision=None,
        )
        return SqliteFts5ActivationResult(
            active_manifest=active,
            previous_manifest=dict(previous) if previous is not None else None,
        )


def _validate_verified_job(candidate: Mapping[str, object], job: Mapping[str, object]) -> None:
    if job.get("job_type") != "rebuild_index":
        raise SqliteFts5ActivationError("sqlite_fts5 activation requires rebuild_index job")
    if job.get("status") != "completed":
        raise SqliteFts5ActivationError("sqlite_fts5 activation requires completed verified job")
    published_outputs = job.get("published_outputs")
    if not isinstance(published_outputs, list) or len(published_outputs) != 1:
        raise SqliteFts5ActivationError("sqlite_fts5 activation requires one verified output")
    output = published_outputs[0]
    if not isinstance(output, Mapping):
        raise SqliteFts5ActivationError("sqlite_fts5 activation verified output must be an object")
    if output.get("published") is not True or output.get("kind") != "other":
        raise SqliteFts5ActivationError("sqlite_fts5 activation verified output must be published")
    manifest_id = _required_string(candidate, "id")
    if output.get("object_id") != f"verified-{manifest_id}":
        raise SqliteFts5ActivationError("sqlite_fts5 activation verified output does not match manifest")
    output_uri = output.get("uri")
    if not isinstance(output_uri, str) or "/recall/index-verifications/" not in output_uri:
        raise SqliteFts5ActivationError("sqlite_fts5 activation requires verification output uri")
    vector = candidate.get("vector")
    if not isinstance(vector, Mapping) or vector.get("enabled") is not False:
        raise SqliteFts5ActivationError("sqlite_fts5 activation requires vector disabled candidate")
    if candidate.get("backend_kind") != "sqlite_fts5":
        raise SqliteFts5ActivationError("sqlite_fts5 activation requires sqlite_fts5 candidate")


def _validate_effect_verification(
    candidate: Mapping[str, object], *, operation_id: str,
    verification_ref: str, artifact_revision: str,
) -> None:
    if candidate.get("backend_kind") != "sqlite_fts5":
        raise SqliteFts5ActivationError("sqlite_fts5 activation requires sqlite_fts5 candidate")
    vector = candidate.get("vector")
    if not isinstance(vector, Mapping) or vector.get("enabled") is not False:
        raise SqliteFts5ActivationError("sqlite_fts5 activation requires vector disabled candidate")
    if not isinstance(operation_id, str) or not operation_id.startswith("eff2_"):
        raise SqliteFts5ActivationError("sqlite_fts5 activation requires Effect-v2 operation id")
    if (
        not isinstance(verification_ref, str)
        or not verification_ref.endswith(f"/{operation_id}")
        or "/recall/index-verifications/" not in verification_ref
    ):
        raise SqliteFts5ActivationError("sqlite_fts5 activation verification does not match Effect")
    if (
        not isinstance(artifact_revision, str)
        or not artifact_revision.startswith("index-rebuild-artifact:sha256:")
    ):
        raise SqliteFts5ActivationError("sqlite_fts5 activation requires artifact revision")


def _active_manifest_payload(
    *,
    candidate: Mapping[str, object],
    verified_job: Mapping[str, object],
    previous: Mapping[str, object] | None,
    activated_by: str,
    activated_at: str,
    database_uri: str | None = None,
) -> dict[str, object]:
    if not activated_by:
        raise SqliteFts5ActivationError("sqlite_fts5 activation requires activated_by")
    published_outputs = verified_job.get("published_outputs")
    if not isinstance(published_outputs, list) or not published_outputs or not isinstance(published_outputs[0], Mapping):
        raise SqliteFts5ActivationError("sqlite_fts5 activation requires verified output")
    verification_ref = _required_string(published_outputs[0], "uri")
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "id": "active",
        "status": "active",
        "backend_kind": "sqlite_fts5",
        "source": "verified_index_rebuild_job",
        "source_fingerprint": _required_string(candidate, "source_fingerprint"),
        "source_count": _required_int(candidate, "source_count"),
        "source_refs": list(_required_string_list(candidate, "source_refs")),
        "index_role": "active_manifest",
        "fts": dict(_required_mapping(candidate, "fts")),
        "vector": {
            "enabled": False,
            "provider": None,
            "dimension": None,
        },
        "verification_ref": verification_ref,
        "verified_job_id": _required_string(verified_job, "id"),
        "previous_backend_kind": previous.get("backend_kind") if isinstance(previous, Mapping) else None,
        "activated_by": activated_by,
        "activated_at": activated_at,
    }
    if database_uri is not None:
        payload["database_uri"] = database_uri
    return payload


def _active_effect_manifest_payload(
    *, candidate: Mapping[str, object], operation_id: str,
    verification_ref: str, artifact_revision: str,
    previous: Mapping[str, object] | None, activated_by: str,
    activated_at: str, database_uri: str,
) -> dict[str, object]:
    if not activated_by:
        raise SqliteFts5ActivationError("sqlite_fts5 activation requires activated_by")
    return {
        "schema_version": "2.0.0",
        "id": "active",
        "status": "active",
        "backend_kind": "sqlite_fts5",
        "source": "verified_index_rebuild_effect",
        "source_fingerprint": _required_string(candidate, "source_fingerprint"),
        "source_count": _required_int(candidate, "source_count"),
        "source_refs": list(_required_string_list(candidate, "source_refs")),
        "index_role": "active_manifest",
        "fts": dict(_required_mapping(candidate, "fts")),
        "vector": {"enabled": False, "provider": None, "dimension": None},
        "verification_ref": verification_ref,
        "verified_operation_id": operation_id,
        "artifact_revision": artifact_revision,
        "previous_backend_kind": previous.get("backend_kind") if isinstance(previous, Mapping) else None,
        "activated_by": activated_by,
        "activated_at": activated_at,
        "database_uri": database_uri,
    }


def _validate_database_uri(database_uri: str) -> None:
    if not isinstance(database_uri, str) or not database_uri:
        raise SqliteFts5ActivationError("sqlite_fts5 activation database_uri must be a non-empty string")
    if not database_uri.startswith("file://"):
        raise SqliteFts5ActivationError("sqlite_fts5 activation database_uri must be a file:// URI")


def _required_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise SqliteFts5ActivationError(f"sqlite_fts5 activation requires {key}")
    return value


def _required_string(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise SqliteFts5ActivationError(f"sqlite_fts5 activation requires {key}")
    return value


def _required_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SqliteFts5ActivationError(f"sqlite_fts5 activation requires non-negative {key}")
    return value


def _required_string_list(mapping: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = mapping.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise SqliteFts5ActivationError(f"sqlite_fts5 activation requires {key}")
    return tuple(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
