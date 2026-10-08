from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from core.ingestion_core import SourceRegistrarPort, SourceSubmission
from core.job_runner import JobRepositoryPort
from core.memory_core import MemoryReaderPort, MemoryWriterPort


LoopStatus = Literal["completed", "failed"]


@dataclass(frozen=True, slots=True)
class SourceJobMemoryResult:
    status: LoopStatus
    source_id: str
    job_id: str
    atom_id: str | None
    scenario_id: str | None
    series_memory_id: str | None
    published_output_ids: tuple[str, ...]
    atom_ids: tuple[str, ...] = ()


class SourceJobMemoryLoop:
    """Runs the minimum Source → Job → Memory loop after Phase 1 contracts exist."""

    def __init__(
        self,
        *,
        source_registrar: SourceRegistrarPort,
        job_repository: JobRepositoryPort,
        memory_reader: MemoryReaderPort,
        memory_writer: MemoryWriterPort,
        namespace_id: str = "default",
        now: str = "2026-06-29T17:00:00+08:00",
    ) -> None:
        self._source_registrar = source_registrar
        self._job_repository = job_repository
        self._memory_reader = memory_reader
        self._memory_writer = memory_writer
        self._namespace_id = namespace_id
        self._now = now

    def run_text(
        self,
        *,
        title: str,
        content: str,
        series_id: str = "series-default",
        project_id: str | None = None,
        fail_after_staging: bool = False,
    ) -> SourceJobMemoryResult:
        source = self._source_registrar.register(
            SourceSubmission(kind="text", title=title, content=content)
        )
        source_id = _required_str(source, "id")
        source_uri = _required_str(source, "storage_uri")
        job_id = f"job-extract-{source_id}"
        job = self._new_job(job_id, source_id, source_uri)
        self._job_repository.save(job)

        atoms = self._build_atoms(source_id, content)
        staged_outputs: list[dict[str, object]] = []
        staged_atom_uris: list[str] = []
        for atom in atoms:
            atom_id = _required_str(atom, "id")
            staged_atom_uri = self._staging_uri(job_id, "atoms", atom_id)
            self._memory_writer.save_candidate("atom", atom)
            staged_outputs.append(self._job_output("atom", staged_atom_uri, atom_id, published=False))
            staged_atom_uris.append(staged_atom_uri)
        job = self._with_completed_step(
            job,
            name="extract_atom_candidates",
            input_refs=[source_uri],
            staged_output_refs=staged_atom_uris,
            current=1,
            total=3,
            message=f"staged {len(atoms)} atom candidate(s)",
        )
        job["checkpoint"] = self._checkpoint(job_id, "publish_atom")
        job["staged_outputs"] = staged_outputs
        self._job_repository.save(job)

        if fail_after_staging:
            failed_job = self._fail_job(job, failed_step="publish_atom")
            self._job_repository.save(failed_job)
            return SourceJobMemoryResult(
                status="failed",
                source_id=source_id,
                job_id=job_id,
                atom_id=None,
                scenario_id=None,
                series_memory_id=None,
                published_output_ids=(),
            )

        published_outputs = self._publish_memory_objects(
            source_id=source_id,
            atoms=atoms,
            series_id=series_id,
            project_id=project_id,
        )
        completed_job = self._complete_job(job, published_outputs)
        self._job_repository.save(completed_job)
        return self._result_from_completed_job(completed_job)

    def resume_publish(
        self,
        job_id: str,
        *,
        series_id: str = "series-default",
        project_id: str | None = None,
    ) -> SourceJobMemoryResult:
        """Resume the publish step from a persisted checkpoint."""

        job = self._job_repository.get(job_id)
        if job is None:
            raise ValueError(f"job not found: {job_id}")
        if job.get("status") == "completed":
            return self._result_from_completed_job(job)
        published_outputs = list(self.publish_staged_job(job, series_id=series_id, project_id=project_id))
        completed_job = self._complete_job(job, published_outputs)
        self._job_repository.save(completed_job)
        return self._result_from_completed_job(completed_job)

    def publish_staged_job(
        self,
        job: Mapping[str, object],
        *,
        series_id: str = "series-default",
        project_id: str | None = None,
    ) -> tuple[Mapping[str, object], ...]:
        """Publish a checkpointed job without mutating its Job repository."""
        checkpoint = job.get("checkpoint")
        if not isinstance(checkpoint, Mapping) or checkpoint.get("resume_step") != "publish_atom":
            raise ValueError("job is not resumable at publish_atom")
        source_id = _required_str(job, "source_id")
        staged_outputs = job.get("staged_outputs")
        if not isinstance(staged_outputs, list) or not staged_outputs:
            raise ValueError("resumable job requires staged outputs")
        atom_outputs = _outputs(staged_outputs, "atom")
        if not atom_outputs:
            raise ValueError("resumable job requires staged atom outputs")
        atoms: list[Mapping[str, object]] = []
        for output in atom_outputs:
            atom_id = _required_str(output, "object_id")
            atom = self._memory_reader.get("atom", atom_id)
            if atom is None:
                atom = self._memory_reader.staged("atom", atom_id)
            if atom is None:
                raise ValueError(f"staged atom not found: {atom_id}")
            atoms.append(atom)
        published_outputs = self._publish_memory_objects(
            source_id=source_id,
            atoms=tuple(atoms),
            series_id=series_id,
            project_id=project_id,
        )
        return tuple(published_outputs)

    def _new_job(self, job_id: str, source_id: str, source_uri: str) -> dict[str, object]:
        return {
            "schema_version": "1.0.0",
            "id": job_id,
            "source_id": source_id,
            "job_type": "extract_memory",
            "idempotency_key": f"extract-memory-{source_id}",
            "status": "running",
            "attempt": 1,
            "max_attempts": 3,
            "lease": {
                "worker_id": "fixture-runtime",
                "lease_token": f"lease-token-{source_id}",
                "acquired_at": self._now,
                "expires_at": "2026-06-29T17:05:00+08:00",
            },
            "progress": {
                "current": 0,
                "total": 3,
                "percent": 0,
                "message": "registered source",
            },
            "steps": [
                {
                    "name": "persist_source",
                    "status": "completed",
                    "attempt": 1,
                    "started_at": self._now,
                    "completed_at": self._now,
                    "progress": 100,
                    "input_refs": [source_uri],
                    "staged_output_refs": [],
                    "log_refs": [self._log_uri(job_id, "persist_source")],
                    "error": None,
                }
            ],
            "error": None,
            "checkpoint": self._checkpoint(job_id, "extract_atom_candidates"),
            "staged_outputs": [],
            "published_outputs": [],
            "log_refs": [self._log_uri(job_id, "job")],
            "created_at": self._now,
            "updated_at": self._now,
        }

    def _with_completed_step(
        self,
        job: Mapping[str, object],
        *,
        name: str,
        input_refs: list[str],
        staged_output_refs: list[str],
        current: int,
        total: int,
        message: str,
    ) -> dict[str, object]:
        updated = dict(job)
        steps = list(updated["steps"]) if isinstance(updated.get("steps"), list) else []
        steps.append(
            {
                "name": name,
                "status": "completed",
                "attempt": 1,
                "started_at": self._now,
                "completed_at": self._now,
                "progress": 100,
                "input_refs": input_refs,
                "staged_output_refs": staged_output_refs,
                "log_refs": [self._log_uri(_required_str(job, "id"), name)],
                "error": None,
            }
        )
        updated["steps"] = steps
        updated["progress"] = {
            "current": current,
            "total": total,
            "percent": int(current / total * 100),
            "message": message,
        }
        updated["updated_at"] = self._now
        return updated

    def _complete_job(
        self,
        job: Mapping[str, object],
        published_outputs: list[dict[str, object]],
    ) -> dict[str, object]:
        if _has_completed_step(job, "publish_memory"):
            updated = dict(job)
        else:
            updated = self._with_completed_step(
                job,
                name="publish_memory",
                input_refs=[self._source_uri(_required_str(job, "source_id"))],
                staged_output_refs=[],
                current=3,
                total=3,
                message="published atom, scenario and series memory",
            )
        updated["status"] = "completed"
        updated["lease"] = None
        updated["error"] = None
        updated["checkpoint"] = None
        updated["staged_outputs"] = []
        updated["published_outputs"] = published_outputs
        updated["progress"] = {
            "current": 3,
            "total": 3,
            "percent": 100,
            "message": "published atom, scenario and series memory",
        }
        return updated

    def _publish_memory_objects(
        self,
        *,
        source_id: str,
        atoms: tuple[Mapping[str, object], ...],
        series_id: str,
        project_id: str | None,
    ) -> list[dict[str, object]]:
        if not atoms:
            raise ValueError("publish requires at least one atom")
        atom_outputs: list[dict[str, object]] = []
        atom_ids: list[str] = []
        source_refs: list[Mapping[str, object]] = []
        for atom in atoms:
            atom_id = _required_str(atom, "id")
            atom_ids.append(atom_id)
            source_refs.append(_first_source_ref(atom, source_id))
            if self._memory_reader.get("atom", atom_id) is None:
                self._memory_writer.publish("atom", atom)
                self._memory_writer.set_trust_status(
                    atom_id,
                    "system_generated",
                    "published by Phase 2 Source / Job / Memory loop",
                )
            atom_outputs.append(self._job_output("atom", self._memory_uri("atoms", atom_id), atom_id, published=True))
        scenario = self._build_scenario(source_id, source_refs, atom_ids, series_id, project_id)
        scenario_id = _required_str(scenario, "id")
        if self._memory_reader.get("scenario", scenario_id) is None:
            self._memory_writer.publish("scenario", scenario)
        series_memory = self._build_series_memory(source_refs, scenario_id, series_id, project_id)
        series_memory_id = _required_str(series_memory, "id")
        existing_series_memory = self._memory_reader.get("series_memory", series_memory_id)
        if existing_series_memory is None:
            self._memory_writer.publish("series_memory", series_memory)
        else:
            merged_series_memory = self._merge_series_memory(existing_series_memory, series_memory)
            self._memory_writer.publish("series_memory", merged_series_memory)
        return [
            *atom_outputs,
            self._job_output("scenario", self._memory_uri("scenarios", scenario_id), scenario_id, published=True),
            self._job_output(
                "series_memory",
                self._memory_uri("series", series_memory_id),
                series_memory_id,
                published=True,
            ),
        ]

    def _result_from_completed_job(self, job: Mapping[str, object]) -> SourceJobMemoryResult:
        source_id = _required_str(job, "source_id")
        job_id = _required_str(job, "id")
        published_outputs = job.get("published_outputs")
        if not isinstance(published_outputs, list) or not published_outputs:
            raise ValueError("completed job requires published outputs")
        atom_ids = tuple(_required_str(output, "object_id") for output in _outputs(published_outputs, "atom"))
        atom_id = atom_ids[0] if atom_ids else None
        scenario_id = _output_id(published_outputs, "scenario")
        series_memory_id = _output_id(published_outputs, "series_memory")
        linked_memory = self._memory_reader.list_by_source(source_id)
        published_ids = tuple(_required_str(item, "id") for item in linked_memory)
        return SourceJobMemoryResult(
            status="completed",
            source_id=source_id,
            job_id=job_id,
            atom_id=atom_id,
            scenario_id=scenario_id,
            series_memory_id=series_memory_id,
            published_output_ids=published_ids,
            atom_ids=atom_ids,
        )

    def _fail_job(self, job: Mapping[str, object], *, failed_step: str) -> dict[str, object]:
        updated = dict(job)
        error = {
            "code": "fixture_publish_failed",
            "message": "Fixture loop failed after staging; no memory was published.",
            "retryable": True,
            "failed_step": failed_step,
            "details": {},
        }
        steps = list(updated["steps"]) if isinstance(updated.get("steps"), list) else []
        steps.append(
            {
                "name": failed_step,
                "status": "failed",
                "attempt": 1,
                "started_at": self._now,
                "completed_at": self._now,
                "progress": 0,
                "input_refs": [],
                "staged_output_refs": [],
                "log_refs": [self._log_uri(_required_str(job, "id"), failed_step)],
                "error": error,
            }
        )
        updated["steps"] = steps
        updated["status"] = "failed"
        updated["lease"] = None
        updated["error"] = error
        updated["published_outputs"] = []
        updated["progress"] = {
            "current": 1,
            "total": 3,
            "percent": 33,
            "message": "failed before publishing formal memory",
        }
        updated["updated_at"] = self._now
        return updated

    def _build_atoms(
        self,
        source_id: str,
        content: str,
    ) -> tuple[dict[str, object], ...]:
        atoms: list[dict[str, object]] = []
        source_suffix = source_id.removeprefix("source-text-")
        for index, segment in enumerate(_segments(content), start=1):
            source_ref = {
                "source_id": source_id,
                "locator": f"char:{segment.start}-{segment.end}",
                "quote": segment.text,
            }
            segment_hash = hashlib.sha256(segment.text.encode("utf-8")).hexdigest()[:8]
            atom_id = f"atom-{source_suffix}-{index:03d}-{segment_hash}"
            atoms.append(
                {
                    "schema_version": "1.0.0",
                    "id": atom_id,
                    "source_id": source_id,
                    "content": segment.text,
                    "atom_type": "fact",
                    "tags": ["fixture", "phase-2"],
                    "confidence": 1.0,
                    "source_refs": [dict(source_ref)],
                    "revision": 1,
                    "created_at": self._now,
                    "updated_at": self._now,
                    "trust_status": "system_generated",
                }
            )
        return tuple(atoms)

    def _build_scenario(
        self,
        source_id: str,
        source_refs: list[Mapping[str, object]],
        atom_ids: list[str],
        series_id: str,
        project_id: str | None,
    ) -> dict[str, object]:
        scenario_id = f"scenario-{source_id.removeprefix('source-text-')}"
        return {
            "schema_version": "1.0.0",
            "id": scenario_id,
            "title": "Fixture Source Memory Scenario",
            "summary": f"Fixture loop converted one text Source into {len(atom_ids)} traceable Atom(s).",
            "atom_ids": list(atom_ids),
            "source_refs": [dict(source_ref) for source_ref in source_refs],
            "tags": ["fixture", "phase-2"],
            "series_id": series_id,
            "project_id": project_id,
            "stale": False,
            "stale_reason": None,
            "revision": 1,
            "created_at": self._now,
            "updated_at": self._now,
            "trust_status": "system_generated",
        }

    def _build_series_memory(
        self,
        source_refs: list[Mapping[str, object]],
        scenario_id: str,
        series_id: str,
        project_id: str | None,
    ) -> dict[str, object]:
        series_memory_id = f"series-memory-{series_id}"
        return {
            "schema_version": "1.0.0",
            "id": series_memory_id,
            "series_id": series_id,
            "scope": "project" if project_id else "series",
            "overview": "Fixture-backed Phase 2 loop has at least one traceable Source-derived scenario.",
            "scenario_ids": [scenario_id],
            "source_refs": [dict(source_ref) for source_ref in source_refs],
            "project_ids": [project_id] if project_id else [],
            "stale": False,
            "stale_reason": None,
            "revision": 1,
            "created_at": self._now,
            "updated_at": self._now,
            "trust_status": "system_generated",
        }

    def _merge_series_memory(
        self,
        existing: Mapping[str, object],
        incoming: Mapping[str, object],
    ) -> dict[str, object]:
        merged = dict(existing)
        scenario_ids = _merge_string_list(existing.get("scenario_ids"), incoming.get("scenario_ids"))
        source_refs = _merge_source_refs(existing.get("source_refs"), incoming.get("source_refs"))
        project_ids = _merge_string_list(existing.get("project_ids"), incoming.get("project_ids"))
        merged["scenario_ids"] = scenario_ids
        merged["source_refs"] = source_refs
        merged["project_ids"] = project_ids
        merged["scope"] = _merged_scope(existing.get("scope"), incoming.get("scope"), project_ids)
        if existing.get("trust_status") not in {"user_confirmed", "trusted"}:
            merged["overview"] = f"Fixture-backed Phase 2 loop has {len(scenario_ids)} traceable Source-derived scenario(s)."
            merged["trust_status"] = incoming.get("trust_status", existing.get("trust_status", "system_generated"))
        merged["stale"] = False
        merged["stale_reason"] = None
        existing_revision = existing.get("revision")
        merged["revision"] = (existing_revision if isinstance(existing_revision, int) else 1) + 1
        merged["updated_at"] = self._now
        return merged

    def _checkpoint(self, job_id: str, resume_step: str) -> dict[str, object]:
        state_hash = hashlib.sha256(f"{job_id}:{resume_step}".encode("utf-8")).hexdigest()
        return {
            "resume_step": resume_step,
            "checkpoint_uri": f"crp://{self._namespace_id}/jobs/{job_id}/checkpoint.json",
            "state_hash": f"sha256:{state_hash}",
            "updated_at": self._now,
        }

    def _job_output(self, kind: str, uri: str, object_id: str, *, published: bool) -> dict[str, object]:
        return {
            "kind": kind,
            "uri": uri,
            "object_id": object_id,
            "published": published,
        }

    def _staging_uri(self, job_id: str, collection: str, object_id: str) -> str:
        return f"crp://{self._namespace_id}/staging/jobs/{job_id}/{collection}/{object_id}"

    def _memory_uri(self, collection: str, object_id: str) -> str:
        return f"crp://{self._namespace_id}/memory/{collection}/{object_id}"

    def _source_uri(self, source_id: str) -> str:
        return f"crp://{self._namespace_id}/sources/{source_id}"

    def _log_uri(self, job_id: str, name: str) -> str:
        return f"crp://{self._namespace_id}/logs/jobs/{job_id}/{name}.log"


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _has_completed_step(job: Mapping[str, object], name: str) -> bool:
    steps = job.get("steps")
    if not isinstance(steps, list):
        return False
    return any(isinstance(step, dict) and step.get("name") == name and step.get("status") == "completed" for step in steps)


def _first_output(outputs: list[object], kind: str) -> Mapping[str, object]:
    for output in outputs:
        if isinstance(output, Mapping) and output.get("kind") == kind:
            return output
    raise ValueError(f"job requires staged {kind} output")


def _outputs(outputs: list[object], kind: str) -> tuple[Mapping[str, object], ...]:
    return tuple(output for output in outputs if isinstance(output, Mapping) and output.get("kind") == kind)


def _output_id(outputs: list[object], kind: str) -> str | None:
    for output in outputs:
        if isinstance(output, Mapping) and output.get("kind") == kind:
            return _required_str(output, "object_id")
    return None


def _first_source_ref(atom: Mapping[str, object], source_id: str) -> Mapping[str, object]:
    refs = atom.get("source_refs")
    if not isinstance(refs, list):
        raise ValueError("atom requires source refs for resume")
    for ref in refs:
        if isinstance(ref, Mapping) and ref.get("source_id") == source_id:
            return ref
    raise ValueError("atom does not reference the job source")


def _merge_string_list(existing: object, incoming: object) -> list[str]:
    merged: list[str] = []
    for values in (existing, incoming):
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, str) and value not in merged:
                merged.append(value)
    return merged


def _merge_source_refs(existing: object, incoming: object) -> list[dict[str, object]]:
    merged: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for refs in (existing, incoming):
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, Mapping):
                continue
            source_id = ref.get("source_id")
            locator = ref.get("locator")
            quote = ref.get("quote")
            if not isinstance(source_id, str) or not isinstance(locator, str) or not isinstance(quote, str):
                continue
            key = (source_id, locator, quote)
            if key in seen:
                continue
            seen.add(key)
            merged.append(dict(ref))
    return merged


def _merged_scope(existing_scope: object, incoming_scope: object, project_ids: list[str]) -> str:
    if existing_scope == "cross_project" or incoming_scope == "cross_project":
        return "cross_project"
    if project_ids or existing_scope == "project" or incoming_scope == "project":
        return "project"
    return "series"


@dataclass(frozen=True, slots=True)
class _TextSegment:
    start: int
    end: int
    text: str


def _segments(content: str) -> tuple[_TextSegment, ...]:
    segments: list[_TextSegment] = []
    position = 0
    for raw_part in content.splitlines(keepends=True):
        line_start = position
        line_end = position + len(raw_part)
        trimmed = raw_part.strip()
        if trimmed:
            leading = len(raw_part) - len(raw_part.lstrip())
            trailing = len(raw_part.rstrip())
            start = line_start + leading
            end = line_start + trailing
            segments.append(_TextSegment(start=start, end=end, text=content[start:end]))
        position = line_end
    if not segments and content:
        segments.append(_TextSegment(start=0, end=len(content), text=content))
    return tuple(segments)
