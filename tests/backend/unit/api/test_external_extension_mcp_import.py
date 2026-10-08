from __future__ import annotations

from pathlib import Path

import pytest

from backend.api.external_extension_mcp_import import (
    ExternalExtensionMCPImportError,
    ExternalExtensionMCPImportReviewService,
)
from backend.security.mcp_approved_server_migration import (
    MCPApprovedServerMigrationAuthority,
    MCPApprovedServerMigrationConflict,
)
from core.external_extensions import MCPImportReviewContext, MCPImportReviewInput


def _review(server_id: str = "fixture") -> MCPImportReviewInput:
    return MCPImportReviewInput(
        contribution_id=server_id,
        source_path=".mcp.json",
        transport="http",
        artifact_ref="crp://external-extension-artifacts/mcp-fixture",
        activation_route="mcp_server_review",
        confirmation_ids=("activate_external_extension", "allow_network_destinations"),
        risk_codes=("mcp_connection",),
        health_checks=("artifact_identity", "mcp_initialize_probe"),
    )


def _context(*reviews: MCPImportReviewInput) -> MCPImportReviewContext:
    selected = reviews or (_review(),)
    return MCPImportReviewContext(
        project_id="project-001",
        extension_id="mcp-fixture",
        intake_ref="crp://external-extension-intakes/intake-fixture",
        artifact_ref="crp://external-extension-artifacts/mcp-fixture",
        artifact_receipt_ref="crp://external-extension-artifact-receipts/mcp-fixture",
        artifact_content_sha256="a" * 64,
        manifest_identity="b" * 64,
        review_plan_identity="c" * 64,
        review_inputs=selected,
    )


def _service(tmp_path: Path, context: MCPImportReviewContext | None = None):
    selected = context or _context()
    return ExternalExtensionMCPImportReviewService(
        tmp_path,
        lambda reference: selected
        if reference == selected.intake_ref
        else (_ for _ in ()).throw(ValueError("unknown intake")),
    )


def _policy(tool_name: str = "fixture.read") -> dict[str, object]:
    return {
        "tool_name": tool_name, "tool_id": tool_name, "version": 1,
        "display_name": "Fixture read", "description": "Reviewed Tool",
        "effect": "read", "data_classes": ["fixture"],
        "input_schema_uri": "crp://schemas/fixture-input",
        "output_schema_uri": "crp://schemas/fixture-output",
        "receipt_schema_uri": None, "operation_semantics": "read_only",
        "execution_mode": "parallel", "resource_locks": ["mcp:fixture"],
        "idempotency": "never_retry",
        "retry_policy": {"max_attempts": 1, "backoff_ms": 0, "retryable_error_codes": []},
        "verification_tool_id": None, "compensation_tool_id": None,
        "mutability": "read_only", "egress_class": "remote",
        "network_scope": ["mcp:fixture"], "data_egress_scope": ["fixture"],
        "timeout_ms": 1000, "required_scopes": [],
        "boundary_requirements": ["mcp_enabled"], "requires_approval": False,
        "tool_schema_revision": 1, "reviewed_input_schema": {"type": "object"},
        "reviewed_output_schema": None, "available": True,
        "remote_receipt_field": None, "reviewed_receipt_schema": None,
    }


def _candidate(*, enabled: bool = False, server_id: str = "fixture") -> dict[str, object]:
    revision = 1
    endpoint = "fixture-endpoint"
    subject = "fixture-subject"
    return {
        "schema_version": "1.1.0",
        "servers": [{
            "server_id": server_id, "enabled": enabled,
            "approval_status": "approved", "approval_revision": revision,
            "transport_kind": "streamable_http",
            "host_connection": {
                "server_id": server_id, "manifest_revision": revision,
                "endpoint_identity": endpoint, "credential_subject_id": subject,
                "transport_generation": revision, "catalog_revision": revision,
            },
            "connection_manifest": {
                "server_id": server_id, "manifest_revision": revision,
                "endpoint_identity": endpoint, "credential_subject_id": subject,
                "transport_generation": revision, "approval_revision": revision,
                "approval_status": "approved", "endpoint_url": "https://mcp.example.test/rpc",
                "headers": None, "secret_header_refs": None, "timeout_seconds": 5.0,
                "max_response_bytes": 1048576, "max_sse_events": 128,
            },
            "tool_policies": [_policy()],
        }],
    }


def test_reviewed_candidate_enters_authority_as_disabled_preview(tmp_path: Path) -> None:
    result = _service(tmp_path).preview_disabled_import(
        intake_ref=_context().intake_ref,
        project_id="project-001",
        reviewed_candidate=_candidate(),
        confirmations=("activate_external_extension", "allow_network_destinations"),
        actor="local-user",
        reason="Reviewed the exact disabled candidate.",
    )

    assert result.state == "previewed"
    assert result.server_ids == ("fixture",)
    authority = MCPApprovedServerMigrationAuthority(tmp_path)
    assert authority.active_snapshot().servers == ()
    status = authority.status(result.migration_id)
    assert status is not None and status.state == "previewed"
    assert status.provenance_ref == result.review_receipt_ref
    assert result.review_receipt_ref.startswith(
        "crp://external-extension-mcp-import-receipts/"
    )
    with pytest.raises(
        MCPApprovedServerMigrationConflict,
        match="provenance confirmation is required",
    ):
        authority.confirm(
            migration_id=result.migration_id,
            expected_revision=result.migration_revision,
            command_id="external-import-confirm-without-receipt",
            confirmed=True,
        )

    replay = _service(tmp_path).preview_disabled_import(
        intake_ref=_context().intake_ref,
        project_id="project-001",
        reviewed_candidate=_candidate(),
        confirmations=("activate_external_extension", "allow_network_destinations"),
        actor="local-user",
        reason="Reviewed the exact disabled candidate.",
    )
    assert replay == result


@pytest.mark.parametrize(
    ("candidate", "confirmations", "message"),
    (
        (_candidate(enabled=True), ("activate_external_extension", "allow_network_destinations"), "remain disabled"),
        (_candidate(), ("activate_external_extension",), "confirmations do not match"),
        ({"schema_version": "1.1.0", "servers": []}, ("activate_external_extension", "allow_network_destinations"), "preserve the active server set"),
    ),
)
def test_import_fails_closed_before_active_pointer_change(
    tmp_path: Path, candidate: object, confirmations: tuple[str, ...], message: str,
) -> None:
    with pytest.raises(ExternalExtensionMCPImportError, match=message):
        _service(tmp_path).preview_disabled_import(
            intake_ref=_context().intake_ref,
            project_id="project-001",
            reviewed_candidate=candidate,
            confirmations=confirmations,
            actor="local-user",
            reason="Reviewed the exact disabled candidate.",
        )

    assert MCPApprovedServerMigrationAuthority(tmp_path).active_snapshot().servers == ()


def test_import_rejects_transport_drift(tmp_path: Path) -> None:
    review = MCPImportReviewInput(
        contribution_id="fixture", source_path=".mcp.json", transport="stdio",
        artifact_ref="crp://external-extension-artifacts/mcp-fixture",
        activation_route="mcp_server_review",
        confirmation_ids=("activate_external_extension", "allow_network_destinations"),
        risk_codes=("mcp_connection",),
        health_checks=("artifact_identity", "mcp_initialize_probe"),
    )
    with pytest.raises(ExternalExtensionMCPImportError, match="transport drifted"):
        _service(tmp_path, _context(review)).preview_disabled_import(
            intake_ref=_context(review).intake_ref,
            project_id="project-001",
            reviewed_candidate=_candidate(),
            confirmations=review.confirmation_ids,
            actor="local-user",
            reason="Reviewed the exact disabled candidate.",
        )


def test_import_cannot_replace_an_existing_approved_server(tmp_path: Path) -> None:
    authority = MCPApprovedServerMigrationAuthority(tmp_path)
    preview = authority.preview(candidate=_candidate(enabled=True), command_id="seed-preview")
    confirmed = authority.confirm(
        migration_id=preview.migration_id, expected_revision=preview.revision,
        command_id="seed-confirm", confirmed=True,
    )
    started = authority.begin_cutover(
        migration_id=confirmed.migration_id, expected_revision=confirmed.revision,
        command_id="seed-cutover-begin",
    )
    revoked = authority.mark_old_revoked(
        migration_id=started.migration_id, expected_revision=started.revision,
        command_id="seed-cutover-revoked",
    )
    authority.commit_cutover(
        migration_id=revoked.migration_id, expected_revision=revoked.revision,
        command_id="seed-cutover-commit",
    )

    with pytest.raises(ExternalExtensionMCPImportError, match="already exists"):
        _service(tmp_path).preview_disabled_import(
            intake_ref=_context().intake_ref,
            project_id="project-001",
            reviewed_candidate=_candidate(enabled=False),
            confirmations=_review().confirmation_ids,
            actor="local-user",
            reason="Reviewed the exact disabled candidate.",
        )

    current = authority.active_snapshot().servers
    assert len(current) == 1 and current[0].enabled is True


def test_import_rejects_cross_project_context_before_preview(tmp_path: Path) -> None:
    with pytest.raises(ExternalExtensionMCPImportError, match="another project"):
        _service(tmp_path).preview_disabled_import(
            intake_ref=_context().intake_ref,
            project_id="project-002",
            reviewed_candidate=_candidate(),
            confirmations=_review().confirmation_ids,
            actor="local-user",
            reason="Reviewed the exact disabled candidate.",
        )

    authority = MCPApprovedServerMigrationAuthority(tmp_path)
    assert authority.active_snapshot().servers == ()
    assert authority.blocking_migration() is None
