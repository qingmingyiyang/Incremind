from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.memory_core.runtime import memory_candidate_id
from .ports import ObjectStorePort


class SourceOutputMemoryCandidateError(ValueError):
    """Raised when Source outputs cannot safely propose reviewable Memory Candidates."""


@dataclass(frozen=True, slots=True)
class SourceOutputMemoryCandidateResult:
    status: str
    project_id: str
    source_id: str
    candidate_id: str
    candidate_status: str
    target_layer: str
    evidence_kind: str
    source_refs_display: tuple[str, ...]
    memory_publication_state: str
    review_state: str
    blocked_operations: tuple[str, ...]


class CreateMemoryCandidateFromSourceOutput:
    """Create pending Memory Candidates from completed Source reads or media outputs."""

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        candidates: ObjectStoreMemoryCandidateRepository | None = None,
        namespace_id: str = "default",
        now: str | None = None,
    ) -> None:
        self._object_store = object_store
        self._candidates = candidates or ObjectStoreMemoryCandidateRepository(object_store)
        self._namespace_id = namespace_id
        self._now = now

    def execute_from_content_read(
        self,
        *,
        source_id: str,
        project_id: str,
        content_read_id: str | None = None,
        proposed_content: str | None = None,
        target_layer: str = "atom",
        candidate_type: str = "other",
        created_at: str | None = None,
        execution_ref: str | None = None,
    ) -> SourceOutputMemoryCandidateResult:
        clean_source_id = _required_input(source_id, "source_id")
        clean_project_id = _required_input(project_id, "project_id")
        clean_execution_ref = _optional_execution_ref(execution_ref)
        source = self._required_source(clean_source_id)
        read_id = content_read_id or f"content-read-{clean_source_id}"
        read_record = self._object_store.read("source_content_reads", read_id)
        if read_record is None:
            raise SourceOutputMemoryCandidateError("source content read record not found")
        if read_record.get("source_id") != clean_source_id:
            raise SourceOutputMemoryCandidateError("source content read does not match source")
        if read_record.get("status") != "completed":
            raise SourceOutputMemoryCandidateError("source content read must be completed")
        preview = _required_str(read_record, "preview")
        content = _candidate_content(proposed_content, preview)
        read_ref = f"crp://{self._namespace_id}/source-content-reads/{read_id}.json"
        source_ref = {"source_id": clean_source_id, "locator": "source:content", "quote": preview}
        input_refs = [
            {
                "kind": "source",
                "object_id": clean_source_id,
                "uri": _source_uri(self._namespace_id, clean_source_id),
            },
            {
                "kind": "source_content_read",
                "object_id": read_id,
                "uri": read_ref,
            },
        ]
        provenance: dict[str, object] = {
            "model_result_id": None,
            "model_request_id": None,
            "recall_result_id": None,
            "document_id": None,
            "document_revision": None,
            "source_content_read_id": read_id,
            "media_processing_output_id": None,
            "media_processing_job_id": None,
            "input_refs": input_refs,
        }
        id_parts = [clean_source_id, read_id, target_layer, candidate_type, content]
        if clean_execution_ref is not None:
            id_parts.append(clean_execution_ref)
        candidate = self._candidate(
            project_id=clean_project_id,
            source_id=clean_source_id,
            target_layer=target_layer,
            candidate_type=candidate_type,
            proposed_content=content,
            source_refs=(source_ref,),
            provenance=provenance,
            created_at=created_at,
            id_parts=tuple(id_parts),
        )
        saved = self._save_or_reuse_candidate(candidate)
        self._mark_content_read_candidate_created(
            source,
            read_record,
            _required_str(saved, "id"),
            _required_str(saved, "created_at"),
            execution_ref=clean_execution_ref,
        )
        return self._result(
            saved,
            source_id=clean_source_id,
            evidence_kind="source_content_read",
            source_refs=(source_ref,),
        )

    def execute_from_media_output(
        self,
        *,
        output_id: str,
        project_id: str,
        proposed_content: str | None = None,
        target_layer: str = "atom",
        candidate_type: str = "other",
        created_at: str | None = None,
        execution_ref: str | None = None,
    ) -> SourceOutputMemoryCandidateResult:
        clean_output_id = _required_input(output_id, "output_id")
        clean_project_id = _required_input(project_id, "project_id")
        clean_execution_ref = _optional_execution_ref(execution_ref)
        output = self._object_store.read("media_processing_outputs", clean_output_id)
        if output is None:
            raise SourceOutputMemoryCandidateError("media processing output not found")
        if output.get("status") != "completed":
            raise SourceOutputMemoryCandidateError("media processing output must be completed")
        source_id = _required_str(output, "source_id")
        source = self._required_source(source_id)
        job_id = _required_str(output, "job_id")
        job = self._object_store.read("media_processing_jobs", job_id)
        if job is None:
            raise SourceOutputMemoryCandidateError("media processing job not found")
        if job.get("source_id") != source_id:
            raise SourceOutputMemoryCandidateError("media processing job does not match source")
        preview = _required_str(output, "preview")
        content = _candidate_content(proposed_content, preview)
        output_kind = _required_str(output, "output_kind")
        source_ref = {"source_id": source_id, "locator": f"media:{output_kind}", "quote": preview}
        output_ref = _required_str(output, "ref")
        job_ref = f"crp://{self._namespace_id}/media-processing-jobs/{job_id}.json"
        candidate = self._candidate(
            project_id=clean_project_id,
            source_id=source_id,
            target_layer=target_layer,
            candidate_type=candidate_type,
            proposed_content=content,
            source_refs=(source_ref,),
            provenance={
                "model_result_id": None,
                "model_request_id": None,
                "recall_result_id": None,
                "document_id": None,
                "document_revision": None,
                "source_content_read_id": None,
                "media_processing_output_id": clean_output_id,
                "media_processing_job_id": job_id,
                "input_refs": [
                    {
                        "kind": "source",
                        "object_id": source_id,
                        "uri": _source_uri(self._namespace_id, source_id),
                    },
                    {
                        "kind": "media_processing_job",
                        "object_id": job_id,
                        "uri": job_ref,
                    },
                    {
                        "kind": "media_processing_output",
                        "object_id": clean_output_id,
                        "uri": output_ref,
                    },
                ],
            },
            created_at=created_at,
            id_parts=(
                source_id,
                clean_output_id,
                target_layer,
                candidate_type,
                content,
                *((clean_execution_ref,) if clean_execution_ref is not None else ()),
            ),
        )
        saved = self._save_or_reuse_candidate(candidate)
        self._mark_media_output_candidate_created(
            source,
            output,
            job,
            _required_str(saved, "id"),
            _required_str(saved, "created_at"),
            execution_ref=clean_execution_ref,
        )
        return self._result(
            saved,
            source_id=source_id,
            evidence_kind="media_processing_output",
            source_refs=(source_ref,),
        )

    def _candidate(
        self,
        *,
        project_id: str,
        source_id: str,
        target_layer: str,
        candidate_type: str,
        proposed_content: str,
        source_refs: tuple[Mapping[str, object], ...],
        provenance: Mapping[str, object],
        created_at: str | None,
        id_parts: tuple[str, ...],
    ) -> dict[str, object]:
        timestamp = created_at or self._now or _utc_now()
        return {
            "schema_version": "1.0.0",
            "id": memory_candidate_id(*id_parts),
            "project_id": project_id,
            "target_layer": target_layer,
            "candidate_type": candidate_type,
            "status": "pending_review",
            "proposed_content": proposed_content,
            "source_refs": [dict(ref) for ref in source_refs],
            "provenance": dict(provenance),
            "review": {
                "requires_user_confirmation": True,
                "auto_promote_allowed": False,
                "reason": "Source 输出只能进入待审记忆候选，不能自动写入长期记忆。",
                "reviewed_by": None,
                "reviewed_at": None,
            },
            "created_at": timestamp,
            "updated_at": timestamp,
        }

    def _required_source(self, source_id: str) -> Mapping[str, object]:
        source = self._object_store.read("sources", source_id)
        if source is None:
            raise SourceOutputMemoryCandidateError("source not found")
        return source

    def _save_or_reuse_candidate(self, candidate: Mapping[str, object]) -> Mapping[str, object]:
        candidate_id = _required_str(candidate, "id")
        existing = self._candidates.get(candidate_id)
        if existing is None:
            return self._candidates.save(candidate)
        if not _same_candidate_intent(existing, candidate):
            raise SourceOutputMemoryCandidateError("existing Memory Candidate payload drifted")
        return existing

    def _mark_content_read_candidate_created(
        self,
        source: Mapping[str, object],
        read_record: Mapping[str, object],
        candidate_id: str,
        created_at: str,
        *,
        execution_ref: str | None = None,
    ) -> None:
        read_id = _required_str(read_record, "id")
        updated_read = dict(read_record)
        updated_read["memory_publication"] = "candidate_created"
        updated_read["memory_candidate_id"] = candidate_id
        if execution_ref is not None:
            updated_read["memory_candidate_execution_ref"] = execution_ref
        self._write_if_changed("source_content_reads", read_id, updated_read)
        source_id = _required_str(source, "id")
        metadata = dict(source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {})
        content_read = dict(metadata.get("content_read") if isinstance(metadata.get("content_read"), Mapping) else {})
        content_read["memory_publication"] = "candidate_created"
        content_read["memory_candidate_id"] = candidate_id
        if execution_ref is not None:
            content_read["memory_candidate_execution_ref"] = execution_ref
        metadata["content_read"] = content_read
        updated_source = dict(source)
        updated_source["metadata"] = metadata
        self._write_if_changed("sources", source_id, updated_source)
        self._write_memory_candidate_event(
            source_id,
            candidate_id,
            "source_content_read",
            read_id,
            created_at,
            execution_ref=execution_ref,
        )

    def _mark_media_output_candidate_created(
        self,
        source: Mapping[str, object],
        output: Mapping[str, object],
        job: Mapping[str, object],
        candidate_id: str,
        created_at: str,
        *,
        execution_ref: str | None = None,
    ) -> None:
        output_id = _required_str(output, "id")
        updated_output = dict(output)
        updated_output["memory_publication"] = "candidate_created"
        updated_output["memory_candidate_id"] = candidate_id
        if execution_ref is not None:
            updated_output["memory_candidate_execution_ref"] = execution_ref
        self._write_if_changed("media_processing_outputs", output_id, updated_output)
        source_id = _required_str(source, "id")
        metadata = dict(source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {})
        media_processing = dict(
            metadata.get("media_processing") if isinstance(metadata.get("media_processing"), Mapping) else {}
        )
        media_processing["memory_publication"] = "candidate_created"
        media_processing["memory_candidate_id"] = candidate_id
        if execution_ref is not None:
            media_processing["memory_candidate_execution_ref"] = execution_ref
        metadata["media_processing"] = media_processing
        updated_source = dict(source)
        updated_source["metadata"] = metadata
        self._write_if_changed("sources", source_id, updated_source)
        self._write_memory_candidate_event(
            source_id,
            candidate_id,
            "media_processing_output",
            output_id,
            created_at,
            execution_ref=execution_ref,
        )
        updated_job = dict(job)
        updated_job["memory_publication"] = "candidate_created"
        updated_job["memory_candidate_id"] = candidate_id
        if execution_ref is not None:
            updated_job["memory_candidate_execution_ref"] = execution_ref
        self._write_if_changed("media_processing_jobs", _required_str(job, "id"), updated_job)

    def _write_memory_candidate_event(
        self,
        source_id: str,
        candidate_id: str,
        evidence_kind: str,
        evidence_id: str,
        created_at: str,
        *,
        execution_ref: str | None = None,
    ) -> str:
        event_id = f"event-memory-candidate-created-{source_id}-{evidence_id}"
        if len(event_id) > 128:
            # Media output identities already include their Source identity.
            # Repeating both can exceed the object-store segment limit.
            event_id = f"event-memory-candidate-created-{evidence_id}"
        if len(event_id) > 128:
            event_id = f"event-memory-candidate-created-{candidate_id}"
        event_ref = f"crp://{self._namespace_id}/activity/{event_id}.json"
        self._write_if_changed(
            "activity_events", event_id, {
                "schema_version": "1.0.0",
                "id": event_id,
                "type": "memory_candidate_created",
                "source_id": source_id,
                "status": "candidate_created",
                "contentRead": evidence_kind == "source_content_read",
                "memoryPublication": "candidate_created",
                "details": {
                    "candidate_id": candidate_id,
                    "evidence_kind": evidence_kind,
                    "evidence_id": evidence_id,
                    "auto_promote_allowed": False,
                    **({"execution_ref": execution_ref} if execution_ref is not None else {}),
                },
                "created_at": created_at,
                "ref": event_ref,
            },
        )
        return event_ref

    def _write_if_changed(self, collection: str, object_id: str, payload: Mapping[str, object]) -> None:
        if self._object_store.read(collection, object_id) == payload:
            return
        self._object_store.write(collection, object_id, payload, expected_revision=None)

    def _result(
        self,
        candidate: Mapping[str, object],
        *,
        source_id: str,
        evidence_kind: str,
        source_refs: tuple[Mapping[str, object], ...],
    ) -> SourceOutputMemoryCandidateResult:
        return SourceOutputMemoryCandidateResult(
            status="candidate_created",
            project_id=_required_str(candidate, "project_id"),
            source_id=source_id,
            candidate_id=_required_str(candidate, "id"),
            candidate_status=_required_str(candidate, "status"),
            target_layer=_required_str(candidate, "target_layer"),
            evidence_kind=evidence_kind,
            source_refs_display=tuple(_display_source_ref(ref) for ref in source_refs),
            memory_publication_state="candidate_created_not_published",
            review_state="pending_user_review",
            blocked_operations=(
                "long_term_memory_publication",
                "auto_promote_memory",
                "provider_execution",
                "source_content_read",
                "media_binary_read",
            ),
        )


def serialize_source_output_memory_candidate(result: SourceOutputMemoryCandidateResult) -> dict[str, object]:
    return {
        "status": result.status,
        "project_id": result.project_id,
        "source_id": result.source_id,
        "candidate_id": result.candidate_id,
        "candidate_status": result.candidate_status,
        "target_layer": result.target_layer,
        "evidence_kind": result.evidence_kind,
        "source_refs_display": list(result.source_refs_display),
        "memory_publication_state": result.memory_publication_state,
        "review_state": result.review_state,
        "blocked_operations": list(result.blocked_operations),
    }


def _candidate_content(proposed_content: str | None, fallback: str) -> str:
    content = proposed_content.strip() if isinstance(proposed_content, str) else ""
    if not content:
        content = fallback.strip()
    if not content:
        raise SourceOutputMemoryCandidateError("Memory Candidate requires proposed content")
    return content


def _same_candidate_intent(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    ignored = {"created_at", "updated_at"}
    return {key: value for key, value in left.items() if key not in ignored} == {
        key: value for key, value in right.items() if key not in ignored
    }


def _source_uri(namespace_id: str, source_id: str) -> str:
    return f"crp://{namespace_id}/sources/{source_id}"


def _display_source_ref(ref: Mapping[str, object]) -> str:
    source_id = ref.get("source_id")
    locator = ref.get("locator")
    if not isinstance(source_id, str) or not isinstance(locator, str):
        raise SourceOutputMemoryCandidateError("source ref display requires source_id and locator")
    return f"{source_id}#{locator}"


def _required_input(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise SourceOutputMemoryCandidateError(f"{field_name} is required")
    clean = value.strip()
    if not clean:
        raise SourceOutputMemoryCandidateError(f"{field_name} is required")
    return clean


def _optional_execution_ref(value: str | None) -> str | None:
    if value is None:
        return None
    prefix = "facts:effect/eff2_"
    digest = value[len(prefix):] if isinstance(value, str) and value.startswith(prefix) else ""
    if (
        not isinstance(value, str)
        or value != value.strip()
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise SourceOutputMemoryCandidateError(
            "execution_ref must identify an effect-v2 immutable fact"
        )
    return value


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise SourceOutputMemoryCandidateError(f"{key} is required")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
