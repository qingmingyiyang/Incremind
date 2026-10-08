from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.memory_core.runtime import memory_candidate_id
from .ports import ObjectStorePort


class ExternalAgentProposalImportError(ValueError):
    """Raised when an external Agent proposal violates the proposal-only boundary."""


@dataclass(frozen=True, slots=True)
class ExternalAgentProposalImportResult:
    status: str
    proposal_id: str
    proposal_type: str
    project_id: str | None
    memory_candidate_id: str | None
    draft_ids: tuple[str, ...]
    review_state: str
    memory_publication_state: str
    blocked_operations: tuple[str, ...]


class ImportExternalAgentProposal:
    """Import external Agent output as a local pending-review proposal only."""

    _ALLOWED_PROPOSAL_TYPES = {
        "memory_candidate_proposal",
        "series_update_proposal",
        "project_skill_update_proposal",
        "document_revision_proposal",
    }
    _FORBIDDEN_TOP_LEVEL_KEYS = {
        "memory_atoms",
        "memory_scenarios",
        "memory_series_memory",
        "project_skills",
        "memory_publications",
        "memory_transitions",
        "staging_atoms",
        "staging_scenarios",
        "staging_series_memory",
        "staging_project_skills",
        "api_keys",
        "cookies",
        "secrets",
    }
    _BLOCKED_OPERATIONS = (
        "direct_long_term_memory_write",
        "direct_project_skill_overwrite",
        "staging_memory_write",
        "automatic_memory_publication",
        "provider_secret_request",
        "cookie_request",
        "remote_upload",
    )

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

    def validate(self, proposal: Mapping[str, object]) -> None:
        """Apply the complete proposal boundary before any caller persists an outbox intent."""
        _validate_boundary(proposal, forbidden_keys=self._FORBIDDEN_TOP_LEVEL_KEYS)

    def execute(self, *, proposal: Mapping[str, object], project_id: str | None = None) -> ExternalAgentProposalImportResult:
        self.validate(proposal)
        proposal_id = _clean_optional(proposal.get("proposal_id")) or _stable_id("external-agent-proposal", proposal)
        proposal_type = _required_str(proposal, "proposal_type")
        if proposal_type not in self._ALLOWED_PROPOSAL_TYPES:
            raise ExternalAgentProposalImportError("proposal_type is not allowed")
        summary = _required_str(proposal, "summary")
        suggested_changes = proposal.get("suggested_changes")
        if not isinstance(suggested_changes, (Mapping, list, str)):
            raise ExternalAgentProposalImportError("suggested_changes is required")
        requires_user_review = proposal.get("requires_user_review")
        if requires_user_review is not True:
            raise ExternalAgentProposalImportError("proposal must require user review")
        source_refs = _proposal_refs(proposal.get("source_refs"), field_name="source_refs")
        evidence_refs = _proposal_refs(proposal.get("evidence_refs"), field_name="evidence_refs")
        clean_project_id = _clean_optional(project_id) or _clean_optional(proposal.get("project_id"))
        submission = {
            "proposal_type": proposal_type,
            "project_id": clean_project_id,
            "summary": summary,
            "source_refs": [_memory_source_ref(ref, fallback_source_id=proposal_id) for ref in source_refs],
            "evidence_refs": [dict(ref) for ref in evidence_refs],
            "suggested_changes": _redact(suggested_changes),
            "requires_user_review": True,
        }
        existing = self._object_store.read("external_agent_proposals", proposal_id)
        if existing is not None:
            stored_submission = existing.get("submission")
            if stored_submission is None:
                stored_submission = _legacy_submission(existing)
            if stored_submission != submission:
                raise ExternalAgentProposalImportError("proposal identity conflicts with existing submission")
            self._ensure_existing_record_complete(existing)
            return _result_from_record(existing)
        timestamp = self._now or _utc_now()
        memory_candidate = None
        review_drafts: tuple[Mapping[str, object], ...] = ()
        if proposal_type == "memory_candidate_proposal":
            memory_candidate = self._create_memory_candidate(
                proposal_id=proposal_id,
                project_id=clean_project_id,
                summary=summary,
                source_refs=source_refs,
                evidence_refs=evidence_refs,
                suggested_changes=suggested_changes,
                timestamp=timestamp,
            )
        else:
            review_drafts = self._create_review_drafts(
                proposal_id=proposal_id,
                proposal_type=proposal_type,
                project_id=clean_project_id,
                summary=summary,
                source_refs=source_refs,
                evidence_refs=evidence_refs,
                suggested_changes=suggested_changes,
                timestamp=timestamp,
            )
        record = {
            "schema_version": "1.0.0",
            "id": proposal_id,
            "status": "pending_review",
            "proposal_type": proposal_type,
            "project_id": clean_project_id,
            "summary": summary,
            "source_refs": [_memory_source_ref(ref, fallback_source_id=proposal_id) for ref in source_refs],
            "evidence_refs": [dict(ref) for ref in evidence_refs],
            "suggested_changes": _redact(suggested_changes),
            "requires_user_review": True,
            "memory_candidate_id": memory_candidate["id"] if memory_candidate is not None else None,
            "draft_ids": [_required_mapping_str(draft, "id") for draft in review_drafts],
            "review": {
                "state": "pending_review",
                "auto_promote_allowed": False,
                "reviewed_by": None,
                "reviewed_at": None,
            },
            "memory_publication": "not_started",
            "blocked_operations": list(self._BLOCKED_OPERATIONS),
            "submission": submission,
            "created_at": timestamp,
            "updated_at": timestamp,
            "ref": f"crp://{self._namespace_id}/external-agent-proposals/{proposal_id}.json",
        }
        try:
            self._object_store.write("external_agent_proposals", proposal_id, record, expected_revision=0)
        except Exception:
            raced = self._object_store.read("external_agent_proposals", proposal_id)
            if raced is None or raced.get("submission") != submission:
                raise
            self._ensure_existing_record_complete(raced)
            return _result_from_record(raced)
        self._write_event(
            proposal_id=proposal_id,
            proposal_type=proposal_type,
            memory_candidate_id=record["memory_candidate_id"] if isinstance(record["memory_candidate_id"], str) else None,
            draft_ids=tuple(str(draft_id) for draft_id in record["draft_ids"] if isinstance(draft_id, str)),
            created_at=timestamp,
        )
        return ExternalAgentProposalImportResult(
            status="pending_review",
            proposal_id=proposal_id,
            proposal_type=proposal_type,
            project_id=clean_project_id,
            memory_candidate_id=record["memory_candidate_id"] if isinstance(record["memory_candidate_id"], str) else None,
            draft_ids=tuple(str(draft_id) for draft_id in record["draft_ids"] if isinstance(draft_id, str)),
            review_state="pending_review",
            memory_publication_state="not_published",
            blocked_operations=self._BLOCKED_OPERATIONS,
        )

    def _create_memory_candidate(
        self,
        *,
        proposal_id: str,
        project_id: str | None,
        summary: str,
        source_refs: tuple[Mapping[str, object], ...],
        evidence_refs: tuple[Mapping[str, object], ...],
        suggested_changes: object,
        timestamp: str,
    ) -> Mapping[str, object]:
        target_layer = _target_layer(suggested_changes)
        original_candidate_type = _candidate_type(suggested_changes)
        candidate_type = _memory_candidate_type(original_candidate_type)
        proposed_content = _proposed_content(summary, suggested_changes)
        candidate = {
            "schema_version": "1.0.0",
            "id": memory_candidate_id("external-agent", proposal_id, target_layer, candidate_type, proposed_content),
            "project_id": project_id,
            "target_layer": target_layer,
            "candidate_type": candidate_type,
            "status": "pending_review",
            "memory_publication_state": "not_published",
            "proposed_content": proposed_content,
            "source_refs": [_memory_source_ref(ref, fallback_source_id=proposal_id) for ref in source_refs],
            "provenance": {
                "external_agent_proposal_id": proposal_id,
                "external_agent_candidate_type": original_candidate_type,
                "source_content_read_id": f"external-agent-proposal:{proposal_id}",
                "media_processing_output_id": None,
                "model_result_id": None,
                "input_refs": [
                    {
                        "kind": "source",
                        "object_id": f"external-agent:{proposal_id}",
                        "uri": f"crp://{self._namespace_id}/external-agent-proposals/{proposal_id}.json",
                    },
                    {
                        "kind": "source_content_read",
                        "object_id": f"external-agent-proposal:{proposal_id}",
                        "uri": f"crp://{self._namespace_id}/external-agent-proposals/{proposal_id}.json#proposal",
                    },
                    {
                        "kind": "external_agent_proposal",
                        "object_id": proposal_id,
                        "uri": f"crp://{self._namespace_id}/external-agent-proposals/{proposal_id}.json",
                    },
                    *[
                        {
                            "kind": "evidence_ref",
                            "object_id": _ref_locator(ref),
                            "uri": _ref_locator(ref),
                        }
                        for ref in evidence_refs
                    ],
                ],
            },
            "review": {
                "requires_user_confirmation": True,
                "auto_promote_allowed": False,
                "reason": "External Agent proposals can only become pending review candidates.",
                "reviewed_by": None,
                "reviewed_at": None,
            },
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        existing = self._object_store.read("memory_candidates", str(candidate["id"]))
        if existing is not None:
            _assert_same_proposal_child(existing, candidate, "memory candidate")
            return existing
        try:
            return self._candidates.save(candidate)
        except Exception:
            raced = self._object_store.read("memory_candidates", str(candidate["id"]))
            if raced is None:
                raise
            _assert_same_proposal_child(raced, candidate, "memory candidate")
            return raced

    def _ensure_existing_record_complete(self, record: Mapping[str, object]) -> None:
        candidate_id = _clean_optional(record.get("memory_candidate_id"))
        if candidate_id is not None and self._object_store.read("memory_candidates", candidate_id) is None:
            raise ExternalAgentProposalImportError("existing proposal memory candidate is missing")
        draft_ids = record.get("draft_ids")
        if not isinstance(draft_ids, list) or any(
            not isinstance(item, str)
            or self._object_store.read("external_agent_review_drafts", item) is None
            for item in draft_ids
        ):
            raise ExternalAgentProposalImportError("existing proposal review draft is missing")
        self._write_event(
            proposal_id=_required_str(record, "id"),
            proposal_type=_required_str(record, "proposal_type"),
            memory_candidate_id=candidate_id,
            draft_ids=tuple(str(item) for item in draft_ids),
            created_at=_required_str(record, "created_at"),
        )

    def _create_review_drafts(
        self,
        *,
        proposal_id: str,
        proposal_type: str,
        project_id: str | None,
        summary: str,
        source_refs: tuple[Mapping[str, object], ...],
        evidence_refs: tuple[Mapping[str, object], ...],
        suggested_changes: object,
        timestamp: str,
    ) -> tuple[Mapping[str, object], ...]:
        draft_type = _draft_type(proposal_type)
        draft_id = _stable_id(f"external-agent-{draft_type}", {"proposal_id": proposal_id, "suggested_changes": _redact(suggested_changes)})
        draft = {
            "schema_version": "1.0.0",
            "id": draft_id,
            "proposal_id": proposal_id,
            "proposal_type": proposal_type,
            "draft_type": draft_type,
            "status": "pending_review",
            "project_id": project_id,
            "target_id": _target_object_id(suggested_changes, draft_type=draft_type),
            "summary": summary,
            "proposed_content": _proposed_content(summary, suggested_changes),
            "suggested_changes": _redact(suggested_changes),
            "source_refs": [_memory_source_ref(ref, fallback_source_id=proposal_id) for ref in source_refs],
            "evidence_refs": [dict(ref) for ref in evidence_refs],
            "review": {
                "state": "pending_review",
                "requires_user_confirmation": True,
                "auto_apply_allowed": False,
                "reviewed_by": None,
                "reviewed_at": None,
            },
            "application": {
                "state": "not_applied",
                "blocked_operations": list(self._BLOCKED_OPERATIONS),
                "writes_long_term_memory": False,
                "writes_staging_memory": False,
            },
            "created_at": timestamp,
            "updated_at": timestamp,
            "ref": f"crp://{self._namespace_id}/external-agent-review-drafts/{draft_id}.json",
        }
        existing = self._object_store.read("external_agent_review_drafts", draft_id)
        if existing is not None:
            _assert_same_proposal_child(existing, draft, "review draft")
            return (existing,)
        try:
            self._object_store.write("external_agent_review_drafts", draft_id, draft, expected_revision=0)
        except Exception:
            raced = self._object_store.read("external_agent_review_drafts", draft_id)
            if raced is None:
                raise
            _assert_same_proposal_child(raced, draft, "review draft")
            return (raced,)
        return (draft,)

    def _write_event(
        self,
        *,
        proposal_id: str,
        proposal_type: str,
        memory_candidate_id: str | None,
        draft_ids: tuple[str, ...],
        created_at: str,
    ) -> None:
        event_id = f"event-external-agent-proposal-imported-{proposal_id}"
        payload = {
                "schema_version": "1.0.0",
                "id": event_id,
                "type": "external_agent_proposal_imported",
                "status": "pending_review",
                "details": {
                    "proposal_id": proposal_id,
                    "proposal_type": proposal_type,
                    "memory_candidate_id": memory_candidate_id,
                    "draft_ids": list(draft_ids),
                    "memory_publication_state": "not_published",
                    "auto_promote_allowed": False,
                },
                "created_at": created_at,
                "ref": f"crp://{self._namespace_id}/activity/{event_id}.json",
            }
        existing = self._object_store.read("activity_events", event_id)
        if existing is not None:
            _assert_same_proposal_child(existing, payload, "activity event")
            return
        try:
            self._object_store.write(
            "activity_events",
            event_id,
            payload,
            expected_revision=0,
            )
        except Exception:
            raced = self._object_store.read("activity_events", event_id)
            if raced is None:
                raise
            _assert_same_proposal_child(raced, payload, "activity event")


def serialize_external_agent_proposal_import(result: ExternalAgentProposalImportResult) -> dict[str, object]:
    return {
        "status": result.status,
        "proposal_id": result.proposal_id,
        "proposal_type": result.proposal_type,
        "project_id": result.project_id,
        "memory_candidate_id": result.memory_candidate_id,
        "draft_ids": list(result.draft_ids),
        "review_state": result.review_state,
        "memory_publication_state": result.memory_publication_state,
        "blocked_operations": list(result.blocked_operations),
    }


def _result_from_record(record: Mapping[str, object]) -> ExternalAgentProposalImportResult:
    draft_ids = record.get("draft_ids")
    blocked = record.get("blocked_operations")
    if (
        record.get("status") != "pending_review"
        or not isinstance(draft_ids, list)
        or not isinstance(blocked, list)
    ):
        raise ExternalAgentProposalImportError("existing proposal record is invalid")
    return ExternalAgentProposalImportResult(
        status="pending_review",
        proposal_id=_required_str(record, "id"),
        proposal_type=_required_str(record, "proposal_type"),
        project_id=_clean_optional(record.get("project_id")),
        memory_candidate_id=_clean_optional(record.get("memory_candidate_id")),
        draft_ids=tuple(str(item) for item in draft_ids if isinstance(item, str)),
        review_state="pending_review",
        memory_publication_state="not_published",
        blocked_operations=tuple(str(item) for item in blocked if isinstance(item, str)),
    )


def _legacy_submission(record: Mapping[str, object]) -> dict[str, object]:
    return {
        "proposal_type": record.get("proposal_type"),
        "project_id": record.get("project_id"),
        "summary": record.get("summary"),
        "source_refs": list(record.get("source_refs") or ()),
        "evidence_refs": list(record.get("evidence_refs") or ()),
        "suggested_changes": record.get("suggested_changes"),
        "requires_user_review": True,
    }


def _assert_same_proposal_child(
    existing: Mapping[str, object], expected: Mapping[str, object], label: str,
) -> None:
    ignored = {"created_at", "updated_at"}
    left = {key: value for key, value in existing.items() if key not in ignored}
    right = {key: value for key, value in expected.items() if key not in ignored}
    if left != right:
        raise ExternalAgentProposalImportError(f"{label} identity conflicts with existing record")


def _validate_boundary(value: object, *, forbidden_keys: set[str], path: str = "") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_str = str(key)
            lower = key_str.lower()
            if lower in forbidden_keys:
                raise ExternalAgentProposalImportError(f"forbidden proposal field: {path + key_str}")
            if _is_sensitive_key(lower):
                raise ExternalAgentProposalImportError(f"sensitive proposal field: {path + key_str}")
            _validate_boundary(item, forbidden_keys=forbidden_keys, path=f"{path}{key_str}.")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_boundary(item, forbidden_keys=forbidden_keys, path=f"{path}{index}.")
    elif isinstance(value, str):
        if _looks_sensitive(value):
            raise ExternalAgentProposalImportError("proposal contains sensitive material")


def _proposal_refs(value: object, *, field_name: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ExternalAgentProposalImportError(f"{field_name} must be a list")
    refs: list[Mapping[str, object]] = []
    for item in value:
        if isinstance(item, str):
            locator = _clean_optional(item)
            if locator is None:
                continue
            refs.append({"locator": locator})
            continue
        if isinstance(item, Mapping):
            locator = _clean_optional(item.get("locator")) or _clean_optional(item.get("ref")) or _clean_optional(item.get("source_id"))
            if locator is None:
                raise ExternalAgentProposalImportError(f"{field_name} item requires locator")
            refs.append({str(key): _redact(val) for key, val in item.items() if isinstance(key, str)})
            continue
        raise ExternalAgentProposalImportError(f"{field_name} item is invalid")
    if not refs:
        raise ExternalAgentProposalImportError(f"{field_name} must not be empty")
    return tuple(refs)


def _target_layer(suggested_changes: object) -> str:
    if isinstance(suggested_changes, Mapping):
        value = _clean_optional(suggested_changes.get("target_layer"))
        if value in {"atom", "scenario", "series_memory", "project_skill"}:
            return value
    return "atom"


def _candidate_type(suggested_changes: object) -> str:
    if isinstance(suggested_changes, Mapping):
        value = _clean_optional(suggested_changes.get("candidate_type"))
        if value:
            return value
    return "external_agent_proposal"


def _memory_candidate_type(candidate_type: str) -> str:
    allowed = {"answer_fact", "answer_decision", "answer_action", "answer_summary", "document_takeaway", "other"}
    if candidate_type in allowed:
        return candidate_type
    return "other"


def _proposed_content(summary: str, suggested_changes: object) -> str:
    if isinstance(suggested_changes, Mapping):
        for key in ("proposed_content", "content", "summary"):
            value = _clean_optional(suggested_changes.get(key))
            if value:
                return value
    if isinstance(suggested_changes, str) and suggested_changes.strip():
        return suggested_changes.strip()
    return summary


def _draft_type(proposal_type: str) -> str:
    match proposal_type:
        case "series_update_proposal":
            return "series_update"
        case "project_skill_update_proposal":
            return "project_skill_update"
        case "document_revision_proposal":
            return "document_revision"
    raise ExternalAgentProposalImportError("proposal_type cannot create review draft")


def _target_object_id(suggested_changes: object, *, draft_type: str) -> str | None:
    if not isinstance(suggested_changes, Mapping):
        return None
    keys = {
        "series_update": ("series_id", "target_series_id"),
        "project_skill_update": ("project_skill_id", "skill_id", "target_project_skill_id"),
        "document_revision": ("document_id", "target_document_id"),
    }[draft_type]
    for key in keys:
        value = _clean_optional(suggested_changes.get(key))
        if value:
            return value
    return None


def _ref_locator(ref: Mapping[str, object]) -> str:
    for key in ("locator", "ref", "source_id", "document_id"):
        value = ref.get(key)
        if isinstance(value, str) and value:
            return value
    return "external-agent-ref"


def _memory_source_ref(ref: Mapping[str, object], *, fallback_source_id: str) -> dict[str, object]:
    source_id = ref.get("source_id")
    locator = _ref_locator(ref)
    memory_ref: dict[str, object] = {
        "source_id": source_id if isinstance(source_id, str) and source_id else f"external-agent:{fallback_source_id}",
        "locator": locator,
    }
    if not isinstance(source_id, str) or not source_id:
        memory_ref["external_locator"] = locator
    quote = ref.get("quote")
    if isinstance(quote, str) and quote:
        memory_ref["quote"] = quote
    return memory_ref


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    clean = _clean_optional(value)
    if clean is None:
        raise ExternalAgentProposalImportError(f"{key} is required")
    return clean


def _required_mapping_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ExternalAgentProposalImportError(f"{key} is required")
    return value


def _clean_optional(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    clean = " ".join(value.strip().split())
    return clean or None


def _stable_id(prefix: str, value: Mapping[str, object]) -> str:
    digest = hashlib.sha256(json.dumps(_redact(value), ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _redact(value: object) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            key_str = str(key)
            if _is_sensitive_key(key_str):
                result[key_str] = "[redacted]"
            elif key_str in {"path", "file_path", "absolute_path", "cookie_path"}:
                result[key_str] = "[local-path-redacted]"
            else:
                result[key_str] = _redact(item)
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value[:200]]
    return value


def _is_sensitive_key(key: str) -> bool:
    lower = key.lower()
    return any(marker in lower for marker in ("api_key", "apikey", "secret", "cookie", "token", "password", "authorization"))


def _looks_sensitive(value: str) -> bool:
    return bool(
        re.search(r"sk-[A-Za-z0-9_-]{20,}", value)
        or re.search(r"(?i)(api_key|apikey|secret|token|authorization)\s*[:=]\s*[\"']?[A-Za-z0-9_.-]{12,}", value)
        or re.search(r"(?i)(cookie_path|cookie_file)\s*[:=]\s*[\"']?[A-Za-z]:\\\\", value)
        or re.search(r"[A-Za-z]:\\\\Users\\\\", value)
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
