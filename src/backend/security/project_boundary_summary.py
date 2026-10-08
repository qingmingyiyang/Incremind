"""Read-only project Boundary Center projection.

This module owns no policy, store, consent, or runtime.  Its inputs are
already-read snapshots, plus the deliberately sparse project tool catalog.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from core.ai_boundary import BoundaryGrant, ProjectBoundaryProfile
from core.ai_tooling import (
    ProjectCapabilityCatalog,
    ProjectCapabilityCatalogEntry,
    ProjectCapabilityProfile,
    ToolDefinition,
    tool_boundary_target_identity,
    tool_from_capability,
)
from core.ai_tooling.contracts import LegacyCapabilityView


GrantStatus = Literal["active", "expiring", "expired", "revoked"]


class ProjectBoundarySummaryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BoundaryGrantSummary:
    grant_id: str
    revision: int
    target: dict[str, str]
    actions: tuple[str, ...]
    destinations: tuple[str, ...]
    data_classes: tuple[str, ...]
    lifecycle_state: GrantStatus
    static_match_eligible: bool
    ineligible_reason: str | None
    invocation_dependent: bool
    redaction_required: bool
    expires_at: str | None


@dataclass(frozen=True, slots=True)
class BoundaryAuthoritySummary:
    kind: str
    enumerable: bool
    scope: str
    revision: int | None


@dataclass(frozen=True, slots=True)
class ProjectBoundarySummary:
    schema_version: str
    semantics_version: str
    expiring_window_hours: int
    project_id: str
    boundary_profile: dict[str, object]
    capability_profile: dict[str, object]
    experience: dict[str, str]
    persistent_grants: tuple[BoundaryGrantSummary, ...]
    anonymous_grant_counts: tuple[tuple[str, int], ...]
    authorities: tuple[BoundaryAuthoritySummary, ...]

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "semantics_version": self.semantics_version,
            "expiring_window_hours": self.expiring_window_hours,
            "project_id": self.project_id,
            "boundary_profile": self.boundary_profile,
            "capability_profile": self.capability_profile,
            "experience": self.experience,
            "persistent_grants": [asdict(item) for item in self.persistent_grants],
            "anonymous_grant_counts": [
                {"reason": reason, "count": count}
                for reason, count in self.anonymous_grant_counts
            ],
            "authorities": [asdict(item) for item in self.authorities],
        }


class ProjectBoundarySummaryProjector:
    """Converts snapshot data into a safe UI DTO without evaluating policy."""

    def project(
        self,
        *,
        boundary: ProjectBoundaryProfile,
        capability: ProjectCapabilityProfile,
        catalog: ProjectCapabilityCatalog,
        capabilities: Sequence[LegacyCapabilityView] = (),
        now: datetime | None = None,
    ) -> ProjectBoundarySummary:
        _validate_binding(boundary, capability, catalog)
        current = _utc(now)
        visible = {entry.stable_id: entry for entry in catalog.entries if entry.selected and entry.state == "available"}
        target_ids = _boundary_target_ids(capabilities, visible)
        grants: list[BoundaryGrantSummary] = []
        hidden: dict[str, int] = {}
        for grant in boundary.persistent_grants:
            mapped = target_ids.get(grant.target_id)
            if mapped is None:
                hidden["unmapped_target"] = hidden.get("unmapped_target", 0) + 1
                continue
            lifecycle = _grant_status(grant.revoked, grant.expires_at, current)
            entry, tool, legacy_target = mapped
            reason = "legacy_target_identity" if legacy_target else _static_ineligible_reason(grant, tool, boundary, lifecycle)
            grants.append(BoundaryGrantSummary(
                grant_id=grant.grant_id, revision=grant.revision,
                target={"kind": entry.kind, "stable_id": entry.stable_id, "display_name": entry.display_name, "source": entry.source, "owner_id": entry.owner_id},
                actions=grant.actions, destinations=grant.destinations, data_classes=grant.data_classes,
                lifecycle_state=lifecycle, static_match_eligible=reason is None, ineligible_reason=reason,
                invocation_dependent=True,
                redaction_required=grant.redaction_required,
                expires_at=grant.expires_at.astimezone(timezone.utc).isoformat() if grant.expires_at else None,
            ))
        return ProjectBoundarySummary(
            schema_version="1.0.0", semantics_version="boundary-summary/v1", expiring_window_hours=24,
            project_id=boundary.project_id,
            boundary_profile={"profile_id": boundary.profile_id, "revision": boundary.revision, "mode": boundary.mode, "remote_default": boundary.remote_default, "denied_effects": list(boundary.denied_effects)},
            capability_profile={"profile_id": capability.profile_id, "revision": capability.revision, "boundary_profile_id": capability.boundary_profile_id, "boundary_profile_revision": capability.boundary_profile_revision},
            experience=_experience(boundary.mode),
            persistent_grants=tuple(grants),
            anonymous_grant_counts=tuple(sorted(hidden.items())),
            authorities=(
                BoundaryAuthoritySummary("project_boundary_profile", True, "project_persistent", boundary.revision),
                BoundaryAuthoritySummary("project_capability_profile", True, "project_selection", capability.revision),
                BoundaryAuthoritySummary("turn_approval", False, "single_turn", None),
                BoundaryAuthoritySummary("session_file_proof", False, "desktop_session", None),
                BoundaryAuthoritySummary("global_provider_consent", False, "global_provider", None),
                BoundaryAuthoritySummary("machine_mcp_approval", False, "machine_mcp", None),
            ),
        )


def _validate_binding(boundary: ProjectBoundaryProfile, capability: ProjectCapabilityProfile, catalog: ProjectCapabilityCatalog) -> None:
    if boundary.project_id != capability.project_id or boundary.project_id != catalog.project_id:
        raise ProjectBoundarySummaryError("project profile identity drifted")
    if (capability.boundary_profile_id, capability.boundary_profile_revision) != (boundary.profile_id, boundary.revision):
        raise ProjectBoundarySummaryError("capability and Boundary profile revisions do not match")
    if (catalog.capability_profile_id, catalog.capability_profile_revision) != (capability.profile_id, capability.revision):
        raise ProjectBoundarySummaryError("catalog and capability profile revisions do not match")
    if (catalog.boundary_profile_id, catalog.boundary_profile_revision) != (boundary.profile_id, boundary.revision):
        raise ProjectBoundarySummaryError("catalog and Boundary profile revisions do not match")


def _utc(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ProjectBoundarySummaryError("summary time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _grant_status(revoked: bool, expires_at: datetime | None, now: datetime) -> GrantStatus:
    if revoked:
        return "revoked"
    if expires_at is not None and expires_at <= now:
        return "expired"
    if expires_at is not None and expires_at <= now + timedelta(hours=24):
        return "expiring"
    return "active"


def _boundary_target_ids(
    capabilities: Sequence[LegacyCapabilityView],
    visible: dict[str, ProjectCapabilityCatalogEntry],
) -> dict[str, tuple[ProjectCapabilityCatalogEntry, ToolDefinition, bool]]:
    """Map only visible tools to the exact request target identity.

    MCP grants include the complete current destination identity.  It is used
    only as a lookup key and is never included in the presentation DTO.
    """
    targets: dict[str, tuple[ProjectCapabilityCatalogEntry, ToolDefinition, bool]] = {}
    by_id = {capability.capability_id: capability for capability in capabilities}
    for stable_id, entry in visible.items():
        capability = by_id.get(stable_id)
        if capability is None:
            continue
        tool = tool_from_capability(capability)
        target = tool_boundary_target_identity(tool, capability.capability_id)
        targets[target] = (entry, tool, False)
        # Legacy grants are display-only compatibility: execution uses only
        # the versioned target above, so an old grant cannot cross-authorize a
        # replacement contract.
        if tool.source != "mcp":
            targets.setdefault(tool.tool_id, (entry, tool, True))
    return targets


def _static_ineligible_reason(
    grant: BoundaryGrant,
    tool: ToolDefinition,
    boundary: ProjectBoundaryProfile,
    lifecycle: GrantStatus,
) -> str | None:
    if lifecycle not in {"active", "expiring"}:
        return lifecycle
    if grant.subject_id != "ai-kernel":
        return "subject_mismatch"
    if boundary.mode == "sealed":
        return "sealed"
    if tool.effect == "delete":
        return "delete_requires_invocation_review"
    if tool.effect not in grant.actions:
        return "action_mismatch"
    if tool.destination not in grant.destinations:
        return "destination_mismatch"
    grant_classes = set(grant.data_classes)
    if grant_classes and not set(tool.data_classes).issubset(grant_classes):
        return "data_class_mismatch"
    if tool.effect in boundary.denied_effects:
        return "effect_denied"
    return None


def _experience(mode: str) -> dict[str, str]:
    return {
        "open": {"tier": "full_access", "description": "允许按项目策略自动执行"},
        "guarded": {"tier": "self_review", "description": "由边界审查后允许、调整或拦截"},
        "sealed": {"tier": "sensitive_denied", "description": "敏感与外发能力处于封闭状态"},
    }[mode]
