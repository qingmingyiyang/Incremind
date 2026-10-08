from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.memory_core.runtime import memory_candidate_id
from .ports import ObjectStorePort

from .four_layer_memory_prompt import build_four_layer_memory_prompt


class FourLayerMemoryCandidateImportError(ValueError):
    """Raised when provider output cannot safely become reviewable Memory Candidates."""


@dataclass(frozen=True, slots=True)
class FourLayerMemoryCandidateImportResult:
    status: str
    project_id: str
    source_id: str
    evidence_kind: str
    candidate_ids: tuple[str, ...]
    candidate_count: int
    insufficient_evidence: tuple[str, ...]
    memory_publication_state: str
    blocked_operations: tuple[str, ...]


class ImportFourLayerMemoryCandidatesFromProviderOutput:
    """Import AI-returned four-layer candidate JSON without publishing or staging memory."""

    _FORBIDDEN_TOP_LEVEL_KEYS = {
        "memory_atoms",
        "memory_scenarios",
        "memory_series_memory",
        "project_skills",
        "memory_publications",
        "memory_transitions",
        "api_keys",
        "cookies",
    }

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
        provider_output: Mapping[str, object],
        content_read_id: str | None = None,
        created_at: str | None = None,
    ) -> FourLayerMemoryCandidateImportResult:
        clean_source_id = _required_input(source_id, "source_id")
        clean_project_id = _required_input(project_id, "project_id")
        source = self._required_source(clean_source_id)
        read_id = content_read_id or f"content-read-{clean_source_id}"
        read_record = self._object_store.read("source_content_reads", read_id)
        if read_record is None:
            raise FourLayerMemoryCandidateImportError("source content read record not found")
        if read_record.get("source_id") != clean_source_id:
            raise FourLayerMemoryCandidateImportError("source content read does not match source")
        if read_record.get("status") != "completed":
            raise FourLayerMemoryCandidateImportError("source content read must be completed")
        preview = _required_str(read_record, "preview")
        source_ref = {"source_id": clean_source_id, "locator": "source:content", "quote": preview}
        return self._execute(
            source=source,
            project_id=clean_project_id,
            provider_output=provider_output,
            source_refs=(source_ref,),
            provenance={
                "model_result_id": None,
                "model_request_id": None,
                "recall_result_id": None,
                "document_id": None,
                "document_revision": None,
                "source_content_read_id": read_id,
                "media_processing_output_id": None,
                "media_processing_job_id": None,
                "input_refs": [
                    {
                        "kind": "source",
                        "object_id": clean_source_id,
                        "uri": _source_uri(self._namespace_id, clean_source_id),
                    },
                    {
                        "kind": "source_content_read",
                        "object_id": read_id,
                        "uri": f"crp://{self._namespace_id}/source-content-reads/{read_id}.json",
                    },
                ],
            },
            evidence_kind="source_content_read",
            evidence_id=read_id,
            created_at=created_at,
        )

    def execute_from_media_output(
        self,
        *,
        output_id: str,
        project_id: str,
        provider_output: Mapping[str, object],
        created_at: str | None = None,
    ) -> FourLayerMemoryCandidateImportResult:
        clean_output_id = _required_input(output_id, "output_id")
        clean_project_id = _required_input(project_id, "project_id")
        output = self._object_store.read("media_processing_outputs", clean_output_id)
        if output is None:
            raise FourLayerMemoryCandidateImportError("media processing output not found")
        if output.get("status") != "completed":
            raise FourLayerMemoryCandidateImportError("media processing output must be completed")
        source_id = _required_str(output, "source_id")
        source = self._required_source(source_id)
        job_id = _required_str(output, "job_id")
        job = self._object_store.read("media_processing_jobs", job_id)
        if job is None:
            raise FourLayerMemoryCandidateImportError("media processing job not found")
        if job.get("source_id") != source_id:
            raise FourLayerMemoryCandidateImportError("media processing job does not match source")
        preview = _required_str(output, "preview")
        output_kind = _required_str(output, "output_kind")
        source_ref = {"source_id": source_id, "locator": f"media:{output_kind}", "quote": preview}
        return self._execute(
            source=source,
            project_id=clean_project_id,
            provider_output=provider_output,
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
                        "uri": f"crp://{self._namespace_id}/media-processing-jobs/{job_id}.json",
                    },
                    {
                        "kind": "media_processing_output",
                        "object_id": clean_output_id,
                        "uri": _required_str(output, "ref"),
                    },
                ],
            },
            evidence_kind="media_processing_output",
            evidence_id=clean_output_id,
            created_at=created_at,
        )

    def _execute(
        self,
        *,
        source: Mapping[str, object],
        project_id: str,
        provider_output: Mapping[str, object],
        source_refs: tuple[Mapping[str, object], ...],
        provenance: Mapping[str, object],
        evidence_kind: str,
        evidence_id: str,
        created_at: str | None,
    ) -> FourLayerMemoryCandidateImportResult:
        _validate_provider_output_boundary(provider_output, source_refs=source_refs)
        timestamp = created_at or self._now or _utc_now()
        candidates_payload = provider_output.get("candidates")
        if not isinstance(candidates_payload, Sequence) or isinstance(candidates_payload, (str, bytes)):
            raise FourLayerMemoryCandidateImportError("provider output candidates must be a list")
        if not candidates_payload:
            raise FourLayerMemoryCandidateImportError("provider output must include at least one candidate")
        allowed_layers = set(
            build_four_layer_memory_prompt(
                source_kind=evidence_kind,
                source_summary=evidence_id,
            ).output_contract["allowed_target_layers"]
        )
        saved_ids: list[str] = []
        for index, candidate_payload in enumerate(candidates_payload):
            candidate = _candidate_from_provider_payload(
                candidate_payload,
                project_id=project_id,
                source_refs=source_refs,
                provenance=provenance,
                timestamp=timestamp,
                id_parts=(_required_str(source, "id"), evidence_id, str(index)),
                allowed_layers=allowed_layers,
            )
            saved = self._candidates.save(candidate)
            saved_ids.append(_required_str(saved, "id"))
        self._write_import_event(
            source_id=_required_str(source, "id"),
            evidence_kind=evidence_kind,
            evidence_id=evidence_id,
            candidate_ids=tuple(saved_ids),
        )
        return FourLayerMemoryCandidateImportResult(
            status="candidates_imported",
            project_id=project_id,
            source_id=_required_str(source, "id"),
            evidence_kind=evidence_kind,
            candidate_ids=tuple(saved_ids),
            candidate_count=len(saved_ids),
            insufficient_evidence=_insufficient_evidence(provider_output),
            memory_publication_state="candidates_created_not_published",
            blocked_operations=(
                "long_term_memory_publication",
                "staging_memory_write",
                "auto_promote_memory",
                "provider_execution",
            ),
        )

    def _required_source(self, source_id: str) -> Mapping[str, object]:
        source = self._object_store.read("sources", source_id)
        if source is None:
            raise FourLayerMemoryCandidateImportError("source not found")
        return source

    def _write_import_event(
        self,
        *,
        source_id: str,
        evidence_kind: str,
        evidence_id: str,
        candidate_ids: tuple[str, ...],
    ) -> None:
        event_id = f"event-four-layer-memory-candidates-imported-{_short_event_key(source_id, evidence_id)}"
        self._object_store.write(
            "activity_events",
            event_id,
            {
                "schema_version": "1.0.0",
                "id": event_id,
                "type": "four_layer_memory_candidates_imported",
                "source_id": source_id,
                "status": "candidates_imported",
                "details": {
                    "candidate_ids": list(candidate_ids),
                    "evidence_kind": evidence_kind,
                    "evidence_id": evidence_id,
                    "auto_promote_allowed": False,
                    "memory_publication_state": "candidates_created_not_published",
                },
                "created_at": self._now or _utc_now(),
                "ref": f"crp://{self._namespace_id}/activity/{event_id}.json",
            },
            expected_revision=None,
        )


def serialize_four_layer_memory_candidate_import(
    result: FourLayerMemoryCandidateImportResult,
) -> dict[str, object]:
    return {
        "status": result.status,
        "project_id": result.project_id,
        "source_id": result.source_id,
        "evidence_kind": result.evidence_kind,
        "candidate_ids": list(result.candidate_ids),
        "candidate_count": result.candidate_count,
        "insufficient_evidence": list(result.insufficient_evidence),
        "memory_publication_state": result.memory_publication_state,
        "blocked_operations": list(result.blocked_operations),
    }


def _candidate_from_provider_payload(
    value: object,
    *,
    project_id: str,
    source_refs: tuple[Mapping[str, object], ...],
    provenance: Mapping[str, object],
    timestamp: str,
    id_parts: tuple[str, ...],
    allowed_layers: set[str],
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise FourLayerMemoryCandidateImportError("candidate must be an object")
    if value.get("status") not in {None, "pending_review"}:
        raise FourLayerMemoryCandidateImportError("candidate status must be pending_review")
    target_layer = _required_candidate_str(value, "target_layer")
    if target_layer not in allowed_layers:
        raise FourLayerMemoryCandidateImportError("candidate target_layer is not allowed")
    candidate_type = _required_candidate_str(value, "candidate_type")
    proposed_content = _required_candidate_str(value, "proposed_content")
    candidate_source_refs = _source_refs(value.get("source_refs")) or tuple(dict(ref) for ref in source_refs)
    _assert_refs_within_evidence(candidate_source_refs, source_refs)
    evidence_refs = _source_refs(value.get("evidence_refs"))
    if evidence_refs:
        _assert_refs_within_evidence(evidence_refs, source_refs)
    review = value.get("review")
    if not isinstance(review, Mapping):
        raise FourLayerMemoryCandidateImportError("candidate review is required")
    if review.get("requires_user_confirmation") is not True:
        raise FourLayerMemoryCandidateImportError("candidate review must require user confirmation")
    if review.get("auto_promote_allowed") is not False:
        raise FourLayerMemoryCandidateImportError("candidate review must disable auto promotion")
    review_prompt = _required_candidate_str(value, "review_prompt")
    return {
        "schema_version": "1.0.0",
        "id": memory_candidate_id(*id_parts, target_layer, candidate_type, proposed_content),
        "project_id": project_id,
        "target_layer": target_layer,
        "candidate_type": candidate_type,
        "status": "pending_review",
        "proposed_content": proposed_content,
        "source_refs": [dict(ref) for ref in candidate_source_refs],
        "provenance": dict(provenance),
        "review": {
            "requires_user_confirmation": True,
            "auto_promote_allowed": False,
            "reason": review_prompt,
            "reviewed_by": None,
            "reviewed_at": None,
        },
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _validate_provider_output_boundary(
    provider_output: Mapping[str, object],
    *,
    source_refs: tuple[Mapping[str, object], ...],
) -> None:
    for key in ("candidates", "insufficient_evidence", "provider_boundary"):
        if key not in provider_output:
            raise FourLayerMemoryCandidateImportError(f"provider output missing required key: {key}")
    forbidden = sorted(
        key for key in provider_output if key in ImportFourLayerMemoryCandidatesFromProviderOutput._FORBIDDEN_TOP_LEVEL_KEYS
    )
    if forbidden:
        raise FourLayerMemoryCandidateImportError(f"provider output includes forbidden key: {forbidden[0]}")
    if not isinstance(provider_output.get("provider_boundary"), Mapping):
        raise FourLayerMemoryCandidateImportError("provider_boundary must be an object")
    if _contains_forbidden_string(provider_output):
        raise FourLayerMemoryCandidateImportError("provider output includes forbidden secret or local path")
    candidates = provider_output.get("candidates")
    if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes)):
        for candidate in candidates:
            if isinstance(candidate, Mapping):
                refs = _source_refs(candidate.get("source_refs"))
                if refs:
                    _assert_refs_within_evidence(refs, source_refs)


def _assert_refs_within_evidence(
    refs: Sequence[Mapping[str, object]],
    allowed_refs: Sequence[Mapping[str, object]],
) -> None:
    allowed = {(_required_str(ref, "source_id"), _required_str(ref, "locator")) for ref in allowed_refs}
    for ref in refs:
        key = (_required_str(ref, "source_id"), _required_str(ref, "locator"))
        if key not in allowed:
            raise FourLayerMemoryCandidateImportError("candidate source_refs must match authorized evidence")


def _source_refs(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    refs: list[Mapping[str, object]] = []
    seen: set[tuple[str, str, str | None]] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        source_id = item.get("source_id")
        locator = item.get("locator")
        quote = item.get("quote")
        if not isinstance(source_id, str) or not source_id:
            continue
        if not isinstance(locator, str) or not locator:
            continue
        key = (source_id, locator, quote if isinstance(quote, str) else None)
        if key in seen:
            continue
        seen.add(key)
        ref: dict[str, object] = {"source_id": source_id, "locator": locator}
        if isinstance(quote, str):
            ref["quote"] = quote
        refs.append(ref)
    return tuple(refs)


def _insufficient_evidence(provider_output: Mapping[str, object]) -> tuple[str, ...]:
    value = provider_output.get("insufficient_evidence")
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise FourLayerMemoryCandidateImportError("insufficient_evidence must be a list")
    reasons: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise FourLayerMemoryCandidateImportError("insufficient_evidence must contain strings")
        reasons.append(item.strip())
    return tuple(reasons)


def _contains_forbidden_string(value: object) -> bool:
    if isinstance(value, str):
        return _looks_like_secret(value) or _looks_like_absolute_path(value)
    if isinstance(value, Mapping):
        return any(_contains_forbidden_string(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_forbidden_string(item) for item in value)
    return False


def _looks_like_secret(value: str) -> bool:
    lowered = value.lower()
    if "cookie" in lowered or "api_key" in lowered or "api key" in lowered:
        return True
    return bool(re.search(r"\bsk-[A-Za-z0-9_-]{12,}\b", value))


def _looks_like_absolute_path(value: str) -> bool:
    return bool(re.search(r"\b[A-Za-z]:\\", value)) or value.startswith("\\\\")


def _source_uri(namespace_id: str, source_id: str) -> str:
    return f"crp://{namespace_id}/sources/{source_id}"


def _short_event_key(source_id: str, evidence_id: str) -> str:
    return hashlib.sha256(f"{source_id}:{evidence_id}".encode("utf-8")).hexdigest()[:16]


def _required_input(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise FourLayerMemoryCandidateImportError(f"{field_name} is required")
    clean = value.strip()
    if not clean:
        raise FourLayerMemoryCandidateImportError(f"{field_name} is required")
    return clean


def _required_candidate_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FourLayerMemoryCandidateImportError(f"candidate {key} is required")
    return value.strip()


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise FourLayerMemoryCandidateImportError(f"{key} is required")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
