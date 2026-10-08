from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from core.product_core.team_memory_candidate_import import (
    ObjectStoreTeamMemoryImportDraftRepository,
)
from core.product_core.object_store_port import (
    ProductObjectStorePort,
    is_revision_conflict,
)


TEAM_SOURCE_STAGING_SCHEMA_VERSION = "1.1.0"
_LEGACY_TEAM_SOURCE_STAGING_SCHEMA_VERSION = "1.0.0"
TEAM_SOURCE_STAGING_COLLECTION = "team_memory_source_staging"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_NAMESPACE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class TeamMemorySourceStagingError(ValueError):
    """Raised when a Team draft cannot safely enter local Source staging."""


class TeamMemorySourceStagingConflict(TeamMemorySourceStagingError):
    """Raised when preview, draft, remote version or staging CAS drifted."""


@dataclass(frozen=True, slots=True)
class TeamMemorySourcePreview:
    preview_id: str
    draft_id: str
    draft_revision: int
    project_id: str
    source_id: str
    source_uri: str
    content_sha256: str
    size_bytes: int
    difference: Mapping[str, object]
    safety: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class TeamMemorySourceStagingResult:
    staging_id: str
    source_id: str
    status: str
    replayed: bool
    record: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class TeamMemorySourceStagingRepository:
    object_store: ProductObjectStorePort
    collection: str = TEAM_SOURCE_STAGING_COLLECTION

    def get(self, staging_id: str) -> Mapping[str, object] | None:
        clean_id = _identifier(staging_id, "staging_id")
        value = self.object_store.read(self.collection, clean_id)
        if value is None:
            return None
        _validate_staging_record(value)
        return dict(value)

    def list_forget_pending(self) -> tuple[Mapping[str, object], ...]:
        """Return staging records whose hard forget was claimed but not finalized."""
        return tuple(
            dict(value)
            for value in self.object_store.list(self.collection)
            if isinstance(value, dict) and value.get("status") == "forgetting"
        )

    def list_for_remote_asset(
        self,
        *,
        service_id: str,
        team_id: str,
        asset_id: str,
        project_id: str,
    ) -> tuple[Mapping[str, object], ...]:
        expected = (
            _identifier(service_id, "service_id"),
            _identifier(team_id, "team_id"),
            _identifier(asset_id, "asset_id"),
            _identifier(project_id, "project_id"),
        )
        results: list[Mapping[str, object]] = []
        for value in self.object_store.list(self.collection):
            _validate_staging_record(value)
            origin = value.get("origin")
            if not isinstance(origin, Mapping):
                continue
            observed = (
                origin.get("service_id"),
                origin.get("team_id"),
                origin.get("asset_id"),
                value.get("project_id"),
            )
            if observed == expected:
                results.append(dict(value))
        return tuple(
            sorted(
                results,
                key=lambda item: (
                    int(_mapping(item, "origin").get("asset_version") or 0),
                    str(item.get("id") or ""),
                ),
            )
        )

    def save(
        self,
        record: Mapping[str, object],
    ) -> tuple[Mapping[str, object], bool]:
        payload = dict(record)
        _validate_staging_record(payload)
        staging_id = _required_identifier(payload, "id")
        existing = self.object_store.read(self.collection, staging_id)
        if existing is not None:
            _validate_staging_record(existing)
            if dict(existing) != payload:
                raise TeamMemorySourceStagingConflict(
                    "team source staging identity conflict"
                )
            return dict(existing), True
        try:
            self.object_store.write(
                self.collection,
                staging_id,
                payload,
                expected_revision=0,
            )
        except Exception as error:
            if not is_revision_conflict(self.object_store, error):
                raise
            existing = self.object_store.read(self.collection, staging_id)
            if existing is not None:
                _validate_staging_record(existing)
            if existing is not None and dict(existing) == payload:
                return dict(existing), True
            raise TeamMemorySourceStagingConflict(
                "team source staging CAS conflict"
            ) from error
        return payload, False

    def replace(
        self,
        record: Mapping[str, object],
        *,
        expected_revision: int,
    ) -> Mapping[str, object]:
        payload = dict(record)
        _validate_staging_record(payload)
        staging_id = _required_identifier(payload, "id")
        try:
            self.object_store.write(
                self.collection,
                staging_id,
                payload,
                expected_revision=expected_revision,
            )
        except Exception as error:
            if not is_revision_conflict(self.object_store, error):
                raise
            raise TeamMemorySourceStagingConflict(
                "team source staging revision conflict"
            ) from error
        return payload

    def revision(self, staging_id: str) -> int:
        return self.object_store.revision(
            self.collection,
            _identifier(staging_id, "staging_id"),
        )


class PrepareTeamMemoryLocalSource:
    """Preview and stage a local Source proposal without writing Source authority."""

    def __init__(
        self,
        *,
        drafts: ObjectStoreTeamMemoryImportDraftRepository,
        staging: TeamMemorySourceStagingRepository,
        namespace_id: str = "default",
    ) -> None:
        self._drafts = drafts
        self._staging = staging
        self._namespace_id = _namespace(namespace_id)

    def preview(self, draft_id: str) -> TeamMemorySourcePreview:
        draft, draft_revision = self._pending_draft(draft_id)
        source = _draft_source(draft)
        project_id = _required_identifier(draft, "project_id")
        content = _required_content(draft)
        digest = _sha256(source.get("content_sha256"), "content_sha256")
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != digest:
            raise TeamMemorySourceStagingConflict(
                "team import draft content hash drifted"
            )
        source_id = _stable_id(
            "source-team",
            str(source["service_id"]),
            str(source["team_id"]),
            str(source["asset_id"]),
            str(source["asset_version"]),
            digest,
            project_id,
        )
        source_uri = f"crp://{self._namespace_id}/sources/{source_id}"
        difference = self._difference(
            source=source,
            project_id=project_id,
            digest=digest,
        )
        preview_id = _stable_id(
            "team-source-preview",
            str(draft["id"]),
            str(draft_revision),
            source_id,
            digest,
            str(difference["state"]),
        )
        return TeamMemorySourcePreview(
            preview_id=preview_id,
            draft_id=str(draft["id"]),
            draft_revision=draft_revision,
            project_id=project_id,
            source_id=source_id,
            source_uri=source_uri,
            content_sha256=digest,
            size_bytes=len(content.encode("utf-8")),
            difference=difference,
            safety=_closed_safety(),
        )

    def stage(
        self,
        draft_id: str,
        *,
        preview_id: str,
        expected_draft_revision: int,
        confirmed: bool,
        staged_at: str,
    ) -> TeamMemorySourceStagingResult:
        if confirmed is not True:
            raise TeamMemorySourceStagingError(
                "team source staging requires explicit confirmation"
            )
        timestamp = _timestamp(staged_at, "staged_at")
        preview = self.preview(draft_id)
        if preview.preview_id != preview_id:
            raise TeamMemorySourceStagingConflict(
                "team source preview identity drifted"
            )
        if preview.draft_revision != expected_draft_revision:
            raise TeamMemorySourceStagingConflict(
                "team import draft revision drifted"
            )
        draft, current_revision = self._pending_draft(draft_id)
        if current_revision != expected_draft_revision:
            raise TeamMemorySourceStagingConflict(
                "team import draft revision drifted"
            )
        source = _draft_source(draft)
        content = _required_content(draft)
        authorization = _mapping(draft, "authorization")
        staging_id = _stable_id(
            "team-source-staging",
            preview.preview_id,
            preview.source_id,
        )
        record = {
            "schema_version": TEAM_SOURCE_STAGING_SCHEMA_VERSION,
            "id": staging_id,
            "preview_id": preview.preview_id,
            "draft_id": preview.draft_id,
            "draft_revision": preview.draft_revision,
            "project_id": preview.project_id,
            "status": "staged",
            "origin": {
                "source_kind": "team_memory_asset",
                "service_id": source["service_id"],
                "team_id": source["team_id"],
                "asset_id": source["asset_id"],
                "asset_type": source["asset_type"],
                "asset_version": source["asset_version"],
                "visibility": source["visibility"],
                "authorization_fingerprint": authorization[
                    "authorization_fingerprint"
                ],
                "inventory_fingerprint": authorization[
                    "inventory_fingerprint"
                ],
            },
            "proposed_source": {
                "schema_version": "1.1.0",
                "id": preview.source_id,
                "type": "text",
                "title": source["asset_name"],
                "capture_mode": "snapshot",
                "storage_uri": preview.source_uri,
                "original_url": None,
                "content_hash": preview.content_sha256,
                "media_type": "text/plain",
                "size_bytes": preview.size_bytes,
                "parser_version": None,
                "processing_state": "captured",
                "created_at": timestamp,
                # The remote asset carries no trusted occurrence timestamp.
                # Authorization.verified_at is deliberately not used here.
                "occurred_at": None,
                "recorded_at": timestamp,
                "imported_from_legacy": False,
                "trust_status": "imported_unverified",
                "metadata": {
                    "encoding": "utf-8",
                    "content": content,
                    "source_kind": "team_memory_asset",
                    "team_import_draft_id": preview.draft_id,
                    "project_id": preview.project_id,
                    "remote_asset_version": source["asset_version"],
                    "remote_content_sha256": preview.content_sha256,
                },
            },
            "difference": dict(preview.difference),
            "receipt": {
                "confirmed": True,
                "source_created": False,
                "memory_created": False,
                "project_skill_created": False,
                "publication_created": False,
                "automatic_recall_enabled": False,
            },
            "safety": _closed_safety(),
            "created_at": timestamp,
            "updated_at": timestamp,
            "occurred_at": None,
            "recorded_at": timestamp,
        }
        saved, replayed = self._staging.save(record)
        return TeamMemorySourceStagingResult(
            staging_id=staging_id,
            source_id=preview.source_id,
            status=str(saved["status"]),
            replayed=replayed,
            record=saved,
        )

    def _pending_draft(
        self,
        draft_id: str,
    ) -> tuple[Mapping[str, object], int]:
        clean_id = _identifier(draft_id, "draft_id")
        draft = self._drafts.get(clean_id)
        if draft is None:
            raise TeamMemorySourceStagingError(
                "team import draft was not found"
            )
        if draft.get("status") != "pending_review":
            raise TeamMemorySourceStagingConflict(
                "team import draft is not pending review"
            )
        revision = self._drafts.revision(clean_id)
        if revision < 1:
            raise TeamMemorySourceStagingConflict(
                "team import draft revision is invalid"
            )
        return draft, revision

    def _difference(
        self,
        *,
        source: Mapping[str, object],
        project_id: str,
        digest: str,
    ) -> dict[str, object]:
        existing = self._staging.list_for_remote_asset(
            service_id=str(source["service_id"]),
            team_id=str(source["team_id"]),
            asset_id=str(source["asset_id"]),
            project_id=project_id,
        )
        if not existing:
            return {
                "state": "new_source",
                "previous_asset_version": None,
                "previous_content_sha256": None,
                "content_changed": False,
            }
        previous = existing[-1]
        origin = _mapping(previous, "origin")
        proposed = _mapping(previous, "proposed_source")
        previous_version = _positive_int(
            origin.get("asset_version"),
            "previous_asset_version",
        )
        previous_digest = _sha256(
            proposed.get("content_hash"),
            "previous_content_sha256",
        )
        current_version = _positive_int(
            source.get("asset_version"),
            "asset_version",
        )
        if current_version < previous_version:
            raise TeamMemorySourceStagingConflict(
                "team asset version regressed"
            )
        if current_version == previous_version and digest != previous_digest:
            raise TeamMemorySourceStagingConflict(
                "team asset content changed without version increment"
            )
        if current_version == previous_version:
            previous_difference = _mapping(previous, "difference")
            return dict(previous_difference)
        return {
            "state": "new_remote_version",
            "previous_asset_version": previous_version,
            "previous_content_sha256": previous_digest,
            "content_changed": digest != previous_digest,
        }


class WithdrawTeamMemorySourceStaging:
    """Erase staged body before Source authority consumes the proposal."""

    def __init__(
        self,
        *,
        staging: TeamMemorySourceStagingRepository,
    ) -> None:
        self._staging = staging

    def execute(
        self,
        staging_id: str,
        *,
        reason: str,
        withdrawn_at: str,
    ) -> TeamMemorySourceStagingResult:
        clean_id = _identifier(staging_id, "staging_id")
        clean_reason = _bounded_text(reason, "reason", maximum=500)
        timestamp = _timestamp(withdrawn_at, "withdrawn_at")
        current = self._staging.get(clean_id)
        if current is None:
            raise TeamMemorySourceStagingError(
                "team source staging was not found"
            )
        if current.get("status") == "withdrawn":
            receipt = _mapping(current, "receipt")
            if receipt.get("withdraw_reason") == clean_reason:
                return TeamMemorySourceStagingResult(
                    staging_id=clean_id,
                    source_id=str(
                        _mapping(current, "proposed_source").get("id")
                    ),
                    status="withdrawn",
                    replayed=True,
                    record=current,
                )
            raise TeamMemorySourceStagingConflict(
                "team source staging already has a terminal disposition"
            )
        if current.get("status") != "staged":
            raise TeamMemorySourceStagingConflict(
                "team source staging is not staged"
            )
        source = dict(_mapping(current, "proposed_source"))
        metadata = dict(_mapping(source, "metadata"))
        metadata.pop("content", None)
        source["metadata"] = metadata
        tombstone = {
            **dict(current),
            "status": "withdrawn",
            "proposed_source": source,
            "receipt": {
                **dict(_mapping(current, "receipt")),
                "withdrawn": True,
                "withdraw_reason": clean_reason,
                "withdrawn_at": timestamp,
            },
            "updated_at": timestamp,
        }
        saved = self._staging.replace(
            tombstone,
            expected_revision=self._staging.revision(clean_id),
        )
        return TeamMemorySourceStagingResult(
            staging_id=clean_id,
            source_id=str(source["id"]),
            status="withdrawn",
            replayed=False,
            record=saved,
        )


def serialize_team_memory_source_preview(
    preview: TeamMemorySourcePreview,
) -> dict[str, object]:
    return {
        "schema_version": TEAM_SOURCE_STAGING_SCHEMA_VERSION,
        "preview_id": preview.preview_id,
        "draft_id": preview.draft_id,
        "draft_revision": preview.draft_revision,
        "project_id": preview.project_id,
        "source_id": preview.source_id,
        "source_uri": preview.source_uri,
        "content_sha256": preview.content_sha256,
        "size_bytes": preview.size_bytes,
        "difference": dict(preview.difference),
        "safety": dict(preview.safety),
        "content_included": False,
    }


def _validate_staging_record(value: Mapping[str, object]) -> None:
    legacy = value.get("schema_version") == _LEGACY_TEAM_SOURCE_STAGING_SCHEMA_VERSION
    required = {
        "schema_version",
        "id",
        "preview_id",
        "draft_id",
        "draft_revision",
        "project_id",
        "status",
        "origin",
        "proposed_source",
        "difference",
        "receipt",
        "safety",
        "created_at",
        "updated_at",
    }
    if not legacy:
        required |= {"occurred_at", "recorded_at"}
    if set(value) != required:
        raise TeamMemorySourceStagingError(
            "team source staging fields are invalid"
        )
    if value.get("schema_version") not in {
        _LEGACY_TEAM_SOURCE_STAGING_SCHEMA_VERSION,
        TEAM_SOURCE_STAGING_SCHEMA_VERSION,
    }:
        raise TeamMemorySourceStagingError(
            "team source staging schema is unsupported"
        )
    _required_identifier(value, "id")
    _required_identifier(value, "preview_id")
    _required_identifier(value, "draft_id")
    _positive_int(value.get("draft_revision"), "draft_revision")
    _required_identifier(value, "project_id")
    status = value.get("status")
    if status not in {
        "staged",
        "committing",
        "completed",
        "withdrawn",
        "abandoned",
        "forgetting",
        "forgotten",
    }:
        raise TeamMemorySourceStagingError(
            "team source staging status is invalid"
        )
    origin = _mapping(value, "origin")
    if set(origin) != {
        "source_kind",
        "service_id",
        "team_id",
        "asset_id",
        "asset_type",
        "asset_version",
        "visibility",
        "authorization_fingerprint",
        "inventory_fingerprint",
    } or origin.get("source_kind") != "team_memory_asset":
        raise TeamMemorySourceStagingError(
            "team source staging origin is invalid"
        )
    for key in ("service_id", "team_id", "asset_id"):
        _required_identifier(origin, key)
    _positive_int(origin.get("asset_version"), "asset_version")
    for key in ("authorization_fingerprint", "inventory_fingerprint"):
        _sha256(origin.get(key), key)
    source = _mapping(value, "proposed_source")
    source_legacy = source.get("schema_version") == "1.0.0"
    expected_source_fields = {
        "schema_version",
        "id",
        "type",
        "title",
        "capture_mode",
        "storage_uri",
        "original_url",
        "content_hash",
        "media_type",
        "size_bytes",
        "parser_version",
        "processing_state",
        "created_at",
        "imported_from_legacy",
        "trust_status",
        "metadata",
    }
    if not source_legacy:
        expected_source_fields |= {"occurred_at", "recorded_at"}
    if set(source) != expected_source_fields or any(
        (
            source.get("schema_version") not in {"1.0.0", "1.1.0"},
            source.get("type") != "text",
            source.get("capture_mode") != "snapshot",
            source.get("original_url") is not None,
            source.get("media_type") != "text/plain",
            source.get("parser_version") is not None,
            source.get("processing_state") != "captured",
            source.get("imported_from_legacy") is not False,
            source.get("trust_status") != "imported_unverified",
        )
    ):
        raise TeamMemorySourceStagingError(
            "team source proposal is invalid"
        )
    source_id = _required_identifier(source, "id")
    source_digest = _sha256(source.get("content_hash"), "content_hash")
    if not isinstance(source.get("title"), str) or not source["title"].strip():
        raise TeamMemorySourceStagingError(
            "team source proposal title is invalid"
        )
    if (
        not isinstance(source.get("storage_uri"), str)
        or not re.fullmatch(
            rf"crp://[a-z0-9][a-z0-9._-]{{0,63}}/sources/{re.escape(source_id)}",
            source["storage_uri"],
        )
    ):
        raise TeamMemorySourceStagingError(
            "team source proposal URI is invalid"
        )
    _positive_int(source.get("size_bytes"), "size_bytes")
    _timestamp(source.get("created_at"), "source.created_at")
    if not source_legacy:
        _nullable_timestamp(source.get("occurred_at"), "source.occurred_at")
        _timestamp(source.get("recorded_at"), "source.recorded_at")
    metadata = _mapping(source, "metadata")
    allowed_metadata = {
        "encoding",
        "content",
        "source_kind",
        "team_import_draft_id",
        "project_id",
        "remote_asset_version",
        "remote_content_sha256",
    }
    if (
        not set(metadata) <= allowed_metadata
        or set(metadata) - {"content"}
        != allowed_metadata - {"content"}
        or metadata.get("encoding") != "utf-8"
        or metadata.get("source_kind") != "team_memory_asset"
    ):
        raise TeamMemorySourceStagingError(
            "team source proposal metadata is invalid"
        )
    _required_identifier(metadata, "team_import_draft_id")
    _required_identifier(metadata, "project_id")
    _positive_int(
        metadata.get("remote_asset_version"),
        "remote_asset_version",
    )
    _sha256(
        metadata.get("remote_content_sha256"),
        "remote_content_sha256",
    )
    if (
        metadata.get("team_import_draft_id") != value.get("draft_id")
        or metadata.get("project_id") != value.get("project_id")
        or metadata.get("remote_asset_version")
        != origin.get("asset_version")
        or metadata.get("remote_content_sha256") != source_digest
    ):
        raise TeamMemorySourceStagingError(
            "team source proposal provenance drifted"
        )
    if status in {"staged", "committing"}:
        content = _required_content({"proposed_content": metadata.get("content")})
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != source.get(
            "content_hash"
        ) or len(content.encode("utf-8")) != source.get("size_bytes"):
            raise TeamMemorySourceStagingError(
                "team source staging content hash mismatch"
            )
    elif "content" in metadata:
        raise TeamMemorySourceStagingError(
            "terminal team source staging must erase content"
        )
    receipt = _mapping(value, "receipt")
    base_receipt = {
        "confirmed",
        "source_created",
        "memory_created",
        "project_skill_created",
        "publication_created",
        "automatic_recall_enabled",
    }
    terminal_receipt = {
        "withdrawn",
        "withdraw_reason",
        "withdrawn_at",
    }
    abandoned_receipt = {
        "abandoned",
        "abandon_reason",
        "abandoned_at",
        "conflict_code",
    }
    operation_receipt = {
        "operation_id",
        "claimed_staging_revision",
        "claimed_at",
    }
    completed_receipt = {
        "completed_at",
        "source_uri",
        "source_revision",
    }
    forgetting_receipt = {
        "forget_operation_id",
        "forget_requested_staging_revision",
        "forget_requested_at",
        "forget_reason",
    }
    forgotten_receipt = {"forgotten_at"}
    expected_receipt = {
        "staged": base_receipt,
        "committing": base_receipt | operation_receipt,
        "completed": base_receipt | operation_receipt | completed_receipt,
        "withdrawn": base_receipt | terminal_receipt,
        "abandoned": base_receipt | operation_receipt | abandoned_receipt,
        "forgetting": (
            base_receipt
            | operation_receipt
            | completed_receipt
            | forgetting_receipt
        ),
        "forgotten": (
            base_receipt
            | operation_receipt
            | completed_receipt
            | forgetting_receipt
            | forgotten_receipt
        ),
    }[str(status)]
    if set(receipt) != expected_receipt or receipt.get("confirmed") is not True:
        raise TeamMemorySourceStagingError(
            "team source staging receipt boundary is invalid"
        )
    if receipt.get("source_created") is not (
        status in {"completed", "forgetting"}
    ):
        raise TeamMemorySourceStagingError(
            "team source staging receipt boundary is invalid"
        )
    for key in (
        "memory_created",
        "project_skill_created",
        "publication_created",
        "automatic_recall_enabled",
    ):
        if receipt.get(key) is not False:
            raise TeamMemorySourceStagingError(
                "team source staging receipt boundary is invalid"
            )
    if status in {
        "committing",
        "completed",
        "abandoned",
        "forgetting",
        "forgotten",
    }:
        _required_identifier(receipt, "operation_id")
        _positive_int(
            receipt.get("claimed_staging_revision"),
            "claimed_staging_revision",
        )
        _timestamp(receipt.get("claimed_at"), "claimed_at")
    if status in {"completed", "forgetting", "forgotten"}:
        _timestamp(receipt.get("completed_at"), "completed_at")
        if receipt.get("source_uri") != source.get("storage_uri"):
            raise TeamMemorySourceStagingError(
                "team source staging completed source URI drifted"
            )
        _positive_int(receipt.get("source_revision"), "source_revision")
    if status == "withdrawn":
        if receipt.get("withdrawn") is not True:
            raise TeamMemorySourceStagingError(
                "team source staging withdrawal receipt is invalid"
            )
        _bounded_text(
            receipt.get("withdraw_reason"),
            "withdraw_reason",
            maximum=500,
        )
        _timestamp(receipt.get("withdrawn_at"), "withdrawn_at")
    if status == "abandoned":
        if (
            receipt.get("abandoned") is not True
            or receipt.get("conflict_code") != "source_identity_conflict"
        ):
            raise TeamMemorySourceStagingError(
                "team source staging abandonment receipt is invalid"
            )
        _bounded_text(
            receipt.get("abandon_reason"),
            "abandon_reason",
            maximum=500,
        )
        _timestamp(receipt.get("abandoned_at"), "abandoned_at")
    if status in {"forgetting", "forgotten"}:
        _required_identifier(receipt, "forget_operation_id")
        _positive_int(
            receipt.get("forget_requested_staging_revision"),
            "forget_requested_staging_revision",
        )
        _timestamp(
            receipt.get("forget_requested_at"),
            "forget_requested_at",
        )
        _bounded_text(
            receipt.get("forget_reason"),
            "forget_reason",
            maximum=500,
        )
    if status == "forgotten":
        _timestamp(receipt.get("forgotten_at"), "forgotten_at")
    difference = _mapping(value, "difference")
    if set(difference) != {
        "state",
        "previous_asset_version",
        "previous_content_sha256",
        "content_changed",
    } or difference.get("state") not in {
        "new_source",
        "new_remote_version",
    }:
        raise TeamMemorySourceStagingError(
            "team source staging difference is invalid"
        )
    if difference.get("state") == "new_source":
        if (
            difference.get("previous_asset_version") is not None
            or difference.get("previous_content_sha256") is not None
            or difference.get("content_changed") is not False
        ):
            raise TeamMemorySourceStagingError(
                "team source staging difference is invalid"
            )
    else:
        _positive_int(
            difference.get("previous_asset_version"),
            "previous_asset_version",
        )
        _sha256(
            difference.get("previous_content_sha256"),
            "previous_content_sha256",
        )
        if not isinstance(difference.get("content_changed"), bool):
            raise TeamMemorySourceStagingError(
                "team source staging difference is invalid"
            )
    safety = _mapping(value, "safety")
    expected_safety = _closed_safety()
    expected_safety["source_authority_written"] = status in {
        "completed",
        "forgetting",
    }
    if dict(safety) != expected_safety:
        raise TeamMemorySourceStagingError(
            "team source staging safety boundary is invalid"
        )
    _timestamp(value.get("created_at"), "created_at")
    _timestamp(value.get("updated_at"), "updated_at")
    if not legacy:
        _nullable_timestamp(value.get("occurred_at"), "occurred_at")
        _timestamp(value.get("recorded_at"), "recorded_at")


def _draft_source(draft: Mapping[str, object]) -> Mapping[str, object]:
    return _mapping(draft, "source")


def _required_content(draft: Mapping[str, object]) -> str:
    content = draft.get("proposed_content")
    if not isinstance(content, str) or not content.strip():
        raise TeamMemorySourceStagingError(
            "team import draft content is unavailable"
        )
    return content


def _closed_safety() -> dict[str, bool]:
    return {
        "network_called": False,
        "provider_called": False,
        "source_authority_written": False,
        "memory_written": False,
        "project_skill_written": False,
        "projection_written": False,
        "automatic_recall_enabled": False,
    }


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    result = value.get(key)
    if not isinstance(result, Mapping):
        raise TeamMemorySourceStagingError(f"{key} is invalid")
    return result


def _required_identifier(value: Mapping[str, object], key: str) -> str:
    return _identifier(value.get(key), key)


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise TeamMemorySourceStagingError(f"{field} is invalid")
    return value


def _namespace(value: object) -> str:
    if not isinstance(value, str) or not _NAMESPACE.fullmatch(value):
        raise TeamMemorySourceStagingError("namespace_id is invalid")
    return value


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise TeamMemorySourceStagingError(f"{field} is invalid")
    return value


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise TeamMemorySourceStagingError(f"{field} is invalid")
    return value


def _bounded_text(value: object, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TeamMemorySourceStagingError(f"{field} is invalid")
    clean = value.strip()
    if len(clean) > maximum:
        raise TeamMemorySourceStagingError(f"{field} is invalid")
    return clean


def _timestamp(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TeamMemorySourceStagingError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise TeamMemorySourceStagingError(f"{field} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TeamMemorySourceStagingError(f"{field} is invalid")
    return value


def _nullable_timestamp(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _timestamp(value, field)


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:32]}"
