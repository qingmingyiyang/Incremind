"""Lazy, request-local associations for the legacy review list only."""

from collections.abc import Mapping
from functools import cached_property


class LegacyReviewReadIndex:
    def __init__(self, documents, jobs, object_store, source_ids, project_id):
        self.documents = documents
        self.jobs = jobs
        self.object_store = object_store
        self.source_ids = frozenset(source_ids)
        self.project_id = project_id

    def _sources(self, entries):
        # One document/job may quote a source more than once. Non-string refs
        # were never equal to a source ID in the unindexed projection.
        return {entry.get("source_id") for entry in entries
                if isinstance(entry, Mapping) and isinstance(entry.get("source_id"), str)} & self.source_ids

    @cached_property
    def documents_by_source(self):
        grouped = {}
        # Other projects and archived documents must still participate in the
        # strict binding/ambiguity check performed by the caller.
        for document in self.documents.list(include_archived=True):
            for source_id in self._sources(document.get("source_refs", ())):
                grouped.setdefault(source_id, []).append(document)
        return grouped

    @cached_property
    def transforms_by_source(self):
        latest = {}
        for job in self.jobs.all():
            if job.get("job_type") != "workbench_content_transform" or job.get("project_id") != self.project_id:
                continue
            for source_id in self._sources(job.get("transform_items", ())):
                previous = latest.get(source_id)
                # max() previously retained the first job when keys tied.
                if previous is None or str(job.get("updated_at") or "") > str(previous.get("updated_at") or ""):
                    latest[source_id] = job
        return latest

    @cached_property
    def reads_by_source(self):
        latest = {}
        for read in self.object_store.list("source_content_reads"):
            source_id = read.get("source_id")
            if isinstance(source_id, str) and source_id in self.source_ids and read.get("status") == "completed":
                # Preserve repository order, not a new timestamp ordering.
                latest[source_id] = read
        return latest
