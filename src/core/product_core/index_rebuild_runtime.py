from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
from threading import Lock
from typing import Callable, Protocol

from core.job_runner import JobRepositoryPort
from core.search_and_recall import (
    ObjectStoreSqliteFts5ActivationRepository,
    RecallIndexEntry,
    RecallQuery,
    SqliteFts5DryRunIndex,
    build_recall_authority_ledger,
    build_recall_entries_from_object_store,
    evaluate_index_freshness,
    sqlite_fts5_verification_query,
)
from .index_rebuild_job import ResumeVerifiedIndexRebuildJob


class IndexRebuildObjectStorePort(Protocol):
    """Product-owned structured-store boundary for index rebuild orchestration."""

    @property
    def namespace_id(self) -> str: ...

    def read(self, collection: str, object_id: str) -> Mapping[str, object] | None: ...

    def list(self, collection: str) -> Sequence[Mapping[str, object]]: ...

    def write(
        self,
        collection: str,
        object_id: str,
        payload: Mapping[str, object],
        expected_revision: int | None,
    ) -> int: ...


class IndexRebuildRuntimeError(ValueError):
    """Raised when a persisted index rebuild cannot safely converge."""


@dataclass(frozen=True, slots=True)
class IndexRebuildRuntimeResult:
    job: Mapping[str, object]
    active_manifest: Mapping[str, object]
    replayed: bool


_LOCKS_GUARD = Lock()
_LOCKS: dict[str, Lock] = {}


def run_index_rebuild_job(
    *,
    object_store: IndexRebuildObjectStorePort,
    jobs: JobRepositoryPort,
    runtime_root: Path,
    job_id: str,
    activated_by: str = "library_index_runtime",
    after_verification_persisted: Callable[[], None] | None = None,
    recall_entries_loader: Callable[[], Sequence[RecallIndexEntry]] | None = None,
) -> IndexRebuildRuntimeResult:
    lock = _job_lock(runtime_root, job_id)
    with lock:
        return _run_locked(
            object_store=object_store,
            jobs=jobs,
            runtime_root=runtime_root,
            job_id=job_id,
            activated_by=activated_by,
            after_verification_persisted=after_verification_persisted,
            recall_entries_loader=recall_entries_loader,
        )


def recover_index_rebuild_jobs(
    *, object_store: IndexRebuildObjectStorePort, jobs: JobRepositoryPort, runtime_root: Path,
    limit: int = 8,
    recall_entries_loader: Callable[[], Sequence[RecallIndexEntry]] | None = None,
) -> tuple[str, ...]:
    recovered: list[str] = []
    candidates = jobs.list_jobs(job_type="rebuild_index", limit=max(1, limit))  # type: ignore[attr-defined]
    for job in candidates:
        job_id = job.get("id")
        if not isinstance(job_id, str) or job.get("status") not in {"pending", "running", "completed"}:
            continue
        try:
            result = run_index_rebuild_job(
                object_store=object_store, jobs=jobs, runtime_root=runtime_root, job_id=job_id,
                activated_by="sidecar_startup_recovery",
                recall_entries_loader=recall_entries_loader,
            )
        except (IndexRebuildRuntimeError, OSError, ValueError):
            continue
        if result.active_manifest.get("verified_job_id") == job_id:
            recovered.append(job_id)
    return tuple(recovered)


def _run_locked(
    *, object_store, jobs, runtime_root: Path, job_id: str, activated_by: str,
    after_verification_persisted: Callable[[], None] | None,
    recall_entries_loader: Callable[[], Sequence[RecallIndexEntry]] | None,
):
    job = jobs.get(job_id)
    if job is None or job.get("job_type") != "rebuild_index":
        raise IndexRebuildRuntimeError("index rebuild job was not found")
    active = object_store.read("recall_index_manifests", "active")
    if job.get("status") == "completed" and isinstance(active, Mapping) and active.get("verified_job_id") == job_id:
        return IndexRebuildRuntimeResult(dict(job), dict(active), True)
    candidate = _candidate_manifest(object_store, job)
    if job.get("status") == "completed":
        verification = object_store.read("recall_index_verifications", job_id)
        if not isinstance(verification, Mapping):
            raise IndexRebuildRuntimeError("completed index rebuild is missing verification evidence")
        activated = ObjectStoreSqliteFts5ActivationRepository(object_store).activate(
            candidate_manifest=candidate,
            verified_job=job,
            activated_by=activated_by,
            database_uri=_required_string(verification, "database_uri"),
        )
        return IndexRebuildRuntimeResult(dict(job), dict(activated.active_manifest), True)
    if job.get("status") == "failed":
        job = _reset_failed_job(job)
        jobs.save(job)
    running = _running_job(job)
    jobs.save(running)
    try:
        entries = _recall_entries(object_store, recall_entries_loader)
        ledger = build_recall_authority_ledger(entries)
        _require_candidate_matches_ledger(candidate, ledger)
        if not entries:
            raise IndexRebuildRuntimeError("index rebuild requires at least one traceable Library entry")
        final_path = _candidate_database_path(runtime_root, job_id)
        part_path = final_path.with_suffix(".sqlite3.part")
        final_path.parent.mkdir(parents=True, exist_ok=True)
        if part_path.exists():
            part_path.unlink()
        query_text = sqlite_fts5_verification_query(entries[0].content)
        if not query_text:
            raise IndexRebuildRuntimeError("index rebuild entry has no searchable content")
        verification = SqliteFts5DryRunIndex(part_path).rebuild_and_query(
            entries,
            manifest=candidate,
            query=RecallQuery(
                text=query_text,
                project_id=None,
                layers=(),
                allowed_trust_statuses=(),
                limit=max(1, min(20, len(entries))),
            ),
        )
        if verification.hit_count < 1:
            raise IndexRebuildRuntimeError("index rebuild verification query returned no traceable hit")
        current_ledger = build_recall_authority_ledger(
            _recall_entries(object_store, recall_entries_loader)
        )
        _require_candidate_matches_ledger(candidate, current_ledger)
        os.replace(part_path, final_path)
        evidence = {
            "schema_version": "1.0.0",
            "id": job_id,
            "status": "ready",
            "backend_kind": "sqlite_fts5",
            "database_uri": final_path.resolve(strict=False).as_uri(),
            "manifest_id": verification.manifest_id,
            "entry_count": verification.entry_count,
            "hit_count": verification.hit_count,
            "hit_object_ids": list(verification.hit_object_ids),
            "vector_enabled": False,
            "source_refs": list(verification.source_refs),
            "source_fingerprint": candidate.get("source_fingerprint"),
        }
        object_store.write("recall_index_verifications", job_id, evidence, expected_revision=None)
        if after_verification_persisted is not None:
            after_verification_persisted()
        completed = ResumeVerifiedIndexRebuildJob(jobs=jobs, namespace_id=object_store.namespace_id).execute(
            job_id=job_id, verification=evidence
        ).job
        activated = ObjectStoreSqliteFts5ActivationRepository(object_store).activate(
            candidate_manifest=candidate,
            verified_job=completed,
            activated_by=activated_by,
            database_uri=evidence["database_uri"],
        )
        return IndexRebuildRuntimeResult(dict(completed), dict(activated.active_manifest), False)
    except Exception as exc:
        latest = jobs.get(job_id)
        if latest is not None and latest.get("status") != "completed":
            jobs.save(_failed_job(latest, str(exc)))
        if isinstance(exc, IndexRebuildRuntimeError):
            raise
        raise IndexRebuildRuntimeError(str(exc)) from exc


def _candidate_manifest(object_store: IndexRebuildObjectStorePort, job: Mapping[str, object]) -> Mapping[str, object]:
    manifest_id = ""
    for step in job.get("steps", []):
        if not isinstance(step, Mapping):
            continue
        for ref in step.get("input_refs", []):
            if isinstance(ref, str) and "/recall/index-manifests/" in ref:
                manifest_id = ref.rsplit("/", 1)[-1]
                break
    candidate = object_store.read("recall_index_manifests", manifest_id) if manifest_id else None
    if not isinstance(candidate, Mapping):
        raise IndexRebuildRuntimeError("index rebuild candidate manifest was not found")
    return candidate


def _recall_entries(
    object_store: IndexRebuildObjectStorePort,
    loader: Callable[[], Sequence[RecallIndexEntry]] | None,
) -> tuple[RecallIndexEntry, ...]:
    values = loader() if loader is not None else build_recall_entries_from_object_store(object_store)
    return tuple(values)


def _require_candidate_matches_ledger(candidate: Mapping[str, object], ledger) -> None:
    freshness = evaluate_index_freshness(candidate, ledger)
    if freshness.status != "fresh":
        raise IndexRebuildRuntimeError("source ledger changed during index rebuild")


def _candidate_database_path(runtime_root: Path, job_id: str) -> Path:
    digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:24]
    return runtime_root / ".rebuild-data" / "recall-indexes" / f"recall-{digest}.sqlite3"


def _running_job(job: Mapping[str, object]) -> dict[str, object]:
    updated = dict(job)
    updated["status"] = "running"
    updated["attempt"] = max(1, int(updated.get("attempt") or 0))
    updated["progress"] = {"current": 1, "total": 3, "percent": 10, "message": "Building verified SQLite FTS5 candidate."}
    updated["error"] = None
    updated["updated_at"] = _utc_now()
    return updated


def _reset_failed_job(job: Mapping[str, object]) -> dict[str, object]:
    updated = dict(job)
    attempt = int(updated.get("attempt") or 0)
    max_attempts = int(updated.get("max_attempts") or 0)
    if attempt >= max_attempts:
        raise IndexRebuildRuntimeError("index rebuild retry budget is exhausted")
    updated["status"] = "pending"
    updated["attempt"] = attempt + 1
    updated["error"] = None
    updated["updated_at"] = _utc_now()
    return updated


def _failed_job(job: Mapping[str, object], message: str) -> dict[str, object]:
    updated = dict(job)
    updated["status"] = "failed"
    updated["lease"] = None
    updated["progress"] = {"current": 1, "total": 3, "percent": 33, "message": "Index rebuild failed; active index was preserved."}
    updated["error"] = {
        "code": "index_rebuild_failed", "message": message, "retryable": True,
        "failed_step": "build_sqlite_fts5_index", "details": {},
    }
    updated["updated_at"] = _utc_now()
    return updated


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _required_string(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise IndexRebuildRuntimeError(f"index rebuild verification requires {key}")
    return value


def _job_lock(runtime_root: Path, job_id: str) -> Lock:
    key = f"{runtime_root.resolve(strict=False)}\n{job_id}"
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, Lock())
