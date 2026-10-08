from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from core.memory_core import (
    MemoryCandidateRepositoryError,
    ObjectStoreMemoryCandidateRepository,
    memory_candidate_id,
)
from core.product_core.team_memory_source_authority_saga import (
    ObjectStoreTeamSourceAuthority,
)
from core.product_core.team_memory_source_staging import (
    TeamMemorySourceStagingRepository,
)


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TARGET_BY_ASSET_TYPE = {
    "skill": "project_skill",
    "chat_memory": "series_memory",
    "llm_wiki": "series_memory",
    "code_graph": "series_memory",
}


class TeamMemorySourceCandidateError(ValueError):
    """Raised when a Team Source cannot safely enter human candidate review."""


class TeamMemorySourceCandidateConflict(TeamMemorySourceCandidateError):
    """Raised when the preview, Source, staging, or candidate identity drifted."""


@dataclass(frozen=True, slots=True)
class TeamMemorySourceCandidatePreview:
    preview_id: str
    candidate_id: str
    staging_id: str
    staging_revision: int
    source_id: str
    source_uri: str
    source_revision: int
    source_content_sha256: str
    project_id: str
    target_layer: str
    candidate_type: str
    proposed_content: str
    difference: Mapping[str, object]
    safety: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class TeamMemorySourceCandidateResult:
    candidate_id: str
    status: str
    target_layer: str
    replayed: bool
    candidate: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class TeamMemorySourceCandidateDispositionResult:
    operation_id: str
    candidate_id: str
    status: str
    replayed: bool
    candidate: Mapping[str, object]


class PrepareTeamMemorySourceCandidate:
    """Create one review-only candidate from an exact completed Team Source."""

    def __init__(
        self,
        *,
        staging: TeamMemorySourceStagingRepository,
        sources: ObjectStoreTeamSourceAuthority,
        candidates: ObjectStoreMemoryCandidateRepository,
    ) -> None:
        self._staging = staging
        self._sources = sources
        self._candidates = candidates

    def preview(self, staging_id: str) -> TeamMemorySourceCandidatePreview:
        clean_staging_id = _identifier(staging_id, "staging_id")
        record = self._staging.get(clean_staging_id)
        if record is None:
            raise TeamMemorySourceCandidateError("team source staging was not found")
        if record.get("status") != "completed":
            raise TeamMemorySourceCandidateConflict(
                "team Source candidate requires completed staging"
            )
        receipt = _mapping(record, "receipt")
        proposal = _mapping(record, "proposed_source")
        source_id = _identifier(proposal.get("id"), "source_id")
        source = self._sources.get(source_id)
        if source is None:
            raise TeamMemorySourceCandidateConflict("completed team Source is missing")
        source_revision = self._sources.revision(source_id)
        expected_revision = _positive_int(receipt.get("source_revision"), "source_revision")
        source_hash = _sha256(source.get("content_hash"), "source content_hash")
        expected_hash = _sha256(proposal.get("content_hash"), "proposed content_hash")
        if (
            source_revision != expected_revision
            or source_hash != expected_hash
            or source.get("storage_uri") != receipt.get("source_uri")
            or source.get("storage_uri") != proposal.get("storage_uri")
        ):
            raise TeamMemorySourceCandidateConflict(
                "completed team Source authority drifted"
            )
        metadata = _mapping(source, "metadata")
        content = _content(metadata.get("content"))
        if (
            hashlib.sha256(content.encode("utf-8")).hexdigest() != source_hash
            or len(content.encode("utf-8")) != source.get("size_bytes")
        ):
            raise TeamMemorySourceCandidateConflict(
                "completed team Source content drifted"
            )
        origin = _mapping(record, "origin")
        asset_type = _identifier(origin.get("asset_type"), "asset_type")
        target_layer = _TARGET_BY_ASSET_TYPE.get(asset_type)
        if target_layer is None:
            raise TeamMemorySourceCandidateError(
                f"team asset type is not supported for candidate review: {asset_type}"
            )
        candidate_type = "other" if target_layer == "project_skill" else "answer_summary"
        staging_revision = self._staging.revision(clean_staging_id)
        preview_id = _stable_id(
            "team-source-candidate-preview",
            clean_staging_id,
            str(staging_revision),
            source_id,
            str(source_revision),
            source_hash,
            target_layer,
            candidate_type,
        )
        candidate_id = memory_candidate_id(
            "team-source",
            clean_staging_id,
            str(staging_revision),
            source_id,
            str(source_revision),
            source_hash,
            target_layer,
            candidate_type,
        )
        return TeamMemorySourceCandidatePreview(
            preview_id=preview_id,
            candidate_id=candidate_id,
            staging_id=clean_staging_id,
            staging_revision=staging_revision,
            source_id=source_id,
            source_uri=str(source["storage_uri"]),
            source_revision=source_revision,
            source_content_sha256=source_hash,
            project_id=_identifier(record.get("project_id"), "project_id"),
            target_layer=target_layer,
            candidate_type=candidate_type,
            proposed_content=content,
            difference={
                "state": "new_candidate",
                "current_revision": None,
                "proposed_source_revision": source_revision,
                "content_sha256": source_hash,
                "content_bytes": len(content.encode("utf-8")),
            },
            safety={
                "requires_user_confirmation": True,
                "candidate_only": True,
                "long_term_memory_written": False,
                "project_skill_written": False,
                "publication_created": False,
                "automatic_recall_enabled": False,
            },
        )

    def stage(
        self,
        staging_id: str,
        *,
        preview_id: str,
        expected_staging_revision: int,
        expected_source_revision: int,
        confirmed: bool,
        created_at: str,
    ) -> TeamMemorySourceCandidateResult:
        if confirmed is not True:
            raise TeamMemorySourceCandidateError(
                "team Source candidate requires explicit confirmation"
            )
        preview = self.preview(staging_id)
        if preview.preview_id != preview_id:
            raise TeamMemorySourceCandidateConflict(
                "team Source candidate preview identity drifted"
            )
        if preview.staging_revision != _positive_int(
            expected_staging_revision, "expected_staging_revision"
        ):
            raise TeamMemorySourceCandidateConflict(
                "team Source staging revision drifted"
            )
        if preview.source_revision != _positive_int(
            expected_source_revision, "expected_source_revision"
        ):
            raise TeamMemorySourceCandidateConflict(
                "team Source authority revision drifted"
            )
        timestamp = _timestamp(created_at, "created_at")
        candidate_id = preview.candidate_id
        source_uri = preview.source_uri
        candidate = {
            "schema_version": "1.0.0",
            "id": candidate_id,
            "project_id": preview.project_id,
            "target_layer": preview.target_layer,
            "candidate_type": preview.candidate_type,
            "status": "pending_review",
            "proposed_content": preview.proposed_content,
            "source_refs": [
                {
                    "source_id": preview.source_id,
                    "locator": source_uri,
                }
            ],
            "provenance": {
                "model_result_id": None,
                "model_request_id": None,
                "recall_result_id": None,
                "document_id": None,
                "document_revision": None,
                "source_id": preview.source_id,
                "source_revision": preview.source_revision,
                "source_content_sha256": preview.source_content_sha256,
                "input_refs": [
                    {
                        "kind": "source",
                        "object_id": preview.source_id,
                        "uri": source_uri,
                    }
                ],
            },
            "review": {
                "requires_user_confirmation": True,
                "auto_promote_allowed": False,
                "reason": (
                    "Team Source 仅进入本地候选审阅；用户确认后仍须经过既有"
                    " Memory 或 Project Skill 发布流程。"
                ),
                "reviewed_by": None,
                "reviewed_at": None,
            },
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        existing = self._candidates.get(candidate_id)
        if existing is not None:
            if _candidate_intent(existing) != _candidate_intent(candidate):
                raise TeamMemorySourceCandidateConflict(
                    "team Source Memory Candidate identity conflict"
                )
            saved = existing
            replayed = True
        else:
            saved = self._candidates.save(candidate)
            replayed = False
        return TeamMemorySourceCandidateResult(
            candidate_id=candidate_id,
            status=str(saved["status"]),
            target_layer=str(saved["target_layer"]),
            replayed=replayed,
            candidate=dict(saved),
        )

    def dispose(
        self,
        candidate_id: str,
        *,
        expected_candidate_revision: int,
        action: str,
        reason: str,
        confirmed: bool,
        reviewed_at: str,
    ) -> TeamMemorySourceCandidateDispositionResult:
        if confirmed is not True:
            raise TeamMemorySourceCandidateError(
                "team Source candidate disposition requires explicit confirmation"
            )
        clean_candidate_id = _identifier(candidate_id, "candidate_id")
        clean_action = action.strip() if isinstance(action, str) else ""
        if clean_action not in {"reject", "withdraw"}:
            raise TeamMemorySourceCandidateError(
                "team Source candidate action must be reject or withdraw"
            )
        clean_reason = _bounded(reason, "reason")
        timestamp = _timestamp(reviewed_at, "reviewed_at")
        candidate = self._candidates.get(clean_candidate_id)
        if candidate is None:
            raise TeamMemorySourceCandidateError("team Source candidate was not found")
        erasure = candidate.get("source_erasure")
        if candidate.get("status") in {"rejected", "withdrawn"} and isinstance(
            erasure, Mapping
        ):
            return self._disposed_replay(
                candidate,
                expected_candidate_revision=expected_candidate_revision,
                action=clean_action,
                reason=clean_reason,
            )
        if candidate.get("status") != "pending_review":
            raise TeamMemorySourceCandidateConflict(
                "team Source candidate is not pending review"
            )
        current_revision = self._candidates.object_store.revision(
            self._candidates.collection,
            clean_candidate_id,
        )
        if current_revision != _positive_int(
            expected_candidate_revision,
            "expected_candidate_revision",
        ):
            raise TeamMemorySourceCandidateConflict(
                "team Source candidate revision drifted"
            )
        provenance = _direct_source_provenance(candidate)
        source_id = _identifier(provenance.get("source_id"), "source_id")
        source_revision = _positive_int(
            provenance.get("source_revision"),
            "source_revision",
        )
        source_hash = _sha256(
            provenance.get("source_content_sha256"),
            "source_content_sha256",
        )
        if not any(
            isinstance(ref, Mapping) and ref.get("source_id") == source_id
            for ref in candidate.get("source_refs", ())
        ):
            raise TeamMemorySourceCandidateConflict(
                "team Source candidate source reference drifted"
            )
        reason_sha256 = hashlib.sha256(clean_reason.encode("utf-8")).hexdigest()
        operation_id = _stable_id(
            "team-source-candidate-erasure",
            clean_candidate_id,
            str(current_revision),
            clean_action,
            reason_sha256,
            source_id,
            str(source_revision),
            source_hash,
        )
        status = "rejected" if clean_action == "reject" else "withdrawn"
        review_reason = (
            "用户拒绝并擦除 Team Source 候选正文。"
            if clean_action == "reject"
            else "用户撤回并擦除 Team Source 候选正文。"
        )
        disposed = {
            **dict(candidate),
            "status": status,
            "proposed_content": "[content erased]",
            "source_refs": [
                {
                    "source_id": ref.get("source_id"),
                    "locator": ref.get("locator"),
                }
                for ref in candidate.get("source_refs", ())
                if isinstance(ref, Mapping)
            ],
            "review": {
                "requires_user_confirmation": True,
                "auto_promote_allowed": False,
                "reason": review_reason,
                "reviewed_by": "user",
                "reviewed_at": timestamp,
            },
            "source_erasure": {
                "schema_version": "1.0.0",
                "state": "content_erased",
                "action": clean_action,
                "operation_id": operation_id,
                "requested_candidate_revision": current_revision,
                "source_id": source_id,
                "source_revision": source_revision,
                "source_content_sha256": source_hash,
                "reason_sha256": reason_sha256,
                "erased_at": timestamp,
            },
            "updated_at": timestamp,
        }
        try:
            saved = self._candidates.update(
                disposed,
                expected_revision=current_revision,
            )
        except MemoryCandidateRepositoryError as error:
            raise TeamMemorySourceCandidateConflict(
                "team Source candidate disposition CAS conflict"
            ) from error
        return TeamMemorySourceCandidateDispositionResult(
            operation_id=operation_id,
            candidate_id=clean_candidate_id,
            status=status,
            replayed=False,
            candidate=dict(saved),
        )

    def _disposed_replay(
        self,
        candidate: Mapping[str, object],
        *,
        expected_candidate_revision: int,
        action: str,
        reason: str,
    ) -> TeamMemorySourceCandidateDispositionResult:
        erasure = _mapping(candidate, "source_erasure")
        reason_sha256 = hashlib.sha256(reason.encode("utf-8")).hexdigest()
        if (
            erasure.get("action") != action
            or erasure.get("requested_candidate_revision")
            != _positive_int(expected_candidate_revision, "expected_candidate_revision")
            or erasure.get("reason_sha256") != reason_sha256
        ):
            raise TeamMemorySourceCandidateConflict(
                "team Source candidate disposition replay drifted"
            )
        return TeamMemorySourceCandidateDispositionResult(
            operation_id=_identifier(erasure.get("operation_id"), "operation_id"),
            candidate_id=_identifier(candidate.get("id"), "candidate_id"),
            status=str(candidate["status"]),
            replayed=True,
            candidate=dict(candidate),
        )


def _candidate_intent(candidate: Mapping[str, object]) -> tuple[object, ...]:
    provenance = _mapping(candidate, "provenance")
    return (
        candidate.get("project_id"),
        candidate.get("target_layer"),
        candidate.get("candidate_type"),
        candidate.get("proposed_content"),
        tuple(
            sorted(
                (str(item.get("source_id")), str(item.get("locator")))
                for item in candidate.get("source_refs", ())
                if isinstance(item, Mapping)
            )
        ),
        provenance.get("source_id"),
        provenance.get("source_revision"),
        provenance.get("source_content_sha256"),
    )


def _mapping(value: Mapping[str, object], field: str) -> Mapping[str, object]:
    nested = value.get(field)
    if not isinstance(nested, Mapping):
        raise TeamMemorySourceCandidateError(f"{field} is invalid")
    return nested


def _direct_source_provenance(candidate: Mapping[str, object]) -> Mapping[str, object]:
    provenance = _mapping(candidate, "provenance")
    if not all(
        provenance.get(field) is not None
        for field in ("source_id", "source_revision", "source_content_sha256")
    ):
        raise TeamMemorySourceCandidateConflict(
            "candidate is not a Team direct-Source candidate"
        )
    return provenance


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise TeamMemorySourceCandidateError(f"{field} is invalid")
    return value


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise TeamMemorySourceCandidateError(f"{field} is invalid")
    return value


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise TeamMemorySourceCandidateError(f"{field} is invalid")
    return value


def _content(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TeamMemorySourceCandidateError("team Source content is unavailable")
    return value


def _bounded(value: object, field: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise TeamMemorySourceCandidateError(f"{field} is invalid")
    return value.strip()


def _timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TeamMemorySourceCandidateError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise TeamMemorySourceCandidateError(f"{field} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TeamMemorySourceCandidateError(f"{field} is invalid")
    return value


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"
