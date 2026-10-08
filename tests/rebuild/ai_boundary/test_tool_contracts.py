from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from core.ai_kernel.ports import CapabilityDefinition
from core.ai_tooling import (
    ToolConnectionIdentity,
    ToolContractError,
    ToolDefinition,
    ToolRetryPolicy,
    tool_boundary_target_identity,
    tool_from_capability,
    tool_from_legacy_capability,
)


def test_legacy_read_capability_maps_to_local_read_tool() -> None:
    tool = tool_from_legacy_capability(_capability(mode="read", semantics="read_only"))
    assert tool.tool_id == "memory.recall"
    assert tool.effect == "read"
    assert tool.destination == "local"
    assert tool.operation_semantics == "read_only"
    assert tool.receipt_schema_uri is None
    assert tool.execution_mode == "parallel"
    assert tool.idempotency == "idempotent"
    assert tool.idempotent is True


def test_legacy_external_capability_maps_to_conservative_receipt_tool() -> None:
    tool = tool_from_legacy_capability(
        _capability(mode="external", semantics="receipt_required", approval=True)
    )
    assert tool.effect == "external"
    assert tool.destination == "provider"
    assert tool.operation_semantics == "receipt_required"
    assert tool.receipt_schema_uri
    assert tool.boundary_requirements == ("legacy_approval",)
    assert tool.execution_mode == "exclusive"
    assert tool.idempotency == "never_retry"
    assert tool.retry_policy.max_attempts == 1
    assert tool.egress_class == "remote"


def test_unknown_legacy_mode_fails_closed_as_platform_effect() -> None:
    tool = tool_from_legacy_capability(_capability(mode="unknown", semantics="none"))
    assert tool.effect == "platform"
    assert tool.destination == "platform"
    assert tool.operation_semantics == "receipt_required"


def test_boundary_target_binds_full_contract_without_exposing_schema_material() -> None:
    original = _tool()
    schema_changed = replace(original, input_schema_uri="crp://changed-input")
    prose_changed = replace(original, display_name="Remove", description="Different model prose")
    first = tool_boundary_target_identity(original, original.tool_id)
    assert tool_boundary_target_identity(schema_changed, schema_changed.tool_id) != first
    assert tool_boundary_target_identity(prose_changed, prose_changed.tool_id) == first
    assert "crp://" not in first


def test_native_mcp_tool_preserves_server_identity_and_transport_contract() -> None:
    native = _mcp_tool()
    capability = CapabilityDefinition(
        "calendar.read", 3, "read", False, "read_only",
        "crp://input", "crp://output", native,
    )

    resolved = tool_from_capability(capability)

    assert resolved is native
    assert resolved.source == "mcp"
    assert resolved.owner_id == "calendar-server"
    assert resolved.destination == "mcp"
    assert resolved.timeout_ms == 12_000
    assert resolved.idempotency == "never_retry"


def test_native_tool_identity_drift_fails_closed() -> None:
    with pytest.raises(ToolContractError, match="identity drifted: version"):
        tool_from_capability(CapabilityDefinition(
            "calendar.read", 4, "read", False, "read_only",
            "crp://input", "crp://output", _mcp_tool(),
        ))


def test_side_effecting_tool_rejects_missing_receipt_schema() -> None:
    with pytest.raises(ToolContractError, match="requires receipt"):
        ToolDefinition(
            tool_id="document.write",
            version=1,
            display_name="Write document",
            description="Writes a document draft",
            source="core",
            owner_id="documents",
            effect="write",
            data_classes=("project_content",),
            destination="local",
            input_schema_uri="crp://input",
            output_schema_uri="crp://output",
            receipt_schema_uri=None,
            operation_semantics="receipt_required",
            execution_mode="exclusive",
            resource_locks=("document:target",),
            idempotency="idempotent",
            retry_policy=ToolRetryPolicy(2, 100, ("timeout",)),
            verification_tool_id=None,
            compensation_tool_id="document.restore",
            mutability="reversible",
            egress_class="local",
            network_scope=(),
            data_egress_scope=(),
            timeout_ms=10_000,
            required_scopes=("project",),
            boundary_requirements=(),
        )


def test_verify_before_retry_requires_verification_tool() -> None:
    with pytest.raises(ToolContractError, match="requires a verification tool"):
        _tool(idempotency="verify_before_retry", verification_tool_id=None)


def test_never_retry_rejects_multiple_attempts() -> None:
    with pytest.raises(ToolContractError, match="one attempt"):
        _tool(
            idempotency="never_retry",
            retry_policy=ToolRetryPolicy(2, 100, ("timeout",)),
        )


def test_irreversible_tool_cannot_run_in_parallel() -> None:
    with pytest.raises(ToolContractError, match="execute exclusively"):
        _tool(execution_mode="parallel")


def test_remote_egress_requires_explicit_network_and_data_scopes() -> None:
    with pytest.raises(ToolContractError, match="network and data scopes"):
        _tool(
            effect="external",
            destination="provider",
            egress_class="remote",
            network_scope=(),
            data_egress_scope=(),
        )


def test_domain_tool_projection_satisfies_strict_v2_schema() -> None:
    schema_path = Path(__file__).resolve().parents[3] / "core-contracts" / "ai" / "tool-definition.schema.json"
    validator = Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")))
    payload = json.loads(json.dumps({"schema_version": "2.0.0", **asdict(
        tool_from_legacy_capability(_capability(mode="external", semantics="receipt_required"))
    )}))
    assert not tuple(validator.iter_errors(payload))
    mcp_payload = json.loads(json.dumps({"schema_version": "2.0.0", **asdict(_mcp_tool())}))
    assert not tuple(validator.iter_errors(mcp_payload))

    payload["execution_mode"] = "parallel"
    assert tuple(validator.iter_errors(payload))


def _tool(**overrides: object) -> ToolDefinition:
    values: dict[str, object] = {
        "tool_id": "document.delete",
        "version": 1,
        "display_name": "Delete document",
        "description": "Deletes one document",
        "source": "core",
        "owner_id": "documents",
        "effect": "delete",
        "data_classes": ("project_content",),
        "destination": "local",
        "input_schema_uri": "crp://input",
        "output_schema_uri": "crp://output",
        "receipt_schema_uri": "crp://receipt",
        "operation_semantics": "receipt_required",
        "execution_mode": "exclusive",
        "resource_locks": ("document:target",),
        "idempotency": "never_retry",
        "retry_policy": ToolRetryPolicy(1, 0, ()),
        "verification_tool_id": None,
        "compensation_tool_id": None,
        "mutability": "irreversible",
        "egress_class": "local",
        "network_scope": (),
        "data_egress_scope": (),
        "timeout_ms": 10_000,
        "required_scopes": ("project",),
        "boundary_requirements": (),
    }
    values.update(overrides)
    return ToolDefinition(**values)  # type: ignore[arg-type]


def _capability(*, mode: str, semantics: str, approval: bool = False) -> CapabilityDefinition:
    return CapabilityDefinition(
        capability_id="memory.recall",
        version=1,
        mode=mode,
        requires_approval=approval,
        operation_semantics=semantics,
        input_schema_uri="crp://input",
        output_schema_uri="crp://output",
    )


def _mcp_tool() -> ToolDefinition:
    return ToolDefinition(
        tool_id="calendar.read",
        version=3,
        display_name="Read calendar",
        description="Reads events from one pinned MCP calendar server",
        source="mcp",
        owner_id="calendar-server",
        effect="read",
        data_classes=("calendar_event",),
        destination="mcp",
        input_schema_uri="crp://input",
        output_schema_uri="crp://output",
        receipt_schema_uri=None,
        operation_semantics="read_only",
        execution_mode="parallel",
        resource_locks=(),
        idempotency="never_retry",
        retry_policy=ToolRetryPolicy(1, 0, ()),
        verification_tool_id=None,
        compensation_tool_id=None,
        mutability="read_only",
        egress_class="remote",
        network_scope=("calendar-server",),
        data_egress_scope=("calendar_event",),
        timeout_ms=12_000,
        required_scopes=("calendar.read",),
        boundary_requirements=("mcp_server_enabled",),
        connection_identity=ToolConnectionIdentity(
            "mcp", "calendar-server", "2025-11-25", 1,
            "calendar-local", "personal-calendar", 1, 1, 1,
        ),
    )
