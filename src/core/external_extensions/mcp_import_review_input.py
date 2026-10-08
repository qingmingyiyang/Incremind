from __future__ import annotations

from dataclasses import dataclass

from .contracts import ExtensionManifest
from .review_projection import ExtensionReviewPlan


class MCPImportReviewInputError(ValueError):
    """Raised when sanitized MCP intake facts cannot form a review input."""


_MISSING_APPROVAL_FIELDS = (
    "approval_revision",
    "connection_or_launch_manifest",
    "credential_subject_id",
    "endpoint_identity",
    "tool_policies",
)


@dataclass(frozen=True, slots=True)
class MCPImportReviewInput:
    """Non-executable MCP server facts awaiting trusted approval assembly.

    This projection intentionally contains no command, argument, header,
    environment value, endpoint URL, secret, or tool policy.  Those values
    cannot become an approved MCP profile without a separate reviewed step.
    """

    contribution_id: str
    source_path: str
    transport: str
    artifact_ref: str
    activation_route: str
    confirmation_ids: tuple[str, ...]
    risk_codes: tuple[str, ...]
    health_checks: tuple[str, ...]
    missing_approval_fields: tuple[str, ...] = _MISSING_APPROVAL_FIELDS

    def __post_init__(self) -> None:
        for field in (
            "confirmation_ids",
            "risk_codes",
            "health_checks",
            "missing_approval_fields",
        ):
            object.__setattr__(self, field, tuple(getattr(self, field)))
        if self.transport not in {"http", "stdio"}:
            raise MCPImportReviewInputError("MCP import transport is invalid")
        if self.activation_route != "mcp_server_review":
            raise MCPImportReviewInputError("MCP import activation route is invalid")
        if "mcp_initialize_probe" not in self.health_checks:
            raise MCPImportReviewInputError("MCP import health review is incomplete")
        if self.missing_approval_fields != _MISSING_APPROVAL_FIELDS:
            raise MCPImportReviewInputError("MCP approval fields cannot be inferred")


@dataclass(frozen=True, slots=True)
class MCPImportReviewContext:
    """Immutable intake ancestry required by the trusted MCP review adapter."""

    project_id: str
    extension_id: str
    intake_ref: str
    artifact_ref: str
    artifact_receipt_ref: str
    artifact_content_sha256: str
    manifest_identity: str
    review_plan_identity: str
    review_inputs: tuple[MCPImportReviewInput, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "review_inputs", tuple(self.review_inputs))
        for value, label in (
            (self.project_id, "MCP import project identity"),
            (self.extension_id, "MCP import extension identity"),
            (self.intake_ref, "MCP import intake reference"),
            (self.artifact_ref, "MCP import artifact reference"),
            (self.artifact_receipt_ref, "MCP import artifact receipt reference"),
        ):
            if not isinstance(value, str) or not value:
                raise MCPImportReviewInputError(f"{label} is invalid")
        for value, label in (
            (self.artifact_content_sha256, "MCP import artifact identity"),
            (self.manifest_identity, "MCP import manifest identity"),
            (self.review_plan_identity, "MCP import review identity"),
        ):
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise MCPImportReviewInputError(f"{label} is invalid")
        if not self.review_inputs:
            raise MCPImportReviewInputError("MCP import review context is empty")
        if any(item.artifact_ref != self.artifact_ref for item in self.review_inputs):
            raise MCPImportReviewInputError("MCP import review context artifact drifted")
        identities = tuple(item.contribution_id for item in self.review_inputs)
        if len(set(identities)) != len(identities):
            raise MCPImportReviewInputError("MCP import review context identities collide")


def derive_mcp_import_review_inputs(
    manifest: ExtensionManifest,
    review_plan: ExtensionReviewPlan,
) -> tuple[MCPImportReviewInput, ...]:
    """Project sanitized MCP contributions into non-executable review work."""

    if manifest.source_format != "mcp_config":
        raise MCPImportReviewInputError("manifest is not an MCP configuration")
    if review_plan.extension_id != manifest.extension_id:
        raise MCPImportReviewInputError("MCP review identity drifted")
    if review_plan.artifact_ref != manifest.source.artifact_ref:
        raise MCPImportReviewInputError("MCP review artifact drifted")
    if review_plan.disposition != "ASK" or review_plan.install_projection != "installed_disabled":
        raise MCPImportReviewInputError("MCP intake is not reviewable")
    if "mcp_server_review" not in review_plan.activation_routes:
        raise MCPImportReviewInputError("MCP review route is missing")

    projected: list[MCPImportReviewInput] = []
    for contribution in manifest.contributions:
        if contribution.kind != "mcp_server_reference":
            raise MCPImportReviewInputError("MCP manifest contains another contribution kind")
        metadata = dict(contribution.metadata)
        if set(metadata) != {"transport"}:
            raise MCPImportReviewInputError("MCP contribution retained unreviewed execution data")
        projected.append(
            MCPImportReviewInput(
                contribution_id=contribution.contribution_id,
                source_path=contribution.source_path,
                transport=metadata["transport"],
                artifact_ref=manifest.source.artifact_ref,
                activation_route="mcp_server_review",
                confirmation_ids=review_plan.confirmation_ids,
                risk_codes=review_plan.risk_codes,
                health_checks=review_plan.health_checks,
            )
        )
    if not projected:
        raise MCPImportReviewInputError("MCP review requires at least one server")
    return tuple(projected)
