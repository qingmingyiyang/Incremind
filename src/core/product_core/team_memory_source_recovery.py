from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from core.product_core.team_memory_candidate_import import (
    DiscardTeamMemoryImportDraft,
    ObjectStoreTeamMemoryImportDraftRepository,
    TeamMemoryCandidateImportConflict,
    TeamMemoryCandidateImportError,
)
from core.product_core.team_memory_source_authority_saga import (
    ObjectStoreTeamSourceAuthority,
    TeamMemorySourceAuthorityConflict,
    TeamMemorySourceAuthorityError,
)
from core.product_core.team_memory_source_staging import (
    TeamMemorySourceStagingConflict,
    TeamMemorySourceStagingRepository,
)
from core.product_core.object_store_port import (
    ProductObjectStorePort,
    is_revision_conflict,
)


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DEPENDENCY_COLLECTIONS = (
    "documents",
    "source_content_reads",
    "source_structures",
    "source_outputs",
    "media_processing_outputs",
    "memory_atoms",
    "memory_scenarios",
    "memory_series_memory",
    "project_skills",
    "memory_candidates",
    "memory_publications",
    "jobs",
)


@dataclass(frozen=True, slots=True)
class TeamSourceRecoveryDiagnosis:
    staging_id: str
    source_id: str
    status: str
    action: str
    source_revision: int | None
    expected_content_sha256: str
    observed_content_sha256: str | None


@dataclass(frozen=True, slots=True)
class TeamSourceForgetResult:
    operation_id: str
    staging_id: str
    source_id: str
    status: str
    replayed: bool
    receipt: Mapping[str, object]


def diagnose_team_source_commit(
    *,
    staging: TeamMemorySourceStagingRepository,
    sources: ObjectStoreTeamSourceAuthority,
    staging_id: str,
) -> TeamSourceRecoveryDiagnosis:
    clean_id = _identifier(staging_id, "staging_id")
    record = staging.get(clean_id)
    if record is None:
        raise TeamMemorySourceAuthorityError("team source staging was not found")
    if record.get("status") != "committing":
        raise TeamMemorySourceAuthorityConflict(
            "team source recovery diagnosis requires committing staging"
        )
    proposal = _mapping(record, "proposed_source")
    source_id = _identifier(proposal.get("id"), "source_id")
    expected_hash = _sha256(proposal.get("content_hash"), "content_hash")
    source = sources.get(source_id)
    if source is None:
        return TeamSourceRecoveryDiagnosis(
            clean_id,
            source_id,
            "resume_source_missing",
            "resume_commit",
            None,
            expected_hash,
            None,
        )
    observed_hash = _optional_sha256(source.get("content_hash"))
    if dict(source) == dict(proposal):
        return TeamSourceRecoveryDiagnosis(
            clean_id,
            source_id,
            "resume_receipt_pending",
            "resume_commit",
            sources.revision(source_id),
            expected_hash,
            observed_hash,
        )
    return TeamSourceRecoveryDiagnosis(
        clean_id,
        source_id,
        "source_identity_conflict",
        "abandon_or_resolve_source",
        sources.revision(source_id),
        expected_hash,
        observed_hash,
    )


class AbandonConflictingTeamSourceCommit:
    """Erase a committing proposal only when a different Source owns its id."""

    def __init__(
        self,
        *,
        staging: TeamMemorySourceStagingRepository,
        sources: ObjectStoreTeamSourceAuthority,
    ) -> None:
        self._staging = staging
        self._sources = sources

    def execute(
        self,
        staging_id: str,
        *,
        expected_staging_revision: int,
        reason: str,
        abandoned_at: str,
    ) -> Mapping[str, object]:
        clean_id = _identifier(staging_id, "staging_id")
        existing = self._staging.get(clean_id)
        clean_reason = _bounded(reason, "reason")
        if existing is not None and existing.get("status") == "abandoned":
            receipt = _mapping(existing, "receipt")
            if (
                receipt.get("conflict_code") == "source_identity_conflict"
                and receipt.get("abandon_reason") == clean_reason
            ):
                return existing
            raise TeamMemorySourceAuthorityConflict(
                "team source staging already has a terminal disposition"
            )
        diagnosis = diagnose_team_source_commit(
            staging=self._staging,
            sources=self._sources,
            staging_id=staging_id,
        )
        if diagnosis.status != "source_identity_conflict":
            raise TeamMemorySourceAuthorityConflict(
                "team source commit can only be abandoned for identity conflict"
            )
        current = self._staging.get(diagnosis.staging_id)
        if current is None or self._staging.revision(
            diagnosis.staging_id
        ) != _positive_int(expected_staging_revision, "expected_staging_revision"):
            raise TeamMemorySourceAuthorityConflict(
                "team source staging revision drifted"
            )
        proposal = _erase_content(_mapping(current, "proposed_source"))
        abandoned = {
            **dict(current),
            "status": "abandoned",
            "proposed_source": proposal,
            "receipt": {
                **dict(_mapping(current, "receipt")),
                "abandoned": True,
                "abandon_reason": clean_reason,
                "abandoned_at": _timestamp(abandoned_at, "abandoned_at"),
                "conflict_code": "source_identity_conflict",
            },
            "updated_at": _timestamp(abandoned_at, "abandoned_at"),
        }
        try:
            return self._staging.replace(
                abandoned,
                expected_revision=expected_staging_revision,
            )
        except TeamMemorySourceStagingConflict as error:
            raise TeamMemorySourceAuthorityConflict(
                "team source abandonment CAS conflict"
            ) from error


class ForgetTeamCreatedSource:
    """Recoverably erase an unreferenced Source created by a completed Team saga."""

    def __init__(
        self,
        *,
        object_store: ProductObjectStorePort,
        staging: TeamMemorySourceStagingRepository,
        after_claimed: Callable[[], None] | None = None,
        after_source_tombstoned: Callable[[], None] | None = None,
        after_source_deleted: Callable[[], None] | None = None,
    ) -> None:
        self._store = object_store
        self._staging = staging
        self._after_claimed = after_claimed
        self._after_source_tombstoned = after_source_tombstoned
        self._after_source_deleted = after_source_deleted

    def execute(
        self,
        staging_id: str,
        *,
        expected_staging_revision: int,
        confirmed: bool,
        reason: str,
        forgotten_at: str,
    ) -> TeamSourceForgetResult:
        if confirmed is not True:
            raise TeamMemorySourceAuthorityError(
                "team Source hard forget requires explicit confirmation"
            )
        timestamp = _timestamp(forgotten_at, "forgotten_at")
        clean_id = _identifier(staging_id, "staging_id")
        record = self._staging.get(clean_id)
        if record is None:
            raise TeamMemorySourceAuthorityError("team source staging was not found")
        clean_reason = _bounded(reason, "reason")
        if record.get("status") == "forgotten":
            return self._replay(
                record,
                expected_staging_revision,
                reason=clean_reason,
            )
        if record.get("status") == "completed":
            record = self._claim(
                record,
                expected_staging_revision=expected_staging_revision,
                reason=clean_reason,
                timestamp=timestamp,
            )
            if self._after_claimed is not None:
                self._after_claimed()
        elif record.get("status") == "forgetting":
            receipt = _mapping(record, "receipt")
            if receipt.get("forget_requested_staging_revision") != expected_staging_revision:
                raise TeamMemorySourceAuthorityConflict(
                    "team Source forget claim revision drifted"
                )
            if receipt.get("forget_reason") != clean_reason:
                raise TeamMemorySourceAuthorityConflict(
                    "team Source forget reason drifted"
                )
        else:
            raise TeamMemorySourceAuthorityConflict(
                "team Source staging is not forgettable"
            )
        source_id = _identifier(
            _mapping(record, "proposed_source").get("id"),
            "source_id",
        )
        operation_id = _identifier(
            _mapping(record, "receipt").get("forget_operation_id"),
            "forget_operation_id",
        )
        source = self._store.read_including_deleted("sources", source_id)
        if source is not None:
            if _source_owned_by_receipt(source, record):
                receipt = _mapping(record, "receipt")
                if (
                    not _is_our_tombstone(
                        source,
                        operation_id,
                    )
                    and self._store.revision("sources", source_id)
                    != receipt.get("source_revision")
                ):
                    raise TeamMemorySourceAuthorityConflict(
                        "team Source authority revision drifted before hard forget"
                    )
                blockers = _dependency_blockers(self._store, source_id)
                if blockers:
                    raise TeamMemorySourceAuthorityConflict(
                        "team Source hard forget blocked by inbound references: "
                        + ", ".join(blockers)
                    )
                source = self._tombstone(
                    source_id=source_id,
                    source=source,
                    operation_id=operation_id,
                    timestamp=timestamp,
                )
                if self._after_source_tombstoned is not None:
                    self._after_source_tombstoned()
                self._delete_tombstone(source_id, source, operation_id)
                if self._after_source_deleted is not None:
                    self._after_source_deleted()
            elif not _is_our_tombstone(source, operation_id):
                raise TeamMemorySourceAuthorityConflict(
                    "team Source authority drifted before hard forget"
                )
            else:
                self._delete_tombstone(source_id, source, operation_id)
        self._erase_origin_draft(record, timestamp)
        completed = self._complete(record, timestamp)
        return TeamSourceForgetResult(
            operation_id,
            clean_id,
            source_id,
            "forgotten",
            False,
            dict(_mapping(completed, "receipt")),
        )

    def _claim(
        self,
        record: Mapping[str, object],
        *,
        expected_staging_revision: int,
        reason: str,
        timestamp: str,
    ) -> Mapping[str, object]:
        staging_id = _identifier(record.get("id"), "staging_id")
        if self._staging.revision(staging_id) != _positive_int(
            expected_staging_revision,
            "expected_staging_revision",
        ):
            raise TeamMemorySourceAuthorityConflict(
                "team Source staging revision drifted"
            )
        source_id = _identifier(
            _mapping(record, "proposed_source").get("id"),
            "source_id",
        )
        source = self._store.read_including_deleted("sources", source_id)
        if (
            source is None
            or not _source_owned_by_receipt(source, record)
            or self._store.revision("sources", source_id)
            != _mapping(record, "receipt").get("source_revision")
        ):
            raise TeamMemorySourceAuthorityConflict(
                "completed team Source authority drifted"
            )
        blockers = _dependency_blockers(self._store, source_id)
        if blockers:
            raise TeamMemorySourceAuthorityConflict(
                "team Source hard forget blocked by inbound references: "
                + ", ".join(blockers)
            )
        operation_id = _forget_operation_id(record, expected_staging_revision)
        claimed = {
            **dict(record),
            "status": "forgetting",
            "receipt": {
                **dict(_mapping(record, "receipt")),
                "forget_operation_id": operation_id,
                "forget_requested_staging_revision": expected_staging_revision,
                "forget_requested_at": timestamp,
                "forget_reason": _bounded(reason, "reason"),
            },
            "updated_at": timestamp,
        }
        try:
            return self._staging.replace(
                claimed,
                expected_revision=expected_staging_revision,
            )
        except TeamMemorySourceStagingConflict as error:
            raise TeamMemorySourceAuthorityConflict(
                "team Source forget claim CAS conflict"
            ) from error

    def _tombstone(
        self,
        *,
        source_id: str,
        source: Mapping[str, object],
        operation_id: str,
        timestamp: str,
    ) -> Mapping[str, object]:
        lifecycle = source.get("library_lifecycle")
        if _is_our_tombstone(source, operation_id):
            return source
        if isinstance(lifecycle, Mapping):
            raise TeamMemorySourceAuthorityConflict(
                "team Source lifecycle drifted before hard forget"
            )
        revision = self._store.revision("sources", source_id)
        updated = {
            **dict(source),
            "library_lifecycle": {
                "status": "deleted",
                "operation_id": operation_id,
                "deleted_at": timestamp,
                "undo_expires_at": timestamp,
                "restored_at": None,
                "hard_forget": True,
            },
        }
        try:
            self._store.write(
                "sources",
                source_id,
                updated,
                expected_revision=revision,
            )
        except Exception as error:
            if not is_revision_conflict(self._store, error):
                raise
            raise TeamMemorySourceAuthorityConflict(
                "team Source tombstone CAS conflict"
            ) from error
        return updated

    def _delete_tombstone(
        self,
        source_id: str,
        source: Mapping[str, object],
        operation_id: str,
    ) -> None:
        current = self._store.read_including_deleted("sources", source_id)
        if current is None:
            return
        if dict(current) != dict(source) or not _is_our_tombstone(
            current,
            operation_id,
        ):
            raise TeamMemorySourceAuthorityConflict(
                "team Source tombstone drifted before physical delete"
            )
        self._store.delete("sources", source_id)

    def _complete(
        self,
        record: Mapping[str, object],
        timestamp: str,
    ) -> Mapping[str, object]:
        staging_id = _identifier(record.get("id"), "staging_id")
        current = self._staging.get(staging_id)
        if current is None or current.get("status") != "forgetting":
            raise TeamMemorySourceAuthorityConflict(
                "team Source forget completion state drifted"
            )
        revision = self._staging.revision(staging_id)
        completed = {
            **dict(current),
            "status": "forgotten",
            "receipt": {
                **dict(_mapping(current, "receipt")),
                "source_created": False,
                "forgotten_at": timestamp,
            },
            "safety": {
                **dict(_mapping(current, "safety")),
                "source_authority_written": False,
            },
            "updated_at": timestamp,
        }
        return self._staging.replace(completed, expected_revision=revision)

    def _erase_origin_draft(
        self,
        record: Mapping[str, object],
        timestamp: str,
    ) -> None:
        draft_id = _identifier(record.get("draft_id"), "draft_id")
        repository = ObjectStoreTeamMemoryImportDraftRepository(self._store)
        draft = repository.get(draft_id)
        if draft is None:
            raise TeamMemorySourceAuthorityConflict(
                "team Source origin draft is unavailable for hard forget"
            )
        if draft.get("status") in {"rejected", "withdrawn"}:
            if draft.get("proposed_content") is None:
                return
            raise TeamMemorySourceAuthorityConflict(
                "terminal team Source origin draft still contains content"
            )
        try:
            DiscardTeamMemoryImportDraft(drafts=repository).execute(
                draft_id,
                disposition="withdrawn",
                reason=_mapping(record, "receipt").get("forget_reason"),
                reviewed_at=timestamp,
            )
        except (
            TeamMemoryCandidateImportConflict,
            TeamMemoryCandidateImportError,
        ) as error:
            raise TeamMemorySourceAuthorityConflict(
                "team Source origin draft could not be erased"
            ) from error

    def _replay(
        self,
        record: Mapping[str, object],
        expected_staging_revision: int,
        *,
        reason: str,
    ) -> TeamSourceForgetResult:
        receipt = _mapping(record, "receipt")
        if receipt.get("forget_requested_staging_revision") != expected_staging_revision:
            raise TeamMemorySourceAuthorityConflict(
                "forgotten team Source request revision drifted"
            )
        if receipt.get("forget_reason") != reason:
            raise TeamMemorySourceAuthorityConflict(
                "forgotten team Source reason drifted"
            )
        source_id = _identifier(
            _mapping(record, "proposed_source").get("id"),
            "source_id",
        )
        if self._store.read_including_deleted("sources", source_id) is not None:
            raise TeamMemorySourceAuthorityConflict(
                "forgotten team Source unexpectedly exists"
            )
        return TeamSourceForgetResult(
            _identifier(receipt.get("forget_operation_id"), "forget_operation_id"),
            _identifier(record.get("id"), "staging_id"),
            source_id,
            "forgotten",
            True,
            dict(receipt),
        )


def _source_owned_by_receipt(
    source: Mapping[str, object],
    record: Mapping[str, object],
) -> bool:
    proposal = _mapping(record, "proposed_source")
    receipt = _mapping(record, "receipt")
    metadata = source.get("metadata")
    return (
        source.get("id") == proposal.get("id")
        and source.get("content_hash") == proposal.get("content_hash")
        and source.get("storage_uri") == receipt.get("source_uri")
        and isinstance(metadata, Mapping)
        and metadata.get("source_kind") == "team_memory_asset"
        and metadata.get("team_import_draft_id") == record.get("draft_id")
    )


def _dependency_blockers(
    store: ProductObjectStorePort,
    source_id: str,
) -> tuple[str, ...]:
    blockers: list[str] = []
    for collection in _DEPENDENCY_COLLECTIONS:
        for item in store.list(collection):
            if collection == "memory_candidates" and _is_erased_terminal_candidate(
                item,
                source_id,
            ):
                continue
            if _contains_source_reference(item, source_id):
                object_id = item.get("id")
                suffix = object_id if isinstance(object_id, str) else "unknown"
                blockers.append(f"{collection}/{suffix}")
    return tuple(sorted(set(blockers)))


def _is_erased_terminal_candidate(
    candidate: Mapping[str, object],
    source_id: str,
) -> bool:
    erasure = candidate.get("source_erasure")
    provenance = candidate.get("provenance")
    action_status = {"reject": "rejected", "withdraw": "withdrawn"}
    return (
        isinstance(erasure, Mapping)
        and isinstance(provenance, Mapping)
        and erasure.get("schema_version") == "1.0.0"
        and erasure.get("state") == "content_erased"
        and candidate.get("status") == action_status.get(erasure.get("action"))
        and candidate.get("proposed_content") == "[content erased]"
        and erasure.get("source_id") == source_id
        and provenance.get("source_id") == source_id
        and erasure.get("source_revision") == provenance.get("source_revision")
        and erasure.get("source_content_sha256")
        == provenance.get("source_content_sha256")
        and isinstance(erasure.get("operation_id"), str)
        and isinstance(erasure.get("reason_sha256"), str)
        and len(erasure["reason_sha256"]) == 64
    )


def _contains_source_reference(value: object, source_id: str) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in {"source_id", "source_uri", "input_uri"} and (
                child == source_id
                or (
                    isinstance(child, str)
                    and child.startswith(f"crp://")
                    and child.endswith(f"/sources/{source_id}")
                )
            ):
                return True
            if key in {"source_refs", "evidence_refs", "input_refs"} and _contains_ref(
                child,
                source_id,
            ):
                return True
            if _contains_source_reference(child, source_id):
                return True
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_source_reference(item, source_id) for item in value)
    return False


def _contains_ref(value: object, source_id: str) -> bool:
    if isinstance(value, str):
        return value == source_id or value.startswith(f"{source_id}#") or value.endswith(
            f"/sources/{source_id}"
        )
    if isinstance(value, Mapping):
        return value.get("source_id") == source_id or any(
            _contains_ref(child, source_id) for child in value.values()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_ref(item, source_id) for item in value)
    return False


def _is_our_tombstone(source: Mapping[str, object], operation_id: str) -> bool:
    lifecycle = source.get("library_lifecycle")
    return (
        isinstance(lifecycle, Mapping)
        and lifecycle.get("status") == "deleted"
        and lifecycle.get("operation_id") == operation_id
        and lifecycle.get("hard_forget") is True
    )


def _erase_content(source: Mapping[str, object]) -> dict[str, object]:
    result = dict(source)
    metadata = dict(_mapping(result, "metadata"))
    metadata.pop("content", None)
    result["metadata"] = metadata
    return result


def _forget_operation_id(
    record: Mapping[str, object],
    staging_revision: int,
) -> str:
    receipt = _mapping(record, "receipt")
    digest = hashlib.sha256(
        "\n".join(
            (
                str(record.get("id")),
                str(staging_revision),
                str(receipt.get("operation_id")),
                str(receipt.get("source_revision")),
            )
        ).encode("utf-8")
    ).hexdigest()
    return f"team-source-forget-{digest[:32]}"


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    result = value.get(key)
    if not isinstance(result, Mapping):
        raise TeamMemorySourceAuthorityError(f"{key} is invalid")
    return result


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise TeamMemorySourceAuthorityError(f"{field} is invalid")
    return value


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise TeamMemorySourceAuthorityError(f"{field} is invalid")
    return value


def _optional_sha256(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else None


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise TeamMemorySourceAuthorityError(f"{field} is invalid")
    return value


def _bounded(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 500:
        raise TeamMemorySourceAuthorityError(f"{field} is invalid")
    return value.strip()


def _timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TeamMemorySourceAuthorityError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise TeamMemorySourceAuthorityError(f"{field} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TeamMemorySourceAuthorityError(f"{field} is invalid")
    return value
