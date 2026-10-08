from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from core.storage_provider import ObjectStorePort, read_json_object_store_collection


@dataclass(slots=True)
class InMemoryJobRepository:
    """In-memory Job repository for deterministic runtime smoke tests."""

    _jobs: dict[str, dict[str, object]] = field(default_factory=dict)

    def get(self, job_id: str) -> Mapping[str, object] | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        return dict(job)

    def save(self, job: Mapping[str, object]) -> None:
        job_id = job.get("id")
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("job requires id")
        self._jobs[job_id] = dict(job)

    def all(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(job) for job in self._jobs.values())

    def delete(self, job_id: str) -> bool:
        return self._jobs.pop(job_id, None) is not None

    def list_jobs(
        self,
        *,
        job_type: str | None = None,
        status: str | None = None,
        source_id: str | None = None,
        limit: int | None = None,
    ) -> tuple[Mapping[str, object], ...]:
        jobs = self.all()
        filtered = [
            job for job in jobs
            if _matches_filter(job, job_type=job_type, status=status, source_id=source_id)
        ]
        if limit is not None and limit >= 0:
            filtered = filtered[:limit]
        return tuple(filtered)


@dataclass(slots=True)
class ObjectStoreJobRepository:
    """Job repository backed by the rebuild ObjectStore."""

    object_store: ObjectStorePort
    collection: str = "jobs"

    def get(self, job_id: str) -> Mapping[str, object] | None:
        return self.object_store.read(self.collection, job_id)

    def save(self, job: Mapping[str, object]) -> None:
        job_id = job.get("id")
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("job requires id")
        self.object_store.write(self.collection, job_id, job, expected_revision=None)

    def all(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(job) for job in self.object_store.list(self.collection))

    def delete(self, job_id: str) -> bool:
        return bool(self.object_store.delete(self.collection, job_id))

    def all_with_storage_ids(self) -> tuple[tuple[str, Mapping[str, object]], ...]:
        root = getattr(self.object_store, "root", None)
        namespace_id = getattr(self.object_store, "namespace_id", None)
        if root is not None and isinstance(namespace_id, str):
            return tuple(
                (record.object_id, dict(record.payload))
                for record in read_json_object_store_collection(
                    root,
                    namespace_id=namespace_id,
                    collection=self.collection,
                )
            )
        return tuple(
            (_legacy_runtime_job_id(job), dict(job))
            for job in self.all()
        )

    def list_jobs(
        self,
        *,
        job_type: str | None = None,
        status: str | None = None,
        source_id: str | None = None,
        limit: int | None = None,
    ) -> tuple[Mapping[str, object], ...]:
        jobs = self.all()
        filtered = [
            job for job in jobs
            if _matches_filter(job, job_type=job_type, status=status, source_id=source_id)
        ]
        if limit is not None and limit >= 0:
            filtered = filtered[:limit]
        return tuple(filtered)


def _matches_filter(
    job: Mapping[str, object],
    *,
    job_type: str | None,
    status: str | None,
    source_id: str | None,
) -> bool:
    if job_type is not None and job.get("job_type") != job_type:
        return False
    if status is not None and job.get("status") != status:
        return False
    if source_id is not None and job.get("source_id") != source_id:
        return False
    return True


def _legacy_runtime_job_id(job: Mapping[str, object]) -> str:
    value = job.get("id")
    if not isinstance(value, str) or not value:
        value = job.get("job_id")
    if not isinstance(value, str) or not value:
        raise ValueError("legacy Job identity is invalid")
    return value
