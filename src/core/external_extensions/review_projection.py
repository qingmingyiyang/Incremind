from __future__ import annotations

from dataclasses import dataclass

from .contracts import ExtensionManifest


class ExtensionReviewError(ValueError):
    """Raised when an intake manifest cannot produce a closed review plan."""


@dataclass(frozen=True, slots=True)
class ExtensionReviewPlan:
    extension_id: str
    artifact_ref: str
    disposition: str
    install_projection: str
    activation_routes: tuple[str, ...]
    confirmation_ids: tuple[str, ...]
    risk_codes: tuple[str, ...]
    health_checks: tuple[str, ...]
    rollback_action: str
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        for field in ("activation_routes", "confirmation_ids", "risk_codes", "health_checks"):
            object.__setattr__(self, field, tuple(getattr(self, field)))
        if self.disposition not in {"AUTO_WITH_NOTICE", "ASK", "REJECT"}:
            raise ExtensionReviewError("review disposition is invalid")
        if self.install_projection not in {"quarantined", "installed_disabled", "ready_content_skill"}:
            raise ExtensionReviewError("install projection is invalid")
        allowed_routes = {
            "application_skill_import", "plugin_review", "mcp_server_review", "hook_review", "marketplace_review",
        }
        if not self.activation_routes or tuple(sorted(set(self.activation_routes))) != self.activation_routes or any(
            route not in allowed_routes for route in self.activation_routes
        ):
            raise ExtensionReviewError("activation routes are invalid")
        if tuple(sorted(set(self.confirmation_ids))) != self.confirmation_ids:
            raise ExtensionReviewError("confirmation ids must be sorted and unique")
        if tuple(sorted(set(self.risk_codes))) != self.risk_codes:
            raise ExtensionReviewError("risk codes must be sorted and unique")
        if tuple(sorted(set(self.health_checks))) != self.health_checks:
            raise ExtensionReviewError("health checks must be sorted and unique")
        if self.schema_version != "1.0.0":
            raise ExtensionReviewError("review plan schema is invalid")


def derive_review_plan(manifest: ExtensionManifest) -> ExtensionReviewPlan:
    kinds = frozenset(item.kind for item in manifest.contributions)
    risks = set(manifest.permission_plan.review_reasons)
    risks.update(issue.code for issue in manifest.issues)
    if manifest.issues:
        return ExtensionReviewPlan(
            extension_id=manifest.extension_id,
            artifact_ref=manifest.source.artifact_ref,
            disposition="REJECT",
            install_projection="quarantined",
            activation_routes=("plugin_review",),
            confirmation_ids=(),
            risk_codes=tuple(sorted(risks)),
            health_checks=("artifact_identity", "manifest_reparse"),
            rollback_action="retain_quarantine_audit",
        )
    if _is_zero_permission_content_skill(manifest, kinds):
        return ExtensionReviewPlan(
            extension_id=manifest.extension_id,
            artifact_ref=manifest.source.artifact_ref,
            disposition="AUTO_WITH_NOTICE",
            install_projection="ready_content_skill",
            activation_routes=("application_skill_import",),
            confirmation_ids=(),
            risk_codes=(),
            health_checks=("application_skill_catalog_load", "artifact_identity", "manifest_reparse"),
            rollback_action="disable_content_projection",
        )
    routes = _activation_routes(kinds)
    if not routes:
        return ExtensionReviewPlan(
            extension_id=manifest.extension_id,
            artifact_ref=manifest.source.artifact_ref,
            disposition="REJECT",
            install_projection="quarantined",
            activation_routes=("plugin_review",),
            confirmation_ids=(),
            risk_codes=tuple(sorted(risks or {"unsupported_contribution_set"})),
            health_checks=("artifact_identity", "manifest_reparse"),
            rollback_action="retain_quarantine_audit",
        )
    confirmations = {"activate_external_extension"}
    if manifest.permission_plan.requires_subprocess:
        confirmations.add("allow_subprocess")
    if manifest.permission_plan.requires_native_build:
        confirmations.add("allow_native_build")
    if manifest.permission_plan.network_destinations:
        confirmations.add("allow_network_destinations")
    if manifest.permission_plan.requires_oauth:
        confirmations.add("allow_oauth")
    if manifest.permission_plan.hook_events:
        confirmations.add("allow_hook_events")
    if manifest.permission_plan.secret_reference_names:
        confirmations.add("allow_secret_references")
    if "environment_or_credential_declaration" in risks or "credential_reference_declaration" in risks:
        confirmations.add("allow_credential_references")
    return ExtensionReviewPlan(
        extension_id=manifest.extension_id,
        artifact_ref=manifest.source.artifact_ref,
        disposition="ASK",
        install_projection="installed_disabled",
        activation_routes=routes,
        confirmation_ids=tuple(sorted(confirmations)),
        risk_codes=tuple(sorted(risks or {"external_activation"})),
        health_checks=tuple(
            sorted({"artifact_identity", "manifest_reparse", *(_route_health_check(route) for route in routes)})
        ),
        rollback_action="restore_previous_active_revision",
    )


def _is_zero_permission_content_skill(manifest: ExtensionManifest, kinds: frozenset[str]) -> bool:
    permissions = manifest.permission_plan
    return (
        manifest.source.is_immutable
        and manifest.source.trust_tier in {"reviewed_source", "managed"}
        and kinds == {"application_skill"}
        and not permissions.network_destinations
        and not permissions.filesystem_scopes
        and not permissions.secret_reference_names
        and not permissions.requires_subprocess
        and not permissions.requires_native_build
        and not permissions.requires_oauth
        and not permissions.hook_events
        and not permissions.review_required
    )


def _activation_routes(kinds: frozenset[str]) -> tuple[str, ...]:
    routes: set[str] = set()
    if "application_skill" in kinds:
        routes.add("application_skill_import")
    if kinds & {"plugin_skill", "plugin_hand", "declarative_tool", "ui_descriptor"}:
        routes.add("plugin_review")
    if "hook_binding" in kinds:
        routes.add("hook_review")
    if "mcp_server_reference" in kinds:
        routes.add("mcp_server_review")
    if kinds & {"repository_artifact", "marketplace_catalog"}:
        routes.add("marketplace_review")
    return tuple(sorted(routes))


def _route_health_check(route: str) -> str:
    return {
        "application_skill_import": "application_skill_catalog_load",
        "plugin_review": "plugin_manifest_health",
        "mcp_server_review": "mcp_initialize_probe",
        "hook_review": "hook_dry_event",
        "marketplace_review": "marketplace_catalog_reparse",
        "unsupported": "unsupported_route",
    }[route]
