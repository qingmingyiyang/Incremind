from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from core.job_runner import JobRepositoryPort
from core.product_core.memory_projection_authority_contract import (
    MemoryProjectionAuthoritySnapshot,
    MemoryProjectionAuthoritySnapshotPort,
    authority_snapshot_fingerprint,
)
from core.product_core.memory_projection_contract import (
    GENERATOR_POLICY_ID,
    MemoryRetrievalProjection,
    PROJECTION_VERSION,
)
from core.product_core.memory_projection_repository import (
    MemoryProjectionRepositoryError,
    ObjectStoreMemoryProjectionRepository,
)


JOB_TYPE = "rebuild_memory_projection"
MAX_ATTEMPTS = 3
PROJECTION_NAMESPACE_ID = "default"


class MemoryProjectionJobRepositoryPort(JobRepositoryPort, Protocol):
    def list_jobs(
        self,
        *,
        job_type: str | None = None,
        status: str | None = None,
        source_id: str | None = None,
        limit: int | None = None,
    ) -> Sequence[Mapping[str, object]]:
        """List durable jobs for startup recovery."""


class MemoryProjectionRebuildJobError(ValueError):
    """Raised when a projection rebuild Job request is invalid."""


class MemoryProjectionRebuildRuntimeError(
    MemoryProjectionRebuildJobError
):
    """Raised when a projection rebuild worker cannot publish safely."""


@dataclass(frozen=True, slots=True)
class MemoryProjectionRebuildJobResult:
    job_id: str
    job: Mapping[str, object]
    manifest: Mapping[str, object]
    replayed: bool


class CreateMemoryProjectionRebuildJob:
    """Persist one deterministic rebuild request without building a projection."""

    def __init__(
        self,
        *,
        jobs: JobRepositoryPort,
        projections: ObjectStoreMemoryProjectionRepository,
        namespace_id: str = "default",
    ) -> None:
        self._jobs = jobs
        self._projections = projections
        self._namespace_id = _required_namespace(namespace_id)

    def execute(
        self,
        *,
        project_id: str,
        authority_identity: str,
        authority_fingerprint: str,
        created_at: str | None = None,
        reopen_completed: bool = False,
    ) -> MemoryProjectionRebuildJobResult:
        project_id = _required_text(project_id, "project_id")
        authority_identity = _required_text(
            authority_identity,
            "authority_identity",
        )
        _require_fingerprint(authority_fingerprint)
        timestamp = created_at or _utc_now()
        job = _pending_job(
            project_id=project_id,
            authority_identity=authority_identity,
            authority_fingerprint=authority_fingerprint,
            namespace_id=self._namespace_id,
            created_at=timestamp,
        )
        job_id = _required_job_text(job, "id")
        manifest = self._projections.begin_rebuild(
            project_id=project_id,
            authority_identity=authority_identity,
            authority_fingerprint=authority_fingerprint,
            job_id=job_id,
            updated_at=timestamp,
        )
        existing = self._jobs.get(job_id)
        if existing is not None:
            _validate_job_identity(
                existing,
                project_id=project_id,
                authority_identity=authority_identity,
                authority_fingerprint=authority_fingerprint,
            )
            if reopen_completed and existing.get("status") == "completed":
                reopened = dict(job)
                reopened["attempt"] = _job_attempt(existing)
                reopened["created_at"] = _required_job_text(
                    existing,
                    "created_at",
                )
                reopened["updated_at"] = timestamp
                self._jobs.save(reopened)
                return MemoryProjectionRebuildJobResult(
                    job_id=job_id,
                    job=reopened,
                    manifest=manifest,
                    replayed=False,
                )
            return MemoryProjectionRebuildJobResult(
                job_id=job_id,
                job=dict(existing),
                manifest=manifest,
                replayed=True,
            )
        self._jobs.save(job)
        return MemoryProjectionRebuildJobResult(
            job_id=job_id,
            job=job,
            manifest=manifest,
            replayed=False,
        )


def run_memory_projection_rebuild_job(
    *,
    jobs: JobRepositoryPort,
    projections: ObjectStoreMemoryProjectionRepository,
    authority: MemoryProjectionAuthoritySnapshotPort,
    job_id: str,
    worker_id: str = "memory-projection-worker",
    now: str | None = None,
    after_artifact_persisted: Callable[[], None] | None = None,
) -> MemoryProjectionRebuildJobResult:
    job = jobs.get(job_id)
    if job is None:
        raise MemoryProjectionRebuildRuntimeError(
            "memory projection rebuild job was not found"
        )
    request = _request_for_job(projections, job_id)
    _validate_job_identity(
        job,
        project_id=request["project_id"],
        authority_identity=request["authority_identity"],
        authority_fingerprint=request["authority_fingerprint"],
    )
    if job.get("status") == "completed":
        return MemoryProjectionRebuildJobResult(
            job_id=job_id,
            job=dict(job),
            manifest=request["manifest"],
            replayed=True,
        )
    attempt = _job_attempt(job) + 1
    if attempt > _job_max_attempts(job):
        raise MemoryProjectionRebuildRuntimeError(
            "memory projection rebuild job exhausted retry budget"
        )
    timestamp = now or _utc_now()
    running = _running_job(
        job,
        attempt=attempt,
        worker_id=_required_text(worker_id, "worker_id"),
        timestamp=timestamp,
    )
    jobs.save(running)
    phase = "load_authority_snapshot"
    try:
        projections.begin_rebuild(
            project_id=request["project_id"],
            authority_identity=request["authority_identity"],
            authority_fingerprint=request["authority_fingerprint"],
            job_id=job_id,
            updated_at=timestamp,
        )
        snapshot = authority.load(request["project_id"])
        candidate = _build_expected_projection(
            snapshot,
            request=request,
            generated_at=_required_job_text(job, "created_at"),
        )
        phase = "build_projection_artifact"
        artifact_id = projections.stage_projection(candidate)
        staged = _artifact_staged_job(
            running,
            artifact_id=artifact_id,
            authority_fingerprint=request["authority_fingerprint"],
            namespace_id=request["namespace_id"],
            timestamp=timestamp,
        )
        jobs.save(staged)
        if after_artifact_persisted is not None:
            after_artifact_persisted()

        phase = "activate_projection"
        current_snapshot = authority.load(request["project_id"])
        current_candidate = _build_expected_projection(
            current_snapshot,
            request=request,
            generated_at=_required_job_text(job, "created_at"),
        )
        if (
            current_candidate.authority_fingerprint
            != candidate.authority_fingerprint
        ):
            raise MemoryProjectionRebuildRuntimeError(
                "authority snapshot changed before projection activation"
            )
        manifest = projections.activate_staged(
            project_id=request["project_id"],
            authority_identity=request["authority_identity"],
            authority_fingerprint=request["authority_fingerprint"],
            job_id=job_id,
            artifact_id=artifact_id,
            updated_at=timestamp,
        )
        if current_snapshot.authority_generation_token is not None:
            projections.bind_generation(
                project_id=request["project_id"],
                authority_identity=request["authority_identity"],
                authority_generation_token=(
                    current_snapshot.authority_generation_token
                ),
                authority_fingerprint=request["authority_fingerprint"],
            )
        completed = _completed_job(
            staged,
            artifact_id=artifact_id,
            namespace_id=request["namespace_id"],
            timestamp=timestamp,
        )
        jobs.save(completed)
        return MemoryProjectionRebuildJobResult(
            job_id=job_id,
            job=completed,
            manifest=manifest,
            replayed=False,
        )
    except Exception as error:
        failure_code = (
            "authority_snapshot_drift"
            if isinstance(error, MemoryProjectionRebuildRuntimeError)
            and "authority snapshot" in str(error)
            else "projection_rebuild_failed"
        )
        try:
            projections.record_failure(
                project_id=request["project_id"],
                authority_identity=request["authority_identity"],
                authority_fingerprint=request["authority_fingerprint"],
                job_id=job_id,
                attempt=attempt,
                failure_code=failure_code,
                recorded_at=timestamp,
            )
        except MemoryProjectionRepositoryError:
            pass
        failed = _failed_job(
            running,
            attempt=attempt,
            failed_step=phase,
            failure_code=failure_code,
            timestamp=timestamp,
        )
        jobs.save(failed)
        raise MemoryProjectionRebuildRuntimeError(
            f"memory projection rebuild failed at {phase}"
        ) from error


def recover_memory_projection_rebuild_jobs(
    *,
    jobs: MemoryProjectionJobRepositoryPort,
    projections: ObjectStoreMemoryProjectionRepository,
    authority: MemoryProjectionAuthoritySnapshotPort,
    namespace_id: str = "default",
) -> tuple[str, ...]:
    namespace_id = _required_namespace(namespace_id)
    for manifest in projections.manifests():
        if manifest.get("status") != "rebuilding":
            continue
        job_id = manifest.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            continue
        if jobs.get(job_id) is not None:
            continue
        recovered_job = _pending_job(
            project_id=_required_mapping_text(manifest, "project_id"),
            authority_identity=_required_mapping_text(
                manifest,
                "authority_identity",
            ),
            authority_fingerprint=_required_mapping_text(
                manifest,
                "requested_authority_fingerprint",
            ),
            namespace_id=namespace_id,
            created_at=_required_mapping_text(manifest, "updated_at"),
        )
        if recovered_job["id"] != job_id:
            raise MemoryProjectionRebuildRuntimeError(
                "rebuilding manifest job identity drifted"
            )
        jobs.save(recovered_job)

    recovered: list[str] = []
    candidates = tuple(jobs.list_jobs(job_type=JOB_TYPE))
    for job in candidates:
        if job.get("status") not in {"pending", "running"}:
            continue
        job_id = job.get("id")
        if not isinstance(job_id, str):
            continue
        try:
            result = run_memory_projection_rebuild_job(
                jobs=jobs,
                projections=projections,
                authority=authority,
                job_id=job_id,
            )
        except MemoryProjectionRebuildRuntimeError:
            continue
        if result.job.get("status") == "completed":
            recovered.append(job_id)
    return tuple(sorted(recovered))


def memory_projection_rebuild_job_id(
    *,
    project_id: str,
    authority_identity: str,
    authority_fingerprint: str,
    namespace_id: str = "default",
) -> str:
    namespace_id = _required_namespace(namespace_id)
    _require_fingerprint(authority_fingerprint)
    digest = _projection_job_digest(
        namespace_id=namespace_id,
        project_id=_required_text(project_id, "project_id"),
        authority_identity=_required_text(
            authority_identity,
            "authority_identity",
        ),
        authority_fingerprint=authority_fingerprint,
    )
    return f"job-memory-projection-{digest[:32]}"


def _request_for_job(
    projections: ObjectStoreMemoryProjectionRepository,
    job_id: str,
) -> dict[str, object]:
    matches = [
        manifest
        for manifest in projections.manifests()
        if manifest.get("job_id") == job_id
    ]
    if len(matches) != 1:
        raise MemoryProjectionRebuildRuntimeError(
            "memory projection rebuild job requires one current manifest"
        )
    manifest = matches[0]
    project_id = _required_mapping_text(manifest, "project_id")
    authority_identity = _required_mapping_text(
        manifest,
        "authority_identity",
    )
    authority_fingerprint = _required_mapping_text(
        manifest,
        "requested_authority_fingerprint",
    )
    expected_job_id = memory_projection_rebuild_job_id(
        project_id=project_id,
        authority_identity=authority_identity,
        authority_fingerprint=authority_fingerprint,
    )
    if expected_job_id != job_id:
        raise MemoryProjectionRebuildRuntimeError(
            "memory projection rebuild manifest identity drifted"
        )
    return {
        "project_id": project_id,
        "authority_identity": authority_identity,
        "authority_fingerprint": authority_fingerprint,
        "namespace_id": "default",
        "manifest": manifest,
    }


def _build_expected_projection(
    snapshot: MemoryProjectionAuthoritySnapshot,
    *,
    request: Mapping[str, object],
    generated_at: str,
) -> MemoryRetrievalProjection:
    if snapshot.project_id != request["project_id"]:
        raise MemoryProjectionRebuildRuntimeError(
            "authority snapshot project identity drifted"
        )
    if snapshot.authority_identity != request["authority_identity"]:
        raise MemoryProjectionRebuildRuntimeError(
            "authority snapshot identity drifted"
        )
    projection = snapshot.build(generated_at=generated_at)
    if projection.authority_fingerprint != request["authority_fingerprint"]:
        raise MemoryProjectionRebuildRuntimeError(
            "authority snapshot fingerprint drifted"
        )
    return projection


def _pending_job(
    *,
    project_id: str,
    authority_identity: str,
    authority_fingerprint: str,
    namespace_id: str,
    created_at: str,
) -> dict[str, object]:
    _parse_datetime(created_at)
    job_id = memory_projection_rebuild_job_id(
        project_id=project_id,
        authority_identity=authority_identity,
        authority_fingerprint=authority_fingerprint,
        namespace_id=namespace_id,
    )
    job_digest = _projection_job_digest(
        namespace_id=namespace_id,
        project_id=project_id,
        authority_identity=authority_identity,
        authority_fingerprint=authority_fingerprint,
    )
    request_ref = (
        f"crp://{namespace_id}/memory/projection-rebuild-requests/"
        f"{authority_fingerprint}/{PROJECTION_VERSION}/"
        f"{GENERATOR_POLICY_ID}"
    )
    return {
        "schema_version": "1.0.0",
        "id": job_id,
        "source_id": f"projection-authority-{authority_fingerprint[:16]}",
        "job_type": JOB_TYPE,
        "idempotency_key": (
            f"memory-projection-rebuild-{job_digest}"
        ),
        "status": "pending",
        "attempt": 0,
        "max_attempts": MAX_ATTEMPTS,
        "lease": None,
        "progress": {
            "current": 0,
            "total": 3,
            "percent": 0,
            "message": "Projection rebuild request is durable and waiting for a worker.",
        },
        "steps": [
            _pending_step("load_authority_snapshot", request_ref),
            _pending_step("build_projection_artifact", request_ref),
            _pending_step("activate_projection", request_ref),
        ],
        "error": None,
        "checkpoint": None,
        "staged_outputs": [],
        "published_outputs": [],
        "log_refs": [f"crp://{namespace_id}/logs/jobs/{job_id}"],
        "created_at": created_at,
        "updated_at": created_at,
    }


def _pending_step(name: str, input_ref: str) -> dict[str, object]:
    return {
        "name": name,
        "status": "pending",
        "attempt": 0,
        "started_at": None,
        "completed_at": None,
        "progress": 0,
        "input_refs": [input_ref],
        "staged_output_refs": [],
        "log_refs": [],
        "error": None,
    }


def _running_job(
    job: Mapping[str, object],
    *,
    attempt: int,
    worker_id: str,
    timestamp: str,
) -> dict[str, object]:
    updated = dict(job)
    expires_at = (
        _parse_datetime(timestamp) + timedelta(minutes=5)
    ).isoformat(timespec="seconds")
    updated["status"] = "running"
    updated["attempt"] = attempt
    updated["lease"] = {
        "worker_id": worker_id,
        "lease_token": _digest(
            _required_job_text(job, "id"),
            str(attempt),
            timestamp,
        ),
        "acquired_at": timestamp,
        "expires_at": expires_at,
    }
    updated["progress"] = {
        "current": 0,
        "total": 3,
        "percent": 0,
        "message": "Loading one coherent authority snapshot.",
    }
    updated["steps"] = [
        _step_status(step, "running" if index == 0 else "pending", attempt, timestamp)
        for index, step in enumerate(_job_steps(job))
    ]
    updated["error"] = None
    updated["checkpoint"] = None
    updated["staged_outputs"] = []
    updated["published_outputs"] = []
    updated["updated_at"] = timestamp
    return updated


def _artifact_staged_job(
    job: Mapping[str, object],
    *,
    artifact_id: str,
    authority_fingerprint: str,
    namespace_id: str,
    timestamp: str,
) -> dict[str, object]:
    artifact_uri = (
        f"crp://{namespace_id}/memory/retrieval-projections/{artifact_id}"
    )
    updated = dict(job)
    attempt = _job_attempt(job)
    updated["progress"] = {
        "current": 2,
        "total": 3,
        "percent": 66,
        "message": "Immutable projection artifact is verified and awaiting activation.",
    }
    updated["steps"] = [
        _step_status(
            step,
            "completed" if index < 2 else "running",
            attempt,
            timestamp,
            staged_output_ref=artifact_uri if index == 1 else None,
        )
        for index, step in enumerate(_job_steps(job))
    ]
    updated["checkpoint"] = {
        "resume_step": "activate_projection",
        "checkpoint_uri": f"crp://{namespace_id}/jobs/{job['id']}/projection-artifact",
        "state_hash": f"sha256:{authority_fingerprint}",
        "updated_at": timestamp,
    }
    updated["staged_outputs"] = [
        {
            "kind": "other",
            "uri": artifact_uri,
            "object_id": artifact_id,
            "published": False,
        }
    ]
    updated["updated_at"] = timestamp
    return updated


def _completed_job(
    job: Mapping[str, object],
    *,
    artifact_id: str,
    namespace_id: str,
    timestamp: str,
) -> dict[str, object]:
    artifact_uri = (
        f"crp://{namespace_id}/memory/retrieval-projections/{artifact_id}"
    )
    updated = dict(job)
    attempt = _job_attempt(job)
    updated["status"] = "completed"
    updated["lease"] = None
    updated["progress"] = {
        "current": 3,
        "total": 3,
        "percent": 100,
        "message": "Projection artifact is active for the requested authority fingerprint.",
    }
    updated["steps"] = [
        _step_status(
            step,
            "completed",
            attempt,
            timestamp,
            staged_output_ref=artifact_uri if index in {1, 2} else None,
        )
        for index, step in enumerate(_job_steps(job))
    ]
    updated["error"] = None
    updated["checkpoint"] = None
    updated["staged_outputs"] = []
    updated["published_outputs"] = [
        {
            "kind": "other",
            "uri": artifact_uri,
            "object_id": artifact_id,
            "published": True,
        }
    ]
    updated["updated_at"] = timestamp
    return updated


def _failed_job(
    job: Mapping[str, object],
    *,
    attempt: int,
    failed_step: str,
    failure_code: str,
    timestamp: str,
) -> dict[str, object]:
    updated = dict(job)
    error = {
        "code": failure_code,
        "message": "Projection rebuild did not activate; retry is allowed within the bounded attempt budget.",
        "retryable": attempt < _job_max_attempts(job),
        "failed_step": failed_step,
        "details": {
            "projection_body_recorded": False,
            "authority_fallback_required": True,
        },
    }
    updated["status"] = "failed"
    updated["attempt"] = attempt
    updated["lease"] = None
    updated["progress"] = {
        "current": 0,
        "total": 3,
        "percent": 0,
        "message": "Projection rebuild failed without activating derived output.",
    }
    updated["steps"] = [
        _step_status(
            step,
            "failed" if step.get("name") == failed_step else (
                "completed"
                if _step_order(str(step.get("name"))) < _step_order(failed_step)
                else "pending"
            ),
            attempt,
            timestamp,
            error=error if step.get("name") == failed_step else None,
        )
        for step in _job_steps(job)
    ]
    updated["error"] = error
    updated["checkpoint"] = None
    updated["staged_outputs"] = []
    updated["published_outputs"] = []
    updated["updated_at"] = timestamp
    return updated


def _step_status(
    step: Mapping[str, object],
    status: str,
    attempt: int,
    timestamp: str,
    *,
    staged_output_ref: str | None = None,
    error: Mapping[str, object] | None = None,
) -> dict[str, object]:
    updated = dict(step)
    updated["status"] = status
    updated["attempt"] = attempt
    updated["started_at"] = (
        timestamp if status != "pending" else None
    )
    updated["completed_at"] = (
        timestamp if status in {"completed", "failed"} else None
    )
    updated["progress"] = (
        100 if status in {"completed", "failed"} else 0
    )
    if staged_output_ref is not None:
        updated["staged_output_refs"] = [staged_output_ref]
    elif status == "pending":
        updated["staged_output_refs"] = []
    updated["error"] = dict(error) if error is not None else None
    return updated


def _validate_job_identity(
    job: Mapping[str, object],
    *,
    project_id: object,
    authority_identity: object,
    authority_fingerprint: object,
) -> None:
    if job.get("job_type") != JOB_TYPE:
        raise MemoryProjectionRebuildJobError(
            "memory projection job type drifted"
        )
    if not all(
        isinstance(value, str)
        for value in (
            project_id,
            authority_identity,
            authority_fingerprint,
        )
    ):
        raise MemoryProjectionRebuildJobError(
            "memory projection job request is incomplete"
        )
    expected_id = memory_projection_rebuild_job_id(
        project_id=str(project_id),
        authority_identity=str(authority_identity),
        authority_fingerprint=str(authority_fingerprint),
    )
    if job.get("id") != expected_id:
        raise MemoryProjectionRebuildJobError(
            "memory projection job identity drifted"
        )
    expected_digest = _projection_job_digest(
        namespace_id=PROJECTION_NAMESPACE_ID,
        project_id=str(project_id),
        authority_identity=str(authority_identity),
        authority_fingerprint=str(authority_fingerprint),
    )
    expected_key = f"memory-projection-rebuild-{expected_digest}"
    if job.get("idempotency_key") != expected_key:
        raise MemoryProjectionRebuildJobError(
            "memory projection job idempotency key drifted"
        )
    if job.get("status") not in {
        "pending",
        "running",
        "failed",
        "completed",
    }:
        raise MemoryProjectionRebuildJobError(
            "memory projection job status is not resumable"
        )


def _job_steps(job: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    steps = job.get("steps")
    if not isinstance(steps, list) or len(steps) != 3:
        raise MemoryProjectionRebuildJobError(
            "memory projection job requires three steps"
        )
    mapped = tuple(step for step in steps if isinstance(step, Mapping))
    if tuple(step.get("name") for step in mapped) != (
        "load_authority_snapshot",
        "build_projection_artifact",
        "activate_projection",
    ):
        raise MemoryProjectionRebuildJobError(
            "memory projection job steps drifted"
        )
    return mapped


def _job_attempt(job: Mapping[str, object]) -> int:
    value = job.get("attempt")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise MemoryProjectionRebuildJobError(
            "memory projection job attempt is invalid"
        )
    return value


def _job_max_attempts(job: Mapping[str, object]) -> int:
    value = job.get("max_attempts")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise MemoryProjectionRebuildJobError(
            "memory projection job max_attempts is invalid"
        )
    return value


def _step_order(name: str) -> int:
    order = {
        "load_authority_snapshot": 0,
        "build_projection_artifact": 1,
        "activate_projection": 2,
    }
    return order.get(name, 99)


def _required_mapping_text(
    mapping: Mapping[str, object],
    key: str,
) -> str:
    return _required_text(mapping.get(key), key)


def _required_job_text(
    job: Mapping[str, object],
    key: str,
) -> str:
    return _required_text(job.get(key), key)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MemoryProjectionRebuildJobError(f"{field} is required")
    return value.strip()


def _required_namespace(value: str) -> str:
    normalized = _required_text(value, "namespace_id")
    if not (
        normalized[0].isalnum()
        and normalized.replace("_", "").replace("-", "").isalnum()
        and normalized == normalized.lower()
    ):
        raise MemoryProjectionRebuildJobError(
            "namespace_id must be a lowercase URI-safe segment"
        )
    if normalized != PROJECTION_NAMESPACE_ID:
        raise MemoryProjectionRebuildJobError(
            "memory projection repository uses the fixed default namespace"
        )
    return normalized


def _require_fingerprint(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise MemoryProjectionRebuildJobError(
            "authority_fingerprint must be lowercase SHA-256"
        )


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise MemoryProjectionRebuildJobError(
            "job timestamp must be ISO 8601"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MemoryProjectionRebuildJobError(
            "job timestamp requires timezone"
        )
    return parsed


def _digest(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _projection_job_digest(
    *,
    namespace_id: str,
    project_id: str,
    authority_identity: str,
    authority_fingerprint: str,
) -> str:
    return _digest(
        namespace_id,
        project_id,
        authority_identity,
        authority_fingerprint,
        PROJECTION_VERSION,
        GENERATOR_POLICY_ID,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
