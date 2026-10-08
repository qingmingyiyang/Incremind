from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from blake3 import blake3

from backend.security.mcp_approved_server_migration import (
    MCPApprovedServerMigrationAuthority,
    MCPApprovedServerMigrationStatus,
)
from backend.security.mcp_approved_servers import (
    MCPApprovedServer,
    MCPApprovedServerSnapshot,
    MCPApprovedServerStoreError,
    canonical_mcp_approved_server_payload,
)
from core.external_extensions import MCPImportReviewContext, MCPImportReviewInput


class ExternalExtensionMCPImportError(ValueError):
    """Safe failure while binding reviewed import facts to MCP authority."""


@dataclass(frozen=True, slots=True)
class MCPDisabledImportPreview:
    artifact_ref: str
    review_receipt_ref: str
    migration_id: str
    migration_revision: int
    active_snapshot_revision: int
    server_ids: tuple[str, ...]
    state: str


class ExternalExtensionMCPImportReviewService:
    """Create a disabled Approved Server candidate from trusted review data.

    The caller supplies a complete reviewed authority payload.  This service
    binds it to sanitized intake facts and the current authority snapshot.  It
    never infers executable details and never confirms or cuts over a migration.
    """

    def __init__(
        self,
        root_dir: Path,
        review_context_loader: Callable[[str], MCPImportReviewContext],
    ) -> None:
        if not callable(review_context_loader):
            raise TypeError("MCP import review context loader must be callable")
        self._authority = MCPApprovedServerMigrationAuthority(Path(root_dir))
        self._review_context_loader = review_context_loader

    def preview_disabled_import(
        self,
        *,
        intake_ref: str,
        project_id: str,
        reviewed_candidate: object,
        confirmations: Iterable[str],
        actor: str,
        reason: str,
    ) -> MCPDisabledImportPreview:
        intake_ref = _text(intake_ref, "MCP import intake reference", 512)
        project_id = _text(project_id, "MCP import project identity", 160)
        actor = _text(actor, "MCP import review actor", 160)
        reason = _text(reason, "MCP import review reason", 2048)
        context = self._review_context_loader(intake_ref)
        if not isinstance(context, MCPImportReviewContext):
            raise ExternalExtensionMCPImportError(
                "MCP import review context is invalid"
            )
        if context.intake_ref != intake_ref:
            raise ExternalExtensionMCPImportError(
                "MCP import review context drifted"
            )
        if context.project_id != project_id:
            raise ExternalExtensionMCPImportError(
                "MCP import review belongs to another project"
            )
        inputs = context.review_inputs
        artifact_ref, expected_ids, expected_confirmations = _review_contract(inputs)
        if artifact_ref != context.artifact_ref:
            raise ExternalExtensionMCPImportError(
                "MCP import review artifact drifted"
            )
        supplied_confirmations = _sorted_unique(confirmations, "confirmation ids")
        if supplied_confirmations != expected_confirmations:
            raise ExternalExtensionMCPImportError(
                "MCP import confirmations do not match the frozen review plan"
            )
        try:
            candidate_payload, candidate = canonical_mcp_approved_server_payload(
                reviewed_candidate
            )
        except MCPApprovedServerStoreError:
            raise ExternalExtensionMCPImportError(
                "reviewed MCP candidate is invalid"
            ) from None
        active_revision, active = self._authority.active_snapshot_state()
        _require_candidate_binding(
            active=active,
            candidate=candidate,
            imported_ids=expected_ids,
            review_inputs=inputs,
        )
        candidate_identity = f"blake3:{blake3(candidate_payload).hexdigest()}"
        provenance = {
            "schema_version": "1.0.0",
            "kind": "external_extension_mcp_import_review",
            "project_id": context.project_id,
            "extension_id": context.extension_id,
            "intake_ref": context.intake_ref,
            "artifact_ref": context.artifact_ref,
            "artifact_receipt_ref": context.artifact_receipt_ref,
            "artifact_content_sha256": context.artifact_content_sha256,
            "manifest_identity": context.manifest_identity,
            "review_plan_identity": context.review_plan_identity,
            "candidate_identity": candidate_identity,
            "affected_server_ids": list(expected_ids),
            "confirmation_ids": list(supplied_confirmations),
            "actor": actor,
            "reason": reason,
            "expected_active_snapshot_revision": active_revision,
        }
        review_receipt_ref = _review_receipt_ref(provenance)
        provenance["provenance_ref"] = review_receipt_ref
        status = self._authority.preview(
            candidate=reviewed_candidate,
            command_id=_preview_command_id(review_receipt_ref),
            expected_active_snapshot_revision=active_revision,
            provenance_ref=review_receipt_ref,
            provenance=provenance,
        )
        return _projection(
            artifact_ref, review_receipt_ref, status, expected_ids,
        )


def _review_contract(
    inputs: tuple[MCPImportReviewInput, ...],
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    if not inputs:
        raise ExternalExtensionMCPImportError("MCP import review inputs are required")
    artifact_refs = {item.artifact_ref for item in inputs}
    if len(artifact_refs) != 1:
        raise ExternalExtensionMCPImportError("MCP import review artifacts are mixed")
    ids = tuple(sorted(item.contribution_id for item in inputs))
    if len(set(ids)) != len(ids):
        raise ExternalExtensionMCPImportError("MCP import server identities are duplicated")
    confirmations = {item.confirmation_ids for item in inputs}
    if len(confirmations) != 1:
        raise ExternalExtensionMCPImportError("MCP import review confirmations drifted")
    return next(iter(artifact_refs)), ids, next(iter(confirmations))


def _require_candidate_binding(
    *,
    active: MCPApprovedServerSnapshot,
    candidate: MCPApprovedServerSnapshot,
    imported_ids: tuple[str, ...],
    review_inputs: tuple[MCPImportReviewInput, ...],
) -> None:
    active_by_id = _by_id(active.servers)
    candidate_by_id = _by_id(candidate.servers)
    if set(imported_ids) & set(active_by_id):
        raise ExternalExtensionMCPImportError(
            "MCP import server identity already exists; use the governed upgrade path"
        )
    expected_all = set(active_by_id) | set(imported_ids)
    if set(candidate_by_id) != expected_all:
        raise ExternalExtensionMCPImportError(
            "reviewed MCP candidate does not preserve the active server set"
        )
    for server_id, current in active_by_id.items():
        if candidate_by_id[server_id] != current:
            raise ExternalExtensionMCPImportError(
                "reviewed MCP candidate changed an unrelated approved server"
            )
    transport_by_id = {item.contribution_id: item.transport for item in review_inputs}
    for server_id in imported_ids:
        record = candidate_by_id[server_id]
        if record.enabled:
            raise ExternalExtensionMCPImportError(
                "new MCP imports must remain disabled before health verification"
            )
        expected_transport = (
            "streamable_http" if transport_by_id[server_id] == "http" else "stdio"
        )
        if record.transport_kind != expected_transport:
            raise ExternalExtensionMCPImportError(
                "reviewed MCP transport drifted from the frozen intake"
            )
        if not record.tool_policies:
            raise ExternalExtensionMCPImportError(
                "reviewed MCP candidate requires explicit Tool policies"
            )


def _by_id(records: tuple[MCPApprovedServer, ...]) -> Mapping[str, MCPApprovedServer]:
    return {record.server_id: record for record in records}


def _sorted_unique(values: Iterable[str], label: str) -> tuple[str, ...]:
    items = tuple(values)
    if any(not isinstance(item, str) or not item for item in items):
        raise ExternalExtensionMCPImportError(f"{label} are invalid")
    normalized = tuple(sorted(set(items)))
    if len(normalized) != len(items):
        raise ExternalExtensionMCPImportError(f"{label} are duplicated")
    return normalized


def _projection(
    artifact_ref: str,
    review_receipt_ref: str,
    status: MCPApprovedServerMigrationStatus,
    server_ids: tuple[str, ...],
) -> MCPDisabledImportPreview:
    return MCPDisabledImportPreview(
        artifact_ref=artifact_ref,
        review_receipt_ref=review_receipt_ref,
        migration_id=status.migration_id,
        migration_revision=status.revision,
        active_snapshot_revision=status.active_snapshot_revision,
        server_ids=server_ids,
        state=status.state,
    )


def _review_receipt_ref(provenance: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(provenance), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return (
        "crp://external-extension-mcp-import-receipts/"
        f"{blake3(encoded).hexdigest()[:40]}"
    )


def _preview_command_id(review_receipt_ref: str) -> str:
    return f"external-mcp-preview-{blake3(review_receipt_ref.encode('utf-8')).hexdigest()[:32]}"


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ExternalExtensionMCPImportError(f"{label} is invalid")
    return value
