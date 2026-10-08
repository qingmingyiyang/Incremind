"""Durable review admission for newly captured old-OS Sources.

The capture Job and its final orchestration Job can have different IDs. An
intent retains the first Job that made the Source visible to processing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from core.storage_provider import SQLiteStructuredRecordStore


COLLECTION = "workspace_review_intents"


@dataclass(slots=True)
class ReviewIntentAdmission:
    records: SQLiteStructuredRecordStore
    object_store: object

    def admit(self, job_id: str, source_ids: Sequence[str], *, existing_job_ok: bool = False) -> None:
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("review intent requires a Job id")
        unique_ids = tuple(dict.fromkeys(source_ids))
        if not unique_ids:
            raise ValueError("review intent requires a Source")
        read = getattr(self.object_store, "read", None)
        revision = getattr(self.object_store, "revision", None)
        if not callable(read) or not callable(revision):
            raise RuntimeError("review intent requires durable Source evidence")
        frozen: list[dict[str, object]] = []
        for source_id in unique_ids:
            if not isinstance(source_id, str) or not source_id:
                raise ValueError("review intent Source id is invalid")
            source = read("sources", source_id)
            if not isinstance(source, Mapping) or source.get("id") != source_id:
                raise ValueError(f"review intent Source is unavailable: {source_id}")
            project_id = source.get("project_id", "default")
            if not isinstance(project_id, str) or not project_id:
                raise ValueError(f"review intent Source project is invalid: {source_id}")
            source_revision = revision("sources", source_id)
            if not isinstance(source_revision, int) or source_revision < 1:
                raise ValueError(f"review intent Source revision is invalid: {source_id}")
            frozen.append({
                "schema_version": "1.0.0", "id": f"review-{source_id}",
                "source_id": source_id, "project_id": project_id,
                "job_id": job_id, "state": "pending",
                "source_revision": source_revision,
            })
        with self.records.begin() as tx:
            for intent in frozen:
                row = tx.read(COLLECTION, str(intent["id"]))
                if row is None:
                    tx.put(COLLECTION, str(intent["id"]), intent, expected_revision=0)
                    continue
                current = row.payload
                if (
                    current.get("id") != intent["id"]
                    or current.get("source_id") != intent["source_id"]
                    or current.get("project_id") != intent["project_id"]
                    or current.get("schema_version") != intent["schema_version"]
                    or not isinstance(current.get("source_revision"), int)
                    or current["source_revision"] < 1
                    or (
                        current.get("job_id") != job_id
                        and (
                            not existing_job_ok
                            or not isinstance(current.get("job_id"), str)
                            or not current["job_id"].startswith("job-capture-")
                        )
                    )
                ):
                    raise ValueError(f"review intent binding conflict: {intent['source_id']}")
            tx.commit()


@dataclass(slots=True)
class ReviewIntentJobRepository:
    """Record capture intent before the first capture Job is saved."""

    repository: object
    admission: ReviewIntentAdmission

    def get(self, job_id: str) -> Mapping[str, object] | None:
        return self.repository.get(job_id)

    def save(self, job: Mapping[str, object]) -> None:
        job_id = job.get("id")
        source_id = job.get("source_id")
        if not isinstance(job_id, str) or not isinstance(source_id, str):
            raise ValueError("capture Job requires Source and Job ids")
        source_ids = [source_id]
        for output in job.get("outputs", ()):
            if isinstance(output, Mapping) and output.get("kind") == "link_sources":
                children = output.get("source_ids")
                if not isinstance(children, list) or not all(isinstance(child, str) for child in children):
                    raise ValueError("collection Job has invalid child Source ids")
                source_ids.extend(children)
        self.admission.admit(job_id, source_ids, existing_job_ok=job_id.startswith("job-intake-"))
        self.repository.save(job)
