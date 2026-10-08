"""外部执行只对宿主登记的原生契约开放窄口声明。"""

from copy import deepcopy

import pytest

from core.ai_kernel.contracts import AIKernelContractError, validate_capability_manifest
from core.ai_kernel.ports import CapabilityDefinition
from core.ai_tooling import ToolDefinition, ToolRetryPolicy, tool_contract_identity


CAPABILITY_ID = "external.task.execute"


def external_capability(*, approval=False):
    tool = ToolDefinition(
        tool_id=CAPABILITY_ID,
        version=1,
        display_name="External task",
        description="Run a frozen external task",
        source="core",
        owner_id="external-task-runner",
        effect="external",
        data_classes=("project_content",),
        destination="provider",
        input_schema_uri="crp://external-task/input-v1",
        output_schema_uri="crp://external-task/output-v1",
        receipt_schema_uri="crp://external-task/receipt-v1",
        operation_semantics="receipt_required",
        execution_mode="parallel",
        resource_locks=("external-task-owner",),
        idempotency="never_retry",
        retry_policy=ToolRetryPolicy(1, 0, ()),
        verification_tool_id=None,
        compensation_tool_id=None,
        mutability="irreversible",
        egress_class="remote",
        network_scope=("external_cli",),
        data_egress_scope=("project_content",),
        timeout_ms=1_230_000,
        required_scopes=(),
        boundary_requirements=("external_execute",),
    )
    return CapabilityDefinition(
        CAPABILITY_ID, 1, "external", approval, "receipt_required",
        tool.input_schema_uri, tool.output_schema_uri, tool,
    )


def external_manifest():
    capability = external_capability()
    return {
        "capability_id": capability.capability_id,
        "version": capability.version,
        "mode": capability.mode,
        "operation_semantics": capability.operation_semantics,
        "requires_approval": capability.requires_approval,
        "tool_exposed": True,
        "write_scope": "external_execute",
        "tool_contract": tool_contract_identity(capability.tool_definition),
    }


def test_precise_native_external_execution_can_omit_per_call_approval():
    manifest = external_manifest()
    assert validate_capability_manifest(manifest) == manifest


@pytest.mark.parametrize("field,value", [
    ("capability_id", "external.other.execute"),
    ("version", 2),
    ("version", True),
    ("mode", "read"),
    ("mode", "write"),
    ("mode", "platform"),
    ("operation_semantics", "none"),
    ("tool_exposed", False),
    ("tool_contract", None),
    ("tool_contract", {"external_execute": True}),
])
def test_external_scope_rejects_unproved_or_different_capability(field, value):
    manifest = external_manifest()
    manifest[field] = value
    with pytest.raises(AIKernelContractError):
        validate_capability_manifest(manifest)


@pytest.mark.parametrize("field,value", [
    ("tool_id", "external.other.execute"),
    ("version", 2),
    ("source", "plugin"),
    ("owner_id", "unreviewed-runner"),
    ("effect", "write"),
    ("destination", "platform"),
    ("operation_semantics", "none"),
    ("receipt_schema_uri", None),
    ("execution_mode", "exclusive"),
    ("mutability", "read_only"),
    ("egress_class", "none"),
    ("idempotency", "idempotent"),
    ("retry_policy", {"max_attempts": 2, "backoff_ms": 0, "retryable_error_codes": []}),
    ("retry_policy", {"max_attempts": 1, "backoff_ms": 1, "retryable_error_codes": []}),
    ("timeout_ms", 60_000),
    ("timeout_ms", 1_200_000),
    ("boundary_requirements", ["external_execute", "draft_create_only"]),
    ("boundary_requirements", []),
    ("available", False),
    ("data_classes", []),
    ("network_scope", []),
    ("data_egress_scope", []),
])
def test_external_execution_cannot_weaken_native_effect_and_cleanup_contract(field, value):
    manifest = deepcopy(external_manifest())
    manifest["tool_contract"][field] = value
    with pytest.raises(AIKernelContractError):
        validate_capability_manifest(manifest)


@pytest.mark.parametrize("mode", ["write", "external", "platform"])
def test_ordinary_mutations_still_require_approval(mode):
    with pytest.raises(AIKernelContractError):
        validate_capability_manifest({
            "mode": mode, "operation_semantics": "receipt_required",
            "requires_approval": False, "tool_exposed": True,
        })


def test_external_scope_does_not_expand_legacy_draft_exception():
    manifest = external_manifest()
    manifest["write_scope"] = "draft_create_only"
    with pytest.raises(AIKernelContractError):
        validate_capability_manifest(manifest)


@pytest.mark.parametrize("approval", [None, 0, "false"])
def test_external_scope_requires_explicit_boolean_approval_contract(approval):
    manifest = external_manifest()
    manifest["requires_approval"] = approval
    with pytest.raises(AIKernelContractError):
        validate_capability_manifest(manifest)


def test_external_scope_rejects_kernel_wide_exclusive_dispatch_gate():
    manifest = external_manifest()
    manifest["tool_contract"]["execution_mode"] = "exclusive"
    with pytest.raises(AIKernelContractError):
        validate_capability_manifest(manifest)
