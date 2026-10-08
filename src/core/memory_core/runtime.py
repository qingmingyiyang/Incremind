from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from core.storage_provider import ObjectStorePort, ObjectStoreRevisionError, SQLiteStructuredRecordStore

from .manual_publication_contract import (
    STAGING_PUBLICATION_CONTEXT_COLLECTION,
    context_id,
    validate_context,
)


class MemoryCandidateRepositoryError(ValueError):
    """Raised when a Memory Candidate would bypass review or provenance rules."""


@dataclass(slots=True)
class InMemoryMemoryStore:
    """Stores staged and published memory objects without filesystem writes."""

    _published: dict[str, dict[str, dict[str, object]]] = field(default_factory=dict)
    _candidates: dict[str, dict[str, dict[str, object]]] = field(default_factory=dict)
    _transitions: list[dict[str, object]] = field(default_factory=list)

    def get(self, layer: str, object_id: str) -> Mapping[str, object] | None:
        item = self._published.get(layer, {}).get(object_id)
        if item is None or not memory_is_readable(item):
            return None
        return dict(item)

    def list(self, layer: str) -> Sequence[Mapping[str, object]]:
        return tuple(dict(item) for item in self._published.get(layer, {}).values() if memory_is_readable(item))

    def list_by_source(self, source_id: str) -> Sequence[Mapping[str, object]]:
        matches: list[Mapping[str, object]] = []
        for objects in self._published.values():
            for item in objects.values():
                if memory_is_readable(item) and _references_source(item, source_id):
                    matches.append(dict(item))
        return tuple(matches)

    def list_by_project(self, project_id: str) -> Sequence[Mapping[str, object]]:
        return _project_memory_from_collections(
            project_id=project_id,
            series_memories=tuple(self._published.get("series_memory", {}).values()),
            scenarios=tuple(self._published.get("scenario", {}).values()),
            atoms=tuple(self._published.get("atom", {}).values()),
        )

    def save_candidate(self, layer: str, payload: Mapping[str, object], *, publication_context: Mapping[str, object] | None = None) -> str:
        object_id = _object_id(payload)
        self._candidates.setdefault(layer, {})[object_id] = dict(payload)
        return object_id

    def publish(self, layer: str, payload: Mapping[str, object]) -> str:
        object_id = _object_id(payload)
        self._published.setdefault(layer, {})[object_id] = dict(payload)
        self._candidates.get(layer, {}).pop(object_id, None)
        return object_id

    def set_trust_status(self, object_id: str, trust_status: str, reason: str) -> None:
        self._transitions.append(
            {
                "object_id": object_id,
                "trust_status": trust_status,
                "reason": reason,
            }
        )

    def staged(self, layer: str, object_id: str) -> Mapping[str, object] | None:
        item = self._candidates.get(layer, {}).get(object_id)
        if item is None:
            return None
        return dict(item)

    def transitions(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self._transitions)


@dataclass(slots=True)
class ObjectStoreMemoryStore:
    """Stores staged and published memory objects in the rebuild ObjectStore."""

    object_store: ObjectStorePort

    def get(self, layer: str, object_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(_published_collection(layer), object_id)
        return item if item is not None and memory_is_readable(item) else None

    def list(self, layer: str) -> Sequence[Mapping[str, object]]:
        return tuple(dict(item) for item in self.object_store.list(_published_collection(layer)) if memory_is_readable(item))

    def list_by_source(self, source_id: str) -> Sequence[Mapping[str, object]]:
        matches: list[Mapping[str, object]] = []
        for layer in ("atom", "scenario", "series_memory"):
            for item in self.object_store.list(_published_collection(layer)):
                if memory_is_readable(item) and _references_source(item, source_id):
                    matches.append(dict(item))
        return tuple(matches)

    def list_by_project(self, project_id: str) -> Sequence[Mapping[str, object]]:
        return _project_memory_from_collections(
            project_id=project_id,
            series_memories=self.object_store.list(_published_collection("series_memory")),
            scenarios=self.object_store.list(_published_collection("scenario")),
            atoms=self.object_store.list(_published_collection("atom")),
        )

    def save_candidate(self, layer: str, payload: Mapping[str, object], *, publication_context: Mapping[str, object] | None = None) -> str:
        object_id = _object_id(payload)
        self.object_store.write(_staged_collection(layer), object_id, payload, expected_revision=None)
        if publication_context is not None:
            normalized = validate_context(publication_context, namespace_id=self.object_store.namespace_id, layer=layer, draft_id=object_id)
            self.object_store.write(STAGING_PUBLICATION_CONTEXT_COLLECTION, context_id(layer, object_id), normalized, expected_revision=0)
        return object_id

    def publish(self, layer: str, payload: Mapping[str, object]) -> str:
        object_id = _object_id(payload)
        self.object_store.write(_published_collection(layer), object_id, payload, expected_revision=None)
        self.object_store.delete(_staged_collection(layer), object_id)
        return object_id

    def set_trust_status(self, object_id: str, trust_status: str, reason: str) -> None:
        transition = {
            "object_id": object_id,
            "trust_status": trust_status,
            "reason": reason,
        }
        transition_id = f"transition-{len(self.object_store.list('memory_transitions')) + 1:04d}-{object_id}"
        self.object_store.write("memory_transitions", transition_id, transition, expected_revision=None)

    def staged(self, layer: str, object_id: str) -> Mapping[str, object] | None:
        return self.object_store.read(_staged_collection(layer), object_id)

    def transitions(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self.object_store.list("memory_transitions"))


@dataclass(frozen=True, slots=True)
class SQLiteMemoryReader:
    """Read-only generic Memory projection backed by the compound SQLite target."""

    records: SQLiteStructuredRecordStore

    def generation_token(self) -> str:
        return self.records.generation_token(
            (
                "memory_atoms",
                "memory_scenarios",
                "memory_series_memory",
            )
        )

    def get(self, layer: str, object_id: str) -> Mapping[str, object] | None:
        record = self.records.read(_published_collection(layer), object_id)
        return dict(record.payload) if record is not None and memory_is_readable(record.payload) else None

    def list(self, layer: str) -> Sequence[Mapping[str, object]]:
        return tuple(dict(record.payload) for record in self.records.list(_published_collection(layer)) if memory_is_readable(record.payload))

    def list_by_source(self, source_id: str) -> Sequence[Mapping[str, object]]:
        return tuple(
            dict(record.payload)
            for layer in ("atom", "scenario", "series_memory")
            for record in self.records.list(_published_collection(layer))
            if memory_is_readable(record.payload) and _references_source(record.payload, source_id)
        )

    def list_by_project(self, project_id: str) -> Sequence[Mapping[str, object]]:
        return _project_memory_from_collections(
            project_id=project_id,
            series_memories=tuple(record.payload for record in self.records.list("memory_series_memory")),
            scenarios=tuple(record.payload for record in self.records.list("memory_scenarios")),
            atoms=tuple(record.payload for record in self.records.list("memory_atoms")),
        )

    def staged(self, layer: str, object_id: str) -> Mapping[str, object] | None:
        record = self.records.read(_staged_collection(layer), object_id)
        return dict(record.payload) if record is not None else None


def _object_id(payload: Mapping[str, object]) -> str:
    object_id = payload.get("id")
    if not isinstance(object_id, str) or not object_id:
        raise ValueError("memory object requires id")
    return object_id


def memory_is_readable(payload: Mapping[str, object]) -> bool:
    return payload.get("lifecycle_status", "active") == "active"


def _published_collection(layer: str) -> str:
    match layer:
        case "atom":
            return "memory_atoms"
        case "scenario":
            return "memory_scenarios"
        case "series_memory":
            return "memory_series_memory"
        case "project_skill":
            return "project_skills"
        case _:
            raise ValueError(f"unsupported memory layer: {layer}")


def _staged_collection(layer: str) -> str:
    match layer:
        case "atom":
            return "staging_atoms"
        case "scenario":
            return "staging_scenarios"
        case "series_memory":
            return "staging_series_memory"
        case "project_skill":
            return "staging_project_skills"
        case _:
            raise ValueError(f"unsupported memory layer: {layer}")


def _references_source(item: Mapping[str, object], source_id: str) -> bool:
    if item.get("source_id") == source_id:
        return True
    source_refs = item.get("source_refs")
    if not isinstance(source_refs, list):
        return False
    return any(isinstance(ref, dict) and ref.get("source_id") == source_id for ref in source_refs)


def _project_memory_from_collections(
    *,
    project_id: str,
    series_memories: Sequence[Mapping[str, object]],
    scenarios: Sequence[Mapping[str, object]],
    atoms: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    project_series = [
        dict(item)
        for item in series_memories
        if memory_is_readable(item) and _string_list_contains(item.get("project_ids"), project_id)
    ]
    project_scenarios = [
        dict(item)
        for item in scenarios
        if memory_is_readable(item) and item.get("project_id") == project_id
    ]
    atom_ids: set[str] = set()
    for scenario in project_scenarios:
        atom_ids.update(_string_items(scenario.get("atom_ids")))
    project_atoms = [
        dict(item)
        for item in atoms
        if memory_is_readable(item) and isinstance(item.get("id"), str) and (
            item.get("project_id") == project_id
            or item["id"] in atom_ids
        )
    ]
    return (
        *sorted(project_series, key=_sort_key),
        *sorted(project_scenarios, key=_sort_key),
        *sorted(project_atoms, key=_sort_key),
    )


def _string_items(values: object) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    return tuple(value for value in values if isinstance(value, str) and value)


def _string_list_contains(values: object, expected: str) -> bool:
    return expected in _string_items(values)


def _sort_key(item: Mapping[str, object]) -> tuple[str, str]:
    updated_at = item.get("updated_at")
    object_id = item.get("id")
    return (
        updated_at if isinstance(updated_at, str) else "",
        object_id if isinstance(object_id, str) else "",
    )


@dataclass(frozen=True, slots=True)
class ObjectStoreMemoryCandidateRepository:
    """Persists reviewable Memory Candidates without publishing long-term memory."""

    object_store: ObjectStorePort
    collection: str = "memory_candidates"

    def save(self, candidate: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(candidate)
        _validate_memory_candidate(payload)
        candidate_id = _required_candidate_str(payload, "id")
        try:
            self.object_store.write(self.collection, candidate_id, payload, expected_revision=0)
        except ObjectStoreRevisionError as exc:
            raise MemoryCandidateRepositoryError(f"memory candidate already exists: {candidate_id}") from exc
        return payload

    def get(self, candidate_id: str) -> Mapping[str, object] | None:
        item = self.object_store.read(self.collection, candidate_id)
        return dict(item) if item is not None else None

    def update(
        self,
        candidate: Mapping[str, object],
        *,
        expected_revision: int | None = None,
    ) -> Mapping[str, object]:
        payload = dict(candidate)
        _validate_memory_candidate(payload)
        candidate_id = _required_candidate_str(payload, "id")
        if self.object_store.read(self.collection, candidate_id) is None:
            raise MemoryCandidateRepositoryError(f"memory candidate not found: {candidate_id}")
        try:
            self.object_store.write(
                self.collection,
                candidate_id,
                payload,
                expected_revision=expected_revision,
            )
        except ObjectStoreRevisionError as exc:
            raise MemoryCandidateRepositoryError(
                f"memory candidate revision conflict: {candidate_id}"
            ) from exc
        return payload

    def list_by_project(self, project_id: str) -> tuple[Mapping[str, object], ...]:
        return tuple(
            dict(item)
            for item in self.object_store.list(self.collection)
            if item.get("project_id") == project_id
        )


def memory_candidate_id(*parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"memory-candidate-{digest}"


def _validate_memory_candidate(candidate: Mapping[str, object]) -> None:
    if _required_candidate_str(candidate, "schema_version") != "1.0.0":
        raise MemoryCandidateRepositoryError("memory candidate schema_version must be 1.0.0")
    _required_candidate_str(candidate, "project_id")
    if candidate.get("target_layer") not in {"atom", "scenario", "persona", "series_memory", "project_skill"}:
        raise MemoryCandidateRepositoryError("memory candidate target_layer is not supported")
    if candidate.get("candidate_type") not in {
        "answer_fact",
        "answer_decision",
        "answer_action",
        "answer_summary",
        "document_takeaway",
        "other",
    }:
        raise MemoryCandidateRepositoryError("memory candidate type is not supported")
    status = candidate.get("status")
    if status not in {"pending_review", "rejected", "promoted", "withdrawn"}:
        raise MemoryCandidateRepositoryError("memory candidate status is not supported")
    _required_candidate_str(candidate, "proposed_content")
    if not _source_refs(candidate.get("source_refs")):
        raise MemoryCandidateRepositoryError("memory candidate requires source refs")
    extraction = candidate.get("extraction")
    if extraction is not None:
        if not isinstance(extraction, Mapping):
            raise MemoryCandidateRepositoryError("memory candidate extraction is invalid")
        if extraction.get("schema_version") != "1.0.0":
            raise MemoryCandidateRepositoryError("memory candidate extraction schema is invalid")
        if extraction.get("method") != "deterministic_local_v1":
            raise MemoryCandidateRepositoryError("memory candidate extraction method is invalid")
        for field in ("preview_id", "item_id", "title"):
            _required_candidate_str(extraction, field)
        start_char = extraction.get("start_char")
        end_char = extraction.get("end_char")
        if (
            not isinstance(start_char, int)
            or isinstance(start_char, bool)
            or start_char < 0
            or not isinstance(end_char, int)
            or isinstance(end_char, bool)
            or end_char <= start_char
        ):
            raise MemoryCandidateRepositoryError("memory candidate extraction range is invalid")
        paragraph_ids = extraction.get("paragraph_ids")
        if (
            not isinstance(paragraph_ids, list)
            or not paragraph_ids
            or not all(
                isinstance(value, str) and re.fullmatch(r"p[0-9]{3,}", value)
                for value in paragraph_ids
            )
        ):
            raise MemoryCandidateRepositoryError(
                "memory candidate extraction paragraph ids are invalid"
            )
        tags = extraction.get("tags")
        if (
            not isinstance(tags, list)
            or not 1 <= len(tags) <= 6
            or not all(isinstance(value, str) and value.strip() for value in tags)
        ):
            raise MemoryCandidateRepositoryError("memory candidate extraction tags are invalid")
    hierarchy_update = candidate.get("hierarchy_update")
    if hierarchy_update is not None:
        if not isinstance(hierarchy_update, Mapping):
            raise MemoryCandidateRepositoryError("memory candidate hierarchy update is invalid")
        if set(hierarchy_update) != {
            "schema_version",
            "layer",
            "object_id",
            "expected_object_revision",
            "base_domain_revision",
            "payload_sha256",
            "authority_identity",
            "proposed",
        }:
            raise MemoryCandidateRepositoryError("memory candidate hierarchy update fields are invalid")
        if hierarchy_update.get("schema_version") != "1.0.0":
            raise MemoryCandidateRepositoryError("memory candidate hierarchy update schema is invalid")
        layer = hierarchy_update.get("layer")
        if layer not in {"scenario", "series_memory"} or layer != candidate.get("target_layer"):
            raise MemoryCandidateRepositoryError("memory candidate hierarchy update layer is invalid")
        _required_candidate_str(hierarchy_update, "object_id")
        for field in ("expected_object_revision", "base_domain_revision"):
            value = hierarchy_update.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise MemoryCandidateRepositoryError(
                    f"memory candidate hierarchy update {field} is invalid"
                )
        payload_sha256 = hierarchy_update.get("payload_sha256")
        if not isinstance(payload_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", payload_sha256):
            raise MemoryCandidateRepositoryError("memory candidate hierarchy update hash is invalid")
        if hierarchy_update.get("authority_identity") != "sqlite:structured-records-v1":
            raise MemoryCandidateRepositoryError("memory candidate hierarchy update authority is invalid")
        proposed = hierarchy_update.get("proposed")
        if (
            not isinstance(proposed, Mapping)
            or proposed.get("id") != hierarchy_update.get("object_id")
            or proposed.get("revision") != hierarchy_update.get("base_domain_revision") + 1
        ):
            raise MemoryCandidateRepositoryError("memory candidate hierarchy update proposal is invalid")
    provenance = candidate.get("provenance")
    if not isinstance(provenance, Mapping):
        raise MemoryCandidateRepositoryError("memory candidate requires provenance")
    model_result_id = provenance.get("model_result_id")
    model_request_id = provenance.get("model_request_id")
    recall_result_id = provenance.get("recall_result_id")
    document_id = provenance.get("document_id")
    document_revision = provenance.get("document_revision")
    source_id = provenance.get("source_id")
    source_revision = provenance.get("source_revision")
    source_content_sha256 = provenance.get("source_content_sha256")
    source_content_read_id = provenance.get("source_content_read_id")
    media_processing_output_id = provenance.get("media_processing_output_id")
    media_processing_job_id = provenance.get("media_processing_job_id")
    companion_message_id = provenance.get("companion_message_id")
    world_feedback_id = provenance.get("world_feedback_id")
    world_event_id = provenance.get("world_event_id")
    world_turn_id = provenance.get("world_feedback_turn_id")
    world_outcome_ref = provenance.get("world_feedback_outcome_ref")
    if document_id is None and document_revision is not None:
        raise MemoryCandidateRepositoryError("document revision requires document id")
    if isinstance(document_id, str) and not isinstance(document_revision, int):
        raise MemoryCandidateRepositoryError("document id requires document revision")
    if media_processing_output_id is None and media_processing_job_id is not None:
        raise MemoryCandidateRepositoryError("media processing job requires media processing output")
    if isinstance(media_processing_output_id, str) and not isinstance(media_processing_job_id, str):
        raise MemoryCandidateRepositoryError("media processing output requires media processing job")
    model_provenance_complete = (
        isinstance(model_result_id, str)
        and isinstance(model_request_id, str)
        and isinstance(recall_result_id, str)
    )
    document_provenance_complete = isinstance(document_id, str) and isinstance(document_revision, int)
    direct_source_provenance_complete = (
        isinstance(source_id, str)
        and bool(source_id.strip())
        and isinstance(source_revision, int)
        and not isinstance(source_revision, bool)
        and source_revision >= 1
        and isinstance(source_content_sha256, str)
        and len(source_content_sha256) == 64
        and all(character in "0123456789abcdef" for character in source_content_sha256)
    )
    direct_source_provenance_present = any(
        value is not None
        for value in (source_id, source_revision, source_content_sha256)
    )
    if direct_source_provenance_present and not direct_source_provenance_complete:
        raise MemoryCandidateRepositoryError(
            "direct Source provenance requires source id, revision, and sha256"
        )
    content_read_provenance_complete = isinstance(source_content_read_id, str)
    media_output_provenance_complete = isinstance(media_processing_output_id, str) and isinstance(
        media_processing_job_id, str
    )
    companion_message_provenance_complete = isinstance(companion_message_id, str) and bool(
        companion_message_id.strip()
    )
    world_feedback_provenance_complete = all(
        isinstance(value, str) and bool(value.strip())
        for value in (
            world_feedback_id,
            world_event_id,
            world_turn_id,
            world_outcome_ref,
        )
    ) and str(world_outcome_ref).startswith("crp://")
    world_feedback_provenance_present = any(
        value is not None
        for value in (
            world_feedback_id,
            world_event_id,
            world_turn_id,
            world_outcome_ref,
        )
    )
    if world_feedback_provenance_present and not world_feedback_provenance_complete:
        raise MemoryCandidateRepositoryError(
            "World Feedback provenance requires feedback, event, Turn, and outcome identities"
        )
    if not (
        model_provenance_complete
        or document_provenance_complete
        or direct_source_provenance_complete
        or content_read_provenance_complete
        or media_output_provenance_complete
        or companion_message_provenance_complete
        or world_feedback_provenance_complete
    ):
        raise MemoryCandidateRepositoryError(
            "memory candidate requires model, document, source output, companion message, or World Feedback provenance"
        )
    input_refs = provenance.get("input_refs")
    if not isinstance(input_refs, Sequence) or isinstance(input_refs, (str, bytes)):
        raise MemoryCandidateRepositoryError("memory candidate requires input refs")
    kinds = {ref.get("kind") for ref in input_refs if isinstance(ref, Mapping)}
    if model_provenance_complete:
        for required_kind in ("recall_result", "model_request", "model_result"):
            if required_kind not in kinds:
                raise MemoryCandidateRepositoryError(f"memory candidate input refs require {required_kind}")
    if document_provenance_complete and "document" not in kinds:
        raise MemoryCandidateRepositoryError("memory candidate input refs require document")
    if direct_source_provenance_complete:
        if "source" not in kinds:
            raise MemoryCandidateRepositoryError("memory candidate input refs require source")
        if not any(
            ref.get("source_id") == source_id
            for ref in _source_refs(candidate.get("source_refs"))
        ):
            raise MemoryCandidateRepositoryError(
                "memory candidate direct Source provenance must match source refs"
            )
    if content_read_provenance_complete:
        for required_kind in ("source", "source_content_read"):
            if required_kind not in kinds:
                raise MemoryCandidateRepositoryError(f"memory candidate input refs require {required_kind}")
    if media_output_provenance_complete:
        for required_kind in ("source", "media_processing_job", "media_processing_output"):
            if required_kind not in kinds:
                raise MemoryCandidateRepositoryError(f"memory candidate input refs require {required_kind}")
    if companion_message_provenance_complete and "companion_message" not in kinds:
        raise MemoryCandidateRepositoryError("memory candidate input refs require companion_message")
    if world_feedback_provenance_complete:
        for required_kind in ("world_feedback", "turn_receipt"):
            if required_kind not in kinds:
                raise MemoryCandidateRepositoryError(
                    f"memory candidate input refs require {required_kind}"
                )
        refs = _source_refs(candidate.get("source_refs"))
        if not any(ref.get("source_id") == world_event_id for ref in refs):
            raise MemoryCandidateRepositoryError(
                "memory candidate World Feedback provenance must match source refs"
            )
        if not any(
            ref.get("source_id") == world_turn_id
            and ref.get("locator") == world_outcome_ref
            for ref in refs
        ):
            raise MemoryCandidateRepositoryError(
                "memory candidate Turn outcome provenance must match source refs"
            )
    review = candidate.get("review")
    if not isinstance(review, Mapping):
        raise MemoryCandidateRepositoryError("memory candidate requires review")
    if status == "pending_review":
        if review.get("requires_user_confirmation") is not True:
            raise MemoryCandidateRepositoryError("pending memory candidate requires user confirmation")
        if review.get("auto_promote_allowed") is not False:
            raise MemoryCandidateRepositoryError("pending memory candidate cannot auto-promote")
        if review.get("reviewed_by") is not None or review.get("reviewed_at") is not None:
            raise MemoryCandidateRepositoryError("pending memory candidate must not be reviewed")
    if status in {"rejected", "promoted", "withdrawn"}:
        if review.get("reviewed_by") not in {"user", "system"}:
            raise MemoryCandidateRepositoryError("reviewed memory candidate requires reviewer")
        if not isinstance(review.get("reviewed_at"), str):
            raise MemoryCandidateRepositoryError("reviewed memory candidate requires reviewed_at")
    _validate_application(
        candidate.get("application"),
        has_application="application" in candidate,
        status=status,
    )
    _validate_source_erasure(
        candidate.get("source_erasure"),
        has_source_erasure="source_erasure" in candidate,
        candidate=candidate,
    )


def _validate_application(value: object, *, has_application: bool, status: object) -> None:
    if not has_application:
        return
    if status != "promoted":
        raise MemoryCandidateRepositoryError("memory candidate application requires promoted status")
    if not isinstance(value, Mapping):
        raise MemoryCandidateRepositoryError("memory candidate application must be an object")
    if set(value) != {"operation_id", "draft_id", "staging_authority"}:
        raise MemoryCandidateRepositoryError("memory candidate application fields are invalid")
    for key in ("operation_id", "draft_id", "staging_authority"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise MemoryCandidateRepositoryError("memory candidate application values are invalid")


def _validate_source_erasure(
    value: object,
    *,
    has_source_erasure: bool,
    candidate: Mapping[str, object],
) -> None:
    if not has_source_erasure:
        return
    if not isinstance(value, Mapping):
        raise MemoryCandidateRepositoryError("memory candidate source erasure must be an object")
    required = {
        "schema_version",
        "state",
        "action",
        "operation_id",
        "requested_candidate_revision",
        "source_id",
        "source_revision",
        "source_content_sha256",
        "reason_sha256",
        "erased_at",
    }
    if set(value) != required:
        raise MemoryCandidateRepositoryError("memory candidate source erasure fields are invalid")
    action = value.get("action")
    expected_status = {"reject": "rejected", "withdraw": "withdrawn"}.get(action)
    if (
        value.get("schema_version") != "1.0.0"
        or value.get("state") != "content_erased"
        or candidate.get("status") != expected_status
        or candidate.get("proposed_content") != "[content erased]"
    ):
        raise MemoryCandidateRepositoryError("memory candidate source erasure state is invalid")
    provenance = candidate.get("provenance")
    if not isinstance(provenance, Mapping):
        raise MemoryCandidateRepositoryError("memory candidate source erasure provenance is invalid")
    for provenance_field in ("source_id", "source_revision", "source_content_sha256"):
        if value.get(provenance_field) != provenance.get(provenance_field):
            raise MemoryCandidateRepositoryError(
                "memory candidate source erasure provenance drifted"
            )
    if not isinstance(value.get("requested_candidate_revision"), int) or isinstance(
        value.get("requested_candidate_revision"), bool
    ) or value["requested_candidate_revision"] < 1:
        raise MemoryCandidateRepositoryError(
            "memory candidate source erasure request revision is invalid"
        )
    for required_field in ("operation_id", "erased_at"):
        if (
            not isinstance(value.get(required_field), str)
            or not value[required_field].strip()
        ):
            raise MemoryCandidateRepositoryError(
                f"memory candidate source erasure {required_field} is invalid"
            )
    reason_hash = value.get("reason_sha256")
    if (
        not isinstance(reason_hash, str)
        or len(reason_hash) != 64
        or any(character not in "0123456789abcdef" for character in reason_hash)
    ):
        raise MemoryCandidateRepositoryError(
            "memory candidate source erasure reason hash is invalid"
        )


def _source_refs(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    refs: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        source_id = item.get("source_id")
        locator = item.get("locator")
        if not isinstance(source_id, str) or not source_id:
            continue
        if not isinstance(locator, str) or not locator:
            continue
        ref: dict[str, object] = {"source_id": source_id, "locator": locator}
        quote = item.get("quote")
        if isinstance(quote, str):
            ref["quote"] = quote
        refs.append(ref)
    return refs


def _required_candidate_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise MemoryCandidateRepositoryError(f"memory candidate requires {key}")
    return value
