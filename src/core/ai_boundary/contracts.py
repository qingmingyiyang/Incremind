from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal


BoundaryMode = Literal["open", "guarded", "sealed"]
BoundaryEffect = Literal["read", "write", "external", "platform", "delete"]
DestinationKind = Literal["local", "provider", "mcp", "platform"]
ScanState = Literal["not_required", "clean", "redacted", "sensitive", "unknown", "local"]
BoundaryOutcome = Literal["allow", "allow_redacted", "ask", "deny"]

_MODES = frozenset({"open", "guarded", "sealed"})
_EFFECTS = frozenset({"read", "write", "external", "platform", "delete"})
_DESTINATIONS = frozenset({"local", "provider", "mcp", "platform"})
_SCAN_STATES = frozenset({"not_required", "clean", "redacted", "sensitive", "unknown", "local"})
_OUTCOMES = frozenset({"allow", "allow_redacted", "ask", "deny"})


class BoundaryContractError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BoundaryGrant:
    grant_id: str
    subject_id: str
    project_id: str
    target_id: str
    actions: tuple[BoundaryEffect, ...]
    data_classes: tuple[str, ...]
    destinations: tuple[DestinationKind, ...]
    expires_at: datetime | None
    revision: int
    revoked: bool = False
    redaction_required: bool = False

    def __post_init__(self) -> None:
        _require_texts(self.grant_id, self.subject_id, self.project_id, self.target_id)
        _validate_enum_values(self.actions, _EFFECTS, "grant action")
        _validate_enum_values(self.destinations, _DESTINATIONS, "grant destination")
        if not self.actions or not self.destinations:
            raise BoundaryContractError("grant requires actions and destinations")
        _validate_names(self.data_classes, "grant data class")
        if self.revision < 1:
            raise BoundaryContractError("grant revision must be positive")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise BoundaryContractError("grant expiry must be timezone-aware")

    def is_active_at(self, now: datetime) -> bool:
        if self.revoked:
            return False
        return self.expires_at is None or self.expires_at > now


@dataclass(frozen=True, slots=True)
class ProjectBoundaryProfile:
    profile_id: str
    project_id: str
    mode: BoundaryMode
    revision: int
    remote_default: Literal["allow", "review", "deny"]
    enabled_sources: tuple[str, ...] = ()
    denied_effects: tuple[BoundaryEffect, ...] = ()
    persistent_grants: tuple[BoundaryGrant, ...] = ()

    def __post_init__(self) -> None:
        _require_texts(self.profile_id, self.project_id)
        if self.mode not in _MODES:
            raise BoundaryContractError("boundary mode is unsupported")
        if self.remote_default not in {"allow", "review", "deny"}:
            raise BoundaryContractError("remote_default is unsupported")
        if self.revision < 1:
            raise BoundaryContractError("profile revision must be positive")
        _validate_names(self.enabled_sources, "enabled source")
        _validate_enum_values(self.denied_effects, _EFFECTS, "denied effect")
        if any(grant.project_id != self.project_id for grant in self.persistent_grants):
            raise BoundaryContractError("profile grant project identity drifted")


@dataclass(frozen=True, slots=True)
class BoundaryRequest:
    request_id: str
    turn_id: str
    project_id: str
    actor_id: str
    target_id: str
    operation_id: str
    idempotency_key: str
    effect: BoundaryEffect
    destination_kind: DestinationKind
    destination_id: str
    data_classes: tuple[str, ...]
    scan_state: ScanState
    reversible: bool
    same_project: bool
    requires_receipt: bool

    def __post_init__(self) -> None:
        _require_texts(
            self.request_id,
            self.turn_id,
            self.project_id,
            self.actor_id,
            self.target_id,
            self.operation_id,
            self.idempotency_key,
            self.destination_id,
        )
        if self.effect not in _EFFECTS:
            raise BoundaryContractError("boundary effect is unsupported")
        if self.destination_kind not in _DESTINATIONS:
            raise BoundaryContractError("destination kind is unsupported")
        if self.scan_state not in _SCAN_STATES:
            raise BoundaryContractError("scan state is unsupported")
        _validate_names(self.data_classes, "request data class")
        if (
            self.effect in {"write", "external", "platform", "delete"}
            or self.destination_kind != "local"
        ) and not self.requires_receipt:
            raise BoundaryContractError("side-effecting request requires a receipt")
        if self.destination_kind != "local" and self.scan_state == "not_required":
            raise BoundaryContractError("remote request requires a scan result")


@dataclass(frozen=True, slots=True)
class BoundaryDecision:
    request_id: str
    outcome: BoundaryOutcome
    reason_codes: tuple[str, ...]
    matched_grant_ids: tuple[str, ...]
    policy_revision: int
    requires_receipt: bool
    redaction_required: bool = False

    def __post_init__(self) -> None:
        _require_texts(self.request_id)
        if self.outcome not in _OUTCOMES:
            raise BoundaryContractError("boundary outcome is unsupported")
        _validate_names(self.reason_codes, "reason code")
        _validate_names(self.matched_grant_ids, "matched grant")
        if not self.reason_codes:
            raise BoundaryContractError("boundary decision requires a reason")
        if self.policy_revision < 1:
            raise BoundaryContractError("policy revision must be positive")
        if self.outcome == "allow_redacted" and not self.redaction_required:
            raise BoundaryContractError("redacted allow requires a transform")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_texts(*values: str) -> None:
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise BoundaryContractError("boundary identities must be non-empty")


def _validate_names(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise BoundaryContractError(f"{label} values must be unique")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise BoundaryContractError(f"{label} values must be non-empty")


def _validate_enum_values(values: tuple[str, ...], allowed: frozenset[str], label: str) -> None:
    _validate_names(values, label)
    if any(value not in allowed for value in values):
        raise BoundaryContractError(f"{label} value is unsupported")
