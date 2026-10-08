from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from core.job_runner import JobRepositoryPort
from core.memory_core import MemoryReaderPort

from .source_job_memory_loop import SourceJobMemoryLoop


ReadinessStatus = Literal["ready", "degraded"]


@dataclass(frozen=True, slots=True)
class RuntimeReadinessCheck:
    name: str
    status: ReadinessStatus
    detail: str


@dataclass(frozen=True, slots=True)
class ProductRuntimeReadiness:
    status: ReadinessStatus
    checks: tuple[RuntimeReadinessCheck, ...]
    completed_job_id: str | None
    resumed_job_id: str | None
    series_memory_id: str | None


class RecallIndexReadinessPort(Protocol):
    def rebuild(
        self,
        entries: Sequence[Mapping[str, object]],
        *,
        source: str,
        rebuilt_at: str | None = None,
        vector_enabled: bool = False,
    ) -> Mapping[str, object]:
        """Build a persistent Recall index in controlled runtime storage."""

    def manifest(self) -> Mapping[str, object] | None:
        """Return the active Recall index manifest."""

    def entries(self) -> Sequence[object]:
        """Return persisted Recall index entries."""


class GetProductRuntimeReadiness:
    """Executable Phase 2 runtime readiness smoke for Source / Job / Memory."""

    def __init__(
        self,
        *,
        loop: SourceJobMemoryLoop,
        jobs: JobRepositoryPort,
        memory: MemoryReaderPort,
        recall_index: RecallIndexReadinessPort | None = None,
    ) -> None:
        self._loop = loop
        self._jobs = jobs
        self._memory = memory
        self._recall_index = recall_index

    def execute(self) -> ProductRuntimeReadiness:
        checks: list[RuntimeReadinessCheck] = []
        completed_job_id: str | None = None
        resumed_job_id: str | None = None
        series_memory_id: str | None = None
        try:
            first = self._loop.run_text(
                title="Runtime readiness first source",
                content="Runtime readiness first atom.\nRuntime readiness second atom.",
                series_id="series-runtime-readiness",
                project_id="project-runtime-alpha",
            )
            completed_job_id = first.job_id
            series_memory_id = first.series_memory_id
            first_job = self._jobs.get(first.job_id)
            first_series = _memory_object(self._memory, "series_memory", first.series_memory_id)
            checks.append(
                _check(
                    "completed_multi_atom_publish",
                    first.status == "completed"
                    and len(first.atom_ids) == 2
                    and _job_completed(first_job)
                    and _series_has_scenarios(first_series, expected_count=1),
                    "first Source published two Atoms, one Scenario and one Series Memory",
                )
            )

            failed = self._loop.run_text(
                title="Runtime readiness failed source",
                content="Runtime readiness staged atom before resume.",
                series_id="series-runtime-readiness",
                project_id="project-runtime-beta",
                fail_after_staging=True,
            )
            failed_job = self._jobs.get(failed.job_id)
            checks.append(
                _check(
                    "checkpoint_staging_boundary",
                    failed.status == "failed"
                    and _job_failed_with_checkpoint(failed_job)
                    and failed_job is not None
                    and failed_job.get("published_outputs") == [],
                    "failed Job preserved checkpoint and kept formal memory unpublished",
                )
            )

            resumed = self._loop.resume_publish(
                failed.job_id,
                series_id="series-runtime-readiness",
                project_id="project-runtime-beta",
            )
            resumed_again = self._loop.resume_publish(
                failed.job_id,
                series_id="series-runtime-readiness",
                project_id="project-runtime-beta",
            )
            resumed_job_id = resumed.job_id
            resumed_job = self._jobs.get(resumed.job_id)
            merged_series = _memory_object(self._memory, "series_memory", resumed.series_memory_id)
            checks.append(
                _check(
                    "resume_publish_idempotent",
                    resumed.status == "completed"
                    and resumed_again == resumed
                    and _job_completed(resumed_job)
                    and _publish_step_count(resumed_job) == 1,
                    "checkpoint resume completed publish exactly once",
                )
            )
            checks.append(
                _check(
                    "series_memory_merge",
                    _series_has_scenarios(merged_series, expected_count=2)
                    and _series_has_projects(merged_series, {"project-runtime-alpha", "project-runtime-beta"}),
                    "Series Memory merged Scenarios and projects from both Sources",
                )
            )
            if self._recall_index is not None:
                self._recall_index.rebuild(
                    (
                        {
                            "object_id": "readiness-skill-alpha",
                            "project_id": "project-runtime-alpha",
                            "layer": "l3_project_skill",
                            "content": "runtime readiness recall skill evidence",
                            "source_refs": ["source-runtime-alpha#char:0-42"],
                            "trust_status": "user_confirmed",
                            "base_score": 0.8,
                        },
                        {
                            "object_id": "readiness-atom-alpha",
                            "project_id": "project-runtime-alpha",
                            "layer": "l1_atom",
                            "content": "runtime readiness recall atom evidence",
                            "source_refs": ["source-runtime-alpha#char:42-84"],
                            "trust_status": "system_generated",
                            "base_score": 0.6,
                        },
                    ),
                    source="runtime-readiness",
                    rebuilt_at="2026-06-30T23:30:00+08:00",
                )
                manifest = self._recall_index.manifest()
                entries = tuple(self._recall_index.entries())
                checks.append(
                    _check(
                        "persistent_recall_index_manifest",
                        _index_manifest_ready(manifest, expected_count=2),
                        "persistent Recall index manifest is lexical, populated and vector-disabled",
                    )
                )
                checks.append(
                    _check(
                        "persistent_recall_index_traceability",
                        len(entries) == 2 and all(_index_entry_has_source_refs(entry) for entry in entries),
                        "persistent Recall index entries preserve traceable source refs",
                    )
                )
        except Exception as error:
            checks.append(RuntimeReadinessCheck("runtime_exception", "degraded", str(error)))

        return ProductRuntimeReadiness(
            status="ready" if checks and all(check.status == "ready" for check in checks) else "degraded",
            checks=tuple(checks),
            completed_job_id=completed_job_id,
            resumed_job_id=resumed_job_id,
            series_memory_id=series_memory_id,
        )


def _check(name: str, passed: bool, detail: str) -> RuntimeReadinessCheck:
    return RuntimeReadinessCheck(name, "ready" if passed else "degraded", detail)


def _memory_object(memory: MemoryReaderPort, layer: str, object_id: str | None) -> Mapping[str, object] | None:
    if object_id is None:
        return None
    return memory.get(layer, object_id)


def _job_completed(job: Mapping[str, object] | None) -> bool:
    return job is not None and job.get("status") == "completed" and job.get("checkpoint") is None and job.get("staged_outputs") == []


def _job_failed_with_checkpoint(job: Mapping[str, object] | None) -> bool:
    checkpoint = job.get("checkpoint") if job is not None else None
    return job is not None and job.get("status") == "failed" and isinstance(checkpoint, Mapping) and checkpoint.get("resume_step") == "publish_atom"


def _publish_step_count(job: Mapping[str, object] | None) -> int:
    steps = job.get("steps") if job is not None else None
    if not isinstance(steps, list):
        return 0
    return sum(1 for step in steps if isinstance(step, Mapping) and step.get("name") == "publish_memory")


def _series_has_scenarios(series: Mapping[str, object] | None, *, expected_count: int) -> bool:
    scenario_ids = series.get("scenario_ids") if series is not None else None
    return isinstance(scenario_ids, list) and len(scenario_ids) == expected_count


def _series_has_projects(series: Mapping[str, object] | None, expected: set[str]) -> bool:
    project_ids = series.get("project_ids") if series is not None else None
    return isinstance(project_ids, list) and set(project_ids) == expected


def _index_manifest_ready(manifest: Mapping[str, object] | None, *, expected_count: int) -> bool:
    vector = manifest.get("vector") if manifest is not None else None
    return (
        manifest is not None
        and manifest.get("backend_kind") == "object_store_lexical"
        and manifest.get("entry_count") == expected_count
        and isinstance(vector, Mapping)
        and vector.get("enabled") is False
    )


def _index_entry_has_source_refs(entry: object) -> bool:
    source_refs = getattr(entry, "source_refs", None)
    if source_refs is None and isinstance(entry, Mapping):
        source_refs = entry.get("source_refs")
    if not isinstance(source_refs, Sequence) or isinstance(source_refs, (str, bytes)):
        return False
    return bool(source_refs) and all(isinstance(ref, str) and "#" in ref for ref in source_refs)
