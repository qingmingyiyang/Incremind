from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from core.product_core.team_memory_candidate_import import (
    ObjectStoreTeamMemoryImportDraftRepository,
)
from core.product_core.team_memory_source_staging import (
    TeamMemorySourceStagingConflict,
    TeamMemorySourceStagingError,
    TeamMemorySourceStagingRepository,
)
from core.product_core.object_store_port import (
    ProductObjectStorePort,
    is_revision_conflict,
)


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class TeamMemorySourceAuthorityError(ValueError):
    """Raised when a staged Team Source cannot be committed safely."""


class TeamMemorySourceAuthorityConflict(TeamMemorySourceAuthorityError):
    """Raised on Source, draft, operation or staging revision drift."""


@dataclass(frozen=True, slots=True)
class TeamMemorySourceAuthorityResult:
    operation_id: str
    staging_id: str
    source_id: str
    source_uri: str
    source_revision: int
    status: str
    replayed: bool
    receipt: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ObjectStoreTeamSourceAuthority:
    object_store: ProductObjectStorePort
    collection: str = "sources"

    def create_exact(
        self,
        source: Mapping[str, object],
    ) -> tuple[Mapping[str, object], int, bool]:
        payload = dict(source)
        source_id = _required_text(payload, "id")
        existing = self.object_store.read(self.collection, source_id)
        if existing is not None:
            if dict(existing) != payload:
                raise TeamMemorySourceAuthorityConflict(
                    "local Source identity already has different content"
                )
            return (
                dict(existing),
                self.object_store.revision(self.collection, source_id),
                True,
            )
        try:
            self.object_store.write(
                self.collection,
                source_id,
                payload,
                expected_revision=0,
            )
        except Exception as error:
            if not is_revision_conflict(self.object_store, error):
                raise
            existing = self.object_store.read(self.collection, source_id)
            if existing is not None and dict(existing) == payload:
                return (
                    dict(existing),
                    self.object_store.revision(self.collection, source_id),
                    True,
                )
            raise TeamMemorySourceAuthorityConflict(
                "local Source create-only CAS conflict"
            ) from error
        return (
            payload,
            self.object_store.revision(self.collection, source_id),
            False,
        )

    def get(self, source_id: str) -> Mapping[str, object] | None:
        value = self.object_store.read(
            self.collection,
            _required_identifier(source_id, "source_id"),
        )
        return dict(value) if value is not None else None

    def revision(self, source_id: str) -> int:
        return self.object_store.revision(
            self.collection,
            _required_identifier(source_id, "source_id"),
        )


class CommitTeamMemoryStagingToSource:
    """Recoverable staged → committing → completed Source creation saga."""

    def __init__(
        self,
        *,
        drafts: ObjectStoreTeamMemoryImportDraftRepository,
        staging: TeamMemorySourceStagingRepository,
        sources: ObjectStoreTeamSourceAuthority,
        after_claimed: Callable[[], None] | None = None,
        after_source_created: Callable[[], None] | None = None,
    ) -> None:
        self._drafts = drafts
        self._staging = staging
        self._sources = sources
        self._after_claimed = after_claimed
        self._after_source_created = after_source_created

    def execute(
        self,
        staging_id: str,
        *,
        expected_staging_revision: int,
        confirmed: bool,
        completed_at: str,
    ) -> TeamMemorySourceAuthorityResult:
        if confirmed is not True:
            raise TeamMemorySourceAuthorityError(
                "local Source creation requires explicit confirmation"
            )
        if (
            not isinstance(expected_staging_revision, int)
            or isinstance(expected_staging_revision, bool)
            or expected_staging_revision < 1
        ):
            raise TeamMemorySourceAuthorityError(
                "expected staging revision is invalid"
            )
        timestamp = _timestamp(completed_at, "completed_at")
        clean_staging_id = _required_identifier(
            staging_id,
            "staging_id",
        )
        record = self._staging.get(clean_staging_id)
        if record is None:
            raise TeamMemorySourceAuthorityError(
                "team source staging was not found"
            )
        status = record.get("status")
        if status == "completed":
            return self._completed_replay(
                record,
                expected_staging_revision=expected_staging_revision,
            )
        if status == "withdrawn":
            raise TeamMemorySourceAuthorityConflict(
                "withdrawn team source staging cannot create Source"
            )
        if status == "staged":
            record = self._claim(
                record,
                expected_staging_revision=expected_staging_revision,
                claimed_at=timestamp,
            )
            if self._after_claimed is not None:
                self._after_claimed()
        elif status == "committing":
            receipt = _mapping(record, "receipt")
            if (
                receipt.get("claimed_staging_revision")
                != expected_staging_revision
            ):
                raise TeamMemorySourceAuthorityConflict(
                    "team source staging claim revision drifted"
                )
        else:
            raise TeamMemorySourceAuthorityConflict(
                "team source staging state is invalid"
            )

        source = dict(_mapping(record, "proposed_source"))
        source_id = _required_identifier(source.get("id"), "source_id")
        operation_id = _operation_id(record, expected_staging_revision)
        receipt = _mapping(record, "receipt")
        if receipt.get("operation_id") != operation_id:
            raise TeamMemorySourceAuthorityConflict(
                "team source staging operation identity drifted"
            )
        saved_source, source_revision, _source_replayed = (
            self._sources.create_exact(source)
        )
        if self._after_source_created is not None:
            self._after_source_created()
        completed = self._complete(
            record,
            source=saved_source,
            source_revision=source_revision,
            completed_at=timestamp,
        )
        return TeamMemorySourceAuthorityResult(
            operation_id=operation_id,
            staging_id=clean_staging_id,
            source_id=source_id,
            source_uri=_required_text(saved_source, "storage_uri"),
            source_revision=source_revision,
            status="completed",
            replayed=False,
            receipt=dict(_mapping(completed, "receipt")),
        )

    def _claim(
        self,
        record: Mapping[str, object],
        *,
        expected_staging_revision: int,
        claimed_at: str,
    ) -> Mapping[str, object]:
        staging_id = _required_identifier(record.get("id"), "staging_id")
        current_revision = self._staging.revision(staging_id)
        if current_revision != expected_staging_revision:
            raise TeamMemorySourceAuthorityConflict(
                "team source staging revision drifted"
            )
        self._validate_pending_draft(record)
        operation_id = _operation_id(record, expected_staging_revision)
        claimed = {
            **dict(record),
            "status": "committing",
            "receipt": {
                **dict(_mapping(record, "receipt")),
                "operation_id": operation_id,
                "claimed_staging_revision": expected_staging_revision,
                "claimed_at": claimed_at,
            },
            "updated_at": claimed_at,
        }
        try:
            return self._staging.replace(
                claimed,
                expected_revision=expected_staging_revision,
            )
        except TeamMemorySourceStagingConflict as error:
            raise TeamMemorySourceAuthorityConflict(
                "team source staging claim CAS conflict"
            ) from error

    def _validate_pending_draft(
        self,
        record: Mapping[str, object],
    ) -> None:
        draft_id = _required_identifier(record.get("draft_id"), "draft_id")
        draft = self._drafts.get(draft_id)
        if draft is None or draft.get("status") != "pending_review":
            raise TeamMemorySourceAuthorityConflict(
                "team import draft is no longer pending review"
            )
        if self._drafts.revision(draft_id) != record.get("draft_revision"):
            raise TeamMemorySourceAuthorityConflict(
                "team import draft revision drifted"
            )
        source = _mapping(record, "proposed_source")
        metadata = _mapping(source, "metadata")
        content = draft.get("proposed_content")
        if (
            not isinstance(content, str)
            or metadata.get("content") != content
            or hashlib.sha256(content.encode("utf-8")).hexdigest()
            != source.get("content_hash")
        ):
            raise TeamMemorySourceAuthorityConflict(
                "team import draft content drifted"
            )

    def _complete(
        self,
        record: Mapping[str, object],
        *,
        source: Mapping[str, object],
        source_revision: int,
        completed_at: str,
    ) -> Mapping[str, object]:
        staging_id = _required_identifier(record.get("id"), "staging_id")
        current_revision = self._staging.revision(staging_id)
        current = self._staging.get(staging_id)
        if current is None or current.get("status") != "committing":
            raise TeamMemorySourceAuthorityConflict(
                "team source staging completion state drifted"
            )
        if _mapping(current, "receipt").get("operation_id") != _mapping(
            record,
            "receipt",
        ).get("operation_id"):
            raise TeamMemorySourceAuthorityConflict(
                "team source staging completion operation drifted"
            )
        proposal = dict(_mapping(current, "proposed_source"))
        metadata = dict(_mapping(proposal, "metadata"))
        metadata.pop("content", None)
        proposal["metadata"] = metadata
        completed = {
            **dict(current),
            "status": "completed",
            "proposed_source": proposal,
            "receipt": {
                **dict(_mapping(current, "receipt")),
                "source_created": True,
                "completed_at": completed_at,
                "source_uri": _required_text(source, "storage_uri"),
                "source_revision": source_revision,
            },
            "safety": {
                **dict(_mapping(current, "safety")),
                "source_authority_written": True,
            },
            "updated_at": completed_at,
        }
        try:
            return self._staging.replace(
                completed,
                expected_revision=current_revision,
            )
        except TeamMemorySourceStagingConflict as error:
            raise TeamMemorySourceAuthorityConflict(
                "team source staging completion CAS conflict"
            ) from error

    def _completed_replay(
        self,
        record: Mapping[str, object],
        *,
        expected_staging_revision: int,
    ) -> TeamMemorySourceAuthorityResult:
        receipt = _mapping(record, "receipt")
        if (
            receipt.get("claimed_staging_revision")
            != expected_staging_revision
        ):
            raise TeamMemorySourceAuthorityConflict(
                "completed team source staging revision drifted"
            )
        proposal = _mapping(record, "proposed_source")
        source_id = _required_identifier(proposal.get("id"), "source_id")
        source = self._sources.get(source_id)
        if source is None:
            raise TeamMemorySourceAuthorityConflict(
                "completed team source staging has no Source authority"
            )
        if (
            source.get("content_hash") != proposal.get("content_hash")
            or source.get("storage_uri") != proposal.get("storage_uri")
            or self._sources.revision(source_id)
            != receipt.get("source_revision")
        ):
            raise TeamMemorySourceAuthorityConflict(
                "completed team source authority drifted"
            )
        return TeamMemorySourceAuthorityResult(
            operation_id=_required_identifier(
                receipt.get("operation_id"),
                "operation_id",
            ),
            staging_id=_required_identifier(record.get("id"), "staging_id"),
            source_id=source_id,
            source_uri=_required_text(source, "storage_uri"),
            source_revision=int(receipt["source_revision"]),
            status="completed",
            replayed=True,
            receipt=dict(receipt),
        )


def _operation_id(
    record: Mapping[str, object],
    claimed_revision: int,
) -> str:
    source = _mapping(record, "proposed_source")
    digest = hashlib.sha256(
        "\n".join(
            (
                str(record.get("id")),
                str(claimed_revision),
                str(source.get("id")),
                str(source.get("content_hash")),
            )
        ).encode("utf-8")
    ).hexdigest()
    return f"team-source-operation-{digest[:32]}"


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    result = value.get(key)
    if not isinstance(result, Mapping):
        raise TeamMemorySourceAuthorityError(f"{key} is invalid")
    return result


def _required_identifier(value: object, field: str) -> str:
    clean = _required_text_value(value, field)
    if not _IDENTIFIER.fullmatch(clean):
        raise TeamMemorySourceAuthorityError(f"{field} is invalid")
    return clean


def _required_text(value: Mapping[str, object], key: str) -> str:
    return _required_text_value(value.get(key), key)


def _required_text_value(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TeamMemorySourceAuthorityError(f"{field} is invalid")
    return value.strip()


def _timestamp(value: object, field: str) -> str:
    clean = _required_text_value(value, field)
    try:
        parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
    except ValueError as error:
        raise TeamMemorySourceAuthorityError(f"{field} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TeamMemorySourceAuthorityError(f"{field} is invalid")
    return clean
