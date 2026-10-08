from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib

from core.job_runner import JobRepositoryPort
from core.search_and_recall import (
    IndexRebuildRequest,
    SqliteFts5DryRunResult,
    SqliteFts5Manifest,
    sqlite_fts5_manifest_payload,
)


class IndexRebuildJobHandoffError(ValueError):
    """Raised when an index rebuild request cannot safely become a Job."""


@dataclass(frozen=True, slots=True)
class IndexRebuildJobHandoffResult:
    job_id: str
    job: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class IndexRebuildJobResumeResult:
    job_id: str
    job: Mapping[str, object]
    published_output_uri: str | None


class CreateIndexRebuildJob:
    """Persist an index rebuild request as a pending Job without executing rebuild work."""

    def __init__(self, *, jobs: JobRepositoryPort, namespace_id: str = "default") -> None:
        self._jobs = jobs
        self._namespace_id = namespace_id

    def execute(
        self,
        *,
        rebuild_request: IndexRebuildRequest,
        candidate_manifest: SqliteFts5Manifest | Mapping[str, object],
        created_at: str | None = None,
    ) -> IndexRebuildJobHandoffResult:
        manifest = sqlite_fts5_manifest_payload(candidate_manifest)
        _validate_handoff(rebuild_request, manifest)
        timestamp = created_at or _utc_now()
        job = _pending_rebuild_job(
            rebuild_request=rebuild_request,
            manifest=manifest,
            namespace_id=self._namespace_id,
            timestamp=timestamp,
        )
        existing = self._jobs.get(str(job["id"]))
        if existing is not None:
            if existing.get("idempotency_key") != job.get("idempotency_key"):
                raise IndexRebuildJobHandoffError("index rebuild job identity drifted")
            return IndexRebuildJobHandoffResult(job_id=str(job["id"]), job=dict(existing))
        self._jobs.save(job)
        return IndexRebuildJobHandoffResult(job_id=str(job["id"]), job=job)


class ResumeVerifiedIndexRebuildJob:
    """Complete a rebuild_index Job only after worker dry-run verification succeeds."""

    def __init__(self, *, jobs: JobRepositoryPort, namespace_id: str = "default") -> None:
        self._jobs = jobs
        self._namespace_id = namespace_id

    def execute(
        self,
        *,
        job_id: str,
        verification: SqliteFts5DryRunResult | Mapping[str, object],
        verified_at: str | None = None,
    ) -> IndexRebuildJobResumeResult:
        job = self._jobs.get(job_id)
        if job is None:
            raise IndexRebuildJobResumeError("index rebuild job was not found")
        _validate_rebuild_job(job)
        if job.get("status") == "completed":
            published = job.get("published_outputs")
            published_uri = _first_output_uri(published)
            return IndexRebuildJobResumeResult(job_id=job_id, job=dict(job), published_output_uri=published_uri)
        timestamp = verified_at or _utc_now()
        try:
            payload = _verification_payload(verification)
            _validate_verification_matches_job(job, payload, self._namespace_id)
        except IndexRebuildJobResumeError as exc:
            failed_job = _failed_verification_job(job, str(exc), timestamp)
            self._jobs.save(failed_job)
            raise
        completed_job = _completed_verification_job(job, payload, self._namespace_id, timestamp)
        self._jobs.save(completed_job)
        return IndexRebuildJobResumeResult(
            job_id=job_id,
            job=completed_job,
            published_output_uri=_required_output_uri(completed_job["published_outputs"][0]),
        )


class IndexRebuildJobResumeError(ValueError):
    """Raised when a rebuild_index Job tries to publish without verified worker output."""


def _validate_handoff(rebuild_request: IndexRebuildRequest, manifest: Mapping[str, object]) -> None:
    if rebuild_request.backend_kind != "sqlite_fts5":
        raise IndexRebuildJobHandoffError("index rebuild job currently requires sqlite_fts5 backend")
    if manifest.get("backend_kind") != rebuild_request.backend_kind:
        raise IndexRebuildJobHandoffError("index rebuild job requires matching manifest backend")
    if manifest.get("source_fingerprint") != rebuild_request.source_fingerprint:
        raise IndexRebuildJobHandoffError("index rebuild job requires matching source fingerprint")
    manifest_refs = manifest.get("source_refs")
    if not isinstance(manifest_refs, list) or tuple(manifest_refs) != rebuild_request.source_refs:
        raise IndexRebuildJobHandoffError("index rebuild job requires matching source refs")
    vector = manifest.get("vector")
    if not isinstance(vector, Mapping) or vector.get("enabled") is not False:
        raise IndexRebuildJobHandoffError("index rebuild job requires vector disabled manifest")


def _pending_rebuild_job(
    *,
    rebuild_request: IndexRebuildRequest,
    manifest: Mapping[str, object],
    namespace_id: str,
    timestamp: str,
) -> dict[str, object]:
    digest = _digest(
        rebuild_request.backend_kind,
        rebuild_request.reason,
        rebuild_request.source_fingerprint,
        *rebuild_request.source_refs,
    )
    job_id = f"job-index-rebuild-{namespace_id}-{digest[:16]}"
    request_uri = f"crp://{namespace_id}/recall/index-rebuild-requests/{digest[:16]}"
    manifest_uri = f"crp://{namespace_id}/recall/index-manifests/{manifest['id']}"
    source_uris = [_source_ref_uri(namespace_id, ref) for ref in rebuild_request.source_refs]
    return {
        "schema_version": "1.0.0",
        "id": job_id,
        "source_id": f"index-ledger-{rebuild_request.source_fingerprint[:16]}",
        "job_type": "rebuild_index",
        "idempotency_key": f"index-rebuild-{digest}",
        "status": "pending",
        "attempt": 0,
        "max_attempts": 2,
        "lease": None,
        "progress": {
            "current": 0,
            "total": 3,
            "percent": 0,
            "message": "Index rebuild request is persisted and waiting for an index worker.",
        },
        "steps": [
            {
                "name": "load_rebuild_request",
                "status": "pending",
                "attempt": 0,
                "started_at": None,
                "completed_at": None,
                "progress": 0,
                "input_refs": [request_uri],
                "staged_output_refs": [],
                "log_refs": [],
                "error": None,
            },
            {
                "name": "build_sqlite_fts5_index",
                "status": "pending",
                "attempt": 0,
                "started_at": None,
                "completed_at": None,
                "progress": 0,
                "input_refs": [manifest_uri, *source_uris],
                "staged_output_refs": [],
                "log_refs": [],
                "error": None,
            },
            {
                "name": "verify_index_traceability",
                "status": "pending",
                "attempt": 0,
                "started_at": None,
                "completed_at": None,
                "progress": 0,
                "input_refs": [manifest_uri],
                "staged_output_refs": [],
                "log_refs": [],
                "error": None,
            },
        ],
        "error": None,
        "checkpoint": None,
        "staged_outputs": [],
        "published_outputs": [],
        "log_refs": [f"crp://{namespace_id}/logs/jobs/{job_id}"],
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _source_ref_uri(namespace_id: str, ref: str) -> str:
    if "#rev:" not in ref:
        raise IndexRebuildJobHandoffError("index rebuild job source refs must use source_id#rev:<revision>")
    source_id, revision = ref.split("#rev:", 1)
    if not source_id or not revision:
        raise IndexRebuildJobHandoffError("index rebuild job source refs require source id and revision")
    return f"crp://{namespace_id}/sources/{source_id}/revisions/{revision}"


def _digest(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _validate_rebuild_job(job: Mapping[str, object]) -> None:
    if job.get("job_type") != "rebuild_index":
        raise IndexRebuildJobResumeError("index rebuild resume requires rebuild_index job")
    if job.get("status") not in {"pending", "running", "failed", "completed"}:
        raise IndexRebuildJobResumeError("index rebuild resume requires resumable job status")
    published_outputs = job.get("published_outputs")
    if job.get("status") != "completed" and isinstance(published_outputs, list) and published_outputs:
        raise IndexRebuildJobResumeError("index rebuild job published before verification")


def _verification_payload(verification: SqliteFts5DryRunResult | Mapping[str, object]) -> dict[str, object]:
    if isinstance(verification, SqliteFts5DryRunResult):
        payload: dict[str, object] = {
            "status": verification.status,
            "backend_kind": verification.backend_kind,
            "database_uri": verification.database_uri,
            "manifest_id": verification.manifest_id,
            "entry_count": verification.entry_count,
            "hit_count": verification.hit_count,
            "hit_object_ids": list(verification.hit_object_ids),
            "vector_enabled": verification.vector_enabled,
            "source_refs": list(verification.source_refs),
        }
    else:
        payload = dict(verification)
    if payload.get("status") != "ready":
        raise IndexRebuildJobResumeError("index rebuild worker verification must be ready")
    if payload.get("backend_kind") != "sqlite_fts5":
        raise IndexRebuildJobResumeError("index rebuild worker verification requires sqlite_fts5 backend")
    if payload.get("vector_enabled") is not False:
        raise IndexRebuildJobResumeError("index rebuild worker verification requires vector disabled")
    manifest_id = payload.get("manifest_id")
    if not isinstance(manifest_id, str) or not manifest_id:
        raise IndexRebuildJobResumeError("index rebuild worker verification requires manifest_id")
    database_uri = payload.get("database_uri")
    if not isinstance(database_uri, str) or not database_uri.startswith("file://"):
        raise IndexRebuildJobResumeError("index rebuild worker verification requires file database_uri")
    for key in ("entry_count", "hit_count"):
        value = payload.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise IndexRebuildJobResumeError(f"index rebuild worker verification requires positive {key}")
    source_refs = payload.get("source_refs")
    if not isinstance(source_refs, list) or not source_refs:
        raise IndexRebuildJobResumeError("index rebuild worker verification requires source_refs")
    for ref in source_refs:
        if not isinstance(ref, str) or "#" not in ref:
            raise IndexRebuildJobResumeError("index rebuild worker verification source refs must be traceable")
    return payload


def _validate_verification_matches_job(
    job: Mapping[str, object],
    verification: Mapping[str, object],
    namespace_id: str,
) -> None:
    expected_manifest_ref = f"crp://{namespace_id}/recall/index-manifests/{verification['manifest_id']}"
    input_refs = tuple(
        ref
        for step in _steps(job)
        for ref in _string_sequence(step.get("input_refs"))
    )
    if expected_manifest_ref not in input_refs:
        raise IndexRebuildJobResumeError("index rebuild worker verification manifest does not match job inputs")


def _failed_verification_job(job: Mapping[str, object], message: str, timestamp: str) -> dict[str, object]:
    updated = dict(job)
    updated["status"] = "failed"
    updated["lease"] = None
    updated["progress"] = {
        "current": 2,
        "total": 3,
        "percent": 66,
        "message": "Index worker verification failed; outputs were not published.",
    }
    updated["error"] = {
        "code": "index_verification_failed",
        "message": message,
        "retryable": True,
        "failed_step": "verify_index_traceability",
        "details": {"guard": "worker_verification_required"},
    }
    updated["checkpoint"] = None
    updated["published_outputs"] = []
    updated["steps"] = _steps_with_status(job, timestamp, failed_step="verify_index_traceability")
    updated["updated_at"] = timestamp
    return updated


def _completed_verification_job(
    job: Mapping[str, object],
    verification: Mapping[str, object],
    namespace_id: str,
    timestamp: str,
) -> dict[str, object]:
    job_id = _required_job_string(job, "id")
    manifest_id = _required_mapping_string(verification, "manifest_id")
    output_uri = f"crp://{namespace_id}/recall/index-verifications/{job_id}"
    updated = dict(job)
    updated["status"] = "completed"
    updated["lease"] = None
    updated["progress"] = {
        "current": 3,
        "total": 3,
        "percent": 100,
        "message": "Index rebuild worker verification completed; candidate artifact is verified.",
    }
    updated["steps"] = _completed_steps(job, output_uri, timestamp)
    updated["error"] = None
    updated["checkpoint"] = None
    updated["staged_outputs"] = []
    updated["published_outputs"] = [
        {
            "kind": "other",
            "uri": output_uri,
            "object_id": f"verified-{manifest_id}",
            "published": True,
        }
    ]
    updated["updated_at"] = timestamp
    return updated


def _completed_steps(job: Mapping[str, object], output_uri: str, timestamp: str) -> list[dict[str, object]]:
    completed: list[dict[str, object]] = []
    for step in _steps(job):
        updated_step = dict(step)
        updated_step["status"] = "completed"
        updated_step["attempt"] = max(_step_attempt(step), 1)
        updated_step["started_at"] = updated_step.get("started_at") or timestamp
        updated_step["completed_at"] = timestamp
        updated_step["progress"] = 100
        updated_step["error"] = None
        if updated_step.get("name") == "verify_index_traceability":
            updated_step["staged_output_refs"] = [output_uri]
        completed.append(updated_step)
    return completed


def _steps_with_status(job: Mapping[str, object], timestamp: str, *, failed_step: str) -> list[dict[str, object]]:
    steps: list[dict[str, object]] = []
    for step in _steps(job):
        updated_step = dict(step)
        name = updated_step.get("name")
        updated_step["attempt"] = max(_step_attempt(step), 1)
        updated_step["started_at"] = updated_step.get("started_at") or timestamp
        if name == failed_step:
            updated_step["status"] = "failed"
            updated_step["completed_at"] = timestamp
            updated_step["progress"] = 100
            updated_step["error"] = {
                "code": "index_verification_failed",
                "message": "Worker verification failed; no outputs were published.",
                "retryable": True,
                "failed_step": failed_step,
                "details": {"guard": "worker_verification_required"},
            }
        else:
            updated_step["status"] = "completed"
            updated_step["completed_at"] = timestamp
            updated_step["progress"] = 100
            updated_step["error"] = None
        steps.append(updated_step)
    return steps


def _steps(job: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    steps = job.get("steps")
    if not isinstance(steps, list) or not steps:
        raise IndexRebuildJobResumeError("index rebuild job requires steps")
    return tuple(step for step in steps if isinstance(step, Mapping))


def _string_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _step_attempt(step: Mapping[str, object]) -> int:
    attempt = step.get("attempt")
    return attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else 0


def _first_output_uri(outputs: object) -> str | None:
    if not isinstance(outputs, list) or not outputs:
        return None
    output = outputs[0]
    if not isinstance(output, Mapping):
        return None
    return _required_output_uri(output)


def _required_output_uri(output: Mapping[str, object]) -> str:
    uri = output.get("uri")
    if not isinstance(uri, str) or not uri:
        raise IndexRebuildJobResumeError("index rebuild job output requires uri")
    return uri


def _required_job_string(job: Mapping[str, object], key: str) -> str:
    value = job.get(key)
    if not isinstance(value, str) or not value:
        raise IndexRebuildJobResumeError(f"index rebuild job requires {key}")
    return value


def _required_mapping_string(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise IndexRebuildJobResumeError(f"index rebuild worker verification requires {key}")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
