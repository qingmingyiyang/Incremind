from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol


class MemoryCandidateReviewRepositoryPort(Protocol):
    """Reads and updates reviewable Memory Candidates."""

    def get(self, candidate_id: str) -> Mapping[str, object] | None:
        """Return one candidate by id."""

    def update(self, candidate: Mapping[str, object]) -> Mapping[str, object]:
        """Persist a reviewed candidate."""


class MemoryDraftWriterPort(Protocol):
    """Writes draft Memory objects without publishing long-term memory."""

    def get(self, layer: str, object_id: str) -> Mapping[str, object] | None:
        """Return one current Memory projection for guarded update candidates."""

    def save_candidate(self, layer: str, payload: Mapping[str, object], *, publication_context: Mapping[str, object] | None = None) -> str:
        """Persist one draft Memory object."""


class MemoryCandidateReviewError(ValueError):
    """Raised when a Memory Candidate review would bypass memory safety rules."""


@dataclass(frozen=True, slots=True)
class MemoryCandidateReviewResult:
    candidate_id: str
    status: str
    reviewed_by: str
    reviewed_at: str
    promoted_layer: str | None = None
    promoted_object_id: str | None = None


def serialize_memory_candidate_review_result(result: MemoryCandidateReviewResult) -> dict[str, object]:
    publication_state = {
        "promoted": _staging_publication_state(result.promoted_layer),
        "rejected": "candidate_rejected_not_published",
        "withdrawn": "candidate_withdrawn_not_published",
    }.get(result.status, "candidate_reviewed_not_published")
    return {
        "candidate_id": result.candidate_id,
        "status": result.status,
        "reviewed_by": result.reviewed_by,
        "reviewed_at": result.reviewed_at,
        "promoted_layer": result.promoted_layer,
        "promoted_object_id": result.promoted_object_id,
        "memory_publication_state": publication_state,
    }


class ReviewMemoryCandidate:
    """Rejects or promotes a Memory Candidate after explicit review."""

    def __init__(
        self,
        *,
        candidates: MemoryCandidateReviewRepositoryPort,
        memory: MemoryDraftWriterPort,
        namespace_id: str = "default",
    ) -> None:
        self._candidates = candidates
        self._memory = memory
        self._namespace_id = namespace_id

    def reject(
        self,
        candidate_id: str,
        *,
        reason: str,
        reviewed_by: str = "user",
        reviewed_at: str | None = None,
    ) -> MemoryCandidateReviewResult:
        candidate = self._pending_candidate(candidate_id)
        timestamp = reviewed_at or _utc_now()
        reviewed = _with_review(
            candidate,
            status="rejected",
            reason=reason,
            reviewed_by=reviewed_by,
            reviewed_at=timestamp,
        )
        saved = self._candidates.update(reviewed)
        review = _review(saved)
        return MemoryCandidateReviewResult(
            candidate_id=_required_str(saved, "id"),
            status=_required_str(saved, "status"),
            reviewed_by=_required_str(review, "reviewed_by"),
            reviewed_at=_required_str(review, "reviewed_at"),
        )

    def withdraw(
        self,
        candidate_id: str,
        *,
        reason: str,
        reviewed_by: str = "user",
        reviewed_at: str | None = None,
    ) -> MemoryCandidateReviewResult:
        if reviewed_by != "user":
            raise MemoryCandidateReviewError("Memory Candidate withdrawal requires user reviewer")
        candidate = self._pending_candidate(candidate_id)
        timestamp = reviewed_at or _utc_now()
        reviewed = _with_review(
            candidate,
            status="withdrawn",
            reason=reason,
            reviewed_by=reviewed_by,
            reviewed_at=timestamp,
        )
        saved = self._candidates.update(reviewed)
        review = _review(saved)
        return MemoryCandidateReviewResult(
            candidate_id=_required_str(saved, "id"),
            status=_required_str(saved, "status"),
            reviewed_by=_required_str(review, "reviewed_by"),
            reviewed_at=_required_str(review, "reviewed_at"),
        )

    def promote_to_layer(
        self,
        candidate_id: str,
        *,
        target_layer: str,
        **_ignored: object,
    ) -> MemoryCandidateReviewResult:
        clean_target_layer = _target_layer(target_layer)
        if clean_target_layer == "project_skill":
            raise MemoryCandidateReviewError("Project Skill promotion requires the durable Project Skill staging saga")
        raise MemoryCandidateReviewError(
            f"{clean_target_layer} promotion requires the durable Memory publication review staging saga"
        )

    def _pending_candidate(self, candidate_id: str) -> Mapping[str, object]:
        candidate = self._candidates.get(candidate_id)
        if candidate is None:
            raise MemoryCandidateReviewError(f"Memory Candidate not found: {candidate_id}")
        if candidate.get("status") != "pending_review":
            raise MemoryCandidateReviewError("Memory Candidate must be pending_review")
        review = _review(candidate)
        if review.get("requires_user_confirmation") is not True:
            raise MemoryCandidateReviewError("Memory Candidate must require user confirmation")
        if review.get("auto_promote_allowed") is not False:
            raise MemoryCandidateReviewError("Memory Candidate cannot auto-promote")
        return candidate


def _with_review(
    candidate: Mapping[str, object],
    *,
    status: str,
    reason: str,
    reviewed_by: str,
    reviewed_at: str,
) -> dict[str, object]:
    if reviewed_by not in {"user", "system"}:
        raise MemoryCandidateReviewError("reviewed_by must be user or system")
    if not reason.strip():
        raise MemoryCandidateReviewError("review reason is required")
    payload = dict(candidate)
    review = dict(_review(candidate))
    review.update(
        {
            "requires_user_confirmation": True,
            "auto_promote_allowed": False,
            "reason": reason.strip(),
            "reviewed_by": reviewed_by,
            "reviewed_at": reviewed_at,
        }
    )
    payload["status"] = status
    payload["review"] = review
    payload["updated_at"] = reviewed_at
    return payload


def _review(candidate: Mapping[str, object]) -> Mapping[str, object]:
    review = candidate.get("review")
    if not isinstance(review, Mapping):
        raise MemoryCandidateReviewError("Memory Candidate requires review")
    return review


def _draft_atom_id(candidate: Mapping[str, object]) -> str:
    portable_id = candidate.get("portable_object_id")
    if isinstance(portable_id, str) and portable_id.strip():
        return portable_id.strip()
    digest = hashlib.sha256(
        "\n".join(
            [
                _required_str(candidate, "id"),
                _required_str(candidate, "project_id"),
                _required_str(candidate, "proposed_content"),
            ]
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"atom-draft-{digest}"


def _draft_layer_id(candidate: Mapping[str, object], layer: str) -> str:
    portable_id = candidate.get("portable_object_id")
    if isinstance(portable_id, str) and portable_id.strip():
        return portable_id.strip()
    digest = hashlib.sha256(
        "\n".join(
            [
                layer,
                _required_str(candidate, "id"),
                _required_str(candidate, "project_id"),
                _required_str(candidate, "proposed_content"),
            ]
        ).encode("utf-8")
    ).hexdigest()[:16]
    prefix = {
        "scenario": "scenario-draft",
        "series_memory": "series-memory-draft",
        "project_skill": "skill-draft",
    }[layer]
    return f"{prefix}-{digest}"


def _draft_for_layer(
    candidate: Mapping[str, object],
    *,
    target_layer: str,
    source_refs: Sequence[Mapping[str, object]],
    timestamp: str,
    atom_type: str | None,
    tags: Sequence[str],
    confidence: float,
    series_id: str | None,
    scenario_ids: Sequence[str],
    atom_ids: Sequence[str],
) -> dict[str, object]:
    if target_layer in {"scenario", "series_memory"} and candidate.get("hierarchy_update") is not None:
        return _hierarchy_update_draft(candidate, source_refs=source_refs, timestamp=timestamp)
    if target_layer == "series_memory" and candidate.get("external_series_update") is not None:
        return _external_series_update_draft(candidate, source_refs=source_refs, timestamp=timestamp)
    if target_layer == "atom":
        return {
            "schema_version": "1.0.0",
            "id": _draft_atom_id(candidate),
            "project_id": _required_str(candidate, "project_id"),
            "source_id": _required_str(source_refs[0], "source_id"),
            "content": _required_str(candidate, "proposed_content"),
            "atom_type": atom_type or _atom_type_for_candidate(_required_str(candidate, "candidate_type")),
            "tags": _dedupe_tags(tags),
            "confidence": confidence,
            "source_refs": [dict(ref) for ref in source_refs],
            "revision": 1,
            "created_at": timestamp,
            "updated_at": timestamp,
            "trust_status": "system_generated",
        }
    if target_layer == "scenario":
        return {
            "schema_version": "1.0.0",
            "id": _draft_layer_id(candidate, "scenario"),
            "title": _title_from_content(_required_str(candidate, "proposed_content")),
            "summary": _required_str(candidate, "proposed_content"),
            "atom_ids": _dedupe_tags(atom_ids),
            "source_refs": [dict(ref) for ref in source_refs],
            "tags": _dedupe_tags(tags),
            "series_id": _optional_clean(series_id),
            "project_id": _required_str(candidate, "project_id"),
            "stale": False,
            "stale_reason": None,
            "revision": 1,
            "created_at": timestamp,
            "updated_at": timestamp,
            "trust_status": "system_generated",
        }
    if target_layer == "series_memory":
        return {
            "schema_version": "1.0.0",
            "id": _draft_layer_id(candidate, "series_memory"),
            "series_id": (
                _optional_clean(series_id)
                or _optional_clean(candidate.get("series_id"))
                or _required_str(candidate, "project_id")
            ),
            "scope": "project",
            "overview": _required_str(candidate, "proposed_content"),
            "scenario_ids": _dedupe_tags(scenario_ids),
            "source_refs": [dict(ref) for ref in source_refs],
            "project_ids": [_required_str(candidate, "project_id")],
            "stale": False,
            "stale_reason": None,
            "revision": 1,
            "created_at": timestamp,
            "updated_at": timestamp,
            "trust_status": "system_generated",
        }
    if target_layer == "project_skill":
        project_id = _required_str(candidate, "project_id")
        skill_id = _draft_layer_id(candidate, "project_skill")
        skill_source_refs = _project_skill_source_refs(source_refs)
        return {
            "schema_version": "1.0.0",
            "id": skill_id,
            "project_id": project_id,
            "name": _title_from_content(_required_str(candidate, "proposed_content")),
            "purpose": _required_str(candidate, "proposed_content"),
            "markdown_uri": f"crp://default/projects/{project_id}/project-skill.md",
            "json_uri": f"crp://default/projects/{project_id}/project-skill.json",
            "markdown_revision": 1,
            "json_revision": 1,
            "required_context": [
                {
                    "context_id": f"ctx-{skill_id}-source",
                    "kind": "source",
                    "object_id": _required_str(source_refs[0], "source_id"),
                    "uri": f"crp://default/sources/{_required_str(source_refs[0], 'source_id')}.json",
                    "reason": "Memory Candidate review evidence for Project / Skill draft.",
                    "stale": False,
                }
            ],
            "output_rules": [
                {
                    "rule_id": f"rule-{skill_id}-candidate",
                    "origin": "ai",
                    "rule": _required_str(candidate, "proposed_content"),
                    "priority": "must",
                    "source_refs": skill_source_refs,
                    "locked_by_user": False,
                }
            ],
            "style_preferences": {"voice": "direct", "format_defaults": ["Markdown"]},
            "update_rules": {
                "patch_strategy": "patch_existing_first",
                "user_edit_policy": "user_wins",
                "allowed_auto_updates": ["append_low_risk_context"],
            },
            "source_refs": skill_source_refs,
            "evidence_refs": skill_source_refs,
            "decision_log": [
                {
                    "decision_id": f"decision-{skill_id}-staging",
                    "reason": "User promoted candidate to staging Project / Skill Memory draft.",
                    "actor": "user",
                    "created_at": timestamp,
                }
            ],
            "conflict": {"status": "none", "conflict_refs": [], "resolution": None},
            "revision": 1,
            "status": "draft",
            "trust_status": "system_generated",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
    raise MemoryCandidateReviewError("Memory Candidate target_layer is not supported")


def _hierarchy_update_draft(
    candidate: Mapping[str, object],
    *,
    source_refs: Sequence[Mapping[str, object]],
    timestamp: str,
) -> dict[str, object]:
    update = candidate.get("hierarchy_update")
    if not isinstance(update, Mapping):
        raise MemoryCandidateReviewError("hierarchy update candidate metadata is invalid")
    required = {
        "schema_version",
        "layer",
        "object_id",
        "expected_object_revision",
        "base_domain_revision",
        "payload_sha256",
        "authority_identity",
        "proposed",
    }
    layer = candidate.get("target_layer")
    proposed = update.get("proposed")
    if (
        set(update) != required
        or update.get("schema_version") != "1.0.0"
        or layer not in {"scenario", "series_memory"}
        or update.get("layer") != layer
        or update.get("authority_identity") != "sqlite:structured-records-v1"
        or not isinstance(proposed, Mapping)
    ):
        raise MemoryCandidateReviewError("hierarchy update candidate metadata is invalid")
    object_id = _required_str(update, "object_id")
    expected_object_revision = _positive_int(
        update.get("expected_object_revision"),
        "hierarchy update object revision",
    )
    base_domain_revision = _positive_int(
        update.get("base_domain_revision"),
        "hierarchy update domain revision",
    )
    normalized = dict(proposed)
    project_id = _required_str(candidate, "project_id")
    proposed_project_matches = (
        normalized.get("project_id") == project_id
        if layer == "scenario"
        else project_id in normalized.get("project_ids", ())
    )
    if (
        normalized.get("id") != object_id
        or normalized.get("revision") != base_domain_revision + 1
        or not proposed_project_matches
    ):
        raise MemoryCandidateReviewError("hierarchy update target evidence drifted")
    expected_hash = hashlib.sha256(
        json.dumps(
            {
                "project_id": project_id,
                "layer": layer,
                "object_id": object_id,
                "expected_object_revision": expected_object_revision,
                "base_domain_revision": base_domain_revision,
                "payload": normalized,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if update.get("payload_sha256") != expected_hash:
        raise MemoryCandidateReviewError("hierarchy update payload evidence drifted")
    normalized.update(
        {
            "source_refs": [dict(ref) for ref in source_refs],
            "trust_status": "system_generated",
            "updated_at": timestamp,
            "hierarchy_update": {
                "candidate_id": _required_str(candidate, "id"),
                "layer": layer,
                "object_id": object_id,
                "expected_object_revision": expected_object_revision,
                "base_domain_revision": base_domain_revision,
                "payload_sha256": expected_hash,
                "authority_identity": "sqlite:structured-records-v1",
            },
        }
    )
    return normalized


def build_memory_staging_draft(
    candidate: Mapping[str, object],
    *,
    target_layer: str,
    source_refs: Sequence[Mapping[str, object]],
    timestamp: str,
    atom_type: str | None = None,
    tags: Sequence[str] = (),
    confidence: float = 0.7,
    series_id: str | None = None,
    scenario_ids: Sequence[str] = (),
    atom_ids: Sequence[str] = (),
) -> dict[str, object]:
    """Build a deterministic generic staging draft without writing storage.

    The durable SQLite review-staging saga reuses exactly the draft shape of
    the established JSON review path.  It deliberately leaves Project Skill
    and External Series authority checks to its caller.
    """

    return _draft_for_layer(
        candidate,
        target_layer=target_layer,
        source_refs=source_refs,
        timestamp=timestamp,
        atom_type=atom_type,
        tags=tags,
        confidence=confidence,
        series_id=series_id,
        scenario_ids=scenario_ids,
        atom_ids=atom_ids,
    )


def _external_series_update_draft(
    candidate: Mapping[str, object],
    *,
    source_refs: Sequence[Mapping[str, object]],
    timestamp: str,
) -> dict[str, object]:
    update = candidate.get("external_series_update")
    if not isinstance(update, Mapping):
        raise MemoryCandidateReviewError("external Series candidate metadata is invalid")
    required = {
        "draft_id",
        "series_id",
        "series_memory_id",
        "expected_object_revision",
        "base_series_revision",
        "payload_sha256",
        "authority_identity",
        "proposed",
    }
    if set(update) != required or update.get("authority_identity") not in {
        "json:object-store-v1",
        "sqlite:structured-records-v1",
    }:
        raise MemoryCandidateReviewError("external Series candidate metadata is invalid")
    draft_id = _required_str(update, "draft_id")
    series_id = _required_str(update, "series_id")
    memory_id = _required_str(update, "series_memory_id")
    expected_object_revision = _positive_int(update.get("expected_object_revision"), "external Series object revision")
    base_series_revision = _positive_int(update.get("base_series_revision"), "external Series base revision")
    payload_sha256 = _required_str(update, "payload_sha256")
    proposed = update.get("proposed")
    if not isinstance(proposed, Mapping):
        raise MemoryCandidateReviewError("external Series candidate proposed payload is invalid")
    normalized = dict(proposed)
    if (
        normalized.get("id") != memory_id
        or normalized.get("series_id") != series_id
        or _positive_int(normalized.get("revision"), "external Series proposed revision")
        != base_series_revision + 1
        or normalized.get("overview") != _required_str(candidate, "proposed_content")
    ):
        raise MemoryCandidateReviewError("external Series candidate target evidence drifted")
    expected_hash = hashlib.sha256(
        json.dumps(
            {
                "draft_id": draft_id,
                "project_id": _required_str(candidate, "project_id"),
                "series_id": series_id,
                "series_memory_id": memory_id,
                "base_object_revision": expected_object_revision,
                "base_series_revision": base_series_revision,
                "payload": normalized,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if payload_sha256 != expected_hash:
        raise MemoryCandidateReviewError("external Series candidate payload evidence drifted")
    normalized.update(
        {
            "source_refs": [dict(ref) for ref in source_refs],
            "trust_status": "system_generated",
            "updated_at": timestamp,
            "external_series_update": {
                "candidate_id": _required_str(candidate, "id"),
                "expected_object_revision": expected_object_revision,
                "base_series_revision": base_series_revision,
                "payload_sha256": payload_sha256,
                "authority_identity": _required_str(update, "authority_identity"),
            },
        }
    )
    return normalized


def _target_layer(value: str) -> str:
    if value not in {"atom", "scenario", "series_memory", "project_skill"}:
        raise MemoryCandidateReviewError("Memory Candidate target_layer is not supported")
    return value


def _project_skill_source_refs(source_refs: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "source_id": _required_str(ref, "source_id"),
            "locator": _required_str(ref, "locator"),
        }
        for ref in source_refs
    ]


def _staging_publication_state(layer: str | None) -> str:
    return {
        "atom": "staging_atom_created_not_published",
        "scenario": "staging_scenario_created_not_published",
        "series_memory": "staging_series_memory_created_not_published",
        "project_skill": "staging_project_skill_created_not_published",
    }.get(layer, "staging_memory_created_not_published")


def _title_from_content(content: str) -> str:
    normalized = " ".join(content.split())
    return normalized[:60] or "Memory draft"


def _optional_clean(value: str | None) -> str | None:
    if value is None:
        return None
    clean = value.strip()
    return clean or None


def _atom_type_for_candidate(candidate_type: str) -> str:
    return {
        "answer_fact": "fact",
        "answer_decision": "decision",
        "answer_action": "action",
        "answer_summary": "other",
        "document_takeaway": "fact",
        "other": "other",
    }[candidate_type]


def _dedupe_tags(tags: Sequence[str]) -> list[str]:
    deduped: list[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            continue
        cleaned = tag.strip()
        if cleaned and cleaned not in deduped:
            deduped.append(cleaned)
    return deduped


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise MemoryCandidateReviewError(f"{key} is required")
    return value


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise MemoryCandidateReviewError(f"{label} is invalid")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
