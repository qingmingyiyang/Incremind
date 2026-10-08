from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from core.product_core.object_store_port import (
    ProductObjectStorePort,
    is_revision_conflict,
)


TEAM_IMPORT_SCHEMA_VERSION = "1.1.0"
_LEGACY_TEAM_IMPORT_SCHEMA_VERSION = "1.0.0"
TEAM_IMPORT_COLLECTION = "team_memory_import_drafts"
TEAM_IMPORT_ASSET_TYPES = frozenset(
    {"chat_memory", "skill", "llm_wiki", "code_graph"}
)
TEAM_IMPORT_VISIBILITIES = frozenset(
    {"private", "team", "restricted", "agent", "task"}
)
TEAM_IMPORT_TARGET_LAYERS = frozenset(
    {"atom", "scenario", "series_memory", "project_skill"}
)
TEAM_IMPORT_CANDIDATE_TYPES = frozenset(
    {
        "answer_fact",
        "answer_decision",
        "answer_action",
        "answer_summary",
        "document_takeaway",
        "other",
    }
)
TEAM_IMPORT_MAX_CONTENT_BYTES = 256 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DRAFT_ID = re.compile(r"^team-memory-import-draft-[0-9a-f]{32}$")


class TeamMemoryCandidateImportError(ValueError):
    """Raised when remote evidence cannot safely become a local review draft."""


class TeamMemoryCandidateImportConflict(TeamMemoryCandidateImportError):
    """Raised when an existing deterministic draft has different content."""


@dataclass(frozen=True, slots=True)
class TeamMemoryCandidateImportResult:
    draft_id: str
    project_id: str
    target_layer: str
    status: str
    replayed: bool
    draft: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class TeamMemoryImportDiscardResult:
    draft_id: str
    status: str
    replayed: bool
    content_erased: bool
    tombstone: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ObjectStoreTeamMemoryImportDraftRepository:
    object_store: ProductObjectStorePort
    collection: str = TEAM_IMPORT_COLLECTION

    def save(
        self,
        draft: Mapping[str, object],
    ) -> tuple[Mapping[str, object], bool]:
        payload = dict(draft)
        _validate_draft(payload)
        draft_id = _required_identifier(payload, "id")
        existing = self.object_store.read(self.collection, draft_id)
        if existing is not None:
            _validate_draft(existing)
            if not _replay_compatible(existing, payload):
                raise TeamMemoryCandidateImportConflict(
                    "team memory import draft identity conflict"
                )
            return dict(existing), True
        try:
            self.object_store.write(
                self.collection,
                draft_id,
                payload,
                expected_revision=0,
            )
        except Exception as error:
            if not is_revision_conflict(self.object_store, error):
                raise
            existing = self.object_store.read(self.collection, draft_id)
            if existing is not None:
                _validate_draft(existing)
            if existing is not None and _replay_compatible(existing, payload):
                return dict(existing), True
            raise TeamMemoryCandidateImportConflict(
                "team memory import draft CAS conflict"
            ) from error
        return payload, False

    def get(self, draft_id: str) -> Mapping[str, object] | None:
        clean_id = _identifier(draft_id, "draft_id")
        value = self.object_store.read(self.collection, clean_id)
        if value is None:
            return None
        _validate_draft(value)
        return dict(value)

    def replace(
        self,
        draft: Mapping[str, object],
        *,
        expected_revision: int,
    ) -> Mapping[str, object]:
        payload = dict(draft)
        _validate_draft(payload)
        draft_id = _required_identifier(payload, "id")
        try:
            self.object_store.write(
                self.collection,
                draft_id,
                payload,
                expected_revision=expected_revision,
            )
        except Exception as error:
            if not is_revision_conflict(self.object_store, error):
                raise
            raise TeamMemoryCandidateImportConflict(
                "team memory import draft revision conflict"
            ) from error
        return payload

    def revision(self, draft_id: str) -> int:
        return self.object_store.revision(
            self.collection,
            _identifier(draft_id, "draft_id"),
        )


class CreateTeamMemoryImportDraft:
    """Persist an explicitly authorized remote body as a non-production draft."""

    def __init__(
        self,
        *,
        drafts: ObjectStoreTeamMemoryImportDraftRepository,
    ) -> None:
        self._drafts = drafts

    def execute(
        self,
        *,
        profile: Mapping[str, object],
        asset: Mapping[str, object],
        access_evidence: Mapping[str, object],
        project_id: str,
        target_layer: str,
        candidate_type: str,
        content: str,
        content_sha256: str,
        consent_id: str,
        confirmed: bool,
        created_at: str,
    ) -> TeamMemoryCandidateImportResult:
        identities = _profile_identities(profile)
        asset_values = _asset(asset, expected_team_id=identities["team_id"])
        access = _access_evidence(
            access_evidence,
            identities=identities,
            asset=asset_values,
        )
        clean_project_id = _identifier(project_id, "project_id")
        clean_target = _choice(
            target_layer,
            "target_layer",
            TEAM_IMPORT_TARGET_LAYERS,
        )
        clean_candidate_type = _choice(
            candidate_type,
            "candidate_type",
            TEAM_IMPORT_CANDIDATE_TYPES,
        )
        exact_content = _content(content)
        digest = _sha256(content_sha256, "content_sha256")
        if hashlib.sha256(exact_content.encode("utf-8")).hexdigest() != digest:
            raise TeamMemoryCandidateImportError(
                "team memory asset body hash mismatch"
            )
        clean_consent_id = _bounded_text(
            consent_id,
            "consent_id",
            minimum=16,
            maximum=160,
        )
        if confirmed is not True:
            raise TeamMemoryCandidateImportError(
                "team memory import requires explicit per-asset confirmation"
            )
        timestamp = _timestamp(created_at, "created_at")
        identity_parts = (
            identities["service_id"],
            identities["team_id"],
            identities["agent_id"],
            identities["user_id"],
            asset_values["asset_id"],
            asset_values["asset_type"],
            str(asset_values["version"]),
            digest,
            clean_project_id,
            clean_target,
            clean_candidate_type,
        )
        draft_id = _stable_id("team-memory-import-draft", *identity_parts)
        source_id = _stable_id(
            "team-memory-source",
            identities["service_id"],
            identities["team_id"],
            asset_values["asset_id"],
            str(asset_values["version"]),
            digest,
        )
        authorization_fingerprint = _digest(
            identities["service_id"],
            identities["team_id"],
            identities["agent_id"],
            identities["user_id"],
        )
        consent_fingerprint = _digest(
            clean_consent_id,
            draft_id,
            authorization_fingerprint,
        )
        draft = {
            "schema_version": TEAM_IMPORT_SCHEMA_VERSION,
            "id": draft_id,
            "project_id": clean_project_id,
            "target_layer": clean_target,
            "candidate_type": clean_candidate_type,
            "status": "pending_review",
            "proposed_content": exact_content,
            "source": {
                "source_id": source_id,
                "source_kind": "team_memory_asset",
                "service_id": identities["service_id"],
                "team_id": identities["team_id"],
                "asset_id": asset_values["asset_id"],
                "asset_type": asset_values["asset_type"],
                "asset_name": asset_values["name"],
                "asset_version": asset_values["version"],
                "visibility": asset_values["visibility"],
                "content_sha256": digest,
                "locator": (
                    f"team-memory:{identities['team_id']}/"
                    f"{asset_values['asset_id']}@{asset_values['version']}"
                ),
            },
            "authorization": {
                "action": "read",
                "authorization_fingerprint": authorization_fingerprint,
                "inventory_fingerprint": access["inventory_fingerprint"],
                "verified_at": access["verified_at"],
                "consent_fingerprint": consent_fingerprint,
                "per_asset_confirmation": True,
            },
            "review": {
                "requires_human_review": True,
                "publication_allowed": False,
                "automatic_recall_allowed": False,
                "auto_promote_allowed": False,
                "reviewed_by": None,
                "reviewed_at": None,
                "reason": (
                    "团队资产先进入本地待审草稿；创建本地 Source、"
                    "差异预览和用户确认后才可进入现有 Memory publication。"
                ),
            },
            "safety": {
                "network_called": False,
                "provider_called": False,
                "remote_write_allowed": False,
                "memory_write_allowed": False,
                "project_skill_write_allowed": False,
                "projection_write_allowed": False,
                "credentials_recorded": False,
                "endpoint_recorded": False,
            },
            "created_at": timestamp,
            "updated_at": timestamp,
            # Team read authorization proves permission, not when the remote
            # material happened.  There is no trusted occurrence clock here.
            "occurred_at": None,
            "recorded_at": timestamp,
        }
        saved, replayed = self._drafts.save(draft)
        return TeamMemoryCandidateImportResult(
            draft_id=draft_id,
            project_id=clean_project_id,
            target_layer=clean_target,
            status="pending_review",
            replayed=replayed,
            draft=saved,
        )


class DiscardTeamMemoryImportDraft:
    """Erase unreviewed remote body while retaining a minimal local receipt."""

    def __init__(
        self,
        *,
        drafts: ObjectStoreTeamMemoryImportDraftRepository,
    ) -> None:
        self._drafts = drafts

    def execute(
        self,
        draft_id: str,
        *,
        disposition: str,
        reason: str,
        reviewed_at: str,
    ) -> TeamMemoryImportDiscardResult:
        clean_id = _identifier(draft_id, "draft_id")
        if disposition not in {"rejected", "withdrawn"}:
            raise TeamMemoryCandidateImportError(
                "team memory import disposition is invalid"
            )
        clean_reason = _bounded_text(
            reason,
            "reason",
            minimum=1,
            maximum=500,
        )
        timestamp = _timestamp(reviewed_at, "reviewed_at")
        current = self._drafts.get(clean_id)
        if current is None:
            raise TeamMemoryCandidateImportError(
                "team memory import draft was not found"
            )
        current_status = current.get("status")
        if current_status in {"rejected", "withdrawn"}:
            review = current.get("review")
            if (
                current_status == disposition
                and isinstance(review, Mapping)
                and review.get("reason") == clean_reason
            ):
                return TeamMemoryImportDiscardResult(
                    draft_id=clean_id,
                    status=disposition,
                    replayed=True,
                    content_erased=current.get("proposed_content") is None,
                    tombstone=current,
                )
            raise TeamMemoryCandidateImportConflict(
                "team memory import draft already has a terminal disposition"
            )
        if current_status != "pending_review":
            raise TeamMemoryCandidateImportConflict(
                "team memory import draft is not pending review"
            )
        tombstone = {
            **dict(current),
            "status": disposition,
            "proposed_content": None,
            "review": {
                "requires_human_review": True,
                "publication_allowed": False,
                "automatic_recall_allowed": False,
                "auto_promote_allowed": False,
                "reviewed_by": "user",
                "reviewed_at": timestamp,
                "reason": clean_reason,
            },
            "updated_at": timestamp,
        }
        saved = self._drafts.replace(
            tombstone,
            expected_revision=self._drafts.revision(clean_id),
        )
        return TeamMemoryImportDiscardResult(
            draft_id=clean_id,
            status=disposition,
            replayed=False,
            content_erased=saved.get("proposed_content") is None,
            tombstone=saved,
        )


def serialize_team_memory_import_result(
    result: TeamMemoryCandidateImportResult,
) -> dict[str, object]:
    source = result.draft.get("source")
    review = result.draft.get("review")
    return {
        "schema_version": TEAM_IMPORT_SCHEMA_VERSION,
        "draft_id": result.draft_id,
        "project_id": result.project_id,
        "target_layer": result.target_layer,
        "status": result.status,
        "replayed": result.replayed,
        "source": {
            key: source[key]
            for key in (
                "source_id",
                "source_kind",
                "team_id",
                "asset_id",
                "asset_type",
                "asset_name",
                "asset_version",
                "visibility",
                "content_sha256",
                "locator",
            )
            if isinstance(source, Mapping) and key in source
        },
        "review": dict(review) if isinstance(review, Mapping) else {},
        "content_included": False,
    }


def _profile_identities(profile: Mapping[str, object]) -> dict[str, str]:
    if profile.get("enabled") is not True:
        raise TeamMemoryCandidateImportError(
            "team memory profile must be enabled"
        )
    return {
        key: _required_identifier(profile, key)
        for key in ("service_id", "team_id", "agent_id", "user_id")
    }


def _asset(
    asset: Mapping[str, object],
    *,
    expected_team_id: str,
) -> dict[str, object]:
    values: dict[str, object] = {
        "asset_id": _required_identifier(asset, "asset_id"),
        "asset_type": _choice(
            asset.get("asset_type"),
            "asset_type",
            TEAM_IMPORT_ASSET_TYPES,
        ),
        "name": _bounded_text(
            asset.get("name"),
            "asset.name",
            minimum=1,
            maximum=160,
        ),
        "visibility": _choice(
            asset.get("visibility"),
            "visibility",
            TEAM_IMPORT_VISIBILITIES,
        ),
        "version": _positive_int(asset.get("version"), "asset.version"),
    }
    if _required_identifier(asset, "team_id") != expected_team_id:
        raise TeamMemoryCandidateImportError(
            "team memory asset team scope mismatch"
        )
    if asset.get("status") != "approved":
        raise TeamMemoryCandidateImportError(
            "team memory import requires an approved asset"
        )
    return values


def _access_evidence(
    value: Mapping[str, object],
    *,
    identities: Mapping[str, str],
    asset: Mapping[str, object],
) -> dict[str, str]:
    for key in ("service_id", "team_id", "agent_id", "user_id"):
        if _required_identifier(value, key) != identities[key]:
            raise TeamMemoryCandidateImportError(
                "team memory access identity mismatch"
            )
    if _required_identifier(value, "asset_id") != asset["asset_id"]:
        raise TeamMemoryCandidateImportError(
            "team memory access asset mismatch"
        )
    if _positive_int(value.get("asset_version"), "asset_version") != asset[
        "version"
    ]:
        raise TeamMemoryCandidateImportError(
            "team memory access version drift"
        )
    if value.get("action") != "read":
        raise TeamMemoryCandidateImportError(
            "team memory import requires read authorization"
        )
    return {
        "inventory_fingerprint": _sha256(
            value.get("inventory_fingerprint"),
            "inventory_fingerprint",
        ),
        "verified_at": _timestamp(value.get("verified_at"), "verified_at"),
    }


def _validate_draft(value: Mapping[str, object]) -> None:
    legacy = value.get("schema_version") == _LEGACY_TEAM_IMPORT_SCHEMA_VERSION
    required_fields = {
        "schema_version",
        "id",
        "project_id",
        "target_layer",
        "candidate_type",
        "status",
        "proposed_content",
        "source",
        "authorization",
        "review",
        "safety",
        "created_at",
        "updated_at",
    }
    if not legacy:
        required_fields |= {"occurred_at", "recorded_at"}
    if set(value) != required_fields:
        raise TeamMemoryCandidateImportError(
            "team memory import draft fields are invalid"
        )
    if value.get("schema_version") not in {
        _LEGACY_TEAM_IMPORT_SCHEMA_VERSION,
        TEAM_IMPORT_SCHEMA_VERSION,
    }:
        raise TeamMemoryCandidateImportError(
            "team memory import draft schema is unsupported"
        )
    draft_id = _required_identifier(value, "id")
    if not _DRAFT_ID.fullmatch(draft_id):
        raise TeamMemoryCandidateImportError(
            "team memory import draft id is invalid"
        )
    _required_identifier(value, "project_id")
    _choice(value.get("target_layer"), "target_layer", TEAM_IMPORT_TARGET_LAYERS)
    _choice(
        value.get("candidate_type"),
        "candidate_type",
        TEAM_IMPORT_CANDIDATE_TYPES,
    )
    status = value.get("status")
    if status not in {"pending_review", "rejected", "withdrawn"}:
        raise TeamMemoryCandidateImportError(
            "team memory import draft status is invalid"
        )
    content = (
        _content(value.get("proposed_content"))
        if status == "pending_review"
        else None
    )
    if status != "pending_review" and value.get("proposed_content") is not None:
        raise TeamMemoryCandidateImportError(
            "terminal team memory import draft must erase content"
        )
    source = value.get("source")
    authorization = value.get("authorization")
    review = value.get("review")
    safety = value.get("safety")
    if not all(
        isinstance(item, Mapping)
        for item in (source, authorization, review, safety)
    ):
        raise TeamMemoryCandidateImportError(
            "team memory import draft sections are invalid"
        )
    assert isinstance(source, Mapping)
    if set(source) != {
        "source_id",
        "source_kind",
        "service_id",
        "team_id",
        "asset_id",
        "asset_type",
        "asset_name",
        "asset_version",
        "visibility",
        "content_sha256",
        "locator",
    }:
        raise TeamMemoryCandidateImportError(
            "team memory import source is invalid"
        )
    service_id = _required_identifier(source, "service_id")
    team_id = _required_identifier(source, "team_id")
    asset_id = _required_identifier(source, "asset_id")
    asset_version = _positive_int(
        source.get("asset_version"),
        "asset_version",
    )
    digest = _sha256(source.get("content_sha256"), "content_sha256")
    if (
        source.get("source_kind") != "team_memory_asset"
        or source.get("source_id")
        != _stable_id(
            "team-memory-source",
            service_id,
            team_id,
            asset_id,
            str(asset_version),
            digest,
        )
        or source.get("locator")
        != f"team-memory:{team_id}/{asset_id}@{asset_version}"
    ):
        raise TeamMemoryCandidateImportError(
            "team memory import source identity is invalid"
        )
    _choice(
        source.get("asset_type"),
        "asset_type",
        TEAM_IMPORT_ASSET_TYPES,
    )
    _bounded_text(
        source.get("asset_name"),
        "asset_name",
        minimum=1,
        maximum=160,
    )
    _choice(
        source.get("visibility"),
        "visibility",
        TEAM_IMPORT_VISIBILITIES,
    )
    if (
        content is not None
        and hashlib.sha256(content.encode("utf-8")).hexdigest()
        != source.get("content_sha256")
    ):
        raise TeamMemoryCandidateImportError(
            "team memory import draft content hash mismatch"
        )
    assert isinstance(review, Mapping)
    assert isinstance(authorization, Mapping)
    if set(authorization) != {
        "action",
        "authorization_fingerprint",
        "inventory_fingerprint",
        "verified_at",
        "consent_fingerprint",
        "per_asset_confirmation",
    }:
        raise TeamMemoryCandidateImportError(
            "team memory import authorization is invalid"
        )
    if (
        authorization.get("action") != "read"
        or authorization.get("per_asset_confirmation") is not True
    ):
        raise TeamMemoryCandidateImportError(
            "team memory import authorization is invalid"
        )
    for key in (
        "authorization_fingerprint",
        "inventory_fingerprint",
        "consent_fingerprint",
    ):
        _sha256(authorization.get(key), key)
    _timestamp(authorization.get("verified_at"), "verified_at")
    if set(review) != {
        "requires_human_review",
        "publication_allowed",
        "automatic_recall_allowed",
        "auto_promote_allowed",
        "reviewed_by",
        "reviewed_at",
        "reason",
    }:
        raise TeamMemoryCandidateImportError(
            "team memory import review boundary is invalid"
        )
    _bounded_text(
        review.get("reason"),
        "review.reason",
        minimum=1,
        maximum=500,
    )
    if (
        review.get("requires_human_review") is not True
        or review.get("publication_allowed") is not False
        or review.get("automatic_recall_allowed") is not False
        or review.get("auto_promote_allowed") is not False
    ):
        raise TeamMemoryCandidateImportError(
            "team memory import review boundary is invalid"
        )
    if status == "pending_review" and (
        review.get("reviewed_by") is not None
        or review.get("reviewed_at") is not None
    ):
        raise TeamMemoryCandidateImportError(
            "pending team memory import draft must not be reviewed"
        )
    if status in {"rejected", "withdrawn"} and (
        review.get("reviewed_by") != "user"
        or not isinstance(review.get("reviewed_at"), str)
    ):
        raise TeamMemoryCandidateImportError(
            "terminal team memory import draft requires user review"
        )
    if isinstance(review.get("reviewed_at"), str):
        _timestamp(review.get("reviewed_at"), "reviewed_at")
    assert isinstance(safety, Mapping)
    if set(safety) != {
        "network_called",
        "provider_called",
        "remote_write_allowed",
        "memory_write_allowed",
        "project_skill_write_allowed",
        "projection_write_allowed",
        "credentials_recorded",
        "endpoint_recorded",
    }:
        raise TeamMemoryCandidateImportError(
            "team memory import safety boundary is invalid"
        )
    if any(value is not False for value in safety.values()):
        raise TeamMemoryCandidateImportError(
            "team memory import safety boundary is invalid"
        )
    _timestamp(value.get("created_at"), "created_at")
    _timestamp(value.get("updated_at"), "updated_at")
    if not legacy:
        _nullable_timestamp(value.get("occurred_at"), "occurred_at")
        _timestamp(value.get("recorded_at"), "recorded_at")


def _content(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TeamMemoryCandidateImportError(
            "team memory asset content is required"
        )
    if len(value.encode("utf-8")) > TEAM_IMPORT_MAX_CONTENT_BYTES:
        raise TeamMemoryCandidateImportError(
            "team memory asset content exceeds the import limit"
        )
    return value


def _required_identifier(value: Mapping[str, object], key: str) -> str:
    return _identifier(value.get(key), key)


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise TeamMemoryCandidateImportError(f"{field} is invalid")
    return value


def _choice(value: object, field: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise TeamMemoryCandidateImportError(f"{field} is invalid")
    return value


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise TeamMemoryCandidateImportError(f"{field} is invalid")
    return value


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise TeamMemoryCandidateImportError(f"{field} is invalid")
    return value


def _bounded_text(
    value: object,
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> str:
    if not isinstance(value, str):
        raise TeamMemoryCandidateImportError(f"{field} is invalid")
    clean = value.strip()
    if not minimum <= len(clean) <= maximum:
        raise TeamMemoryCandidateImportError(f"{field} is invalid")
    return clean


def _timestamp(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TeamMemoryCandidateImportError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise TeamMemoryCandidateImportError(f"{field} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TeamMemoryCandidateImportError(f"{field} is invalid")
    return value


def _nullable_timestamp(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _timestamp(value, field)


def _digest(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}-{_digest(*parts)[:32]}"


def _replay_compatible(
    existing: Mapping[str, object],
    requested: Mapping[str, object],
) -> bool:
    stable_keys = (
        "id",
        "project_id",
        "target_layer",
        "candidate_type",
        "status",
        "proposed_content",
        "source",
        "review",
        "safety",
    )
    if any(existing.get(key) != requested.get(key) for key in stable_keys):
        return False
    existing_authorization = existing.get("authorization")
    requested_authorization = requested.get("authorization")
    if not isinstance(existing_authorization, Mapping) or not isinstance(
        requested_authorization, Mapping
    ):
        return False
    return all(
        existing_authorization.get(key) == requested_authorization.get(key)
        for key in (
            "action",
            "authorization_fingerprint",
            "per_asset_confirmation",
        )
    )
