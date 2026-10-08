from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from backend.security.project_boundary_summary import ProjectBoundarySummaryError, ProjectBoundarySummaryProjector
from core.ai_boundary import BoundaryGrant, ProjectBoundaryProfile
from core.ai_kernel import CapabilityDefinition
from core.ai_tooling import ProjectCapabilityCatalog, ProjectCapabilityCatalogEntry, ProjectCapabilityProfile, ToolConnectionIdentity, ToolDefinition, ToolRetryPolicy, tool_boundary_target_identity


NOW = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)


def test_summary_classifies_grants_in_utc_and_sealed_makes_visible_grant_ineffective() -> None:
    profile = _boundary(mode="sealed", grants=(
        _grant("active"), _grant("soon", expires_at=NOW + timedelta(hours=1)),
        _grant("old", expires_at=NOW), _grant("gone", revoked=True),
    ))
    summary = ProjectBoundarySummaryProjector().project(
        boundary=profile, capability=_capability(), catalog=_catalog(), capabilities=(_core_capability(),), now=NOW,
    )
    grants = {item.grant_id: item for item in summary.persistent_grants}
    assert {key: item.lifecycle_state for key, item in grants.items()} == {
        "active": "active", "soon": "expiring", "old": "expired", "gone": "revoked",
    }
    assert all(item.static_match_eligible is False and item.ineligible_reason in {"sealed", "expired", "revoked"} for item in grants.values())
    assert summary.experience["tier"] == "sensitive_denied"


def test_summary_hides_orphan_and_disabled_targets_without_mcp_or_plugin_identity() -> None:
    profile = _boundary(grants=(_grant("visible"), _grant("hidden", target_id="mcp.private")))
    summary = ProjectBoundarySummaryProjector().project(
        boundary=profile, capability=_capability(), catalog=_catalog(), capabilities=(_core_capability(),), now=NOW,
    )
    payload = str(summary.to_public_dict())
    assert [item.grant_id for item in summary.persistent_grants] == ["visible"]
    assert summary.anonymous_grant_counts == (("unmapped_target", 1),)
    assert "mcp.private" not in payload and "connection" not in payload


def test_summary_rejects_capability_or_catalog_revision_drift() -> None:
    with pytest.raises(ProjectBoundarySummaryError, match="capability and Boundary"):
        ProjectBoundarySummaryProjector().project(
            boundary=_boundary(), capability=_capability(boundary_profile_revision=2), catalog=_catalog(), capabilities=(_core_capability(),), now=NOW,
        )
    with pytest.raises(ProjectBoundarySummaryError, match="catalog and capability"):
        ProjectBoundarySummaryProjector().project(
            boundary=_boundary(), capability=_capability(), catalog=_catalog(capability_profile_revision=9), capabilities=(_core_capability(),), now=NOW,
        )


def test_other_authorities_are_not_enumerated_as_project_grants() -> None:
    summary = ProjectBoundarySummaryProjector().project(
        boundary=_boundary(), capability=_capability(), catalog=_catalog(), capabilities=(_core_capability(),), now=NOW,
    )
    kinds = {item.kind: item for item in summary.authorities}
    payload = summary.to_public_dict()
    assert payload["schema_version"] == "1.0.0"
    assert payload["semantics_version"] == "boundary-summary/v1"
    assert payload["expiring_window_hours"] == 24
    assert payload["boundary_profile"]["denied_effects"] == []
    schema_path = Path(__file__).resolve().parents[4] / "core-contracts" / "ai" / "project-boundary-summary.schema.json"
    Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8"))).validate(payload)
    assert kinds["project_boundary_profile"].revision == 1
    for kind in ("turn_approval", "session_file_proof", "global_provider_consent", "machine_mcp_approval"):
        assert kinds[kind].enumerable is False and kinds[kind].revision is None


def test_summary_maps_only_current_exact_mcp_target_and_hides_stale_connection_identity() -> None:
    capability = _mcp_capability()
    tool = capability.tool_definition
    assert tool is not None
    current = tool_boundary_target_identity(tool, capability.capability_id)
    profile = _boundary(grants=(_grant("current", target_id=current), _grant("stale", target_id=current.replace(":r5:", ":r4:"))))
    catalog = _catalog(entries=(ProjectCapabilityCatalogEntry("tool", "mcp.calendar", "Calendar", "mcp", "calendar", True, "available", "selected", {"contract_version": 1}),))
    summary = ProjectBoundarySummaryProjector().project(boundary=profile, capability=_capability(), catalog=catalog, capabilities=(capability,), now=NOW)
    assert [item.grant_id for item in summary.persistent_grants] == ["current"]
    assert summary.persistent_grants[0].target == {"kind": "tool", "stable_id": "mcp.calendar", "display_name": "Calendar", "source": "mcp", "owner_id": "calendar"}
    assert "mcp:calendar" not in str(summary.to_public_dict())
    assert summary.anonymous_grant_counts == (("unmapped_target", 1),)


def test_summary_marks_non_kernel_subject_ineligible_and_exposes_redaction_flag() -> None:
    summary = ProjectBoundarySummaryProjector().project(boundary=_boundary(grants=(_grant("grant", revoked=False, subject_id="person", redaction_required=True),)), capability=_capability(), catalog=_catalog(), capabilities=(_core_capability(),), now=NOW)
    grant = summary.persistent_grants[0]
    assert grant.static_match_eligible is False
    assert grant.ineligible_reason == "subject_mismatch"
    assert grant.redaction_required is True


def test_summary_keeps_legacy_tool_grant_visible_but_never_marks_it_eligible() -> None:
    legacy = BoundaryGrant(
        "legacy", "ai-kernel", "project-a", "core.read", ("read",),
        ("project_content",), ("local",), None, 1,
    )
    summary = ProjectBoundarySummaryProjector().project(
        boundary=_boundary(grants=(legacy,)), capability=_capability(), catalog=_catalog(),
        capabilities=(_core_capability(),), now=NOW,
    )
    item = summary.persistent_grants[0]
    assert item.static_match_eligible is False
    assert item.ineligible_reason == "legacy_target_identity"


def _boundary(*, mode: str = "guarded", grants: tuple[BoundaryGrant, ...] = (), denied_effects: tuple[str, ...] = ()) -> ProjectBoundaryProfile:
    return ProjectBoundaryProfile("project-boundary-project-a", "project-a", mode, 1, "review", denied_effects=denied_effects, persistent_grants=grants)  # type: ignore[arg-type]


def _capability(**changes: object) -> ProjectCapabilityProfile:
    fields: dict[str, object] = dict(profile_id="project-capability-project-a", project_id="project-a", revision=3,
        boundary_profile_id="project-boundary-project-a", boundary_profile_revision=1, enabled_sources=("core",),
        enabled_skill_ids=(), enabled_plugin_ids=(), enabled_mcp_server_ids=(), allowed_tool_ids=(), denied_tool_ids=(),
        preferred_model_tier="standard", memory_scope="project_only", cross_project_grant_ids=(), output_style_profile_id=None)
    fields.update(changes)
    return ProjectCapabilityProfile(**fields)  # type: ignore[arg-type]


def _catalog(**changes: object) -> ProjectCapabilityCatalog:
    fields: dict[str, object] = dict(project_id="project-a", capability_profile_id="project-capability-project-a",
        capability_profile_revision=3, boundary_profile_id="project-boundary-project-a", boundary_profile_revision=1,
        registry_generation=2, supported_kinds=("tool",), entries=(ProjectCapabilityCatalogEntry("tool", "core.read", "Read", "core", "core", True, "available", "selected", {"contract_version": 1}),), excluded_reason_counts=())
    fields.update(changes)
    return ProjectCapabilityCatalog(**fields)  # type: ignore[arg-type]


def _target(capability: CapabilityDefinition) -> str:
    tool = capability.tool_definition
    assert tool is not None
    return tool_boundary_target_identity(tool, capability.capability_id)


def _grant(grant_id: str, *, target_id: str | None = None, expires_at: datetime | None = None, revoked: bool = False, subject_id: str = "ai-kernel", redaction_required: bool = False) -> BoundaryGrant:
    target_id = target_id or _target(_core_capability())
    return BoundaryGrant(grant_id, subject_id, "project-a", target_id, ("read",), ("project_content",), ("local",), expires_at, 1, revoked, redaction_required)


def _mcp_capability() -> CapabilityDefinition:
    tool = ToolDefinition(tool_id="mcp.calendar", version=1, display_name="Calendar", description="private", source="mcp", owner_id="calendar", effect="read", data_classes=("project_content",), destination="mcp", input_schema_uri="crp://input", output_schema_uri="crp://output", receipt_schema_uri=None, operation_semantics="read_only", execution_mode="parallel", resource_locks=(), idempotency="idempotent", retry_policy=ToolRetryPolicy(1, 1, ()), verification_tool_id=None, compensation_tool_id=None, mutability="read_only", egress_class="remote", network_scope=("mcp",), data_egress_scope=("project_content",), timeout_ms=1000, required_scopes=(), boundary_requirements=(), connection_identity=ToolConnectionIdentity("mcp", "calendar", "1", 1, "endpoint", "subject", 1, 5, 1))
    return CapabilityDefinition(tool.tool_id, tool.version, tool.effect, False, tool.operation_semantics, tool.input_schema_uri, tool.output_schema_uri, tool)


def _core_capability(*, effect: str = "read", destination: str = "local", data_classes: tuple[str, ...] = ("project_content",)) -> CapabilityDefinition:
    side_effect = effect in {"write", "delete", "external", "platform"}
    semantics = "receipt_required" if side_effect else "read_only"
    tool = ToolDefinition(tool_id="core.read", version=1, display_name="Read", description="private", source="core", owner_id="core", effect=effect, data_classes=data_classes, destination=destination, input_schema_uri="crp://input", output_schema_uri="crp://output", receipt_schema_uri="crp://receipt" if side_effect else None, operation_semantics=semantics, execution_mode="exclusive" if effect == "delete" else "parallel", resource_locks=(), idempotency="idempotent", retry_policy=ToolRetryPolicy(1, 1, ()), verification_tool_id=None, compensation_tool_id=None, mutability="read_only", egress_class="none", network_scope=(), data_egress_scope=(), timeout_ms=1000, required_scopes=(), boundary_requirements=())
    return CapabilityDefinition(tool.tool_id, tool.version, tool.effect, side_effect, tool.operation_semantics, tool.input_schema_uri, tool.output_schema_uri, tool)


@pytest.mark.parametrize(
    ("tool", "grant", "boundary", "reason"),
    [
        (_core_capability(effect="write"), _grant("grant"), _boundary(), "action_mismatch"),
        (_core_capability(destination="provider"), _grant("grant"), _boundary(), "destination_mismatch"),
        (_core_capability(data_classes=("private",)), _grant("grant"), _boundary(), "data_class_mismatch"),
        (_core_capability(effect="write"), BoundaryGrant("grant", "ai-kernel", "project-a", "core.read", ("write",), ("project_content",), ("local",), None, 1), _boundary(denied_effects=("write",)), "effect_denied"),
        (_core_capability(effect="delete"), _grant("grant"), _boundary(), "delete_requires_invocation_review"),
    ],
)
def test_summary_checks_static_grant_match_requirements(tool, grant, boundary, reason) -> None:
    grant = replace(grant, target_id=_target(tool))
    boundary = _boundary(mode=boundary.mode, grants=(grant,), denied_effects=boundary.denied_effects)
    summary = ProjectBoundarySummaryProjector().project(boundary=boundary, capability=_capability(), catalog=_catalog(), capabilities=(tool,), now=NOW)
    item = summary.persistent_grants[0]
    assert item.static_match_eligible is False
    assert item.ineligible_reason == reason
    assert item.invocation_dependent is True
